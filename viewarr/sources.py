"""Outside sources the admin adds: the plugin contacts no outside site unless its address has been added in the
plugin's settings (like playlist and guide links in Dispatcharr itself). Each setting shows an example to paste.

An empty setting: that source is never contacted, and the features that use it work from Dispatcharr's own data only
(the guide), or do nothing. server.apply() points the helper modules at the addresses every round.
"""
import os
from urllib.parse import urlparse

# name → (setting, example). The example is only ever shown, never used by itself (except once for an existing
# install, see upgrade()).
SOURCES = {
    "sports": ("src_sports", "https://site.api.espn.com"),
    "uk_tv": ("src_uk_tv", "https://www.live-footballontv.com"),
    "sky": ("src_sky", "https://awk.epgsky.com"),
    "justwatch": ("src_justwatch", "https://apis.justwatch.com"),
}
MARK = "sources_set"


def base(settings, name):
    """The added address as "https://host" (whatever path was pasted after it), or "" when none or not usable."""
    raw = str((settings or {}).get(SOURCES[name][0]) or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    u = urlparse(raw)
    if u.scheme not in ("https", "http") or not u.netloc or " " in u.netloc:
        return ""
    return "%s://%s" % (u.scheme, u.netloc)


def upgrade(data_dir, log=None):
    """Once: an install from before these settings existed (the plugin has run here: its status file is there) gets
    every example filled in, so nothing it used stops working. A fresh install starts with all of them empty."""
    from apps.plugins.models import PluginConfig
    conf = PluginConfig.objects.filter(key="viewarr").first()
    if conf is None:
        return
    settings = dict(conf.settings or {})
    if settings.get(MARK):
        return
    existing = os.path.exists(os.path.join(data_dir, "server_status.json"))
    if existing:
        for setting, example in SOURCES.values():
            if not str(settings.get(setting) or "").strip():
                settings[setting] = example
    settings[MARK] = True
    conf.settings = settings
    conf.save(update_fields=["settings"])
    if log:
        log("outside sources: %s" % ("examples filled in for this existing install" if existing
                                     else "none added yet (fresh install)"))


def summary(settings):
    """{name: added address or ""} for the status and the activity page."""
    return {name: base(settings, name) for name in SOURCES}
