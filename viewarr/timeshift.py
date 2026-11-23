"""Timeshift for Viewarr: pause, rewind and fast-forward live TV on any channel, from a buffer kept on the server.

While a TV watches a channel, ffmpeg copies that channel from Dispatcharr's own proxy (which shares the one provider
connection with everyone watching it, so the buffer costs no extra connection) into a sliding HLS playlist of the
last couple of hours. The files sit under Dispatcharr's /data/logos folder, which its web server already serves
without a sign-in, in a folder named by a random token, so only a TV that was told the token can find it.

How a TV asks (Dispatcharr only lets admins run plugin actions, and every user must be able to pause, so the asking
goes through each user's own app data, which any signed-in user can write, watched here every second):

  TV writes   viewarr_timeshift_req  {"tvs": {"<tv id>": {"uuid": "<channel uuid>" or null, "at": <ms>}}}
              (each TV only changes its own entry; "at" changes every ~20 s while it watches: the keep-alive)
  plugin writes viewarr_timeshift    {"v": 1, "on": true, "window_s": 7200, "at": <s>,
                                      "buffers": {"<uuid>": {"url": "/logos/viewarr-ts/<token>/index.m3u8",
                                                             "started_at": <s>, "ready": true}},
                                      "refused": {"<uuid>": "<why>"}}

A buffer starts when any TV asks for its channel, is shared by every TV on that channel, and stops (ffmpeg ended,
folder deleted) as soon as no TV wants it: a TV that moved to another channel or stopped lets go at once, and one
that simply went quiet (switched off, lost its network) is let go after KEEPALIVE_S. Leftovers from a crash are
cleared when the service starts.
"""

import json
import os
import re
import secrets
import shutil
import signal
import threading
import time

REQ_KEY = "viewarr_timeshift_req"
REPLY_KEY = "viewarr_timeshift"
VERSION = 1

ROOT = "/data/logos/viewarr-ts"
URL_BASE = "/logos/viewarr-ts"
# Where the buffer files really live (setting "timeshift_folder", a path inside Dispatcharr's container): a folder of
# this plugin's own (SUBDIR) inside it, linked in at ROOT so Dispatcharr's web server still serves it. Empty, or a
# folder that can't be used: Dispatcharr's own data folder (ROOT itself). Only SUBDIR is ever written or cleared, so
# nothing else in the chosen folder is touched.
SUBDIR = "viewarr-timeshift"
# Folders that are never a sensible place (the container's own system folders), refused outright.
SYSTEM_DIRS = ("/", "/app", "/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin",
               "/sys", "/usr", "/var", "/data")
# Before 0.6.8 the buffer could be kept in the recordings folder; whatever was left there is cleared once.
OLD_RECORDINGS_DIR = "/data/recordings/.viewarr-timeshift"
# Dispatcharr's own web server inside the container (uWSGI's HTTP port, which nginx hands /proxy/ to); overridable.
PROXY_BASE = os.environ.get("VIEWARR_TS_PROXY", "http://127.0.0.1:5656")
FFMPEG = "/usr/local/bin/ffmpeg" if os.path.exists("/usr/local/bin/ffmpeg") else "ffmpeg"

SEGMENT_S = 4
KEEPALIVE_S = 60
POLL_S = 1.0
SETTINGS_EVERY_S = 10
DEFAULT_WINDOW_MIN = 120
DEFAULT_MAX = 3
# Refused below this much free space (a 2-hour HD buffer is about 6 GB).
MIN_FREE_BYTES = 10 * 1024 ** 3
# While buffers run, free space is looked at every SPACE_EVERY_S; below LOW_FREE_BYTES the newest buffer is stopped
# (and that channel left alone for LOW_SPACE_REST_S), so a filling disk never fills up completely.
LOW_FREE_BYTES = 5 * 1024 ** 3
SPACE_EVERY_S = 60
LOW_SPACE_REST_S = 300
# suggest_storage() writes a probe file in each place: its answer is kept this long for the status.
SUGGEST_TTL_S = 3600
RESTARTS = 5
RESTART_WINDOW_S = 600
ENTRY_TTL_S = 600

# A channel uuid, or the handle of a stream a TV plays directly (Dispatcharr's preview hash).
_UUID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _popen():
    """The standard library's Popen where gevent keeps it, else subprocess.Popen as the measuring thread uses (in a
    restarted Dispatcharr gevent's get_original returned None here, and every start failed)."""
    import subprocess
    try:
        from gevent import monkey
        original = monkey.get_original("subprocess", "Popen")
        if callable(original):
            return original
    except Exception:
        pass
    return subprocess.Popen


def settings_of(settings):
    s = settings or {}
    on = s.get("timeshift", True) is not False
    try:
        window = int(str(s.get("timeshift_window") or DEFAULT_WINDOW_MIN).strip())
    except ValueError:
        window = DEFAULT_WINDOW_MIN
    try:
        most = int(str(s.get("timeshift_max") or DEFAULT_MAX).strip())
    except ValueError:
        most = DEFAULT_MAX
    folder = str(s.get("timeshift_folder") or "").strip()
    if len(folder) > 1:
        folder = folder.rstrip("/")
    return on, max(10, min(window, 480)) * 60, max(1, min(most, 10)), folder


def _place_info(path):
    """(writable, free bytes, device id) for a place, or None when it can't be used."""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".viewarr-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        st = os.statvfs(path)
        return (True, st.f_bavail * st.f_frsize, os.stat(path).st_dev)
    except OSError:
        return None


_suggested = {"at": 0.0, "key": None, "value": None}


def suggest_storage(folder="", fresh=False):
    """Where the buffer goes for the [folder] setting, and why. Returns (real folder or None for Dispatcharr's own
    folder, {"folder": free GB or None, "data": free GB}, reason or ""). Kept for SUGGEST_TTL_S (asked every few
    seconds) unless [fresh] or the setting changed."""
    if (not fresh and _suggested["key"] == folder and _suggested["value"] is not None
            and time.time() - _suggested["at"] < SUGGEST_TTL_S):
        return _suggested["value"]
    value = _suggest_storage(folder)
    _suggested.update(at=time.time(), key=folder, value=value)
    return value


def _folder_problem(folder):
    """Why [folder] can't hold the buffer, or None."""
    if not folder.startswith("/"):
        return "the folder must be a full path inside Dispatcharr's container, starting with /"
    if folder in SYSTEM_DIRS or folder.startswith(("/proc/", "/sys/", "/dev/", "/etc/", "/usr/", "/app/")):
        return "%s is one of the container's own folders" % folder
    if os.path.realpath(folder).startswith(os.path.dirname(ROOT) + "/"):
        return "%s is inside the folder the buffer is served from" % folder
    if not os.path.isdir(folder):
        return "%s doesn't exist in Dispatcharr's container (add it as a path or volume first)" % folder
    return None


def _suggest_storage(folder):
    data = _place_info(os.path.dirname(ROOT))  # /data/logos
    gb = lambda i: round(i[1] / 1e9) if i else None
    if not folder:
        return None, {"folder": None, "data": gb(data)}, ""
    problem = _folder_problem(folder)
    info = None if problem else _place_info(os.path.join(folder, SUBDIR))
    free = {"folder": gb(info), "data": gb(data)}
    if problem:
        return None, free, problem
    if info is None:
        return None, free, "%s can't be written to" % folder
    if info[1] < MIN_FREE_BYTES:
        return None, free, "%s has under 10 GB free" % folder
    return os.path.join(folder, SUBDIR), free, ""


def _clear_old_recordings_dir(log=None):
    """Removes the buffer folder older versions could keep in the recordings folder (only this plugin's own hidden
    folder; nothing else in recordings is touched)."""
    try:
        if os.path.isdir(OLD_RECORDINGS_DIR) and not os.path.islink(OLD_RECORDINGS_DIR):
            shutil.rmtree(OLD_RECORDINGS_DIR, ignore_errors=True)
            if log:
                log("timeshift: removed the old buffer folder %s" % OLD_RECORDINGS_DIR)
    except OSError:
        pass


def use_storage(real, log=None):
    """Points ROOT at [real]: a plain folder in Dispatcharr's data folder (None), or a link to the plugin's own
    folder in the chosen place. Only called with no buffer running."""
    try:
        if real is None:
            if os.path.islink(ROOT):
                os.unlink(ROOT)
            os.makedirs(ROOT, exist_ok=True)
        else:
            os.makedirs(real, exist_ok=True)
            if os.path.islink(ROOT):
                if os.readlink(ROOT) == real:
                    return True
                os.unlink(ROOT)
            elif os.path.isdir(ROOT):
                shutil.rmtree(ROOT, ignore_errors=True)
            os.symlink(real, ROOT)
        if log:
            log("timeshift: buffer folder is %s" % (real or ROOT))
        return True
    except OSError as e:
        if log:
            log("timeshift: buffer folder %s couldn't be used (%s)" % (real or ROOT, e))
        return False


class Buffer:
    def __init__(self, uuid, window_s):
        self.uuid = uuid
        self.token = secrets.token_hex(16)
        self.dir = os.path.join(ROOT, self.token)
        self.window_s = window_s
        self.started_at = int(time.time())
        self.proc = None
        self.ready = False
        self.restarts = []
        self.log_path = None

    @property
    def url(self):
        return "%s/%s/index.m3u8" % (URL_BASE, self.token)

    def command(self, append):
        flags = "delete_segments+program_date_time+independent_segments+temp_file"
        if append:
            flags += "+append_list+discont_start"
        return [
            FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-user_agent", "Viewarr-Timeshift", "-rw_timeout", "20000000",
            "-fflags", "+genpts+discardcorrupt",
            "-i", "%s/proxy/ts/stream/%s" % (PROXY_BASE, self.uuid),
            "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-f", "hls", "-hls_time", str(SEGMENT_S),
            # A sliding window: the playlist lists the last window_s and older segments are deleted. No playlist type,
            # so players treat it as live with a seekable window (a DVR window), not as a recording that grows.
            "-hls_list_size", str(max(3, self.window_s // SEGMENT_S)),
            "-hls_delete_threshold", "2",
            "-hls_flags", flags,
            "-hls_segment_filename", os.path.join(self.dir, "seg_%06d.ts"),
            os.path.join(self.dir, "index.m3u8"),
        ]

    def spawn(self, log_dir, append=False):
        os.makedirs(self.dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, "ts_%s.log" % self.token)
        err = open(self.log_path, "ab")
        try:
            self.proc = _popen()(self.command(append), stdin=open(os.devnull, "rb"), stdout=open(os.devnull, "wb"),
                                 stderr=err, close_fds=True, start_new_session=True)
        finally:
            err.close()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def segments(self):
        try:
            with open(os.path.join(self.dir, "index.m3u8"), encoding="utf-8", errors="replace") as f:
                return f.read().count("#EXTINF")
        except OSError:
            return 0

    def last_error(self):
        try:
            with open(self.log_path, "rb") as f:
                f.seek(max(0, os.path.getsize(self.log_path) - 300))
                lines = f.read().decode("utf-8", "replace").strip().splitlines()
            return lines[-1][:160] if lines else ""
        except Exception:
            return ""

    def kill(self):
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
                for _ in range(20):
                    if p.poll() is not None:
                        break
                    time.sleep(0.1)
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)
        if self.log_path:
            try:
                os.remove(self.log_path)
            except OSError:
                pass


def sweep(log=None, log_dir=None):
    """Leftovers from an earlier run (a crash or a restart of Dispatcharr): stray ffmpeg copies, their folders and
    their ffmpeg logs (ts_*.log in [log_dir])."""
    killed = 0
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read()
            except OSError:
                continue
            if b"ffmpeg" in cmd and b"/viewarr-ts/" in cmd:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    killed += 1
                except OSError:
                    pass
    except OSError:
        pass
    left = 0
    if os.path.isdir(ROOT):
        for name in os.listdir(ROOT):
            shutil.rmtree(os.path.join(ROOT, name), ignore_errors=True)
            left += 1
    logs = 0
    if log_dir and os.path.isdir(log_dir):
        for name in os.listdir(log_dir):
            if name.startswith("ts_") and name.endswith(".log"):
                try:
                    os.remove(os.path.join(log_dir, name))
                    logs += 1
                except OSError:
                    pass
    if log and (killed or left or logs):
        log("timeshift: cleared %d leftover buffer folder(s), %d stray ffmpeg, %d old log(s)" % (left, killed, logs))


class Manager:
    def __init__(self, data_dir, settings_fn, log=None):
        self.data_dir = data_dir
        self.log_dir = os.path.join(data_dir, "timeshift")
        self.settings_fn = settings_fn
        self.failed_until = {}
        self.log = log or (lambda m: None)
        self.buffers = {}
        self.refused = {}
        self.seen = {}  # (user id, tv id) → (its last "at", server time we saw it change, uuid)
        self.on, self.window_s, self.most = True, DEFAULT_WINDOW_MIN * 60, DEFAULT_MAX
        self.settings_at = 0.0
        self.published = {}  # user id → the reply last written to them
        self.space_at = 0.0

    # ---- the loop

    def run(self, stop):
        sweep(self.log, self.log_dir)
        try:
            while not stop.is_set():
                try:
                    self.round()
                except Exception as exc:
                    self.log("timeshift: round failed: %s" % exc)
                    stop.wait(5)
                stop.wait(POLL_S)
        finally:
            for b in list(self.buffers.values()):
                b.kill()
            self.buffers.clear()

    def _settings(self):
        if time.time() - self.settings_at < SETTINGS_EVERY_S:
            return
        self.settings_at = time.time()
        try:
            raw = self.settings_fn() or {}
            self.on, self.window_s, self.most, folder = settings_of(raw)
            # "all" channels, or only those without the provider's catch-up (the TVs skip asking for those).
            self.channels = "no_catchup" if str(raw.get("timeshift_channels") or "all").strip() == "no_catchup" else "all"
            # A new place only once nothing is buffered, so a buffer being watched never moves.
            self.folder = folder
            real, _free, why = suggest_storage(folder)
            if not getattr(self, "placed", False) or (real != getattr(self, "real", None) and not self.buffers):
                log = getattr(self, "log", None)
                if not getattr(self, "placed", False):
                    _clear_old_recordings_dir(log)
                if use_storage(real, log):
                    self.real, self.placed = real, True
                    if log and why:
                        log("timeshift: using Dispatcharr's own folder: %s" % why)
        except Exception:
            pass

    def round(self):
        from django.db import close_old_connections
        from apps.accounts.models import UserAppData

        self._settings()
        now = time.time()
        close_old_connections()
        rows = list(UserAppData.objects.filter(key=REQ_KEY).values_list("user_id", "value", "updated_at"))
        wanted = {}  # uuid → user ids watching it
        users = set()
        live_keys = set()
        for user_id, value, updated in rows:
            tvs = (value or {}).get("tvs") if isinstance(value, dict) else None
            if not isinstance(tvs, dict):
                continue
            for tv, entry in tvs.items():
                if not isinstance(entry, dict):
                    continue
                key = (user_id, str(tv)[:40])
                at, uuid = entry.get("at"), entry.get("uuid")
                uuid = str(uuid) if uuid and _UUID.match(str(uuid)) else None
                old = self.seen.get(key)
                if old is None:
                    # First sight (e.g. after a restart): as fresh as the row itself, so a TV switched off long ago
                    # doesn't start a buffer.
                    self.seen[key] = (at, updated.timestamp() if updated else 0, uuid)
                elif old[0] != at or old[2] != uuid:
                    # A keep-alive or a change of channel: counted from now, by the server's clock (a TV whose own
                    # clock is wrong still works).
                    self.seen[key] = (at, now, uuid)
                live_keys.add(key)
                _, seen_at, seen_uuid = self.seen[key]
                if seen_uuid and now - seen_at <= KEEPALIVE_S:
                    wanted.setdefault(seen_uuid, set()).add(user_id)
                    users.add(user_id)
        for key in list(self.seen):
            if key not in live_keys or now - self.seen[key][1] > ENTRY_TTL_S:
                self.seen.pop(key, None)

        if not self.on:
            wanted = {}
        changed = False
        # (Failed starts whose wait is over are forgotten.)
        for uuid in [u for u, t in self.failed_until.items() if t <= now]:
            self.failed_until.pop(uuid, None)
        changed = self._check_space(now) or changed
        # Let go of what nobody wants any more.
        for uuid in [u for u in self.buffers if u not in wanted]:
            b = self.buffers.pop(uuid)
            b.kill()
            self.log("timeshift: stopped %s after %d min" % (uuid, (now - b.started_at) // 60))
            changed = True
        # Start what's newly wanted (up to the limit), keep the rest running.
        for uuid in sorted(wanted, key=lambda u: u not in self.buffers):
            b = self.buffers.get(uuid)
            if b is None:
                # (After a failed start, that channel waits a minute before trying again: no start every second.)
                if time.time() < self.failed_until.get(uuid, 0):
                    continue
                why = self._refuse_reason()
                if why:
                    if self.refused.get(uuid) != why:
                        self.refused[uuid] = why
                        self.log("timeshift: refused %s (%s)" % (uuid, why))
                        changed = True
                    continue
                b = Buffer(uuid, self.window_s)
                try:
                    b.spawn(self.log_dir)
                except Exception as exc:
                    self.failed_until[uuid] = time.time() + 60
                    self.refused[uuid] = "ffmpeg didn't start"
                    self.log("timeshift: couldn't start ffmpeg for %s: %s" % (uuid, exc))
                    b.kill()
                    changed = True
                    continue
                self.buffers[uuid] = b
                self.refused.pop(uuid, None)
                self.log("timeshift: started %s (window %d min, for %d user(s))" % (uuid, self.window_s // 60, len(wanted[uuid])))
                changed = True
                continue
            if not b.alive():
                b.restarts = [t for t in b.restarts if now - t < RESTART_WINDOW_S]
                if len(b.restarts) >= RESTARTS:
                    self.log("timeshift: %s keeps stopping (%s); given up" % (uuid, b.last_error() or "no message"))
                    self.buffers.pop(uuid).kill()
                    self.refused[uuid] = "the stream keeps stopping"
                    changed = True
                    continue
                b.restarts.append(now)
                self.log("timeshift: %s stopped (%s); starting it again" % (uuid, b.last_error() or "no message"))
                try:
                    b.spawn(self.log_dir, append=True)
                except Exception:
                    pass
            if not b.ready and b.segments() >= 2:
                b.ready = True
                changed = True
        # Refusals are forgotten once nobody asks for that channel.
        for uuid in [u for u in self.refused if u not in wanted]:
            self.refused.pop(uuid)
            changed = True
        self._publish(users, changed)

    def _check_space(self, now):
        """Every SPACE_EVERY_S while buffers run: below LOW_FREE_BYTES free, the newest buffer is stopped (refused
        "low disk space", that channel left alone LOW_SPACE_REST_S). True when one was stopped."""
        if not self.buffers or now - self.space_at < SPACE_EVERY_S:
            return False
        self.space_at = now
        try:
            st = os.statvfs(ROOT)
            free = st.f_bavail * st.f_frsize
        except OSError:
            return False
        if free >= LOW_FREE_BYTES:
            return False
        uuid = max(self.buffers, key=lambda u: self.buffers[u].started_at)
        self.buffers.pop(uuid).kill()
        self.refused[uuid] = "low disk space"
        self.failed_until[uuid] = now + LOW_SPACE_REST_S
        self.log("timeshift: stopped %s: low disk space (%.1f GB free in the buffer folder)" % (uuid, free / 1e9))
        return True

    def _refuse_reason(self):
        if len(self.buffers) >= self.most:
            return "%d channels are already buffered" % self.most
        try:
            os.makedirs(ROOT, exist_ok=True)
            st = os.statvfs(ROOT)
            if st.f_bavail * st.f_frsize < MIN_FREE_BYTES:
                return "not enough free space"
        except OSError:
            return "the buffer folder can't be made"
        return None

    def reply(self):
        return {
            "v": VERSION, "on": self.on, "window_s": self.window_s, "segment_s": SEGMENT_S,
            "channels": getattr(self, "channels", "all"),
            "buffers": {u: {"url": b.url, "started_at": b.started_at, "ready": b.ready} for u, b in self.buffers.items()},
            "refused": dict(self.refused),
        }

    def _publish(self, users, changed):
        """The state to each user with a TV asking (and a last copy to one that just stopped). Written only when it
        changed, or every 30 s as a sign of life."""
        from django.contrib.auth import get_user_model
        from apps.accounts.models import UserAppData

        body = self.reply()
        now = time.time()
        targets = set(users) | set(self.published)
        for user_id in targets:
            last = self.published.get(user_id)
            if last is not None and last[0] == body and now - last[1] < 30:
                continue
            value = dict(body, at=int(now))
            size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            try:
                user = get_user_model().objects.filter(id=user_id).only("id").first()
                if user is None:
                    self.published.pop(user_id, None)
                    continue
                UserAppData.objects.update_or_create(user=user, key=REPLY_KEY, defaults={"value": value, "byte_size": size})
            except Exception as exc:
                self.log("timeshift: couldn't write to user %s: %s" % (user_id, exc))
                continue
            if user_id in users:
                self.published[user_id] = (body, now)
            else:
                self.published.pop(user_id, None)  # its last copy written: nothing more until it asks again


_thread = None
_manager = None


def start(stop, data_dir, settings_fn, log=None):
    """The timeshift thread, once per service; it ends (stopping every buffer) when the service's stop event is set."""
    global _thread, _manager
    if _thread is not None and _thread.is_alive():
        return _thread
    _manager = Manager(data_dir, settings_fn, log)
    _thread = threading.Thread(target=_manager.run, args=(stop,), name="viewarr-timeshift", daemon=True)
    _thread.start()
    return _thread


def status():
    m = _manager
    if m is None:
        return {"on": None, "buffers": 0}
    folder = getattr(m, "folder", "")
    _real, free, why = suggest_storage(folder)
    real = getattr(m, "real", None)
    return {"on": m.on, "window_min": m.window_s // 60, "buffers": len(m.buffers),
            "channels": sorted(m.buffers), "refused": dict(m.refused),
            "folder": folder, "in_folder": real is not None, "why": why, "free_gb": free}
