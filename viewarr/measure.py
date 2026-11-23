"""Measuring streams at night: real picture size and codec for every stream, so Best stream first (streamorder.py)
and Viewarr's Sources menu work from facts rather than name tags.

Dispatcharr only fills a stream's ``stream_stats`` when the stream plays through its proxy, so most streams never get
measured. This runs ffprobe on each unmeasured (or 30-day-old) stream linked to a real channel, one at a time, inside
a quiet window (setting ``measure_window``, e.g. 01:00-07:00 local time), and writes the result in Dispatcharr's own
``stream_stats`` shape. The main channels come first: the channel groups listed in ``measure_groups``, in that order,
each group whole (the preferred provider's streams first, by channel number), then every other group alphabetically
(or nothing else, with ``measure_only_listed``). Event Channel Managarr's groups are never measured.

The provider counts every connection, so a stream is only probed while the provider has a line to spare: with two or
more lines, one is always left free for watching; with a single line, only when nobody is watching. After each probe
there's a pause (longer after a failure), because providers keep counting a connection for a while after it closes.
A stream that can't be probed is left alone for a week; after three failures it counts as a failing stream for the
ranking (``fails()``).

The probing runs in its own thread started by the service (server.py), because one probe of a dead stream can take
20 s and a batch would otherwise break the service's five-minute rhythm; the thread stops with the service.
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone


def _load_clock():
    """clock.py beside this file (the house's local time; Dispatcharr's own processes run in UTC)."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("viewarr_clock", os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = _load_clock()

STATE_FILE = "measure.json"
RETRY_DAYS = 7
STALE_DAYS = 30
FAILS_FOR_HEALTH = 3
PROBE_TIMEOUT_S = 20
PAUSE_OK_S = 2
PAUSE_FAIL_S = 30
IDLE_WAIT_S = 60
DEFAULT_WINDOW = "01:00-07:00"
DEFAULT_GROUPS = "Entertainment, Movies, Sports, News, Kids, Music, UK Regional, Club TV"
NOW_LIMIT = 20
NOW_SECONDS = 150
# "Measure now" (plugin.py) leaves this file in the data folder rather than probing in the web request; the thread
# below picks it up within a minute and runs the batch.
NOW_FLAG = "measure_now.flag"

_lock = threading.Lock()  # one probe at a time, whichever thread or action asks


# ---- Window

def parse_window(spec):
    """"HH:MM-HH:MM" → (start minutes, end minutes), or None when empty or malformed."""
    try:
        a, b = (p.strip() for p in str(spec or "").split("-", 1))
        h1, m1 = (int(x) for x in a.split(":"))
        h2, m2 = (int(x) for x in b.split(":"))
        return h1 * 60 + m1, h2 * 60 + m2
    except Exception:
        return None


def window_open(spec, now=None):
    """True when local time is inside the window (which may cross midnight)."""
    w = parse_window(spec)
    if not w or w[0] == w[1]:
        return False
    t = clock.local_now(now or time.time())
    mins = t.hour * 60 + t.minute
    start, end = w
    return start <= mins < end if start < end else (mins >= start or mins < end)


def night_key(spec, now=None):
    """The date the current (or most recent) window started on: the counters' name for "tonight"."""
    w = parse_window(spec)
    t = clock.local_now(now or time.time())
    if w and t.hour * 60 + t.minute < w[0]:
        t -= timedelta(days=1)
    return t.strftime("%Y-%m-%d")


# ---- State file

def _path(data_dir):
    return os.path.join(data_dir, STATE_FILE)


def load_state(data_dir):
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st = st if isinstance(st, dict) else {}
    st.setdefault("streams", {})
    st.setdefault("night", {"key": "", "done": 0, "failed": 0})
    st.setdefault("left", None)
    st.setdefault("group", None)
    return st


def save_state(data_dir, state):
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, _path(data_dir))


def _night(state, spec, now):
    key = night_key(spec, now)
    if state["night"].get("key") != key:
        state["night"] = {"key": key, "done": 0, "failed": 0}
    return state["night"]


def fails(data_dir):
    """{stream id: [last try time]} for streams that failed three probes or more, merged by the ranking as failures."""
    out = {}
    for sid, e in load_state(data_dir)["streams"].items():
        if (e.get("fails") or 0) >= FAILS_FOR_HEALTH and str(sid).isdigit():
            out[int(sid)] = [int(e.get("last_try") or 0)]
    return out


def status(data_dir):
    st = load_state(data_dir)
    return {"done_tonight": st["night"].get("done", 0), "failed": st["night"].get("failed", 0),
            "left": st.get("left"), "group": st.get("group")}


# ---- Candidates

def parse_groups(text):
    """The priority list of channel groups, lower case, in order."""
    return [g.strip().lower() for g in str(text or "").replace("\n", ",").split(",") if g.strip()]


def _managed_group_ids():
    """Event Channel Managarr's groups: its channels come and go and aren't measured."""
    from apps.channels.models import ChannelGroup
    from apps.plugins.models import PluginConfig
    try:
        conf = PluginConfig.objects.filter(key="event_channel_managarr", enabled=True).first()
        text = ((conf.settings or {}).get("channel_groups") if conf else "") or ""
        names = {g.strip().lower() for g in str(text).split(",") if g.strip()}
    except Exception:
        names = set()
    # Plus the plugin's own "Groups never reported" (club TV: off air most of the day, a probe would just time out).
    try:
        conf = PluginConfig.objects.filter(key="viewarr").first()
        text = (conf.settings or {}).get("quiet_groups") if conf else None
        names |= {g.strip().lower() for g in str("Club TV" if text is None else text).replace("\n", ",").split(",") if g.strip()}
    except Exception:
        names.add("club tv")
    # (Group names can carry a trailing space in Dispatcharr; the settings text is stripped, so strip these too.)
    return [g.id for g in ChannelGroup.objects.only("id", "name") if (g.name or "").strip().lower() in names]


def _links():
    """{stream id: [(channel group name, channel number)]} over every channel outside the managed groups."""
    from apps.channels.models import ChannelStream
    rows = ChannelStream.objects.exclude(channel__channel_group_id__in=_managed_group_ids()).values_list(
        "stream_id", "channel__channel_group__name", "channel__channel_number")
    links = {}
    for sid, group, chno in rows:
        links.setdefault(sid, []).append((group or "", chno if chno is not None else 1e9))
    return links


def _place(stream_links, listed):
    """Where a stream sits in the order: (group rank, group name, lowest channel number), best of its channels."""
    def rank(group):
        g = group.lower()
        return listed.index(g) if g in listed else len(listed)
    return min((rank(g), g.lower(), chno) for g, chno in stream_links)


def _needs_measure(row, cutoff):
    stats = row.get("stream_stats")
    if not isinstance(stats, dict) or "resolution" not in stats:
        return True
    at = row.get("stream_stats_updated_at")
    return at is None or at < cutoff


def _recently_tried(state, sid, now):
    e = state["streams"].get(str(sid))
    return bool(e) and now - (e.get("last_try") or 0) < RETRY_DAYS * 86400


def _provider_order(todo, first):
    """{account id: position}: the named provider first (else the one with most to do), then the rest by count."""
    counts, names = {}, {}
    for r in todo:
        counts[r["m3u_account_id"]] = counts.get(r["m3u_account_id"], 0) + 1
        names[r["m3u_account_id"]] = (r.get("m3u_account__name") or "").lower()
    wanted = (first or "").strip().lower()
    preferred = next((a for a, n in names.items() if wanted and n == wanted), None)
    if preferred is None and counts:
        preferred = max(counts, key=counts.get)
    ordered = sorted(counts, key=lambda a: (a != preferred, -counts[a], a or 0))
    return {a: i for i, a in enumerate(ordered)}


def candidates(state=None, settings=None, now=None):
    """Streams to measure, in order. Each is a dict with id, name, url, m3u_account_id, m3u_account__name, group."""
    from django.db.models import Q
    from apps.channels.models import Stream
    now = now or time.time()
    state = state or {"streams": {}}
    settings = settings or {}
    listed = parse_groups(settings.get("measure_groups", DEFAULT_GROUPS))
    only_listed = settings.get("measure_only_listed") is True
    links = _links()
    cutoff = datetime.fromtimestamp(now, timezone.utc) - timedelta(days=STALE_DAYS)
    rows = Stream.objects.filter(id__in=list(links)).filter(
        Q(stream_stats__isnull=True) | Q(stream_stats_updated_at__isnull=True) | Q(stream_stats_updated_at__lt=cutoff)
    ).values("id", "name", "url", "m3u_account_id", "m3u_account__name", "stream_stats", "stream_stats_updated_at")
    todo = []
    for r in rows:
        if not r["url"] or not _needs_measure(r, cutoff) or _recently_tried(state, r["id"], now):
            continue
        rank, group, chno = _place(links[r["id"]], listed)
        if only_listed and rank >= len(listed):
            continue
        todo.append({"id": r["id"], "name": r["name"], "url": r["url"], "m3u_account_id": r["m3u_account_id"],
                     "m3u_account__name": r.get("m3u_account__name"), "group": group, "_key": (rank, group, chno)})
    provider = _provider_order(todo, settings.get("measure_first"))
    todo.sort(key=lambda r: (r["_key"][0], r["_key"][1], provider.get(r["m3u_account_id"], 99), r["_key"][2], r["id"]))
    return todo


# ---- The provider's lines

def line_free(account_id):
    """The connection rule, read fresh each time from the provider's active profiles: with max_streams >= 2, probe
    only while current_viewers + 1 <= max_streams - 1 (one line always left for watching); with max_streams == 1,
    only while nobody is watching; an unlimited provider (0) is always free."""
    from apps.m3u.models import M3UAccountProfile
    profiles = list(M3UAccountProfile.objects.filter(m3u_account_id=account_id, is_active=True).only("max_streams", "current_viewers"))
    if not profiles:
        return False
    max_streams = sum(p.max_streams for p in profiles)
    viewers = sum(p.current_viewers for p in profiles)
    if max_streams == 0:
        return True
    if max_streams == 1:
        return viewers == 0
    return viewers + 1 <= max_streams - 1


_UA = {}


def user_agent_for(account_id):
    """The account's own user agent when it has one, else Dispatcharr's default."""
    if account_id in _UA:
        return _UA[account_id]
    ua = None
    try:
        from apps.m3u.models import M3UAccount
        acc = M3UAccount.objects.select_related("user_agent").filter(id=account_id).first()
        ua = acc.user_agent.user_agent if acc and acc.user_agent_id and acc.user_agent else None
        if not ua:
            from core.models import CoreSettings
            ua = CoreSettings.get_default_user_agent()
    except Exception:
        pass
    _UA[account_id] = ua or "Mozilla/5.0"
    return _UA[account_id]


# ---- The probe

def probe_command(url, user_agent):
    return ["ffprobe", "-v", "error", "-user_agent", user_agent, "-rw_timeout", "10000000",
            "-analyzeduration", "4000000", "-probesize", "4000000",
            "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt,channels,sample_rate",
            "-show_entries", "format=format_name", "-of", "json", url]


def _fps(text):
    try:
        a, b = str(text or "0/1").split("/")
        return round(float(a) / float(b), 2) if float(b) else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _channels_text(n):
    return {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(n, str(n) if n else None)


def stats_from(info):
    """ffprobe's JSON → Dispatcharr's stream_stats shape, or None without a video stream."""
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and s.get("width") and s.get("height")), None)
    if video is None:
        return None
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None) or {}
    fps = _fps(video.get("avg_frame_rate")) or _fps(video.get("r_frame_rate"))
    out = {"resolution": "%dx%d" % (int(video["width"]), int(video["height"])), "video_codec": video.get("codec_name"),
           "pixel_format": video.get("pix_fmt"), "stream_type": str((info.get("format") or {}).get("format_name") or "").split(",")[0] or None}
    if fps:
        out["source_fps"] = fps
    if audio:
        out["audio_codec"] = audio.get("codec_name")
        out["audio_channels"] = _channels_text(audio.get("channels"))
        try:
            out["sample_rate"] = int(audio.get("sample_rate"))
        except (TypeError, ValueError):
            pass
    return {k: v for k, v in out.items() if v is not None}


def probe(url, user_agent):
    """(stats, error text): stats is None when the probe failed."""
    try:
        r = subprocess.run(probe_command(url, user_agent), capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except OSError as exc:
        return None, "ffprobe: %s" % exc
    if r.returncode != 0 and not r.stdout.strip():
        err = (r.stderr or "").strip().splitlines()
        return None, err[-1][:160] if err else "ffprobe failed"
    try:
        stats = stats_from(json.loads(r.stdout or "{}"))
    except ValueError:
        return None, "unreadable ffprobe output"
    return (stats, None) if stats else (None, "no video stream")


def record(stream_id, stats):
    from django.utils import timezone as dj_tz
    from apps.channels.models import Stream
    Stream.objects.filter(id=stream_id).update(stream_stats=stats, stream_stats_updated_at=dj_tz.now())


# ---- Batches

def _note(state, row, ok, now, spec):
    night = _night(state, spec, now)
    e = state["streams"].setdefault(str(row["id"]), {"fails": 0})
    e["last_try"] = int(now)
    e["name"] = row["name"][:80]
    if ok:
        e["fails"] = 0
        e["ok"] = int(now)
        night["done"] += 1
    else:
        e["fails"] = (e.get("fails") or 0) + 1
        night["failed"] += 1


def _prune(state, now):
    state["streams"] = {k: v for k, v in state["streams"].items() if now - (v.get("last_try") or 0) < STALE_DAYS * 86400}


def probe_one(state, row, spec, log=None):
    """Probe one candidate and record the outcome. Returns True when it was measured."""
    now = time.time()
    stats, err = probe(row["url"], user_agent_for(row["m3u_account_id"]))
    if stats:
        record(row["id"], stats)
    _note(state, row, bool(stats), now, spec)
    if log and not stats:
        log("measure: %s (%s): %s" % (row["name"][:60], row.get("m3u_account__name"), err))
    return bool(stats)


def _wait(stop, seconds):
    if stop is not None:
        stop.wait(seconds)
    else:
        time.sleep(seconds)


def _announce(state, todo, i, log):
    """Log the group being worked through when it changes: "Measuring: Entertainment, 212 left"."""
    group = todo[i]["group"] or "(no group)"
    if state.get("group") != group:
        state["group"] = group
        if log:
            log("Measuring: %s, %d left" % (group, sum(1 for r in todo[i:] if (r["group"] or "(no group)") == group)))


def run_batch(data_dir, settings, limit, log=None, ignore_window=False, stop=None, seconds=None):
    """Up to [limit] probes, one at a time, each only while the provider has a line free and (unless ignored) the
    window is open. Returns {"done", "failed", "busy", "left"}."""
    from django.db import close_old_connections
    spec = settings.get("measure_window", DEFAULT_WINDOW)
    out = {"done": 0, "failed": 0, "busy": 0, "left": None}
    t0 = time.time()
    with _lock:
        close_old_connections()
        state = load_state(data_dir)
        todo = candidates(state, settings)
        out["left"] = state["left"] = len(todo)
        tried = 0
        for i, row in enumerate(todo):
            if tried >= limit or (stop is not None and stop.is_set()) or (seconds and time.time() - t0 > seconds):
                break
            if not ignore_window and not window_open(spec):
                break
            if not line_free(row["m3u_account_id"]):
                out["busy"] += 1
                if out["busy"] >= 3:
                    break  # the house is watching: try again later
                continue
            _announce(state, todo, i, log)
            tried += 1
            ok = probe_one(state, row, spec, log)
            out["done" if ok else "failed"] += 1
            state["left"] = len(todo) - tried
            save_state(data_dir, state)
            _wait(stop, PAUSE_OK_S if ok else PAUSE_FAIL_S)
        if not todo:
            state["group"] = None
        _prune(state, time.time())
        save_state(data_dir, state)
        close_old_connections()
    return out


# ---- The thread

def take_flag(data_dir, name):
    """True (and the file gone) when the flag file [name] was left in the data folder."""
    try:
        os.remove(os.path.join(data_dir, name))
        return True
    except OSError:
        return False


def _run_now(stop, data_dir, settings, log):
    res = run_batch(data_dir, settings, NOW_LIMIT, ignore_window=True, seconds=NOW_SECONDS, log=log, stop=stop)
    if log:
        log("measure: \"Measure now\": %d measured, %d failed, %s left%s" % (
            res["done"], res["failed"], res["left"] if res["left"] is not None else "?",
            "; the provider had no line free" if res["busy"] and not res["done"] else ""))
    with _lock:
        st = load_state(data_dir)
        st["last_now"] = dict(res, at=int(time.time()))
        save_state(data_dir, st)


def _loop(stop, data_dir, settings_fn, log):
    next_log = 0
    while not stop.is_set():
        try:
            settings = settings_fn() or {}
            if take_flag(data_dir, NOW_FLAG):
                _run_now(stop, data_dir, settings, log)
            on = settings.get("measure_on", True) is not False
            if on and window_open(settings.get("measure_window", DEFAULT_WINDOW)):
                res = run_batch(data_dir, settings, limit=10, log=log, stop=stop)
                if log and res["done"] + res["failed"] and time.time() > next_log:
                    log("measure: %d measured, %d failed, %d left" % (res["done"], res["failed"], res["left"] or 0))
                    next_log = time.time() + 600
                if res["done"] + res["failed"] == 0:
                    stop.wait(IDLE_WAIT_S)  # nothing to do, or the house is watching
                continue
        except Exception as exc:
            if log:
                log("measure: thread round failed: %s" % exc)
        stop.wait(IDLE_WAIT_S)


_thread = None


def start(stop, data_dir, settings_fn, log=None):
    """Start the measuring thread once per service; it ends when the service's stop event is set."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_loop, args=(stop, data_dir, settings_fn, log), name="viewarr-measure", daemon=True)
    _thread.start()
    return _thread
