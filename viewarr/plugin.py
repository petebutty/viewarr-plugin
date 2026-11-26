"""Viewarr house settings for Dispatcharr, and server-side sports (server.py, sports.py), Best stream first
(streamorder.py), reports from the TVs (reports.py), measuring (measure.py) and live timeshift (timeshift.py).

Backups of an admin's whole Viewarr setup are kept in Dispatcharr's plugin data folder (admins only).

The settings live in Dispatcharr's own plugin settings, which every signed-in user can read: Viewarr fetches them
from /api/plugins/plugins/ when it starts. Admins change them here or from Viewarr (Settings → Profiles → Away
addresses). Only addresses are kept, never secrets.
"""

import importlib.util
import json
import os
import shutil
import re
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "plugin.json"), encoding="utf-8") as f:
        _MANIFEST = json.load(f)
except Exception:  # a broken manifest mustn't stop Dispatcharr loading the plugin list
    _MANIFEST = {}


# The plugin's own data folder (backups, state): the one Dispatcharr hands over in context["data_dir"], never served by
# any Dispatcharr address and kept when the plugin itself is updated or reinstalled. Until a context has been seen
# (import time, before the first action or leadership hook) it's where that folder has always been.
_OLD_DATA = os.path.join(os.path.dirname(_HERE), "_data", "viewarr")
_DATA = _OLD_DATA
# Dispatcharr's watched logos folder (its web server serves it): where the timeshift's link sits.
_LOGOS = "/data/logos"


def _backups():
    return os.path.join(_DATA, "backups")


def _carry_over(old, new):
    """Whatever the old data folder holds that the new one doesn't (backups, state) is copied across, so a change of
    place loses nothing. The old folder is left as it is; nothing already in the new one is replaced."""
    if not os.path.isdir(old) or os.path.abspath(old) == os.path.abspath(new):
        return
    for root, _dirs, files in os.walk(old):
        rel = os.path.relpath(root, old)
        dest = new if rel == "." else os.path.join(new, rel)
        os.makedirs(dest, exist_ok=True)
        for name in files:
            target = os.path.join(dest, name)
            if not os.path.exists(target):
                try:
                    shutil.copy2(os.path.join(root, name), target)
                except OSError:
                    pass


def _use(context):
    """Takes the plugin's folders from the context Dispatcharr gives every action and leadership hook."""
    global _DATA, _LOGOS
    ctx = context or {}
    try:
        data = ctx.get("data_dir")
        if data:
            data = str(data)
            if os.path.abspath(data) != os.path.abspath(_DATA):
                os.makedirs(data, exist_ok=True)
                _carry_over(_OLD_DATA, data)
                _DATA = data
    except Exception:
        pass
    try:
        logos = ((ctx.get("dispatcharr") or {}).get("directories") or {}).get("logos")
        if logos:
            _LOGOS = str(logos)
    except Exception:
        pass
_KEEP = 30
_ID = re.compile(r"^[0-9]{13}$")


def _hhmm(ts):
    """HH:MM in the house's time zone (clock.py: Dispatcharr's processes run in UTC)."""
    try:
        spec = importlib.util.spec_from_file_location("viewarr_clock", os.path.join(_HERE, "clock.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.strftime("%H:%M", ts)
    except Exception:
        return time.strftime("%H:%M", time.gmtime(ts)) + " UTC"


class Plugin:
    name = _MANIFEST.get("name", "Viewarr")
    version = _MANIFEST.get("version", "0.1.0")
    description = _MANIFEST.get("description", "House settings for Viewarr.")
    author = _MANIFEST.get("author", "")
    help_url = _MANIFEST.get("help_url", "")
    fields = _MANIFEST.get("fields", [])
    actions = _MANIFEST.get("actions", [])

    # ---- Server-side sports (server.py): Live now and where to watch each game, worked out here every few minutes
    # for the whole house and published to every user's app data, so the TVs don't each do it. Runs on the one
    # Dispatcharr process holding the plugin's leadership (Dispatcharr 0.34.0+: the persistent_service capability).

    @property
    def _status_path(self):
        return os.path.join(_DATA, "server_status.json")

    def _server(self):
        spec = importlib.util.spec_from_file_location("viewarr_server", os.path.join(_HERE, "server.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # (A fresh copy each time: it's told where the folders are.)
        mod.DATA_DIR = _DATA
        mod.LOGOS_DIR = _LOGOS
        return mod

    def _module(self, name):
        spec = importlib.util.spec_from_file_location("viewarr_" + name, os.path.join(_HERE, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def on_leader_acquired(self, context):
        log = (context or {}).get("logger")
        _use(context)
        os.makedirs(_DATA, exist_ok=True)
        server = self._server()
        context["start_service"](lambda stop: server.service(stop, log=log, status_path=self._status_path), name="sports")

    def on_leader_lost(self, context):
        # (Dispatcharr stops the service itself; nothing else is held.)
        return None

    def _server_status(self):
        try:
            with open(self._status_path, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return {"status": "success", "message": "The sports service hasn't finished a round yet."}
        if st.get("error"):
            return {"status": "error", "message": "Last round failed: " + st["error"], "result": st}
        when = _hhmm(st.get("at", 0))
        return {"status": "success", "result": st, "message":
                f"Last round {when}: {st.get('live', 0)} live now, {st.get('games', 0)} games placed, "
                f"{st.get('bytes', 0) // 1024} KB to {st.get('users', 0)} users in {st.get('seconds', 0)} s; "
                f"{st.get('recorded', 0)} recordings made; "
                f"watch sync: {(st.get('watch') or {}).get('users', 0)} users, {(st.get('watch') or {}).get('pulled', 0)} in, "
                f"{(st.get('watch') or {}).get('pushed', 0)} out; "
                f"streams reordered: {(st.get('order') or {}).get('reordered', 0)} this round, "
                f"{(st.get('order') or {}).get('week', 0)} this week; "
                f"reports: {(st.get('reports') or {}).get('handled', 0)} handled, "
                f"{(st.get('reports') or {}).get('applied', 0)} changes; "
                f"measured: {self._measured_text(st.get('measured') or {})}; "
                f"live timeshift: {self._timeshift_text(st.get('timeshift') or {})}"
                + (f"; switched off: {', '.join(st['off'])}" if st.get("off") else "") + "."}

    @staticmethod
    def _timeshift_text(t):
        if t.get("on") is False:
            return "off"
        n = t.get("buffers", 0)
        where = ""
        if "mapped" in t:
            free, need = t.get("free_gb"), t.get("need_gb")
            where = (f"; kept in /data/timeshift ({'its own disk' if t['mapped'] else 'inside Dispatcharr' + chr(39) + 's data folder'}"
                     + (f", {free} GB free" if free is not None else ", can't be written to") + ")")
            if free is not None and need and free < need:
                where += f"; may run short: the settings can need about {need} GB"
        return f"{n} channel(s) buffered" + (f", window {t['window_min']} min" if t.get("window_min") else "") + where

    @staticmethod
    def _measured_text(m):
        left = m.get("left")
        group = f" (on {m.get('group')})" if m.get("group") else ""
        return f"{m.get('done_tonight', 0)} tonight, {m.get('failed', 0)} failed, {left if left is not None else '?'} left{group}"

    def _measure_now(self, context):
        """One batch of up to 20 probes, ignoring the window but never the provider's spare line: queued for the
        measuring thread (it can take a few minutes, too long for a web request)."""
        try:
            ms = self._module("measure")
            self._queue(ms.NOW_FLAG)
            last = (ms.load_state(_DATA).get("last_now") or {})
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't queue measuring: {exc}"}
        prev = (f" Last time ({_hhmm(last['at'])}): measured {last.get('done', 0)}, failed {last.get('failed', 0)}, "
                f"{last.get('left') if last.get('left') is not None else '?'} still to do." if last.get("at") else "")
        return {"status": "success", "queued": True, "result": last or None,
                "message": f"Measuring queued: up to {ms.NOW_LIMIT} streams, starting within a minute (the results "
                           f"show in the plugin status).{prev}"}

    # ---- Best stream first and reports from the TVs (streamorder.py, reports.py), run by the service each round.

    def _stream_order_plan(self):
        """What Best stream first would change now, without changing anything (a dry run after a night's measuring)."""
        try:
            so = self._module("streamorder")
            me = self._module("measure")
            extra = {}
            try:
                extra = me.fails(_DATA)
            except Exception:
                pass
            plan = so.plan(_DATA, extra_fails=extra)
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't work out the order: {exc}"}
        lines = [f"{x['name']}: {x['current_first']} -> {x['proposed_first']} (gain {x['gain']})"
                 for x in sorted(plan, key=lambda x: -x["gain"])]
        return {"status": "success", "result": {"count": len(plan), "changes": lines[:200]},
                "message": f"{len(plan)} channel(s) would change. " + ("; ".join(lines[:25]) if lines else "")}

    def _stream_order_reset(self):
        try:
            n = self._module("streamorder").reset(_DATA)
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't put the order back: {exc}"}
        return {"status": "success", "message": f"Stream order put back on {n} channel(s). If \"Best stream first\" "
                                                f"stays on, the next round may move them again."}

    def _undo_change(self, params, context):
        # (Viewarr sends the id as a parameter; from Dispatcharr's own page, paste it into the "Change to undo" box.)
        cid = str(params.get("id") or ((context or {}).get("settings") or {}).get("undo_id") or "").strip()
        if not cid:
            return {"status": "error", "message": "Which change? Give its id (see \"Recent changes from reports\")."}
        try:
            ok, msg = self._module("reports").undo(_DATA, cid)
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't undo: {exc}"}
        return {"status": "success" if ok else "error", "message": msg}

    def _reports_status(self):
        try:
            return {"status": "success", "message": self._module("reports").status_text(_DATA)}
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't read the change log: {exc}"}

    @staticmethod
    def _queue(flag):
        """Leaves [flag] in the data folder for the service's threads, which pick it up within a minute (long jobs
        don't run inside the web request, where the web server gives up after a few minutes)."""
        os.makedirs(_DATA, exist_ok=True)
        with open(os.path.join(_DATA, flag), "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))

    def _server_refresh(self, context):
        try:
            self._queue("refresh.flag")      # (server.REFRESH_FLAG)
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't queue a refresh: {exc}"}
        last = self._server_status()
        return {"status": "success", "result": last.get("result"), "queued": True,
                "message": "Refresh queued: the sports service runs a round within a minute. Until then: "
                           + last.get("message", "")}

    def run(self, action, params, context):
        params = params or {}
        _use(context)
        if action == "server_status":
            return self._server_status()
        if action == "server_refresh":
            return self._server_refresh(context)
        if action == "stream_order_plan":
            return self._stream_order_plan()
        if action == "stream_order_reset":
            return self._stream_order_reset()
        if action == "undo_change":
            return self._undo_change(params, context)
        if action == "reports_status":
            return self._reports_status()
        if action == "measure_now":
            return self._measure_now(context)
        if action == "just_added_from_tv":
            return self._just_added_from_tv(params, context)
        if action.startswith("backup_") and ((context or {}).get("settings") or {}).get("backups", True) is False:
            return {"status": "error", "message": "Backups are switched off in the Viewarr plugin's settings."}
        if action == "backup_save":
            return self._backup_save(params)
        if action == "backup_list":
            return self._backup_list()
        if action == "backup_get":
            return self._backup_get(params)
        if action == "backup_delete":
            return self._backup_delete(params)
        return {"status": "error", "message": f"Unknown action '{action}'."}

    # (backup_save / backup_list / backup_get / backup_delete are called by Viewarr itself and left out of plugin.json
    # on purpose: Dispatcharr's manifest has no way to hide an action, so each would show as a button on the plugin's
    # page that does nothing useful there. Manifest version 1 lets undeclared actions run; a move to version 2 would
    # need them declared.)
    # Backups: Viewarr's whole setup as Viewarr saves it (its settings, and its sign-ins already encrypted on the TV
    # with the admin's own key). Admins only: Dispatcharr lets only admins run plugin actions.

    # just_added_from_tv: called by Viewarr itself (undeclared, as the backups): a TV's own JustWatch lists for the
    # house, since JustWatch refuses servers behind a VPN (justadded.from_tv). Admins only, as every plugin action.
    def _just_added_from_tv(self, params, context):
        log = (context or {}).get("logger")
        try:
            got = self._module("justadded").from_tv(_DATA, params.get("data"), log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't take the Just added lists: {exc}"}
        return {"status": "success", "result": got, "message": "Just added lists shared with everyone."}

    def _backup_save(self, params):
        data = params.get("data")
        if not isinstance(data, dict):
            return {"status": "error", "message": "No setup to save."}
        os.makedirs(_backups(), exist_ok=True)
        bid = str(int(time.time() * 1000))
        entry = {
            "id": bid,
            "name": str(params.get("name") or "Backup")[:80],
            "savedAt": int(bid),
            "data": data,
        }
        with open(os.path.join(_backups(), bid + ".json"), "w", encoding="utf-8") as f:
            json.dump(entry, f)
        # Only the newest few are kept.
        for old in sorted(self._ids(), reverse=True)[_KEEP:]:
            try:
                os.remove(os.path.join(_backups(), old + ".json"))
            except OSError:
                pass
        return {"status": "success", "message": "Backup saved.", "id": bid}

    def _ids(self):
        if not os.path.isdir(_backups()):
            return []
        return [n[:-5] for n in os.listdir(_backups()) if n.endswith(".json") and _ID.match(n[:-5])]

    def _backup_list(self):
        items = []
        for bid in sorted(self._ids(), reverse=True):
            path = os.path.join(_backups(), bid + ".json")
            try:
                with open(path, encoding="utf-8") as f:
                    e = json.load(f)
                items.append({"id": bid, "name": e.get("name", "Backup"), "savedAt": e.get("savedAt", int(bid)),
                              "device": (e.get("data") or {}).get("device", ""), "size": os.path.getsize(path)})
            except Exception:
                continue
        return {"status": "success", "message": f"{len(items)} backup(s).", "backups": items}

    def _backup_get(self, params):
        bid = str(params.get("id") or "")
        if not _ID.match(bid) or not os.path.isfile(os.path.join(_backups(), bid + ".json")):
            return {"status": "error", "message": "That backup isn't there."}
        with open(os.path.join(_backups(), bid + ".json"), encoding="utf-8") as f:
            e = json.load(f)
        return {"status": "success", "message": "Backup found.", "backup": e}

    def _backup_delete(self, params):
        bid = str(params.get("id") or "")
        if not _ID.match(bid):
            return {"status": "error", "message": "That backup isn't there."}
        try:
            os.remove(os.path.join(_backups(), bid + ".json"))
        except OSError:
            return {"status": "error", "message": "That backup isn't there."}
        return {"status": "success", "message": "Backup deleted."}
