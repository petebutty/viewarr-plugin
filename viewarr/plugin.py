"""Viewarr house settings for Dispatcharr.

Backups of an admin's whole Viewarr setup are kept in Dispatcharr's plugin data folder (admins only).

The settings live in Dispatcharr's own plugin settings, which every signed-in user can read: Viewarr fetches them
from /api/plugins/plugins/ when it starts. Admins change them here or from Viewarr (Settings → Profiles → Away
addresses). Only addresses are kept, never secrets.
"""

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

    def run(self, action, params, context):
        params = params or {}
        if action == "check":
            return self._check((context or {}).get("settings") or {})
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
                req = urllib.request.Request(away.rstrip("/") + "/", headers={"User-Agent": "Viewarr-plugin"})
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
