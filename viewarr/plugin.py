"""Viewarr house settings for Dispatcharr, and server-side sports (server.py, sports.py), Best stream first
(streamorder.py) and reports from the TVs (reports.py).

Backups of an admin's whole Viewarr setup are kept in Dispatcharr's plugin data folder (admins only).

The settings live in Dispatcharr's own plugin settings, which every signed-in user can read: Viewarr fetches them
from /api/plugins/plugins/ when it starts. Admins change them here or from Viewarr (Settings → Profiles → Away
addresses). Only addresses are kept, never secrets.
"""

import importlib.util
import json
import os
import re
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "plugin.json"), encoding="utf-8") as f:
        _MANIFEST = json.load(f)
except Exception:  # a broken manifest mustn't stop Dispatcharr loading the plugin list
    _MANIFEST = {}


# Backups live beside the plugins, in Dispatcharr's plugin data folder: never served by any Dispatcharr address, and
# kept when the plugin itself is updated or reinstalled.
_DATA = os.path.join(os.path.dirname(_HERE), "_data", "viewarr")
_BACKUPS = os.path.join(_DATA, "backups")
_KEEP = 30
_ID = re.compile(r"^[0-9]{13}$")


def parse_away(text):
    """'home = away' lines → list of (home, away). Blank lines and lines starting with # are skipped."""
    pairs = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        home, away = (part.strip() for part in line.split("=", 1))
        if home and away:
            if "://" not in away:
                away = "https://" + away
            pairs.append((home, away))
    return pairs


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

    _status_path = os.path.join(_DATA, "server_status.json")

    def _server(self):
        spec = importlib.util.spec_from_file_location("viewarr_server", os.path.join(_HERE, "server.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _module(self, name):
        spec = importlib.util.spec_from_file_location("viewarr_" + name, os.path.join(_HERE, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def on_leader_acquired(self, context):
        log = (context or {}).get("logger")
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
        when = time.strftime("%H:%M", time.localtime(st.get("at", 0)))
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
                f"measured: {self._measured_text(st.get('measured') or {})}."}

    @staticmethod
    def _measured_text(m):
        left = m.get("left")
        group = f" (on {m.get('group')})" if m.get("group") else ""
        return f"{m.get('done_tonight', 0)} tonight, {m.get('failed', 0)} failed, {left if left is not None else '?'} left{group}"

    def _measure_now(self, context):
        """One batch of up to 20 probes straight away, ignoring the window but never the provider's spare line."""
        settings = (context or {}).get("settings") or {}
        log = (context or {}).get("logger")
        try:
            ms = self._module("measure")
            res = ms.run_batch(_DATA, settings, ms.NOW_LIMIT, ignore_window=True, seconds=ms.NOW_SECONDS,
                               log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't measure: {exc}"}
        busy = "; the provider had no line free" if res["busy"] and not res["done"] else ""
        return {"status": "success", "result": res,
                "message": f"Measured {res['done']}, failed {res['failed']}, {res['left']} still to do{busy}."}

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

    def _server_refresh(self, context):
        log = (context or {}).get("logger")
        try:
            st = self._server().publish(log)
        except Exception as exc:
            return {"status": "error", "message": f"Couldn't refresh: {exc}"}
        try:
            os.makedirs(_DATA, exist_ok=True)
            with open(self._status_path, "w", encoding="utf-8") as f:
                json.dump(st, f)
        except Exception:
            pass
        return {"status": "success", "result": st,
                "message": f"Refreshed: {st.get('live', 0)} live now, {st.get('games', 0)} games placed."}

    def run(self, action, params, context):
        params = params or {}
        if action == "server_status":
            return self._server_status()
        if action == "server_refresh":
            return self._server_refresh(context)
        if action == "check":
            return self._check((context or {}).get("settings") or {})
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
        if action == "backup_save":
            return self._backup_save(params)
        if action == "backup_list":
            return self._backup_list()
        if action == "backup_get":
            return self._backup_get(params)
        if action == "backup_delete":
            return self._backup_delete(params)
        return {"status": "error", "message": f"Unknown action '{action}'."}

    # Backups: Viewarr's whole setup as Viewarr saves it (its settings, and its sign-ins already encrypted on the TV
    # with the admin's own key). Admins only: Dispatcharr lets only admins run plugin actions.

    def _backup_save(self, params):
        data = params.get("data")
        if not isinstance(data, dict):
            return {"status": "error", "message": "No setup to save."}
        os.makedirs(_BACKUPS, exist_ok=True)
        bid = str(int(time.time() * 1000))
        entry = {
            "id": bid,
            "name": str(params.get("name") or "Backup")[:80],
            "savedAt": int(bid),
            "data": data,
        }
        with open(os.path.join(_BACKUPS, bid + ".json"), "w", encoding="utf-8") as f:
            json.dump(entry, f)
        # Only the newest few are kept.
        for old in sorted(self._ids(), reverse=True)[_KEEP:]:
            try:
                os.remove(os.path.join(_BACKUPS, old + ".json"))
            except OSError:
                pass
        return {"status": "success", "message": "Backup saved.", "id": bid}

    def _ids(self):
        if not os.path.isdir(_BACKUPS):
            return []
        return [n[:-5] for n in os.listdir(_BACKUPS) if n.endswith(".json") and _ID.match(n[:-5])]

    def _backup_list(self):
        items = []
        for bid in sorted(self._ids(), reverse=True):
            path = os.path.join(_BACKUPS, bid + ".json")
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
        if not _ID.match(bid) or not os.path.isfile(os.path.join(_BACKUPS, bid + ".json")):
            return {"status": "error", "message": "That backup isn't there."}
        with open(os.path.join(_BACKUPS, bid + ".json"), encoding="utf-8") as f:
            e = json.load(f)
        return {"status": "success", "message": "Backup found.", "backup": e}

    def _backup_delete(self, params):
        bid = str(params.get("id") or "")
        if not _ID.match(bid):
            return {"status": "error", "message": "That backup isn't there."}
        try:
            os.remove(os.path.join(_BACKUPS, bid + ".json"))
        except OSError:
            return {"status": "error", "message": "That backup isn't there."}
        return {"status": "success", "message": "Backup deleted."}

    def _check(self, settings):
        pairs = parse_away(settings.get("away", ""))
        if not pairs:
            return {"status": "success", "message": "No outside addresses set yet."}
        lines = []
        bad = 0
        for home, away in pairs:
            try:
                req = urllib.request.Request(away.rstrip("/") + "/", headers={"User-Agent": "Viewarr-Plugin/0.5 (+https://github.com/petebutty/viewarr-plugin)"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    lines.append(f"{away}: answers ({r.status})")
            except urllib.error.HTTPError as e:
                # Any HTTP answer (even 401/404 from a locked-down front page) means the address is reachable.
                lines.append(f"{away}: answers ({e.code})")
            except Exception as e:
                bad += 1
                lines.append(f"{away}: no answer ({type(e).__name__})")
        status = "success" if bad == 0 else "error"
        return {"status": status, "message": "\n".join(lines)}
