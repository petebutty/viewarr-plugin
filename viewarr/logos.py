"""Channel logos for Viewarr: fills in (or replaces) the logos of the house's channels from two sources, in order:

  1. Sky's own channel logos, for channels the TV guide (guide.py) has matched to a Sky channel (only while the TV
     guide is on, since it's what keeps those matches fresh);
  2. the tv-logo/tv-logos collection on GitHub (https://github.com/tv-logo/tv-logos), for the other channels in the
     lineup, matched by name inside the chosen country's folder;
  3. anything neither source has is left alone.

Off by default (setting "guide_logos"). "missing" fills in only channels with no logo or a broken one; "all" uses the
source's logo for every channel it has one for. Its own thread: once a day (05:15, after the morning guide run), a
couple of minutes after its settings change, and after each TV guide run.

Safety:
  * every change is recorded first in logo_backup.json: the channel's earlier logo id and the logo id set here;
  * switching it off (or a source no longer giving a channel a logo) puts the earlier logo back, unless someone has
    changed that channel's logo since (the channel's logo is no longer the one set here): such a channel is never
    touched again ("hands_off" in the backup);
  * channels with a hand-set logo override in Dispatcharr (ChannelOverride.logo) are left alone;
  * an existing Logo row with the same URL is reused rather than making a duplicate;
  * every new logo URL is checked once (a tiny ranged GET, cached) before it's used.

Which channels tv-logos may touch: those in the lineup the TV guide uses (enabled in a channel profile, not radio),
leaving out channels Dispatcharr made automatically from a provider's groups (24/7 and event channels), any group
whose name starts "24/7", and names that look like event slots ("Event 3", "PPV 12", "20:00", "A vs B").
Sky's logos are used for every channel the TV guide matched to Sky (the guide's own lineup rules).
"""

import importlib.util
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request


def _load_clock():
    """clock.py beside this file (the house's local time; Dispatcharr's own processes run in UTC)."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("viewarr_clock", os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = _load_clock()

_HERE = os.path.dirname(os.path.abspath(__file__))

UA = "Viewarr-Plugin/0.6 (+https://github.com/petebutty/viewarr-plugin)"

STATE_FILE = "logo_state.json"
BACKUP_FILE = "logo_backup.json"
URLS_FILE = "logo_urls.json"
TVLOGOS_FILE = "logo_tvlogos.json"
LOCK_FILE = "logos.lock"

RUN_AT = (5, 15)                 # local time of the daily run
TICK_S = 60
SETTLE_S = 120                   # after a settings change, wait this long for more changes before running
TVLOGOS_EVERY_S = 86400          # the tv-logos folder listing is read at most once a day
TVLOGOS_RETRY_S = 6 * 3600       # ...or this long after a failed read
URL_OK_TTL = 30 * 86400          # a logo URL that worked is trusted this long
URL_BAD_TTL = 86400              # one that didn't is tried again after this long
MAX_CHECKS = 400                 # at most this many URL checks per run (the rest wait for the next run)
CHECK_DELAY = 0.25               # pause after each URL check

TVL_ACCEPT = 0.9                 # a tv-logos name match at least this good is taken (exact names score 1.0)

MODES = ("off", "missing", "all")
SOURCES = ("sky_tvlogos", "sky", "tvlogos")
# tv-logos country folder → (label, the country code at the end of its file names)
COUNTRIES = {
    "united-kingdom": ("United Kingdom", "uk"), "ireland": ("Ireland", "ie"),
    "united-states": ("United States", "us"), "canada": ("Canada", "ca"), "australia": ("Australia", "au"),
    "germany": ("Germany", "de"), "france": ("France", "fr"), "spain": ("Spain", "es"), "italy": ("Italy", "it"),
    "netherlands": ("Netherlands", "nl"),
}
DEFAULT_COUNTRY = "united-kingdom"
TVL_API = "https://api.github.com/repos/tv-logo/tv-logos"
TVL_RAW = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries/%s/%s"

# A logo whose name or address says it's a stand-in counts as missing.
_PLACEHOLDER = re.compile(r"(?i)placeholder|no[-_ ]?logo|default[-_ ]?logo|blank\.(png|jpe?g|svg)|missing\.(png|jpe?g)")
_EVENT_NAME = re.compile(r"(?i)\b\d{1,2}[:.]\d{2}\b|\bevents?\s*\d+|\bppv\s*\d+|\s(vs?\.?|@)\s")
# File-name words that mark a variant rather than part of the name: kept as a rank (lower is better) or skipped.
_VARIANT_RANK = {"hz": 1, "white": 2}
_VARIANT_SKIP = {"light", "icon", "badge", "mosaic"}

_GUIDE = None


def _guide():
    """guide.py beside this file (its name matching and its state file), loaded by path once."""
    global _GUIDE
    if _GUIDE is None:
        spec = importlib.util.spec_from_file_location("viewarr_guide_for_logos", os.path.join(_HERE, "guide.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _GUIDE = mod
    return _GUIDE


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------------------------
# Settings

def config(settings):
    s = settings or {}
    raw = s.get("guide_logos", "off")
    if raw is True:                      # (a plain on / off switch from an older app: on = fill in missing ones)
        raw = "missing"
    mode = str(raw or "off").strip().lower()
    if mode not in MODES:
        mode = "off"
    sources = str(s.get("logos_sources") or "sky_tvlogos").strip()
    if sources not in SOURCES:
        sources = "sky_tvlogos"
    country = str(s.get("logos_country") or DEFAULT_COUNTRY).strip()
    if country not in COUNTRIES:
        country = DEFAULT_COUNTRY
    guide_on = s.get("guide", False) is True or str(s.get("guide")).lower() == "true"
    use_sky = sources in ("sky_tvlogos", "sky") and guide_on
    use_tvl = sources in ("sky_tvlogos", "tvlogos")
    sig = json.dumps([mode, sources, country, guide_on])
    return {"mode": mode, "on": mode != "off", "sources": sources, "country": country, "guide_on": guide_on,
            "sky": use_sky, "tvlogos": use_tvl, "sig": sig}


# ---------------------------------------------------------------------------------------------------------------
# Checking a logo address: a tiny ranged GET, the first bytes must be a picture. Results are cached.

_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"<svg", b"<?xm", b"\x00\x00\x01\x00", b"BM")


class Checker:
    def __init__(self, data_dir, stop=None, limit=MAX_CHECKS):
        self.path = os.path.join(data_dir, URLS_FILE)
        self.cache = _load_json(self.path, {})
        self.stop = stop
        self.limit = limit
        self.checked = 0
        self.dirty = False

    def _fetch(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-63", "Accept": "image/*"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status not in (200, 206):
                    return False
                head = r.read(64)
        except Exception:
            return False
        head = head.lstrip()
        return bool(head) and any(head.startswith(m) for m in _MAGIC)

    def ok(self, url, now=None):
        """True / False, or None when the check had to wait (this run's checks are used up, or stopping)."""
        if not url:
            return False
        if url.startswith("/data/"):
            return os.path.isfile(os.path.normpath(url)) and os.path.getsize(os.path.normpath(url)) > 0
        if not url.startswith(("http://", "https://")):
            return False
        now = now or time.time()
        hit = self.cache.get(url)
        if hit and now - hit[1] < (URL_OK_TTL if hit[0] else URL_BAD_TTL):
            return bool(hit[0])
        if self.checked >= self.limit or (self.stop is not None and self.stop.is_set()):
            return None if not hit else bool(hit[0])
        self.checked += 1
        good = self._fetch(url)
        self.cache[url] = [good, int(now)]
        self.dirty = True
        if self.stop is not None:
            self.stop.wait(CHECK_DELAY)
        else:
            time.sleep(CHECK_DELAY)
        return good

    def save(self):
        if not self.dirty:
            return
        now = time.time()
        # (Entries not looked at for 90 days are dropped.)
        self.cache = {u: v for u, v in self.cache.items() if now - v[1] < 90 * 86400}
        _save_json(self.path, self.cache)


# ---------------------------------------------------------------------------------------------------------------
# tv-logos: one folder listing per country per day (two GitHub API requests), kept on disk.

def _gh_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def tvlogos_files(data_dir, country, log=None, now=None, fetch=True):
    """The country folder's top-level .png file names (its sub-folders hd / obsolete / other / screen-bug aren't
    used). Read at most once a day; the last good listing is used when GitHub can't be reached."""
    now = now or time.time()
    path = os.path.join(data_dir, TVLOGOS_FILE)
    cache = _load_json(path, {})
    entry = (cache.get("countries") or {}).get(country) or {}
    fresh = entry.get("files") and now - entry.get("at", 0) < TVLOGOS_EVERY_S
    tried_recently = now - entry.get("tried", 0) < TVLOGOS_RETRY_S
    if fresh or not fetch or (tried_recently and entry.get("files")):
        return entry.get("files") or []
    entry["tried"] = int(now)
    files = None
    listing = _gh_json(TVL_API + "/contents/countries")
    sha = None
    if isinstance(listing, list):
        sha = next((x.get("sha") for x in listing if x.get("name") == country and x.get("type") == "dir"), None)
    if sha:
        tree = _gh_json(TVL_API + "/git/trees/" + sha)
        if isinstance(tree, dict) and isinstance(tree.get("tree"), list):
            files = sorted(x["path"] for x in tree["tree"]
                           if x.get("type") == "blob" and str(x.get("path", "")).lower().endswith(".png"))
    if files:
        entry.update(files=files, at=int(now), sha=sha)
        if log:
            log("logos: tv-logos %s listing read (%d logos)" % (COUNTRIES[country][0], len(files)))
    elif log:
        log("logos: couldn't read the tv-logos %s listing; %s" % (
            COUNTRIES[country][0], "using the last one" if entry.get("files") else "tv-logos skipped this time"))
    cache.setdefault("countries", {})[country] = entry
    try:
        _save_json(path, cache)
    except Exception:
        pass
    return entry.get("files") or []


def tvlogos_index(files, country):
    """[(Norm, variant rank, file name)] for matching. "challenge-plus-uk.png" is Challenge +1, "fox-hits-plus-2-au"
    is +2; "hz" (horizontal) and "white" versions rank after the plain one; "light" versions (for light backgrounds)
    and icons are skipped."""
    g = _guide()
    cc = COUNTRIES.get(country, ("", ""))[1]
    out = []
    for fn in files:
        stem = fn[:-4].lower()
        toks = stem.split("-")
        if cc and len(toks) > 1 and toks[-1] == cc:
            toks = toks[:-1]
        if any(t in _VARIANT_SKIP for t in toks):
            continue
        rank = sum(_VARIANT_RANK.get(t, 0) for t in toks)
        toks = [t for t in toks if t not in _VARIANT_RANK]
        words = []
        i = 0
        while i < len(toks):
            t = toks[i]
            if t == "plus" and i + 1 < len(toks) and toks[i + 1].isdigit() and i + 2 == len(toks):
                words.append("+" + toks[i + 1])
                i += 2
                continue
            if t == "plus" and i + 1 == len(toks) and i > 0:
                words.append("+1")
            else:
                words.append(t)
            i += 1
        name = " ".join(words)
        if name:
            out.append((g.Norm(g.norm_text(name)), rank, fn))
    return out


def tvlogos_match(name, index, by_key=None):
    """The best file for a channel name, or None. Exact names first; otherwise a close match that is clearly ahead
    of the next one."""
    g = _guide()
    cn = g.Norm(g.norm_text(name))
    cands = by_key.get(cn.key(), []) if by_key is not None else index
    scored = []
    for nm, rank, fn in cands:
        sc = g.score(cn, nm)
        if sc > 0:
            scored.append((sc, -rank, fn, nm.squash))
    if not scored:
        return None
    scored.sort(reverse=True)
    best = scored[0]
    if best[0] >= 0.999:
        return best[2]
    if best[0] < TVL_ACCEPT:
        return None
    rival = next((s for s in scored[1:] if s[3] != best[3]), None)
    if rival and rival[0] > best[0] - 0.05:
        return None
    return best[2]


# ---------------------------------------------------------------------------------------------------------------
# Dispatcharr: candidate channels

def _channels(ids=None):
    """The lineup (as the TV guide sees it) with what logos needs: {id: {...}}."""
    from apps.channels.models import Channel, ChannelProfile, ChannelProfileMembership
    qs = Channel.objects.select_related("logo", "channel_group").filter(is_radio=False)
    if ChannelProfile.objects.exists():
        on = set(ChannelProfileMembership.objects.filter(enabled=True).values_list("channel_id", flat=True))
        qs = qs.filter(id__in=on)
    if ids is not None:
        qs = qs.filter(id__in=ids)
    out = {}
    for c in qs.only("id", "name", "channel_number", "auto_created", "logo_id", "logo__url", "logo__name",
                     "channel_group__name"):
        group = c.channel_group.name if c.channel_group_id else ""
        out[c.id] = {"id": c.id, "name": c.name or "", "logo_id": c.logo_id,
                     "logo_url": c.logo.url if c.logo_id else None, "logo_name": c.logo.name if c.logo_id else None,
                     "auto": bool(c.auto_created), "group": group or "",
                     "number": float(c.channel_number) if c.channel_number is not None else None}
    return out


def _tvlogos_candidate(ch):
    if ch["auto"] or ch["group"].strip().startswith("24/7"):
        return False
    return not _EVENT_NAME.search(ch["name"])


def _sky_logos(data_dir):
    """{channel id: (Sky logo url, Sky name)} from the TV guide's last run, if the guide is on and has run."""
    g = _guide()
    st = _load_json(os.path.join(data_dir, g.STATE_FILE), {})
    if not st.get("on") or not st.get("matches"):
        return {}, st.get("last_run")
    by_sid = {s["id"]: s for s in (st.get("services") or [])}
    out = {}
    for cid, m in st["matches"].items():
        svc = by_sid.get(m.get("sid"))
        if svc and svc.get("logo"):
            out[int(cid)] = (svc["logo"], svc.get("name") or m.get("sky"))
    return out, st.get("last_run")


# ---------------------------------------------------------------------------------------------------------------
# One run (also the dry run behind the preview)

def run(data_dir, settings, stop=None, log=None, dry=False):
    """Works out each candidate channel's logo and applies it (dry=False). Returns the result dict:
    {"filled", "replaced", "put_back", "kept", "released", "channels": {id: "sky" | "tv-logos" | "kept"}, ...}."""
    from django.db import close_old_connections
    from apps.channels.models import Channel, ChannelOverride, Logo
    close_old_connections()
    t0 = time.time()
    cfg = config(settings)
    bpath = os.path.join(data_dir, BACKUP_FILE)
    backup = _load_json(bpath, {})
    chans = backup.setdefault("channels", {})        # str(id): {"prev", "set", "src", "url"}
    hands_off = set(str(x) for x in backup.get("hands_off") or [])
    # (The preview, run while someone waits on the button, only uses addresses already checked and the last tv-logos
    # listing: no new checks or fetches, so it answers in a second or two; what it can't judge counts as "waiting".)
    checker = Checker(data_dir, stop, limit=0 if dry else MAX_CHECKS)
    res = {"filled": 0, "replaced": 0, "put_back": 0, "kept": 0, "released": 0, "waiting": 0,
           "from_sky": 0, "from_tvlogos": 0, "channels": {}, "dry": dry, "mode": cfg["mode"]}
    examples = []

    sky, guide_run = (_sky_logos(data_dir) if (cfg["on"] and cfg["sky"]) else ({}, None))
    index = by_key = None
    if cfg["on"] and cfg["tvlogos"]:
        files = tvlogos_files(data_dir, cfg["country"], log=log, fetch=not dry)
        index = tvlogos_index(files, cfg["country"])
        by_key = {}
        for item in index:
            by_key.setdefault(item[0].key(), []).append(item)

    lineup = _channels()
    # Channels the plugin changed but which have left the lineup still need putting back.
    gone = [int(c) for c in chans if int(c) not in lineup]
    if gone:
        found = _channels_any(gone)
        lineup.update(found)
        for c in gone:
            if c not in found:
                chans.pop(str(c), None)     # the channel was deleted: nothing to put back
    overridden = set(ChannelOverride.objects.filter(logo__isnull=False).values_list("channel_id", flat=True))
    logo_ids = {}

    def logo_for(url, name):
        lid = logo_ids.get(url)
        if lid is None:
            row = Logo.objects.filter(url=url).only("id").first()
            if row is None:
                if dry:
                    return -1
                row = Logo.objects.create(name=(name or "Logo")[:255], url=url)
            lid = logo_ids[url] = row.id
        return lid

    def set_logo(cid, lid):
        if not dry:
            ch = Channel.objects.filter(id=cid).first()
            if ch is not None:
                ch.logo_id = lid
                # (updated_at too, so apps that sync channels by it notice.)
                ch.save(update_fields=["logo", "updated_at"])

    def put_back(key, ch):
        entry = chans.pop(key, None)
        if entry is None:
            return
        if ch is not None and ch["logo_id"] == entry.get("set"):
            prev = entry.get("prev")
            if prev is not None and not Logo.objects.filter(id=prev).exists():
                prev = None
            set_logo(ch["id"], prev)
            res["put_back"] += 1
        else:
            res["released"] += 1          # changed by hand since: left as it is

    for cid, ch in sorted(lineup.items()):
        key = str(cid)
        entry = chans.get(key)
        # Changed by hand since the plugin set it: never touched again.
        if entry is not None and ch["logo_id"] != entry.get("set"):
            chans.pop(key, None)
            hands_off.add(key)
            res["released"] += 1
            continue
        if key in hands_off or cid in overridden:
            continue
        if not cfg["on"] or ch.get("_gone"):
            if entry is not None:
                put_back(key, ch)
            continue

        # The logo the channel had before the plugin (what "missing" judges).
        orig_id = entry.get("prev") if entry else ch["logo_id"]
        orig_url = ch["logo_url"] if not entry else _logo_url(orig_id)
        orig_name = ch["logo_name"] if not entry else None

        wants = []
        if cid in sky:
            wants.append(("sky", sky[cid][0], sky[cid][1]))
        if index is not None and _tvlogos_candidate(ch):
            fn = tvlogos_match(ch["name"], index, by_key)
            if fn:
                wants.append(("tv-logos", TVL_RAW % (cfg["country"], fn), fn))
        if not wants:
            if entry is not None:
                put_back(key, ch)
            continue

        missing = False
        if cfg["mode"] == "missing":
            if orig_id is None or not orig_url or _PLACEHOLDER.search(orig_url or "") or _PLACEHOLDER.search(orig_name or ""):
                missing = True
            else:
                good = checker.ok(orig_url)
                if good is None:
                    res["waiting"] += 1
                    continue
                missing = not good
            if not missing:
                if entry is not None:
                    put_back(key, ch)        # (it had a working logo after all: "all" before, now "missing")
                res["kept"] += 1
                res["channels"][key] = "kept"
                continue
        else:
            missing = orig_id is None

        # The first source whose logo address works (Sky, then tv-logos).
        pick, wait = None, False
        for src, url, label in wants:
            good = checker.ok(url)
            if good is None:
                wait = True
                break
            if good:
                pick = (src, url, label)
                break
        if pick is None:
            if wait:
                res["waiting"] += 1
                continue
            if entry is not None:
                put_back(key, ch)
            res["kept"] += 1
            res["channels"][key] = "kept"
            continue
        src, url, label = pick
        lid = logo_for(url, ch["name"])
        res["channels"][key] = src
        res["from_sky" if src == "sky" else "from_tvlogos"] += 1
        if ch["logo_id"] == lid and lid != -1:
            continue                       # already this logo
        if entry is None:
            chans[key] = {"prev": ch["logo_id"], "set": lid, "src": src, "url": url}
        else:
            entry.update(set=lid, src=src, url=url)
        set_logo(cid, lid)
        res["filled" if missing else "replaced"] += 1
        if len(examples) < 40:
            examples.append("%s → %s (%s)" % (ch["name"], label, src))

    checker.save()
    res["examples"] = examples
    res["seconds"] = round(time.time() - t0, 1)
    res["checks"] = checker.checked
    res["changed"] = len(chans)
    if not dry:
        backup["hands_off"] = sorted(hands_off, key=lambda x: int(x))
        backup["at"] = int(time.time())
        _save_json(bpath, backup)
        st = {"at": int(time.time()), "sig": cfg["sig"], "guide_run": guide_run, "mode": cfg["mode"],
              "sources": cfg["sources"], "country": cfg["country"],
              **{k: v for k, v in res.items() if k not in ("dry",)}}
        _save_json(os.path.join(data_dir, STATE_FILE), st)
    if log and (not dry):
        log("guide: logos — %d filled, %d replaced, %d put back" % (res["filled"], res["replaced"], res["put_back"]))
    return res


def _channels_any(ids):
    """Channels by id whatever their lineup status (for putting back ones that left the lineup)."""
    from apps.channels.models import Channel
    out = {}
    for c in Channel.objects.select_related("logo", "channel_group").filter(id__in=ids):
        out[c.id] = {"id": c.id, "name": c.name or "", "logo_id": c.logo_id,
                     "logo_url": c.logo.url if c.logo_id else None, "logo_name": c.logo.name if c.logo_id else None,
                     "auto": True, "group": c.channel_group.name if c.channel_group_id else "", "number": None,
                     "_gone": True}
    return out


def _logo_url(lid):
    if lid is None:
        return None
    from apps.channels.models import Logo
    return Logo.objects.filter(id=lid).values_list("url", flat=True).first()


# ---------------------------------------------------------------------------------------------------------------
# Status

def status(data_dir):
    st = _load_json(os.path.join(data_dir, STATE_FILE), {})
    backup = _load_json(os.path.join(data_dir, BACKUP_FILE), {})
    return {"at": st.get("at"), "mode": st.get("mode"), "filled": st.get("filled", 0),
            "replaced": st.get("replaced", 0), "put_back": st.get("put_back", 0), "kept": st.get("kept", 0),
            "from_sky": st.get("from_sky", 0), "from_tvlogos": st.get("from_tvlogos", 0),
            "changed": len(backup.get("channels") or {}), "hands_off": len(backup.get("hands_off") or [])}


def per_channel(data_dir):
    """{channel id (str): "sky" | "tv-logos" | "kept"} from the last run."""
    return (_load_json(os.path.join(data_dir, STATE_FILE), {}).get("channels") or {})


def status_text(data_dir, settings):
    cfg = config(settings)
    s = status(data_dir)
    if not cfg["on"]:
        return "Channel logos are off." + (" %d channel(s) still to be put back." % s["changed"] if s["changed"] else "")
    if not s["at"]:
        return "Channel logos are on (%s); the first run hasn't finished yet." % cfg["mode"]
    when = clock.strftime("%a %H:%M", s["at"])
    return ("Channel logos (%s, %s), last run %s: %d channel(s) use a logo from Sky, %d from tv-logos; %d kept their "
            "own; %d changed by the plugin in all (put back when switched off); %d changed by hand since, left alone."
            % (cfg["mode"], {"sky_tvlogos": "Sky, then tv-logos", "sky": "Sky only", "tvlogos": "tv-logos only"}[cfg["sources"]],
               when, s["from_sky"], s["from_tvlogos"], s["kept"], s["changed"], s["hands_off"]))


def preview_text(res):
    head = ("Preview (nothing changed): %d would be filled in, %d replaced, %d put back; %d keep their own logo; "
            "%d from Sky, %d from tv-logos" % (res["filled"], res["replaced"], res["put_back"], res["kept"],
                                               res["from_sky"], res["from_tvlogos"]))
    if res.get("waiting"):
        head += ("; %d not judged (the preview only uses logo addresses already checked; the next run checks the rest)"
                 if res.get("dry") else "; %d not checked yet (too many to check at once)") % res["waiting"]
    return head + "." + ((" For example: " + "; ".join(res["examples"][:25])) if res.get("examples") else "")


# ---------------------------------------------------------------------------------------------------------------
# The thread

def _last_slot(now):
    best = 0
    for back in (0, 1):
        t = clock.local_epoch(clock.local_today(now - back * 86400), RUN_AT[0], RUN_AT[1])
        if best < t <= now:
            best = t
    return best


class _Lock:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, LOCK_FILE)
        self.f = None

    def __enter__(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.f = open(self.path, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.f.close()
            self.f = None
            return False
        return True

    def __exit__(self, *a):
        if self.f is not None:
            import fcntl
            fcntl.flock(self.f, fcntl.LOCK_UN)
            self.f.close()


def run_now(data_dir, settings, stop=None, log=None):
    with _Lock(data_dir) as got:
        if not got:
            return None
        return run(data_dir, settings, stop=stop, log=log)


def _tick(data_dir, settings_fn, stop, log, mem):
    settings = settings_fn() or {}
    cfg = config(settings)
    st = _load_json(os.path.join(data_dir, STATE_FILE), {})
    now = time.time()
    if not cfg["on"]:
        # Off: put back what the plugin changed (once; again only if something is still left).
        if (_load_json(os.path.join(data_dir, BACKUP_FILE), {}).get("channels")) or st.get("mode") not in (None, "off"):
            run_now(data_dir, settings, stop=stop, log=log)
        return
    if mem.get("sig") != cfg["sig"]:
        mem["sig"] = cfg["sig"]
        mem["sig_seen"] = now
    gst = _load_json(os.path.join(data_dir, _guide().STATE_FILE), {})
    due = (
        not st.get("at")
        or (st.get("sig") != cfg["sig"] and now - mem.get("sig_seen", 0) >= SETTLE_S)  # settings changed
        or st.get("at", 0) < _last_slot(now)                                         # the daily run
        or (cfg["sky"] and gst.get("last_run") and gst.get("last_run") != st.get("guide_run")
            and now - gst.get("last_run", 0) >= 180)                                 # after each guide run
        or (st.get("waiting") and now - st.get("at", 0) >= 3600)                     # checks left over
    )
    if due:
        run_now(data_dir, settings, stop=stop, log=log)


def _loop(stop, data_dir, settings_fn, log):
    mem = {}
    stop.wait(45)
    while not stop.is_set():
        try:
            _tick(data_dir, settings_fn, stop, log, mem)
        except Exception as exc:
            if log:
                log("logos: round failed: %s" % exc)
        stop.wait(TICK_S)


_thread = None


def start(stop, data_dir, settings_fn, log=None):
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_loop, args=(stop, data_dir, settings_fn, log), name="viewarr-logos", daemon=True)
    _thread.start()
    return _thread
