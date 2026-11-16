"""Viewarr house settings for Dispatcharr.

The settings live in Dispatcharr's own plugin settings, which every signed-in user can read: Viewarr fetches them
from /api/plugins/plugins/ when it starts. Admins change them here or from Viewarr (Settings → Profiles → Away
addresses). Only addresses are kept, never secrets.
"""

import json
import os
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(_HERE, "plugin.json"), encoding="utf-8") as f:
        _MANIFEST = json.load(f)
except Exception:  # a broken manifest mustn't stop Dispatcharr loading the plugin list
    _MANIFEST = {}


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
        if action == "check":
            return self._check((context or {}).get("settings") or {})
        return {"status": "error", "message": f"Unknown action '{action}'."}

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
