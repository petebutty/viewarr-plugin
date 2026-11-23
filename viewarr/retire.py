"""Removing the Sky TV guide and channel logos (both gone in 0.6.8: Schedules Direct, built into Dispatcharr, does the
guide properly, logos included). Run at every start of the service until it has nothing left to do:

  * channels the guide pointed at "Viewarr: Sky guide" go back to the guide they had (guide_backup.json), unless
    someone chose another guide for them since;
  * logos the plugin set go back to the earlier one (logo_backup.json), unless someone changed that logo since;
  * the "Viewarr: Sky guide" source and its file are deleted once no channel uses it any more (a channel still on it,
    chosen by hand, keeps its listings until someone moves it to another guide);
  * the guide's review list in the TVs' app data (viewarr_guide) and the old settings go.
"""
import json
import os
import shutil

DONE = "retired_guide_logos.json"
GUIDE_DIR = "/data/logos/viewarr-epg"
OLD_FILES = ("guide_state.json", "guide_cache.json.gz", "guide.lock", "guide_refresh.flag",
             "logo_state.json", "logo_urls.json", "logo_tvlogos.json", "logos.lock")
OLD_SETTINGS = ("guide", "guide_source", "guide_sky_region", "guide_days", "guide_apply", "guide_overrides",
                "guide_logos", "guide_logos_country", "src_sky")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _guide_sources():
    from apps.epg.models import EPGSource
    return [s for s in EPGSource.objects.all()
            if (s.custom_properties or {}).get("viewarr_guide") or (s.name or "").startswith("Viewarr: ")]


def _put_back_guides(data_dir, ours):
    from apps.channels.models import Channel
    from apps.epg.models import EPGData
    path = os.path.join(data_dir, "guide_backup.json")
    chans = (_load(path, {}) or {}).get("channels") or {}
    n = 0
    for cid, prev in chans.items():
        ch = Channel.objects.filter(id=int(cid)).first()
        if ch is None or not (ch.epg_data_id is None or ch.epg_data_id in ours):
            continue      # deleted, or a guide chosen by hand since: left as it is
        if prev is not None and not EPGData.objects.filter(id=prev).exists():
            prev = None
        ch.epg_data_id = prev
        ch.save(update_fields=["epg_data"])
        n += 1
    if os.path.exists(path):
        os.replace(path, path + ".retired")
    return n


def _put_back_logos(data_dir):
    from apps.channels.models import Channel, Logo
    path = os.path.join(data_dir, "logo_backup.json")
    chans = (_load(path, {}) or {}).get("channels") or {}
    n = 0
    for cid, entry in (chans.items() if isinstance(chans, dict) else []):
        ch = Channel.objects.filter(id=int(cid)).first()
        if ch is None or ch.logo_id != entry.get("set"):
            continue      # deleted, or changed by hand since
        prev = entry.get("prev")
        if prev is not None and not Logo.objects.filter(id=prev).exists():
            prev = None
        ch.logo_id = prev
        ch.save(update_fields=["logo", "updated_at"])
        n += 1
    if os.path.exists(path):
        os.replace(path, path + ".retired")
    return n


def run(data_dir, log=None):
    if os.path.exists(os.path.join(data_dir, DONE)):
        return
    from apps.accounts.models import UserAppData
    from apps.channels.models import Channel
    from apps.epg.models import EPGData
    from apps.plugins.models import PluginConfig
    srcs = _guide_sources()
    ours = set(EPGData.objects.filter(epg_source__in=srcs).values_list("id", flat=True)) if srcs else set()
    guides = _put_back_guides(data_dir, ours)
    logos = _put_back_logos(data_dir)
    UserAppData.objects.filter(key="viewarr_guide").delete()
    conf = PluginConfig.objects.filter(key="viewarr").first()
    if conf is not None and any(k in (conf.settings or {}) for k in OLD_SETTINGS):
        conf.settings = {k: v for k, v in (conf.settings or {}).items() if k not in OLD_SETTINGS}
        conf.save(update_fields=["settings"])
    for name in OLD_FILES:
        try:
            os.remove(os.path.join(data_dir, name))
        except OSError:
            pass
    still = Channel.objects.filter(epg_data_id__in=ours).count() if ours else 0
    if still == 0:
        for s in srcs:
            s.delete()
        shutil.rmtree(GUIDE_DIR, ignore_errors=True)
    if log and (guides or logos or srcs):
        log("Sky TV guide and channel logos removed: %d channel(s) back on their earlier guide, %d logo(s) put back%s"
            % (guides, logos, (", the plugin's guide source kept while %d channel(s) still use it" % still) if still
               else (", the plugin's guide source deleted" if srcs else "")))
    if still == 0:
        with open(os.path.join(data_dir, DONE), "w", encoding="utf-8") as f:
            json.dump({"done": True}, f)
