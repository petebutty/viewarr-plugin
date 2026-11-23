"""TV guide for Viewarr: listings fetched straight from a listings source, matched to the channels in Dispatcharr,
written as an XMLTV file and added to Dispatcharr as a guide source of its own.

Off by default. Switched on (setting "guide"), it runs in its own thread beside the sports service:

  * twice a day (04:30 and 16:30, the server's local time), straight away when switched on, and a few minutes after
    its settings change, it reads the source's channel list for the chosen region, matches the house's channels to
    it by name (see "Matching" below), and fetches the listings of the MATCHED channels only, for "guide_days" days;
  * it writes /data/logos/viewarr-epg/guide.xml (Dispatcharr's web server serves /data/logos without a sign-in) and
    keeps one Dispatcharr guide source ("Viewarr: Sky guide") pointed at it, refreshed after every write;
  * only with "guide_apply" on does it point each matched channel at this guide. The guide each channel had before is
    kept in guide_backup.json and put back when "guide_apply" (or the guide) is switched off. With it off, nothing in
    Dispatcharr's channel-to-guide mapping changes, so both guides can be compared side by side first.

The review list (matched channels with their confidence, near misses that need an override) goes to the plugin's
status and to every user's app data under "viewarr_guide", so Viewarr can show it.

Listings sources plug in: each is a ListingsSource (see that class for the connector interface). Sky (UK) is
implemented; Schedules Direct (US / Canada) is a documented stub that can't be chosen yet.
"""

import difflib
import gzip
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape, quoteattr


def _load_clock():
    """clock.py beside this file (the house's local time; Dispatcharr's own processes run in UTC)."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("viewarr_clock", os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = _load_clock()

UA = "Viewarr-Plugin/0.6 (+https://github.com/petebutty/viewarr-plugin)"

OUT_DIR = "/data/logos/viewarr-epg"
OUT_FILE = os.path.join(OUT_DIR, "guide.xml")
URL_PATH = "/logos/viewarr-epg/guide.xml"
# Where Dispatcharr itself fetches the file from: its own web server inside the container (nginx, on the port the
# web interface uses). Overridable for unusual installs.
INTERNAL_BASE = os.environ.get("VIEWARR_GUIDE_BASE") or ("http://127.0.0.1:%s" % (os.environ.get("DISPATCHARR_PORT") or "9191"))

APP_KEY = "viewarr_guide"
MAX_BYTES = 120_000
STATE_FILE = "guide_state.json"
BACKUP_FILE = "guide_backup.json"
CACHE_FILE = "guide_cache.json.gz"
LOGO_STATE_FILE = "logo_state.json"   # written by logos.py (channel logos from Sky / tv-logos)
LOCK_FILE = "guide.lock"
REFRESH_FLAG = "guide_refresh.flag"   # left by the "Fetch the TV guide now" button; the thread runs it within a minute

RUN_AT = ((4, 30), (16, 30))     # local times of the twice-daily fetch
TICK_S = 60                      # how often the thread looks at its settings
SETTLE_S = 300                   # after a settings change, at most one run per this many seconds
RETRY_FAILED_S = 1800            # a run that got nothing is tried again after this long
APPLY_EVERY_S = 1800             # the channel mapping is checked this often (sooner just after a run)

ACCEPT = 0.82                    # a name match at least this good is taken
ACCEPT_SAME_NUMBER = 0.55        # ...or this good when the channel number is the same as the source's
REVIEW_FLOOR = 0.65              # unmatched channels at least this close are listed as near misses


# ---------------------------------------------------------------------------------------------------------------
# HTTP, politely: a couple of requests at a time, a short pause after each, a few retries with growing waits.

class Http:
    def __init__(self, stop=None, log=None, delay=0.5, tries=3):
        self.stop = stop
        self.log = log or (lambda m: None)
        self.delay = delay
        self.tries = tries
        self.count = 0
        self.failed = 0

    def _sleep(self, s):
        if self.stop is not None:
            self.stop.wait(s)
        else:
            time.sleep(s)

    def _get(self, url, headers):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
        except Exception:
            return 0, b""

    def json(self, url, headers=None):
        """The decoded JSON body, or None after the retries (or at once on a 4xx that retrying won't change)."""
        for attempt in range(self.tries):
            if self.stop is not None and self.stop.is_set():
                return None
            self.count += 1
            code, body = self._get(url, headers)
            self._sleep(self.delay)
            if 200 <= code < 300:
                try:
                    return json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    pass
            elif code in (400, 401, 403, 404, 410):
                break
            wait = 2 * (3 ** attempt)                 # 2 s, 6 s, 18 s
            if code in (429, 503):
                wait = max(wait, 30)                  # told to slow down: a proper pause
            if attempt + 1 < self.tries:
                self._sleep(wait)
        self.failed += 1
        return None


# ---------------------------------------------------------------------------------------------------------------
# Listings sources (the connector interface)

class ListingsSource:
    """A listings source the guide can use. To add one, subclass this, fill in the attributes and the three methods,
    and add an instance to SOURCES (and its key to the "guide_source" options in plugin.json once it works).

    Attributes:
      key          the value stored in the "guide_source" setting, e.g. "sky_uk"
      label        shown to people, e.g. "Sky (UK)"
      selectable   False while it's only a stub: it is then refused even if the setting names it
      epg_name     the Dispatcharr guide source this one writes to, e.g. "Viewarr: Sky guide"
      channel_prefix  the XMLTV channel ids are channel_prefix + service id, e.g. "sky.4061" (keep them stable:
                   Dispatcharr's channel-to-guide mapping is by this id)

    Methods (http is an Http above: use it for every request, it is polite and counts them):
      services(settings, http) -> [service] or None on failure. A service is a dict:
          {"id": "4061", "name": "Sky One HD", "number": "106" or None, "logo": url or None, "hd": bool}
        Only TV services worth matching (no radio, no adult, no placeholders).
      schedules(settings, ids, days, http, cache) -> ({id: [programme]}, failed_count)
          programmes for the given service ids from today for `days` days. A programme is a dict:
          {"start": epoch s, "stop": epoch s, "title": str, "sub_title": str|None, "desc": str|None,
           "category": [str], "season": int|None, "episode": int|None, "new": bool, "live": bool,
           "icon": url|None, "rating": str|None, "subtitles": bool, "hd": bool}
          `cache` is a dict the guide keeps between runs (saved to disk): use it to keep the last good listings
          for anything that fails this time.
      match_names(service) -> [names]: the names to match the house's channels against, already passed through
          norm_text() (the service's own name, plus any known renames). Default: [norm_text(service["name"])].
      region_label(settings) -> str: for the status line.
    """

    key = ""
    label = ""
    selectable = False
    epg_name = ""
    channel_prefix = ""

    def services(self, settings, http):
        raise NotImplementedError

    def schedules(self, settings, ids, days, http, cache):
        raise NotImplementedError

    def match_names(self, service):
        return [norm_text(service["name"])]

    def region_label(self, settings):
        return ""


class SkyUK(ListingsSource):
    """Sky's own public listings (the ones its website and apps use), no account needed.

      channel list:  GET https://awk.epgsky.com/hawk/linear/services/<bouquet>/<sub-bouquet>
                     {"services": [{"sid", "c" (EPG number), "t" (name, often abbreviated), "sg" (genre group),
                                    "sf" ("HD"/"SD"), "adult", ...}]}
      listings:      GET https://awk.epgsky.com/hawk/linear/schedule/<YYYYMMDD>/<sid>,<sid>,...   (up to 20 sids)
                     {"schedule": [{"sid", "events": [{"eid", "st" (start, epoch s), "d" (seconds), "t" (title),
                                    "sy" (synopsis), "eg" (genre), "seasonnumber", "episodenumber", "new", "s"
                                    (subtitles), "hd", "r" (rating), "programmeuuid", ...}]}]}
      headers:       X-SkyOTT-Territory: GB
      logos:         https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/500/500/skychb<sid>.png
      pictures:      https://images.metadata.sky.com/pd-image/<programmeuuid>/16-9/640
    """

    key = "sky_uk"
    label = "Sky (UK)"
    selectable = True
    epg_name = "Viewarr: Sky guide"
    channel_prefix = "sky."

    HAWK = ""  # the Sky listings address the admin added + /hawk/linear (server.apply_sources); empty: not used
    HEADERS = {"X-SkyOTT-Territory": "GB"}
    LOGO = "https://d2n0069hmnqmmx.cloudfront.net/epgdata/1.0/newchanlogos/500/500/skychb%s.png"
    PICTURE = "https://images.metadata.sky.com/pd-image/%s/16-9/640"
    PER_REQUEST = 20
    WORKERS = 2
    # The regions offered in the settings (bouquet/sub-bouquet). The national channels are the same everywhere;
    # the region changes BBC One, ITV / STV / UTV and the channel numbers.
    REGIONS = {
        "4101/1": "London", "4101/7": "England: North West", "4101/12": "England: North East",
        "4101/21": "England: East", "4101/24": "England: West", "4101/3": "England: West Midlands",
        "4102/37": "Scotland", "4102/36": "Scotland: Border", "4104/32": "Wales",
        "4104/33": "Northern Ireland", "4104/34": "Channel Islands",
    }
    DEFAULT_REGION = "4101/1"
    # Sky's genre numbers ("eg").
    GENRES = {1: "Shopping", 2: "Children", 3: "Entertainment", 4: "Music", 5: "News / Documentary", 6: "Movie",
              7: "Sports"}

    def region(self, settings):
        r = str((settings or {}).get("guide_sky_region") or self.DEFAULT_REGION).strip()
        return r if re.match(r"^\d{4}/\d{1,3}$", r) else self.DEFAULT_REGION

    def region_label(self, settings):
        r = self.region(settings)
        return self.REGIONS.get(r, r)

    def services(self, settings, http):
        body = http.json("%s/services/%s" % (self.HAWK, self.region(settings)), self.HEADERS)
        if not isinstance(body, dict) or not isinstance(body.get("services"), list):
            return None
        out = []
        for s in body["services"]:
            sid, name, num = str(s.get("sid") or ""), (s.get("t") or "").strip(), str(s.get("c") or "")
            if not sid or not name or s.get("adult"):
                continue
            # Radio (numbered 0101…, genre 4) and the channel line-up placeholder (genre 0) aren't matched.
            if num.startswith("0") or s.get("sg") in (0, 4):
                continue
            out.append({"id": sid, "name": name, "number": num or None, "logo": self.LOGO % sid,
                        "hd": str(s.get("sf") or "").upper() == "HD", "movies": s.get("sg") == 6})
        return out

    # Sky's names are cut to 14 characters ("SkySpMainEvHD", "SkyDocmntrsHD"); these turn them back into words
    # before matching (both sides go through the same normaliser, see norm()).
    def match_names(self, service):
        base = norm_text(service["name"])
        names = [base]
        if service.get("movies") and base.startswith("sky ") and " cinema " not in " %s " % base:
            names.append("sky cinema " + base[4:])            # "Sky Action HD" is Sky Cinema Action
        for pat, alt in _SKY_RENAMED:
            for n in list(names):
                if re.search(pat, n):
                    names.append(re.sub(pat, alt, n))
        return list(dict.fromkeys(names))

    def schedules(self, settings, ids, days, http, cache):
        today = datetime.now(timezone.utc).date()
        dates = [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(max(1, days))]
        ids = sorted(set(ids), key=lambda x: int(x) if x.isdigit() else 0)
        jobs = [(d, ids[i:i + self.PER_REQUEST]) for d in dates for i in range(0, len(ids), self.PER_REQUEST)]
        by_day = cache.setdefault("days", {})
        failed = 0

        def one(job):
            d, batch = job
            return d, batch, http.json("%s/schedule/%s/%s" % (self.HAWK, d, ",".join(batch)), self.HEADERS)

        with ThreadPoolExecutor(max_workers=self.WORKERS) as pool:
            results = list(pool.map(one, jobs))
        for d, batch, body in results:
            day = by_day.setdefault(d, {})
            if not isinstance(body, dict) or not isinstance(body.get("schedule"), list):
                failed += 1
                continue  # the cached copy of that day (if any) stays
            got = {}
            for s in body["schedule"]:
                sid = str(s.get("sid") or "")
                if sid in batch:
                    got[sid] = [p for p in (self._programme(e) for e in (s.get("events") or [])) if p]
            for sid in batch:
                # (A channel Sky answered for but with nothing listed: an empty day, unless we had one before.)
                if got.get(sid) or sid not in day:
                    day[sid] = got.get(sid, [])
        # Days long gone are dropped from the cache.
        for d in [d for d in by_day if d < (today - timedelta(days=1)).strftime("%Y%m%d")]:
            del by_day[d]
        out = {}
        for sid in ids:
            seen = set()
            progs = []
            for d in sorted(by_day):
                for p in by_day[d].get(sid, []):
                    k = p.get("eid") or (p["start"], p["title"])
                    if k not in seen:
                        seen.add(k)
                        progs.append(p)
            progs.sort(key=lambda p: p["start"])
            out[sid] = progs
        return out, failed

    _NEW = re.compile(r"^new:\s*", re.I)
    _LIVE = re.compile(r"^live\b", re.I)
    _CODES = re.compile(r"(\s*\[[A-Z0-9,]{1,12}\])+\s*$")
    _EPTITLE = re.compile(r"^([^.:!?]{2,60}):\s+(.+)$", re.S)

    def _programme(self, e):
        try:
            st, d = int(e.get("st")), int(e.get("d") or 0)
        except (TypeError, ValueError):
            return None
        title = (e.get("t") or "").strip()
        if not title or d <= 0:
            return None
        new = bool(e.get("new"))
        if self._NEW.match(title):
            title, new = self._NEW.sub("", title).strip(), True
        desc = self._CODES.sub("", (e.get("sy") or "").strip()) or None
        season = _int(e.get("seasonnumber"))
        episode = _int(e.get("episodenumber"))
        sub = None
        # An episode's synopsis usually starts with its own title ("Sore Winners: The Brovaries face…").
        if desc and episode:
            m = self._EPTITLE.match(desc)
            if m and len(m.group(1).split()) <= 8:
                sub, desc = m.group(1).strip(), m.group(2).strip()
        rating = str(e.get("r") or "").strip()
        uuid = e.get("programmeuuid")
        return {
            "eid": e.get("eid"), "start": st, "stop": st + d, "title": title, "sub_title": sub, "desc": desc,
            "category": [self.GENRES[e["eg"]]] if e.get("eg") in self.GENRES else [],
            "season": season if season and season > 0 else None,
            "episode": episode if episode and episode > 0 else None,
            "new": new, "live": bool(self._LIVE.match(title)),
            "icon": (self.PICTURE % uuid) if uuid else None,
            "rating": rating if rating and rating != "--" else None,
            "subtitles": bool(e.get("s")), "hd": bool(e.get("hd")),
        }


class SchedulesDirect(ListingsSource):
    """Schedules Direct (US / Canada): NOT IMPLEMENTED YET, can't be chosen.

    What a connector needs (JSON API, https://json.schedulesdirect.org/20141201, documented at
    https://github.com/SchedulesDirect/JSON-Service/wiki/API-20141201):

      * a paid account: settings "guide_sd_username" and "guide_sd_password" (send sha1(password) to POST /token,
        keep the token for 24 h; never log either), and "guide_sd_lineup" (e.g. "USA-OTA-10001"), added with
        PUT /lineups/<lineup> once (an account may hold only a few lineups, and changes are limited per day);
      * services():  GET /lineups/<lineup> → "stations" (stationID, name, callsign, logo) and "map" (channel
        numbers): return them as services with id = stationID, name = name (callsign as a second match name);
      * schedules(): POST /schedules/md5 with the stationIDs and dates first, then POST /schedules only for days whose
        MD5 changed (keep the MD5s in `cache`), at most 5000 stationID-days per request; then POST /programs with the
        programIDs not already cached (at most 5000 per request), and build the programmes from both
        (title120, episodeTitle150, descriptions, genres, metadata Gracenote season/episode, "new", "liveTapeDelay");
      * Schedules Direct limits requests per day: fetch once a day, not twice, and never retry in a loop.

    Note: Dispatcharr has its own built-in Schedules Direct guide source (Add EPG → Schedules Direct API). Use that
    for the listings themselves; this connector would only add Viewarr's matching, review list and comparison.
    """

    key = "schedules_direct"
    label = "Schedules Direct (US / Canada)"
    selectable = False
    epg_name = "Viewarr: Schedules Direct guide"
    channel_prefix = "sd."

    def services(self, settings, http):
        raise NotImplementedError("Schedules Direct isn't available yet")

    def schedules(self, settings, ids, days, http, cache):
        raise NotImplementedError("Schedules Direct isn't available yet")


SOURCES = {s.key: s for s in (SkyUK(), SchedulesDirect())}
DEFAULT_SOURCE = "sky_uk"


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------------
# Settings

def config(settings):
    s = settings or {}
    key = str(s.get("guide_source") or DEFAULT_SOURCE).strip()
    src = SOURCES.get(key)
    if src is None or not src.selectable:
        src = SOURCES[DEFAULT_SOURCE]
    try:
        days = int(str(s.get("guide_days") or 8).strip())
    except ValueError:
        days = 8
    days = max(1, min(days, 8))
    on = s.get("guide", False) is True or str(s.get("guide")).lower() == "true"
    apply_ = s.get("guide_apply", False) is True or str(s.get("guide_apply")).lower() == "true"
    overrides = str(s.get("guide_overrides") or "")
    sig = json.dumps([src.key, src.region_label(s), days, overrides.strip()], ensure_ascii=False)
    return {"on": on, "apply": on and apply_, "source": src, "days": days, "overrides": overrides, "sig": sig,
            "settings": s}


# ---------------------------------------------------------------------------------------------------------------
# Matching: the house's channel names against the source's, normalised (case, accents, "HD"/"FHD"/"UHD", "+1",
# "&"/"and", provider prefixes such as "UK:", abbreviations, region words such as Scotland / STV).

_QUALITY_RE = re.compile(r"(?i)\b(1080p|720p|2160p|1080i|50fps|60fps|h\.?26[45]|hevc)\b")
_UHD_RE = re.compile(r"(?i)\b(4k|uhd|ultra\s*hd)\b")
_CAMEL1 = re.compile(r"(?<=[a-z])(?=[A-Z])")
_CAMEL2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_DIGIT = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_PREFIX = re.compile(r"^\s*[a-z]{2,3}\s*[|:\-]\s*")
_DROP = {"hd", "fhd", "sd", "hq", "raw", "backup"}
_OPTIONAL = {"and", "the", "tv", "channel"}
_REGION = {"scotland", "wales", "cymru", "northern", "ireland", "london", "england", "yorkshire", "lincolnshire",
           "midlands", "tyne", "tees", "anglia", "granada", "meridian", "border", "islands", "north", "south", "east",
           "west"}
# Phrases rewritten before matching (Sky's abbreviations mostly; harmless on full names).
_PHRASES = [(re.compile(p), r) for p, r in [
    (r"\bsky sp\b", "sky sports"), (r"\bf ?ball\b", "football"), (r"\bmain ev\b", "main event"),
    (r"\bsky sports pl\b", "sky sports premier league"), (r"\bdocu?mn?trs\b|\bdocmntrs\b", "documentaries"),
    (r"\bcomedy cent\b", "comedy central"), (r"\bdisc\b", "discovery"), (r"\banimal plnt\b", "animal planet"),
    (r"\bfood netwrk\b", "food network"), (r"\bcartoon net(wrk)?\b", "cartoon network"), (r"^cn\b", "cartoon network"),
    (r"\bcrime plus inv\b", "crime plus investigation"), (r"\bparl\b", "parliament"), (r"\bcine\b", "cinema"),
    (r"\bnat ?geo\b", "national geographic"), (r"\bjr\b", "junior"), (r"\bbox off\b", "box office"),
    (r"\btnts\b", "tnt sports"), (r"\bxmas\b", "christmas"), (r"^g\b", "great"), (r"\bmst hve\b", "must have"),
    (r"\bsc(i)? ?fi\b", "scifi"), (r"\bhor\b", "horror"), (r"\bxtra\b", "extra"), (r"(?<=\w) x$", " extra"),
    (r"\bscot\b", "scotland"), (r"\bni\b", "northern ireland"), (r"\bwal\b", "wales"), (r"\brb\b", "red button"),
    (r"^bbc 1\b", "bbc one"), (r"^bbc 2\b", "bbc two"), (r"^bbc 3\b", "bbc three"), (r"^bbc 4\b", "bbc four"),
    (r"^(bbc (one|two)) lon\b", r"\1 london"), (r"^(bbc (one|two)) ne\b", r"\1 north east"),
    (r"^(bbc (one|two)) nw\b", r"\1 north west"), (r"^(bbc (one|two)) yks\b", r"\1 yorkshire"),
    (r"^(bbc (one|two)) y and l\b", r"\1 yorkshire lincolnshire"), (r"^(bbc (one|two)) wm\b", r"\1 west midlands"),
    (r"^(bbc (one|two)) emid\b", r"\1 east midlands"), (r"^(bbc (one|two)) se\b", r"\1 south east"),
    (r"^(bbc (one|two)) sw\b", r"\1 south west"), (r"^(bbc (one|two)) wst\b", r"\1 west"),
    (r"^(bbc (one|two)) sth\b", r"\1 south"), (r"^(bbc (one|two)) ci\b", r"\1 channel islands"),
]]
# Known renames on Sky: extra names a service also answers to.
_SKY_RENAMED = [
    (r"^sky one$", "sky showcase"), (r"^id$", "investigation discovery"), (r"^stv$", "itv 1 scotland"),
    (r"^utv$", "itv 1 northern ireland"), (r"^premier (\d+)$", r"premier sports \1"),
]


def norm_text(name):
    """A name as plain lower-case words, with Sky's glued-together abbreviations split and expanded."""
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    s = _QUALITY_RE.sub(" ", s)
    s = _UHD_RE.sub(" UHD ", s)
    s = s.replace("'", "").replace("’", "")
    s = _CAMEL2.sub(" ", _CAMEL1.sub(" ", s))
    s = _DIGIT.sub(" ", s)
    s = s.lower()
    s = re.sub(r"\+\s*(\d+)", r" plus\1 ", s)
    s = s.replace("+", " plus ").replace("&", " and ")
    s = _PREFIX.sub("", s, count=1)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    if s.startswith("uk "):
        s = s[3:]
    s = " ".join(t for t in s.split() if t not in _DROP)
    for pat, rep in _PHRASES:
        s = pat.sub(rep, s)
    return " ".join(s.split())


class Norm:
    __slots__ = ("text", "core", "digits", "plus", "uhd", "region", "squash")

    def __init__(self, text):
        self.text = text
        toks = text.split()
        self.plus = tuple(sorted(t for t in toks if re.match(r"^plus\d+$", t)))
        self.uhd = "uhd" in toks
        self.digits = frozenset(t.lstrip("0") or "0" for t in toks if t.isdigit())
        self.region = frozenset(t for t in toks if t in _REGION)
        self.core = [t for t in toks if not t.isdigit() and t not in _OPTIONAL and t not in _REGION and t != "uhd"
                     and not re.match(r"^plus\d+$", t)]
        self.squash = "".join(t for t in toks if t not in self.region and t != "uhd")

    def key(self):
        return (self.digits, self.plus, self.uhd)


def _subseq(short, long):
    it = iter(long)
    return all(c in it for c in short)


def _tok(a, b):
    if a == b:
        return 1.0
    s, l = (a, b) if len(a) <= len(b) else (b, a)
    # A shortened word ("itvx" / "itv", "docs" / "documentaries"); a short start of a long word counts less
    # ("god" isn't "godzilla").
    if len(s) >= 2 and l.startswith(s):
        return 0.85 if len(s) * 2 >= len(l) else 0.65
    # Letters dropped ("plnt" / "planet"): only for longer words, so "cnn" isn't "centurions".
    if len(s) >= 4 and len(s) * 2 >= len(l) and s[0] == l[0] and _subseq(s, l):
        return 0.75
    return 0.0


def _cover(a, b):
    if not a:
        return 1.0 if not b else 0.0
    used = [False] * len(b)
    got = total = 0.0
    for t in sorted(a, key=len, reverse=True):
        best, bi = 0.0, -1
        for i, u in enumerate(b):
            if not used[i]:
                m = _tok(t, u)
                if m > best:
                    best, bi = m, i
                    if m == 1.0:
                        break
        if bi >= 0:
            used[bi] = True
        w = max(len(t), 3)   # (one-letter words like the "U" of U&Dave still count for something)
        got += best * w
        total += w
    return got / total if total else 0.0


def score(a, b):
    """0..1: how well two normalised names agree. "+1", UHD and numbers must agree exactly, and two different
    regions never match (BBC One London isn't BBC One Scotland); a region on one side only costs a little."""
    if a.key() != b.key():
        return 0.0
    if a.region and b.region and a.region != b.region:
        return 0.0
    if bool(a.region) != bool(b.region):
        # A region on one side only: fine when the rest is a full name ("BBC Two England" / "BBC Two"), not when the
        # region is most of the name ("BBC" isn't "BBC Scotland", "The Great North" isn't "Great! TV").
        side = a if a.region else b
        if len(side.core) + len(side.digits) < 2:
            return 0.0
    if a.squash == b.squash:
        s = 1.0
    else:
        s = min(_cover(a.core, b.core), _cover(b.core, a.core))
    if bool(a.region) != bool(b.region):
        s -= 0.05
    return max(0.0, s)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else None


def parse_overrides(text):
    """'Dispatcharr channel name = source channel name' pairs, separated by ';' or new lines. The right side may be a
    source channel name (as in the review list), its id ("4061" or "sky.4061"), or "-" / "none" for "never match"."""
    pairs = []
    for part in re.split(r"[;\n]", text or ""):
        part = part.strip()
        if not part or part.startswith("#") or "=" not in part:
            continue
        left, right = (x.strip() for x in part.split("=", 1))
        if left:
            pairs.append((left, right))
    return pairs


def match(channels, services, source, overrides_text=""):
    """channels: [{"id", "name", "number", "epg_name"}]; services: the source's list.
    Returns (matches {channel id: {...}}, review [near misses], override problems [str])."""
    svc_names = []
    for s in services:
        for n in source.match_names(s):
            svc_names.append((Norm(n), s))
    by_key = {}
    for nm, s in svc_names:
        by_key.setdefault(nm.key(), []).append((nm, s))
    by_number = {}
    for s in services:
        n = _num(s.get("number"))
        if n is not None:
            by_number.setdefault(n, s)
    by_id = {s["id"]: s for s in services}

    # Overrides first.
    forced, problems = {}, []
    names_lc = {}
    for c in channels:
        names_lc.setdefault(norm_text(c["name"]), []).append(c)
    for left, right in parse_overrides(overrides_text):
        chans = names_lc.get(norm_text(left))
        if not chans:
            problems.append("no channel called \"%s\"" % left)
            continue
        target = None
        r = right.strip()
        if r.lower() in ("", "-", "none", "off"):
            target = False
        else:
            rid = r[len(source.channel_prefix):] if source.channel_prefix and r.lower().startswith(source.channel_prefix) else r
            if rid in by_id:
                target = by_id[rid]
            else:
                exact = [s for s in services if s["name"].lower() == r.lower()]
                if exact:
                    target = exact[0]
                else:
                    rn = Norm(norm_text(r))
                    best = max(((score(rn, nm), s) for nm, s in svc_names), key=lambda x: x[0], default=(0, None))
                    if best[0] >= ACCEPT:
                        target = best[1]
            if target is None:
                problems.append("no %s channel called \"%s\"" % (source.label, right))
                continue
        for c in chans:
            forced[c["id"]] = target

    matches, review = {}, []
    for c in channels:
        number = _num(c.get("number"))
        if c["id"] in forced:
            t = forced[c["id"]]
            if t is not False:
                matches[c["id"]] = {"sid": t["id"], "sky": t["name"], "score": 1.0, "how": "override",
                                    "same_number": number is not None and _num(t.get("number")) == number}
            continue
        cn = Norm(norm_text(c["name"]))
        cands = by_key.get(cn.key(), [])
        best = None
        for nm, s in cands:
            sc = score(cn, nm)
            if sc <= 0:
                continue
            same = number is not None and _num(s.get("number")) == number
            ok = sc >= ACCEPT or (same and sc >= ACCEPT_SAME_NUMBER)
            rank = (ok, sc + (0.1 if same else 0.0), same, s.get("hd", False), -(_num(s.get("number")) or 9999))
            if best is None or rank > best[0]:
                best = (rank, sc, same, s)
        if best is not None:
            _, sc, same, s = best
            if sc >= ACCEPT or (same and sc >= ACCEPT_SAME_NUMBER):
                matches[c["id"]] = {"sid": s["id"], "sky": s["name"], "score": round(min(1.0, sc + (0.1 if same else 0)), 2),
                                    "how": "exact" if sc >= 0.999 else ("name and number" if same else "name"),
                                    "same_number": same}
                continue
        # Not matched: a near miss is listed for review (a close name, or a source channel on the same number).
        guess = best[3]["name"] if best else None
        gscore = round(best[1], 2) if best else 0.0
        same_num = by_number.get(number) if number is not None else None
        if gscore >= REVIEW_FLOOR or same_num is not None:
            review.append({"id": c["id"], "name": c["name"], "number": c.get("number"),
                           "guess": guess, "guess_score": gscore,
                           "same_number": same_num["name"] if same_num else None})
    return matches, review, problems


# ---------------------------------------------------------------------------------------------------------------
# Dispatcharr: the house's channels, the guide source, the channel mapping

def lineup():
    """The channels in the house's lineup: enabled in at least one channel profile (every channel when there are no
    profiles), radio left out."""
    from apps.channels.models import Channel, ChannelProfile, ChannelProfileMembership
    qs = Channel.objects.select_related("epg_data", "epg_data__epg_source").filter(is_radio=False)
    if ChannelProfile.objects.exists():
        ids = set(ChannelProfileMembership.objects.filter(enabled=True).values_list("channel_id", flat=True))
        qs = qs.filter(id__in=ids)
    out = []
    for c in qs.only("id", "name", "channel_number", "epg_data__name", "epg_data__epg_source__name"):
        out.append({"id": c.id, "name": c.name or "", "number": float(c.channel_number) if c.channel_number is not None else None,
                    "epg_id": c.epg_data_id,
                    "epg_name": c.epg_data.name if c.epg_data_id else None,
                    "epg_source": c.epg_data.epg_source.name if c.epg_data_id and c.epg_data.epg_source_id else None})
    return out


def ensure_epg_source(source, log=None):
    """The Dispatcharr guide source for this listings source, made once (Dispatcharr refreshes a new source itself)
    or kept pointing at the file. Returns (source, created)."""
    from apps.epg.models import EPGSource
    url = INTERNAL_BASE + URL_PATH
    src = EPGSource.objects.filter(name=source.epg_name).first()
    if src is None:
        src = EPGSource.objects.create(name=source.epg_name, source_type="xmltv", url=url, is_active=True,
                                       refresh_interval=0, custom_properties={"viewarr_guide": source.key})
        if log:
            log("guide: made Dispatcharr guide source \"%s\" (%s)" % (source.epg_name, url))
        return src, True
    fix = {}
    if src.url != url:
        fix["url"] = url
    if src.source_type != "xmltv":
        fix["source_type"] = "xmltv"
    if fix:
        EPGSource.objects.filter(pk=src.pk).update(**fix)
    return src, False


def refresh_epg_source(src_id):
    """Ask Dispatcharr to read the file again, the way its own Refresh button does (on the plugins queue)."""
    try:
        from apps.plugins.internal_tasks import dispatch_plugin_task
        dispatch_plugin_task("apps.epg.tasks.refresh_epg_data", args=[src_id])
        return
    except Exception:
        pass
    from apps.epg.tasks import refresh_epg_data
    refresh_epg_data.delay(src_id)


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


def reconcile(data_dir, cfg, state, log=None):
    """Points the matched channels at this guide (guide_apply on), or puts back what they had (off). The guide each
    channel had before is kept in guide_backup.json until it's put back. Channels with a hand-set guide override in
    Dispatcharr (ChannelOverride.epg_data) are left alone. Returns (pointed, put back, waiting for Dispatcharr)."""
    from apps.channels.models import Channel, ChannelOverride
    from apps.epg.models import EPGData, EPGSource

    source = cfg["source"]
    bpath = os.path.join(data_dir, BACKUP_FILE)
    backup = _load_json(bpath, {})
    chans = backup.setdefault("channels", {})
    src = EPGSource.objects.filter(name=source.epg_name).first()
    ours_by_tvg = dict(EPGData.objects.filter(epg_source=src).values_list("tvg_id", "id")) if src else {}
    ours = set(ours_by_tvg.values())
    pointed = restored = waiting = 0
    changed = False

    def put_back(cid):
        nonlocal restored, changed
        prev = chans.pop(cid, None)
        changed = True
        ch = Channel.objects.filter(id=int(cid)).first()
        # Only if it still shows this guide (or none): a guide someone chose by hand since is kept.
        if ch is not None and (ch.epg_data_id is None or ch.epg_data_id in ours):
            if prev is not None and not EPGData.objects.filter(id=prev).exists():
                prev = None
            ch.epg_data_id = prev
            ch.save(update_fields=["epg_data"])
            restored += 1

    wanted = {}
    if cfg["apply"] and src is not None:
        for cid, m in (state.get("matches") or {}).items():
            wanted[str(cid)] = ours_by_tvg.get(source.channel_prefix + m["sid"])
        overridden = set(str(x) for x in ChannelOverride.objects.filter(epg_data__isnull=False).values_list("channel_id", flat=True))
        for ch in Channel.objects.filter(id__in=[int(c) for c in wanted]).only("id", "epg_data"):
            cid = str(ch.id)
            target = wanted.get(cid)
            if cid in overridden:
                continue
            if not target:
                waiting += 1      # Dispatcharr hasn't read that channel from the file yet
                continue
            if ch.epg_data_id != target:
                if cid not in chans:
                    chans[cid] = ch.epg_data_id
                ch.epg_data_id = target
                ch.save(update_fields=["epg_data"])
                pointed += 1
                changed = True
    # Everything in the backup that shouldn't point here any more goes back.
    for cid in list(chans):
        if cid not in wanted or (not cfg["apply"]):
            put_back(cid)
    if changed:
        backup["at"] = int(time.time())
        _save_json(bpath, backup)
    if log and (pointed or restored):
        log("guide: %d channel(s) pointed at %s, %d put back to their earlier guide" % (pointed, source.epg_name, restored))
    return pointed, restored, waiting


# ---------------------------------------------------------------------------------------------------------------
# XMLTV

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _x(s):
    return escape(_CTRL.sub("", str(s)))


def _t(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def write_xmltv(source, channels, programmes, path=OUT_FILE):
    """channels: [(service, [display names])]; programmes: {service id: [programme]}. Written to a temporary file
    beside the real one, then renamed over it, so Dispatcharr never reads half a file. Returns programmes written."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    n = 0
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
        f.write('<tv generator-info-name="Viewarr plugin" generator-info-url="https://github.com/petebutty/viewarr-plugin"'
                ' source-info-name=%s>\n' % quoteattr(source.label))
        for svc, names in channels:
            f.write('  <channel id=%s>\n' % quoteattr(source.channel_prefix + svc["id"]))
            for nm in names:
                f.write('    <display-name lang="en">%s</display-name>\n' % _x(nm))
            if svc.get("logo"):
                f.write('    <icon src=%s/>\n' % quoteattr(svc["logo"]))
            f.write('  </channel>\n')
        for svc, _ in channels:
            cid = quoteattr(source.channel_prefix + svc["id"])
            for p in programmes.get(svc["id"], []):
                out = ['  <programme start="%s" stop="%s" channel=%s>' % (_t(p["start"]), _t(p["stop"]), cid),
                       '<title lang="en">%s</title>' % _x(p["title"])]
                if p.get("sub_title"):
                    out.append('<sub-title lang="en">%s</sub-title>' % _x(p["sub_title"]))
                if p.get("desc"):
                    out.append('<desc lang="en">%s</desc>' % _x(p["desc"]))
                for cat in p.get("category") or []:
                    out.append('<category lang="en">%s</category>' % _x(cat))
                if p.get("icon"):
                    out.append('<icon src=%s/>' % quoteattr(p["icon"]))
                se, ep = p.get("season"), p.get("episode")
                # (Only with a season too: Sky numbers news bulletins and weather as "episodes" of a year-long run.)
                if se and ep:
                    out.append('<episode-num system="xmltv_ns">%d.%d.</episode-num>' % (se - 1, ep - 1))
                    out.append('<episode-num system="onscreen">S%02dE%02d</episode-num>' % (se, ep))
                if p.get("hd"):
                    out.append('<video><quality>HDTV</quality></video>')
                if p.get("new"):
                    out.append('<new/>')
                if p.get("live"):
                    # (Not in the XMLTV DTD, but Dispatcharr reads it, and Viewarr's Live now uses it.)
                    out.append('<live/>')
                if p.get("subtitles"):
                    out.append('<subtitles type="teletext"/>')
                if p.get("rating"):
                    out.append('<rating system="BBFC"><value>%s</value></rating>' % _x(p["rating"]))
                out.append('</programme>\n')
                f.write("".join(out))
                n += 1
        f.write('</tv>\n')
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return n


# ---------------------------------------------------------------------------------------------------------------
# One run

def _cache_load(data_dir, key):
    try:
        with gzip.open(os.path.join(data_dir, CACHE_FILE), "rt", encoding="utf-8") as f:
            c = json.load(f)
        if c.get("key") == key:
            return c
    except Exception:
        pass
    return {"key": key, "days": {}}


def _cache_save(data_dir, cache):
    path = os.path.join(data_dir, CACHE_FILE)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, path)


def run(data_dir, cfg, stop=None, log=None, touch_dispatcharr=True):
    """Channel list → matching → listings → XMLTV → Dispatcharr's guide source. Returns the new state."""
    from django.db import close_old_connections
    close_old_connections()
    t0 = time.time()
    source = cfg["source"]
    spath = os.path.join(data_dir, STATE_FILE)
    state = _load_json(spath, {})
    state["last_attempt"] = int(t0)
    http = Http(stop, log)
    region = source.region_label(cfg["settings"])

    if getattr(source, "HAWK", None) == "":
        state.update(error="no Sky listings address added (plugin settings: Sky listings)", sig_run=cfg["sig"], on=True)
        _save_json(spath, state)
        return state
    services = source.services(cfg["settings"], http)
    if services:
        state["services"] = services
        state["services_at"] = int(t0)
    else:
        services = state.get("services") if state.get("services_region") == region else None
        if not services:
            state.update(error="couldn't read the %s channel list" % source.label, sig_run=cfg["sig"], on=True)
            _save_json(spath, state)
            if log:
                log("guide: %s %s: couldn't read the channel list (%d requests); kept the last guide" % (source.label, region, http.count))
            return state
    state["services_region"] = region

    channels = lineup()
    matches, review, problems = match(channels, services, source, cfg["overrides"])
    by_sid = {s["id"]: s for s in services}
    sids = sorted({m["sid"] for m in matches.values()})

    cache = _cache_load(data_dir, "%s|%s" % (source.key, region))
    programmes, failed = source.schedules(cfg["settings"], sids, cfg["days"], http, cache)
    now = int(time.time())
    horizon = now + cfg["days"] * 86400 + 86400
    for sid in list(programmes):
        programmes[sid] = [p for p in programmes[sid] if p["stop"] > now - 6 * 3600 and p["start"] < horizon]
    try:
        _cache_save(data_dir, cache)
    except Exception as exc:
        if log:
            log("guide: couldn't save the listings cache: %s" % exc)

    # Each source channel is named after the first (lowest-numbered) house channel matched to it, then its own name.
    names = {}
    for c in sorted(channels, key=lambda c: (c["number"] is None, c["number"] or 0, c["name"])):
        m = matches.get(c["id"])
        if m:
            names.setdefault(m["sid"], [])
            if c["name"] not in names[m["sid"]]:
                names[m["sid"]].append(c["name"])
    xml_channels = [(by_sid[sid], (names.get(sid, [])[:1] + [by_sid[sid]["name"]])) for sid in sids if sid in by_sid]
    written = 0
    if any(programmes.get(sid) for sid in sids):
        written = write_xmltv(source, xml_channels, programmes)
    elif log:
        log("guide: no listings at all this time; the last guide file is kept")

    src_id = None
    if touch_dispatcharr and written:
        try:
            src, created = ensure_epg_source(source, log)
            src_id = src.id
            if not created:
                refresh_epg_source(src.id)
        except Exception as exc:
            if log:
                log("guide: couldn't update Dispatcharr's guide source: %s" % exc)

    by_id = {c["id"]: c for c in channels}
    state.update({
        "on": True, "error": None, "sig_run": cfg["sig"], "last_run": now, "seconds": round(time.time() - t0, 1),
        "source": source.key, "source_label": source.label, "region": region, "days": cfg["days"],
        "epg_source": source.epg_name, "epg_source_id": src_id or state.get("epg_source_id"),
        "matches": {str(k): v for k, v in matches.items()},
        "review": review, "problems": problems,
        "counts": {"services": len(services), "channels": len(channels), "matched": len(matches),
                   "sources": len(sids), "programmes": written, "requests": http.count, "failed": failed,
                   "overrides": sum(1 for m in matches.values() if m["how"] == "override"), "review": len(review)},
        "names": {str(k): by_id[k]["name"] for k in matches if k in by_id},
        "numbers": {str(k): by_id[k]["number"] for k in matches if k in by_id},
    })
    _save_json(spath, state)
    if log:
        c = state["counts"]
        log("guide: %s %s: %d of %d channels matched (%d by override) to %d of %d %s channels; %d programmes for %d "
            "day(s); %d requests, %d failed%s; %.0f s" % (
                source.label, region, c["matched"], c["channels"], c["overrides"], c["sources"], c["services"],
                source.label, written, cfg["days"], c["requests"], failed,
                " (kept the last good listings for those)" if failed else "", time.time() - t0))
    return state


# ---------------------------------------------------------------------------------------------------------------
# What Viewarr reads (viewarr_guide) and the status line

def _logo_state(data_dir):
    return _load_json(os.path.join(data_dir, LOGO_STATE_FILE), {}) if data_dir else {}


def app_value(state, cfg, applied=0, data_dir=None):
    names, numbers = state.get("names") or {}, state.get("numbers") or {}
    logos = _logo_state(data_dir)
    per = (logos.get("channels") or {}) if logos.get("mode") not in (None, "off") else {}
    # "logo": where the channel's logo comes from: "sky", "tv-logos" or "kept" (its own); absent when logos are off.
    matched = [dict({"id": int(cid), "name": names.get(cid, ""), "number": numbers.get(cid), "source_name": m["sky"],
                     "source_id": m["sid"], "score": m["score"], "how": m["how"]},
                    **({"logo": per[cid]} if cid in per else {}))
               for cid, m in (state.get("matches") or {}).items()]
    matched.sort(key=lambda x: (x["number"] is None, x["number"] or 0, x["name"]))
    review = sorted(state.get("review") or [], key=lambda x: (x.get("number") is None, x.get("number") or 0, x["name"]))
    v = {"v": 1, "on": bool(cfg["on"]), "apply": bool(cfg["apply"]), "source": state.get("source"),
         "source_label": state.get("source_label"), "region": state.get("region"), "days": state.get("days"),
         "url": URL_PATH, "epg_source": state.get("epg_source"), "at": state.get("last_run"),
         "error": state.get("error"), "counts": state.get("counts") or {}, "applied": applied,
         "problems": state.get("problems") or [], "matched": matched, "review": review}
    if per:
        v["logos"] = {"mode": logos.get("mode"), "at": logos.get("at"), "filled": logos.get("filled", 0),
                      "replaced": logos.get("replaced", 0), "put_back": logos.get("put_back", 0),
                      "from_sky": logos.get("from_sky", 0), "from_tvlogos": logos.get("from_tvlogos", 0),
                      "kept": logos.get("kept", 0)}
    while len(json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
        if v["review"]:
            v["review"] = v["review"][: int(len(v["review"]) * 0.8)]
        elif v["matched"]:
            v["matched"] = v["matched"][: int(len(v["matched"]) * 0.8)]
        else:
            break
    return v


def publish(value):
    from django.contrib.auth import get_user_model
    from apps.accounts.models import UserAppData
    size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    for user in get_user_model().objects.filter(is_active=True).only("id"):
        UserAppData.objects.update_or_create(user=user, key=APP_KEY, defaults={"value": value, "byte_size": size})


def status(data_dir):
    st = _load_json(os.path.join(data_dir, STATE_FILE), {})
    backup = _load_json(os.path.join(data_dir, BACKUP_FILE), {})
    return {"on": st.get("on", False), "at": st.get("last_run"), "region": st.get("region"),
            "counts": st.get("counts") or {}, "error": st.get("error"),
            "applied": len((backup.get("channels") or {}))}


def status_text(data_dir, settings):
    cfg = config(settings)
    st = _load_json(os.path.join(data_dir, STATE_FILE), {})
    backup = _load_json(os.path.join(data_dir, BACKUP_FILE), {})
    if not cfg["on"]:
        left = len(backup.get("channels") or {})
        return "TV guide is off." + (" %d channel(s) still to be put back to their earlier guide." % left if left else "")
    if not st.get("last_run"):
        return "TV guide is on; the first fetch hasn't finished yet." + (" Last problem: " + st["error"] if st.get("error") else "")
    c = st.get("counts") or {}
    when = clock.strftime("%a %H:%M", st["last_run"])
    applied = len(backup.get("channels") or {})
    lines = ["TV guide: %s %s, fetched %s: %d of %d channels matched to %d %s channels, %d programmes for %d day(s) "
             "(%d requests, %d failed). %s" % (
                 st.get("source_label"), st.get("region"), when, c.get("matched", 0), c.get("channels", 0),
                 c.get("sources", 0), st.get("source_label"), c.get("programmes", 0), st.get("days", 0),
                 c.get("requests", 0), c.get("failed", 0),
                 ("Applied: %d channel(s) use this guide." % applied) if cfg["apply"] else
                 "Side by side: no channel's guide has been changed (\"Point matched channels at this guide\" is off).")]
    if st.get("error"):
        lines.append("Last problem: " + st["error"])
    lg = _logo_state(data_dir)
    per = lg.get("channels") or {}
    if lg.get("mode") not in (None, "off") and lg.get("at"):
        mine = [per.get(cid) for cid in (st.get("matches") or {})]
        lines.append("Logos (%s, %s): last run %d filled, %d replaced, %d put back; of the matched channels %d have the "
                     "logo from Sky, %d from tv-logos, %d kept their own." % (
                         lg.get("mode"), clock.strftime("%a %H:%M", lg["at"]), lg.get("filled", 0),
                         lg.get("replaced", 0), lg.get("put_back", 0), mine.count("sky"), mine.count("tv-logos"),
                         mine.count("kept")))
    else:
        per = {}
        lines.append("Logos: off (\"Channel logos from Sky\").")
    if st.get("problems"):
        lines.append("Overrides not understood: " + "; ".join(st["problems"][:10]))
    names, numbers = st.get("names") or {}, st.get("numbers") or {}
    weak = sorted(((m["score"], names.get(cid, cid), m["sky"], cid) for cid, m in (st.get("matches") or {}).items()
                   if m["how"] not in ("exact", "override") and m["score"] < 0.95))
    lw = {"sky": "logo: from Sky", "tv-logos": "logo: from tv-logos", "kept": "logo: kept"}
    if weak:
        lines.append("Least sure matches: " + "; ".join(
            "%s = %s (%.2f%s)" % (n, s, sc, (", " + lw[per[cid]]) if per.get(cid) in lw else "")
            for sc, n, s, cid in weak[:15]))
    review = sorted(st.get("review") or [], key=lambda x: (x.get("number") is None, x.get("number") or 0))
    if review:
        def one(r):
            hint = r.get("same_number") or r.get("guess")
            return "%s%s" % (r["name"], (" (maybe %s)" % hint) if hint else "")
        lines.append("Not matched, worth a look (%d; add \"name = %s name\" to the overrides): %s" % (
            len(review), st.get("source_label"), "; ".join(one(r) for r in review[:40])))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# Comparing with the guide each channel uses now

def _title_norm(t):
    t = re.sub(r"(?i)^(new|live)\s*:?\s*", "", t or "")
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _same_title(a, b):
    a, b = _title_norm(a), _title_norm(b)
    if not a or not b:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def compare(data_dir, settings, only_source=None, hours=24, slack_min=5):
    """For each matched channel: the next `hours` of this guide against the guide the channel uses now (or used
    before "Point matched channels at this guide" moved it). A programme agrees when the other guide has one with the
    same title starting within `slack_min` minutes. Returns (per-channel rows, overall %)."""
    from apps.channels.models import Channel
    from apps.epg.models import EPGData, ProgramData
    cfg = config(settings)
    source = cfg["source"]
    st = _load_json(os.path.join(data_dir, STATE_FILE), {})
    backup = (_load_json(os.path.join(data_dir, BACKUP_FILE), {}).get("channels") or {})
    cache = _cache_load(data_dir, "%s|%s" % (source.key, st.get("region")))
    now = int(time.time())
    end = now + hours * 3600
    ours = {}
    for d in sorted(cache.get("days") or {}):
        for sid, progs in cache["days"][d].items():
            for p in progs:
                if p["start"] < end and p["stop"] > now:
                    ours.setdefault(sid, {})[(p["start"], p["title"])] = p
    rows = []
    tot_ok = tot_n = 0
    matches = st.get("matches") or {}
    chans = {c.id: c for c in Channel.objects.select_related("epg_data", "epg_data__epg_source").filter(id__in=[int(x) for x in matches])}
    for cid, m in matches.items():
        ch = chans.get(int(cid))
        if ch is None:
            continue
        epg = ch.epg_data
        if epg is not None and epg.epg_source and epg.epg_source.name == source.epg_name:
            prev = backup.get(cid)
            epg = EPGData.objects.select_related("epg_source").filter(id=prev).first() if prev else None
        if epg is None:
            rows.append({"channel": ch.name, "other": None, "agree": None, "ours": 0, "theirs": 0})
            continue
        other_name = epg.epg_source.name if epg.epg_source_id else "?"
        if only_source and only_source.lower() not in other_name.lower():
            continue
        mine = sorted(ours.get(m["sid"], {}).values(), key=lambda p: p["start"])
        theirs = list(ProgramData.objects.filter(epg=epg, start_time__lt=datetime.fromtimestamp(end, timezone.utc),
                                                 end_time__gt=datetime.fromtimestamp(now, timezone.utc))
                      .values_list("title", "start_time"))
        theirs = [(t or "", int(s.timestamp())) for t, s in theirs]
        # Only the stretch both guides cover (one may simply end sooner).
        if mine and theirs:
            limit = min(mine[-1]["stop"], max(s for _, s in theirs) + 1)
            mine = [p for p in mine if p["start"] < limit]
            theirs = [x for x in theirs if x[1] < limit]
        ok = 0
        for p in mine:
            if any(abs(s - p["start"]) <= slack_min * 60 and _same_title(p["title"], t) for t, s in theirs):
                ok += 1
        n = max(len(mine), len(theirs))
        pct = round(100.0 * ok / n) if n else None
        rows.append({"channel": ch.name, "number": float(ch.channel_number) if ch.channel_number is not None else None,
                     "other": other_name, "agree": pct, "ours": len(mine), "theirs": len(theirs)})
        tot_ok += ok
        tot_n += n
    rows.sort(key=lambda r: (r["agree"] is None, r["agree"] if r["agree"] is not None else 0, r["channel"]))
    return rows, (round(100.0 * tot_ok / tot_n) if tot_n else None)


# ---------------------------------------------------------------------------------------------------------------
# The thread

def _last_slot(now):
    """The most recent scheduled run time (04:30 or 16:30 local) at or before now."""
    best = 0
    for back in (0, 1):
        day = clock.local_today(now - back * 86400)
        for h, m in RUN_AT:
            t = clock.local_epoch(day, h, m)
            if best < t <= now:
                best = t
    return best


class _Lock:
    """One run at a time across processes (the service's thread and a "Fetch now" button)."""

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
    """A full run straight away (the thread's runs and the "Fetch the TV guide now" button), then the channel
    mapping and the app data. Returns (state, message)."""
    cfg = config(settings)
    if not cfg["on"]:
        return None, "The TV guide is switched off in the plugin's settings."
    with _Lock(data_dir) as got:
        if not got:
            return None, "A guide fetch is already running; try again in a minute."
        state = run(data_dir, cfg, stop=stop, log=log)
        applied = 0
        try:
            reconcile(data_dir, cfg, state, log)
            applied = len((_load_json(os.path.join(data_dir, BACKUP_FILE), {}).get("channels") or {}))
        except Exception as exc:
            if log:
                log("guide: couldn't update the channel mapping: %s" % exc)
        try:
            publish(app_value(state, cfg, applied, data_dir))
        except Exception as exc:
            if log:
                log("guide: couldn't publish the review list: %s" % exc)
    return state, status_text(data_dir, settings)


def take_flag(data_dir, name):
    """True (and the file gone) when the flag file [name] was left in the data folder."""
    try:
        os.remove(os.path.join(data_dir, name))
        return True
    except OSError:
        return False


def fetch_running(data_dir):
    """True while a guide fetch holds the lock (in any process)."""
    with _Lock(data_dir) as got:
        return not got


def _tick(data_dir, settings_fn, stop, log, mem):
    from django.db import close_old_connections
    close_old_connections()
    settings = settings_fn() or {}
    cfg = config(settings)
    asked = take_flag(data_dir, REFRESH_FLAG)   # ("Fetch now": dropped while the guide is off)
    spath = os.path.join(data_dir, STATE_FILE)
    state = _load_json(spath, {})
    now = time.time()
    if not cfg["on"]:
        # Off: channels go back to the guide they had, the TVs are told, nothing is fetched.
        if mem.get("was_on", state.get("on")) or (_load_json(os.path.join(data_dir, BACKUP_FILE), {}).get("channels")):
            try:
                reconcile(data_dir, cfg, state, log)
            except Exception as exc:
                if log:
                    log("guide: couldn't put the channels back: %s" % exc)
            if state.get("on"):
                state["on"] = False
                _save_json(spath, state)
            try:
                publish(app_value(state, cfg, 0, data_dir))
            except Exception:
                pass
        mem["was_on"] = False
        return
    mem["was_on"] = True
    last_run = state.get("last_run") or 0
    last_try = state.get("last_attempt") or 0
    due = (
        asked                                                                 # the "Fetch now" button
        or not state.get("on")                                                # just switched on
        or (state.get("sig_run") != cfg["sig"] and now - last_try >= SETTLE_S)  # region, days or overrides changed
        or last_run < _last_slot(now) and now - last_try >= SETTLE_S          # 04:30 / 16:30
        or (state.get("error") and now - last_try >= RETRY_FAILED_S)          # the last try got nothing
    )
    if due:
        run_now(data_dir, settings, stop=stop, log=log)
        mem["apply_at"] = now + 120   # Dispatcharr reads the file in the background: check again shortly
        mem["apply_tries"] = 5
        mem["applied_sig"] = (cfg["apply"],)
        return
    # Between runs: the channel mapping follows "guide_apply" at once, and is checked again now and then (Dispatcharr
    # makes its entries for the file's channels a little after each refresh).
    if mem.get("applied_sig") != (cfg["apply"],) or now >= mem.get("apply_at", 0):
        try:
            pointed, restored, waiting = reconcile(data_dir, cfg, state, log)
            if mem.get("applied_sig") != (cfg["apply"],) or pointed or restored:
                applied = len((_load_json(os.path.join(data_dir, BACKUP_FILE), {}).get("channels") or {}))
                publish(app_value(state, cfg, applied, data_dir))
            tries = mem.get("apply_tries", 0)
            if waiting and tries > 0:
                mem["apply_tries"] = tries - 1
                mem["apply_at"] = now + 120
            else:
                mem["apply_at"] = now + APPLY_EVERY_S
        except Exception as exc:
            if log:
                log("guide: couldn't update the channel mapping: %s" % exc)
            mem["apply_at"] = now + 300
        mem["applied_sig"] = (cfg["apply"],)


def _loop(stop, data_dir, settings_fn, log):
    mem = {}
    stop.wait(20)  # let the sports service's first round get going first
    while not stop.is_set():
        try:
            _tick(data_dir, settings_fn, stop, log, mem)
        except Exception as exc:
            if log:
                log("guide: round failed: %s" % exc)
        stop.wait(TICK_S)


_thread = None


def start(stop, data_dir, settings_fn, log=None):
    """The guide thread, once per service; it ends when the service's stop event is set."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return _thread
    _thread = threading.Thread(target=_loop, args=(stop, data_dir, settings_fn, log), name="viewarr-guide", daemon=True)
    _thread.start()
    return _thread
