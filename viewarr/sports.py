"""
Viewarr's sports-to-channel matching and "Live now", ported from the Android TV app (Kotlin) so the Dispatcharr
plugin can work them out once for every TV in the house.

Stdlib only. Entry point: compute(now_s, channels, programmes, followed, plugin_groups, http_get=None, log=None).

Kotlin sources (app: io/github/petebutty/viewarr/):
  data/sports/GameMatch.kt     -> norm, aliases, GameMatch scoring
  data/sports/SkySchedule.kt   -> SkySchedule
  data/sports/Sports.kt        -> ESPN fetches, SportFollow decoding, leagues
  data/guide/SportsEvents.kt   -> PLACEHOLDER / clean() / SPORTS (sport detection)
  ui/home/ClubChannels.kt      -> ClubChannels
  ui/home/GamePlacer.kt        -> GamePlacer (guide -> listings -> event channel -> club channel)
  ui/home/SportsHub.kt         -> shown games per follow, liveGames
  ui/guide/GuideViewModel.kt   -> ukMatch, blackout, channelForGame, liveEventsNow and helpers

Times are milliseconds inside (as in the app) and seconds in what compute() returns.
"""

import html as _html
import json
import re
import threading
import time as _time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _LONDON = ZoneInfo("Europe/London")
except Exception:  # (no tz database: UTC; only the 3pm blackout and "17:30" texts shift)
    _LONDON = timezone.utc

H = 3_600_000
MIN = 60_000
DAY = 86_400_000

SURE, POSSIBLE, NO = 2, 1, 0
NOT_ON_UK_TV = "\u0000not on UK TV"  # GuideViewModel.NOT_ON_UK_TV


# ---------------------------------------------------------------------------------------------------------------
# Small helpers (HttpJson.kt: str / child / array)

def _str(o, k):
    """JsonElement?.str: a primitive's content, else None."""
    if not isinstance(o, dict):
        return None
    v = o.get(k)
    if v is None or isinstance(v, (dict, list)):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _child(o, k):
    return o.get(k) if isinstance(o, dict) else None


def _arr(o, k):
    v = o.get(k) if isinstance(o, dict) else None
    return v if isinstance(v, list) else []


def _long(s):
    try:
        return int(str(s))
    except Exception:
        try:
            return int(float(str(s)))
        except Exception:
            return None


def _parse_iso_ms(s):
    """OffsetDateTime.parse(...).toInstant().toEpochMilli(); ESPN's "2026-11-21T15:00Z" too."""
    if not s:
        return None
    try:
        t = s.strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            return None
        return int(d.timestamp() * 1000)
    except Exception:
        return None


def _hhmm(ms):
    """Dates.time (24 h, the house's zone)."""
    return datetime.fromtimestamp(ms / 1000, _LONDON).strftime("%H:%M")


def _oset(items):
    """An insertion-ordered set (Kotlin's toSet() is a LinkedHashSet)."""
    return list(dict.fromkeys(items))


def _default_http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "Viewarr-Plugin/0.5 (+https://github.com/petebutty/viewarr-plugin)", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception:
        return 0, ""


class _Http:
    """HttpJson.get: (ok, code, body json)."""

    def __init__(self, get, log):
        self.get = get or _default_http_get
        self.log = log
        self.count = 0

    def json(self, url, headers=None):
        self.count += 1
        try:
            code, text = self.get(url, headers or {})
        except Exception as e:  # (an injected getter that throws: a failed request)
            if self.log:
                self.log("sports: request failed: %s" % type(e).__name__)
            return False, 0, None
        ok = 200 <= (code or 0) < 300
        body = None
        if ok:
            try:
                body = json.loads(text)
            except Exception:
                body = None
        return ok, code, body


# ---------------------------------------------------------------------------------------------------------------
# Sports.kt: SportGame, leagues, Sport.of

class Game:
    """SportGame. [start] in ms. An event (race, tournament, fight card) has away == ""."""
    __slots__ = ("id", "league", "league_name", "home", "away", "home_logo", "away_logo", "start", "state", "score",
                 "detail", "home_names", "away_names")

    def __init__(self, id, league, league_name, home, away, home_logo, away_logo, start, state, score, detail,
                 home_names=None, away_names=None):
        self.id, self.league, self.league_name = id, league, league_name
        self.home, self.away, self.home_logo, self.away_logo = home, away, home_logo, away_logo
        self.start, self.state, self.score, self.detail = start, state, score, detail
        self.home_names, self.away_names = home_names or [], away_names or []

    @property
    def is_event(self):
        return not self.away.strip()

    @property
    def title(self):
        return self.home if self.is_event else "%s v %s" % (self.home, self.away)


# Sports.kt SportsRepository.leagues: (path, name, short, teams)
LEAGUES = [
    ("soccer/sco.1", "Scottish Premiership", True), ("soccer/sco.2", "Scottish Championship", True),
    ("soccer/sco.tennents", "Scottish Cup", True), ("soccer/sco.cis", "Scottish League Cup", True),
    ("soccer/eng.1", "Premier League", True), ("soccer/eng.2", "Championship", True),
    ("soccer/eng.3", "League One", True), ("soccer/eng.4", "League Two", True), ("soccer/eng.fa", "FA Cup", True),
    ("soccer/eng.league_cup", "Carabao Cup", True), ("soccer/uefa.champions", "Champions League", True),
    ("soccer/uefa.europa", "Europa League", True), ("soccer/uefa.europa.conf", "Conference League", True),
    ("soccer/uefa.nations", "Nations League", True), ("soccer/fifa.worldq.uefa", "World Cup Qualifying (Europe)", True),
    ("soccer/fifa.world", "World Cup", True), ("soccer/esp.1", "La Liga", True), ("soccer/ita.1", "Serie A", True),
    ("soccer/ger.1", "Bundesliga", True), ("soccer/fra.1", "Ligue 1", True), ("soccer/ned.1", "Eredivisie", True),
    ("soccer/por.1", "Primeira Liga", True), ("soccer/usa.1", "MLS", True),
    ("rugby/180659", "Six Nations", True), ("rugby/267979", "Premiership Rugby", True),
    ("rugby/270557", "United Rugby Championship", True), ("rugby/244293", "The Rugby Championship", True),
    ("rugby/164205", "Rugby World Cup", True), ("rugby-league/3", "NRL", True),
    ("basketball/nba", "NBA", True), ("basketball/wnba", "WNBA", True),
    ("basketball/mens-college-basketball", "College Basketball", True), ("football/nfl", "NFL", True),
    ("football/college-football", "College Football", True), ("hockey/nhl", "NHL", True), ("baseball/mlb", "MLB", True),
    ("racing/f1", "Formula 1", False), ("racing/irl", "IndyCar", False),
    ("racing/nascar-premier", "NASCAR Cup Series", False), ("mma/ufc", "UFC", False),
    ("tennis/atp", "ATP Tennis", False), ("tennis/wta", "WTA Tennis", False), ("golf/pga", "PGA Tour", False),
    ("golf/eur", "DP World Tour", False), ("golf/lpga", "LPGA Tour", False),
]
_LEAGUE_BY_PATH = {p: (n, t) for p, n, t in LEAGUES}
_SPORT_OF = {"soccer": "Football", "rugby": "Rugby", "rugby-league": "Rugby", "golf": "Golf", "tennis": "Tennis",
             "racing": "Motor", "mma": "Fighting", "basketball": "Basketball", "football": "AmericanFootball",
             "hockey": "Hockey", "baseball": "Baseball", "cricket": "Cricket"}
_NO_TEAMS = {"Golf", "Tennis", "Motor", "Fighting"}


def _league(path):
    """SportsRepository.league(path): (name, teams)."""
    if path in _LEAGUE_BY_PATH:
        return _LEAGUE_BY_PATH[path]
    sport = _SPORT_OF.get(path.split("/", 1)[0], "Other")
    return path.split("/", 1)[-1], sport not in _NO_TEAMS


# ---------------------------------------------------------------------------------------------------------------
# GameMatch.kt

_DROP = {"fc", "afc", "cf", "sc", "the", "ac", "as", "ssc", "sv", "fk", "sk"}
_GENERIC = {
    "united", "city", "town", "athletic", "athletico", "county", "rovers", "wanderers", "albion", "real", "club",
    "sporting", "football", "rangers", "saint", "north", "south", "east", "west", "under", "women", "ladies", "academy",
    "manchester", "sheffield", "bristol", "dundee", "milton", "keynes", "queens", "park", "hotspur", "forest", "palace",
    "villa", "borussia",
}
_EXTRA = {
    "manchester united": ["man utd", "man united"], "manchester city": ["man city"],
    "tottenham hotspur": ["spurs", "tottenham"], "wolverhampton wanderers": ["wolves"],
    "brighton and hove albion": ["brighton"], "nottingham forest": ["nottm forest", "notts forest"],
    "queens park rangers": ["qpr"], "sheffield wednesday": ["sheff wed", "sheffield weds"],
    "sheffield united": ["sheff utd", "sheffield utd"], "west bromwich albion": ["west brom"],
    "heart of midlothian": ["hearts"], "hibernian": ["hibs"], "inverness caledonian thistle": ["inverness ct", "inverness"],
    "paris saint germain": ["psg", "paris sg"], "internazionale": ["inter milan", "inter"],
    "bayern munich": ["bayern", "bayern munchen"], "milton keynes dons": ["mk dons"],
    "newcastle united": ["newcastle", "newcastle utd"], "west ham united": ["west ham", "west ham utd"],
    "leeds united": ["leeds", "leeds utd"], "afc bournemouth": ["bournemouth"], "athletic club": ["athletic bilbao"],
    "atletico madrid": ["atletico de madrid"], "borussia dortmund": ["dortmund"], "dundee united": ["dundee utd"],
    "oxford united": ["oxford utd"], "cambridge united": ["cambridge utd"], "peterborough united": ["peterborough"],
    "wycombe wanderers": ["wycombe"], "bolton wanderers": ["bolton"], "blackburn rovers": ["blackburn"],
    "bristol rovers": ["bristol rov"], "plymouth argyle": ["plymouth"], "preston north end": ["preston"],
    "huddersfield town": ["huddersfield"], "ipswich town": ["ipswich"], "luton town": ["luton"],
    "leicester city": ["leicester"], "norwich city": ["norwich"], "stoke city": ["stoke"],
    "swansea city": ["swansea"], "cardiff city": ["cardiff"], "hull city": ["hull"], "coventry city": ["coventry"],
    "birmingham city": ["birmingham"], "derby county": ["derby"], "charlton athletic": ["charlton"],
    "wigan athletic": ["wigan"], "oldham athletic": ["oldham"], "st mirren": ["saint mirren"],
    "st johnstone": ["saint johnstone"],
    "la clippers": ["los angeles clippers"], "la lakers": ["los angeles lakers"], "la galaxy": ["los angeles galaxy"],
}
_YOUTH = re.compile(r"\b(u\s?\d{2}|under\s?\d{2}|youth|academy|reserves?|b team|ii)\b")
_WOMEN = re.compile(r"\b(women|womens|ladies|w)\b")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_EVENT_GENERIC = {
    "grand", "prix", "championship", "championships", "open", "tour", "fight", "night", "presented", "pres", "cup",
    "classic", "invitational", "series", "race", "round", "final", "finals", "tournament", "international", "world",
    "masters", "event", "ufc", "golf", "tennis", "formula", "nascar", "indycar", "atp", "wta", "pga", "lpga", "day",
    "week", "main", "card",
}

_norm_cache = {}


def norm(s):
    """GameMatch.norm: accents off, lower case, & -> and, letters and digits only, FC/AFC/the... dropped."""
    r = _norm_cache.get(s)
    if r is not None:
        return r
    t = unicodedata.normalize("NFD", s)
    t = "".join(c for c in t if not unicodedata.category(c).startswith("M")).lower()
    t = _NON_ALNUM.sub(" ", t.replace("&", " and "))
    r = " ".join(w for w in t.split(" ") if w.strip() and w not in _DROP)
    if len(_norm_cache) > 200_000:
        _norm_cache.clear()
    _norm_cache[s] = r
    return r


def aliases(names):
    """GameMatch.aliases: every name for a team (ESPN's, normalised, plus the ones TV uses)."""
    base = _oset(n for n in (norm(x) for x in names) if len(n) >= 3)
    return _oset(base + [e for b in base for e in _EXTRA.get(b, [])])


def _telling(al):
    return _oset(w for a in al for w in a.split(" ") if len(w) >= 5 and w not in _GENERIC)


def _sport_words(league):
    """GameMatch.sportWords."""
    if league.startswith("racing/f1"):
        return ["f1", "formula 1", "formula one", "grand prix", "gp"]
    if league.startswith("racing/irl"):
        return ["indycar", "indy"]
    if league.startswith("racing/nascar"):
        return ["nascar"]
    if league.startswith("racing/"):
        return ["racing", "grand prix", "gp", "motogp", "motorsport"]
    if league.startswith("mma/"):
        return ["ufc", "mma", "fight night"]
    if league.startswith("golf/"):
        return ["golf", "pga", "lpga", "dp world", "liv"]
    if league.startswith("tennis/"):
        return ["tennis", "atp", "wta"]
    if league.startswith("boxing"):
        return ["boxing", "fight"]
    return []


class _Prepared:
    """GameMatch.Prepared: a game's names and words, worked out once."""
    __slots__ = ("home", "away", "home_t", "away_t", "youth", "women", "event_words", "event_telling", "sport_words")


_prepared = {}


def _prep(g):
    """GameMatch.prep."""
    key = "%s|%s|%s|%s" % (g.league, g.id, g.home, g.away)
    p = _prepared.get(key)
    if p is not None:
        return p
    if len(_prepared) > 3000:
        _prepared.clear()
    p = _Prepared()
    p.home = aliases(g.home_names or [g.home])
    p.away = aliases(g.away_names or [g.away])
    game_text = " " + norm(g.home + " " + g.away + " " + g.league_name) + " "
    words = _oset(w for a in p.home for w in a.split(" ") if len(w) >= 3)
    p.home_t, p.away_t = _telling(p.home), _telling(p.away)
    p.youth = bool(_YOUTH.search(game_text))
    p.women = bool(_WOMEN.search(game_text)) or ".w." in g.league
    p.event_words = words
    p.event_telling = [w for w in words if len(w) >= 4 and w not in _EVENT_GENERIC and not w.isdigit()]
    p.sport_words = [norm(w) for w in _sport_words(g.league)]
    _prepared[key] = p
    return p


def _team_in(text, al, tell):
    """GameMatch.teamIn."""
    if any(" %s " % a in text for a in al):
        return 2
    if any(" %s " % t in text for t in tell):
        return 1
    return 0


def _side_mismatch(text, p):
    return bool(_YOUTH.search(text)) != p.youth or (bool(_WOMEN.search(text)) and not p.women)


def _event_in(text, p):
    """GameMatch.eventIn."""
    sport = any(" %s " % w in text for w in p.sport_words)
    tell = p.event_telling
    if tell:
        named = any(" %s " % w in text for w in tell)
    else:
        named = bool(p.event_words) and all(" %s " % w in text for w in p.event_words)
    if sport and named:
        return SURE
    if named and sum(1 for w in tell if " %s " % w in text) >= 2:
        return POSSIBLE
    return NO


def _verdict(h, a):
    """GameMatch.verdict."""
    if h >= 1 and a >= 1 and h + a >= 3:
        return SURE
    if (h == 2 and a == 0) or (a == 2 and h == 0):
        return POSSIBLE
    return NO


def norm_text_is_game(text, g):
    """GameMatch.normTextIsGame: [text] already normalised and padded with a space each side."""
    p = _prep(g)
    if g.is_event:
        return _event_in(text, p)
    if _side_mismatch(text, p):
        return NO
    return _verdict(_team_in(text, p.home, p.home_t), _team_in(text, p.away, p.away_t))


def text_is_game(raw, g):
    """GameMatch.textIsGame."""
    return norm_text_is_game(" " + norm(raw) + " ", g)


def teams_are_game(home, away, g):
    """GameMatch.teamsAreGame: a listing's two team names, either way round."""
    if g.is_event:
        return text_is_game("%s %s" % (home, away), g)
    text = " " + norm("%s %s" % (home, away)) + " "
    p = _prep(g)
    if _side_mismatch(text, p):
        return NO
    h = " " + norm(home) + " "
    a = " " + norm(away) + " "
    straight = _verdict(_team_in(h, p.home, p.home_t), _team_in(a, p.away, p.away_t))
    swapped = _verdict(_team_in(h, p.away, p.away_t), _team_in(a, p.home, p.home_t))
    return max(straight, swapped)


def search_words(g):
    """GameMatch.searchWords: each team's most telling word (events: their name's words)."""
    p = _prep(g)
    if g.is_event:
        ws = _oset(w for a in p.home for w in a.split(" ") if len(w) >= 4 and w not in _EVENT_GENERIC)[:4]
        return ws or _sport_words(g.league)[:2]
    out = []
    for tell, al in ((p.home_t, p.home), (p.away_t, p.away)):
        if tell:
            best = tell[0]
            for t in tell:
                if len(t) > len(best):
                    best = t
            out.append(best)
        else:
            out.extend(al)
    return _oset(out)[:8]


# ---------------------------------------------------------------------------------------------------------------
# SportsEvents.kt: placeholders, clean(), sport detection

_PLACEHOLDER = re.compile(
    r"^(ended|finished|full time|ft)\b|^no (live |scheduled )?(event|events|programme|programmes|program|programs|game|games"
    r"|match|matches|fixture|fixtures|coverage|broadcast|stream)\b|^nothing (on|scheduled|live)\b|^(tba|tbc|to be announced"
    r"|off air|closedown|stand ?by|coming soon|coming up|upcoming|check back)\b|^event (ended|finished|over)"
    r"|\bno (fixtures|events|games|matches) today\b", re.I)
_LEADING_TIME = re.compile(r"^\d{1,2}[:.]\d{2}\s*(am|pm)?\s*[-–:|]?\s*", re.I)
_NAMED_TWICE = re.compile(r"^(\S+)\b(?:\s+\S+){0,2}?\s+\1\b")


def _r(words):
    return re.compile(r"\b(%s)\b" % words, re.I)


_SPORTS = [
    (_r("tennis|atp|wta|wimbledon|roland garros|us open tennis"), "Tennis"),
    (_r("rugby|six nations|urc|premiership rugby|super league|nrl"), "Rugby"),
    (_r("f1|formula 1|formula one|grand prix|indycar|nascar"), "Motor racing"),
    (_r("motogp|moto gp|superbikes?|wsbk|speedway"), "Motorbikes"),
    (_r("ufc|mma|boxing|matchroom|fight night|bellator|pfl|wwe|aew|wrestling|ppv|pay per view"), "Fight"),
    (_r("cricket|ashes|ipl|t20|odi|test match"), "Cricket"),
    (_r("golf|pga|ryder cup|the open|lpga|dp world"), "Golf"),
    (_r("darts|pdc|premier league darts"), "Darts"),
    (_r("snooker|pool|world matchplay snooker"), "Snooker"),
    (_r("nfl|american football|super bowl|ncaaf"), "American football"),
    (_r("nba|basketball|ncaab|euroleague basketball"), "Basketball"),
    (_r("nhl|ice hockey|hockey"), "Hockey"),
    (_r("mlb|baseball"), "Baseball"),
    (_r("horse racing|racing tv|cheltenham|ascot|grand national"), "Horse racing"),
    (_r("cycling|tour de france|giro|vuelta"), "Cycling"),
    (_r("efl|epl|spfl|premier league|championship|league one|league two|fa cup|carabao|uefa|champions league|europa"
        "|conference league|la liga|serie a|bundesliga|ligue 1|mls|football|soccer|scottish cup|nifl|vs?"), "Football"),
]


def clean(title):
    """SportsEvents.clean: None for a placeholder; else the tidied name."""
    t = (title or "").strip()
    if not t or _PLACEHOLDER.search(t):
        return None
    t = _LEADING_TIME.sub("", t)
    t = _NAMED_TWICE.sub(r"\1", t)
    t = t.strip()
    return t or None


def sport_of(text):
    """SportsEvents.sport: the sport's name, or None."""
    for rx, name in _SPORTS:
        if rx.search(text):
            return name
    return None


# ---------------------------------------------------------------------------------------------------------------
# Sports.kt: follows, ESPN (module-level caches persist between compute() runs in the plugin process)

_SEP, _REC = "\u001f", "\u001e"


class Cache:
    """What's kept between compute() rounds (the caller may hold one and pass it in; else a module-wide one is used).
    ESPN scoreboards / schedules: 5 min while one of the follow's games is live, starts within 3 h or was live in
    the last half hour, else 60 min for a league and 6 h for a team's schedule and results (see games_ttl()); an
    empty answer 15 min. Headline 2 min, Sky's days 6 h, the UK listings 6 h. (Score alerts don't depend on this:
    alerts.py polls the live games' scoreboards itself.)"""
    GAMES_TTL = 5 * MIN          # a follow with something on (live, soon, or just finished)
    LEAGUE_TTL = 60 * MIN        # a league with nothing on soon
    TEAM_TTL = 6 * H             # a team's schedule and results with nothing on soon
    EMPTY_TTL = 15 * MIN         # an empty answer (or a failed fetch)
    HOT_AHEAD = 3 * H
    HOT_AFTER = 30 * MIN
    HEADLINE_TTL = 2 * MIN
    SKY_TTL = 6 * H
    UK_TTL = 6 * H

    def __init__(self):
        self.lock = threading.Lock()
        self.games = {}       # shortcutId -> (at ms, [Game])
        self.live_seen = {}   # shortcutId -> when a fetch last found one of its games live (ms)
        self.not_in = {}      # "team|competition" -> at ms (24 h)
        self.headline = None  # (at ms, [Game])
        self.sky_days = {}    # date -> (at ms, [showing])
        self.sky_rest = 0     # after Sky didn't answer: left alone 10 minutes
        self.uk = None        # (at ms, [listing])
        self.uk_rest = 0      # after the listings site said 429 / 503: an hour


DEFAULT_CACHE = Cache()
_BASE = "https://site.api.espn.com/apis/site/v2/sports"


def decode_follows(followed):
    """SportsRepository.decode, tolerant: the app's "Kind<US>league<US>id<US>name<US>logo<US>pinned" records joined
    with <RS>, inside {"t": "s", "v": ...} as the app backs it up, or already-decoded dicts. Union, one per shortcut."""
    out, seen = [], set()

    def add(kind, league, id_, name):
        if kind not in ("Team", "League") or not league or not id_:
            return
        sid = "sport:team:%s:%s" % (league, id_) if kind == "Team" else "sport:league:%s" % league
        if sid in seen:
            return
        seen.add(sid)
        out.append({"kind": kind, "league": league, "id": id_, "name": name or id_, "sid": sid})

    def walk(o):
        if o is None:
            return
        if isinstance(o, str):
            for rec in o.split(_REC):
                p = rec.split(_SEP)
                if len(p) >= 6:
                    add(p[0], p[1], p[2], p[3])
            return
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if isinstance(o, dict):
            if "kind" in o and "league" in o:
                k = str(o.get("kind") or "")
                k = k[:1].upper() + k[1:].lower()
                add(k, str(o.get("league") or ""), str(o.get("id") or o.get("league") or ""), o.get("name"))
            elif "v" in o:
                walk(o.get("v"))
            elif "sports_follow" in o:
                walk(o.get("sports_follow"))

    walk(followed)
    return out


def _headline_game(e, league, league_name):
    """SportsRepository.headlineGame."""
    id_ = _str(e, "id")
    if id_ is None:
        return None
    start = _parse_iso_ms(_str(e, "date"))
    if start is None:
        return None
    st = _child(_child(e, "fullStatus"), "type")
    state = _str(st, "state") or _str(e, "status") or "pre"
    sides = _arr(e, "competitors")
    lname, lteams = _league(league)
    teams = lteams and len(sides) == 2 and all(_str(s, "type") != "athlete" for s in sides)
    if not teams:
        title = _str(e, "name")
        if title is None:
            return None
        return Game(id_, league, league_name or lname, title, "", None, None, start, state, None, _str(st, "shortDetail"),
                    home_names=_oset(x for x in (title, _str(e, "shortName")) if x is not None))
    h = next((s for s in sides if _str(s, "homeAway") == "home"), sides[0])
    a = next((s for s in sides if _str(s, "homeAway") == "away"), sides[1])

    def names(c):
        return _oset(x for x in (_str(c, "displayName"), _str(c, "shortDisplayName"), _str(c, "name"), _str(c, "location"))
                     if x is not None)

    score = None
    if state != "pre":
        sc = [x for x in ((_str(h, "score") or "").strip() and _str(h, "score"),
                          (_str(a, "score") or "").strip() and _str(a, "score")) if x]
        score = " – ".join(sc) if len(sc) == 2 else None
    hn, an = _str(h, "displayName"), _str(a, "displayName")
    if hn is None or an is None:
        return None
    return Game(id_, league, league_name or lname, hn, an, _str(h, "logo"), _str(a, "logo"), start, state, score,
                _str(st, "shortDetail"), home_names=names(h), away_names=names(a))


def headline(http, now_ms, cache):
    """SportsRepository.headline: ESPN's headline games across every sport today (one request, kept 2 minutes)."""
    c = cache.headline
    if c and now_ms - c[0] < cache.HEADLINE_TTL:
        return c[1]
    ok, _, body = http.json("https://site.web.api.espn.com/apis/v2/scoreboard/header?lang=en&region=gb")
    if not ok or body is None:
        return c[1] if c else []
    out = []
    for sp in _arr(body, "sports"):
        sport = _str(sp, "slug")
        if sport is None:
            continue
        for lg in _arr(sp, "leagues"):
            slug = _str(lg, "slug")
            if slug is None:
                continue
            path = sport + "/" + slug
            for e in _arr(lg, "events"):
                g = _headline_game(e, path, _str(lg, "name") or _str(lg, "abbreviation"))
                if g:
                    out.append(g)
    cache.headline = (now_ms, out)
    return out


def _game(e, league):
    """SportsRepository.game: one ESPN scoreboard / schedule event."""
    id_ = _str(e, "id")
    if id_ is None:
        return None
    comps = _arr(e, "competitions")
    comp = comps[0] if comps else None
    start = _parse_iso_ms(_str(e, "date"))
    if start is None:
        return None
    sides = _arr(comp, "competitors")

    def side(which):
        for s in sides:
            if _str(s, "homeAway") == which:
                return s
        i = 0 if which == "home" else 1
        return sides[i] if i < len(sides) else None

    status = _child(_child(comp, "status"), "type")
    if status is None:
        status = _child(_child(e, "status"), "type")
    lname, lteams = _league(league)
    league_name = _str(_child(e, "league"), "name") or lname
    if not lteams:
        title = _str(e, "name")
        if title is None:
            return None
        return Game(id_, league, league_name, title, "", None, None, start, _str(status, "state") or "pre", None,
                    _str(status, "shortDetail"), home_names=_oset(x for x in (title, _str(e, "shortName")) if x is not None))
    h, a = side("home"), side("away")

    def name(c):
        return _str(_child(c, "team"), "displayName") or _str(_child(c, "athlete"), "displayName")

    def names(c):
        t = _child(c, "team")
        n = [x for x in (_str(t, "displayName"), _str(t, "shortDisplayName"), _str(t, "name"), _str(t, "location"))
             if x is not None]
        if not n:
            n = [x for x in (_str(_child(c, "athlete"), "displayName"),) if x is not None]
        return _oset(n)

    def score(c):
        v = _child(c, "score")
        if v is not None and not isinstance(v, (dict, list)):
            return str(v)
        return _str(v, "displayValue")

    def logo(c):
        t = _child(c, "team")
        ls = _arr(t, "logos")
        return (_str(ls[0], "href") if ls else None) or _str(t, "logo")

    state = _str(status, "state") or "pre"
    sc = None
    if state != "pre":
        s2 = [x for x in (score(h), score(a)) if x is not None]
        sc = " – ".join(s2) if len(s2) == 2 else None
    home = name(h) or _str(e, "name")
    if home is None:
        return None
    return Game(id_, league, league_name, home, name(a) or "", logo(h), logo(a), start, state, sc,
                _str(status, "shortDetail"), home_names=names(h), away_names=names(a))


def _cups_for(league):
    """SportsRepository.cupsFor: a football league's teams' other competitions."""
    if not league.startswith("soccer/"):
        return []
    europe = ["soccer/uefa.champions", "soccer/uefa.europa", "soccer/uefa.europa.conf"]
    country = league[len("soccer/"):].split(".", 1)[0]
    cups = {"sco": ["soccer/sco.tennents", "soccer/sco.cis", "soccer/sco.challenge"],
            "eng": ["soccer/eng.fa", "soccer/eng.league_cup"], "esp": ["soccer/esp.copa_del_rey"],
            "ger": ["soccer/ger.dfb_pokal"], "ita": ["soccer/ita.coppa_italia"],
            "fra": ["soccer/fra.coupe_de_france"]}.get(country, [])
    return [] if league in europe else cups + europe


def _team_games(http, f, now_ms, pool, cache):
    """SportsRepository.teamGames: fixtures + results in its league, fixtures in its cups and Europe."""
    cups = [c for c in _cups_for(f["league"])
            if (lambda t: t is None or now_ms - t > 24 * H)(cache.not_in.get("%s|%s" % (f["id"], c)))]
    asks = [("%s/%s/teams/%s/schedule?fixture=true" % (_BASE, f["league"], f["id"]), f["league"]),
            ("%s/%s/teams/%s/schedule" % (_BASE, f["league"], f["id"]), f["league"])] + \
           [("%s/%s/teams/%s/schedule?fixture=true" % (_BASE, c, f["id"]), c) for c in cups]

    def one(ask):
        u, c = ask
        ok, code, body = http.json(u)
        games = [g for g in (_game(e, c) for e in _arr(body, "events")) if g] if ok else []
        if c != f["league"] and code and ((ok and not games) or 400 <= code <= 499):
            cache.not_in["%s|%s" % (f["id"], c)] = now_ms
        return games

    out, seen = [], set()
    for gs in pool.map(one, asks):
        for g in gs:
            if g.id not in seen:
                seen.add(g.id)
                out.append(g)
    return sorted(out, key=lambda g: g.start)


def _league_games(http, league, now_ms, pool, horizon_days):
    """SportsRepository.leagueGames: the plain scoreboard, then match days from its calendar. (Only days up to
    [horizon_days] ahead are fetched: compute() reports 3 days, the app's 35-day look-ahead isn't needed.)"""
    today = datetime.fromtimestamp(now_ms / 1000, _LONDON).date()

    def board(q):
        ok, _, body = http.json("%s/%s/scoreboard%s" % (_BASE, league, q))
        return body if ok else None

    plain = board("")

    def day(s):
        try:
            return date.fromisoformat(s[:10]) if s else None
        except Exception:
            return None

    lgs = _arr(plain, "leagues")
    cal = _arr(lgs[0], "calendar") if lgs else []
    days = set()
    horizon = today + timedelta(days=35)
    if cal and not isinstance(cal[0], (dict, list)):
        ds = [d for d in (day(str(x)) for x in cal) if d and today <= d <= horizon]
        days.update(ds[:5])
    else:
        rounds = []
        for it in cal:
            ent = _arr(it, "entries")
            rounds.extend(ent if ent else [it])
        for r in rounds:
            st = day(_str(r, "startDate"))
            if st is None:
                continue
            en = day(_str(r, "endDate")) or st + timedelta(days=3)
            if en < today or st > horizon:
                continue
            d = today if st < today else st
            last = min(en, d + timedelta(days=27))
            while d <= last and len(days) < 28:
                days.add(d)
                d += timedelta(days=1)
        if not days:
            days.update(today + timedelta(days=i) for i in range(14))
    want = sorted(d for d in days if d <= today + timedelta(days=horizon_days))
    boards = list(pool.map(lambda d: board("?dates=" + d.strftime("%Y%m%d")), want))
    out, seen = [], set()
    for b in [plain] + boards:
        for e in _arr(b, "events"):
            g = _game(e, league)
            if g and g.id not in seen:
                seen.add(g.id)
                out.append(g)
    return sorted(out, key=lambda g: g.start)


def games_ttl(f, games, now_ms, cache):
    """How long a follow's cached games stay good, judged now (not when fetched, so a game drawing near makes an
    old copy stale): short while something is live, starts within 3 hours (or should have started) or was live in
    the last half hour; else long."""
    if not games:
        return cache.EMPTY_TTL
    if now_ms - cache.live_seen.get(f["sid"], 0) < cache.HOT_AFTER:
        return cache.GAMES_TTL
    for g in games:
        if g.state == "in" or (g.state == "pre" and g.start <= now_ms + cache.HOT_AHEAD):
            return cache.GAMES_TTL
    return cache.TEAM_TTL if f["kind"] == "Team" else cache.LEAGUE_TTL


def follow_games(http, f, now_ms, pool, cache):
    """SportsRepository.games: a follow's games, cached for games_ttl()."""
    c = cache.games.get(f["sid"])
    if c and now_ms - c[0] < games_ttl(f, c[1], now_ms, cache):
        return c[1]
    gs = _team_games(http, f, now_ms, pool, cache) if f["kind"] == "Team" else _league_games(http, f["league"], now_ms, pool, 4)
    if any(g.state == "in" for g in gs):
        cache.live_seen[f["sid"]] = now_ms
    if gs or not c:
        cache.games[f["sid"]] = (now_ms, gs)
    else:
        # (Nothing came back but there was a copy: keep it, and ask again in a quarter of an hour.)
        cache.games[f["sid"]] = (now_ms - games_ttl(f, c[1], now_ms, cache) + cache.EMPTY_TTL, c[1])
        return c[1]
    return gs


# ---------------------------------------------------------------------------------------------------------------
# SkySchedule.kt

_SKY_BASE = "https://awk.epgsky.com/hawk/linear/schedule"
_SKY_CHANNELS = [
    ("4002", "Sky Sports Main Event"), ("4010", "Sky Sports Premier League"), ("3939", "Sky Sports Football"),
    ("3940", "Sky Sports+"), ("4022", "Sky Sports Action"), ("4090", "Sky Sports Mix"), ("4081", "Sky Sports Cricket"),
    ("4026", "Sky Sports Golf"), ("3835", "Sky Sports F1"), ("1284", "Sky Sports Tennis"), ("4032", "Sky Sports Racing"),
    ("1035", "Sky Sports Box Office"),
    ("3625", "TNT Sports 1"), ("3627", "TNT Sports 2"), ("3629", "TNT Sports 3"), ("4040", "TNT Sports 4"),
    ("1336", "TNT Sports Ultimate"), ("1210", "TNT Sports Box Office"),
    ("1289", "Premier Sports 1"), ("1290", "Premier Sports 2"),
    ("3000", "BBC One"), ("2082", "BBC One"), ("2075", "BBC Two"), ("6504", "ITV1"), ("4075", "Channel 4"),
    ("4058", "Channel 5"), ("2296", "S4C"), ("3021", "BBC Alba"), ("1003", "MUTV"), ("3845", "LFCTV"),
]
_SKY_NAME = dict(_SKY_CHANNELS)
_SKY_LIVE = re.compile(r"^live\b", re.I)
_SKY_REPLAY = re.compile(r"\b(hlts|highlights|replay|rewind|classic|greatest|preview|review|reaction|best of)\b", re.I)


def _sky_day(http, d, now_ms, cache):
    """SkySchedule.day: one day's listings for the Sky ids (≤ 20 a request), kept 6 hours."""
    with cache.lock:
        c = cache.sky_days.get(d)
        if c and now_ms - c[0] < cache.SKY_TTL:
            return c[1]
        if now_ms < cache.sky_rest:
            return c[1] if c else None
        out = []
        ids = [k for k, _ in _SKY_CHANNELS]
        for i in range(0, len(ids), 20):
            batch = ids[i:i + 20]
            ok, _, body = http.json("%s/%s/%s" % (_SKY_BASE, d.strftime("%Y%m%d"), ",".join(batch)),
                                    {"X-SkyOTT-Territory": "GB"})
            if not ok or body is None:
                cache.sky_rest = now_ms + 10 * MIN
                return c[1] if c else None
            for s in _arr(body, "schedule"):
                name = _SKY_NAME.get(_str(s, "sid"))
                if name is None:
                    continue
                for e in _arr(s, "events"):
                    st = _long(_str(e, "st"))
                    if st is None:
                        continue
                    dur = _long(_str(e, "d")) or 0
                    t = _str(e, "t") or ""
                    out.append({"channel": name, "title": t, "start": st * 1000, "end": (st + dur) * 1000,
                                "match": " " + norm(t) + " "})
        for k in [k for k in cache.sky_days if k < d - timedelta(days=2)]:
            del cache.sky_days[k]
        cache.sky_days[d] = (now_ms, out)
        return out


def sky_find(http, g, now_ms, cache):
    """SkySchedule.find: None (out of range / no answer), ([], "", 0) (in range, not there), or (channels, title, start)."""
    kick = datetime.fromtimestamp(g.start / 1000, timezone.utc).date()
    today = datetime.fromtimestamp(now_ms / 1000, timezone.utc).date()
    if kick < today or kick > today + timedelta(days=7):
        return None
    lists = [_sky_day(http, kick, now_ms, cache), _sky_day(http, kick - timedelta(days=1), now_ms, cache) if kick > today else []]
    shows = [s for l in lists if l is not None for s in l]
    if not shows:
        return None
    hits = [s for s in shows if g.start - 3 * H <= s["start"] <= g.start + H and s["end"] > g.start
            and not _SKY_REPLAY.search(s["title"]) and norm_text_is_game(s["match"], g) == SURE]
    if not hits:
        return [], "", 0
    best = sorted(hits, key=lambda s: (not _SKY_LIVE.search(s["title"]), abs(s["start"] - g.start)))
    top = best[0]
    return _oset(s["channel"] for s in best if s["title"] == top["title"]), top["title"], top["start"]


# ---------------------------------------------------------------------------------------------------------------
# GuideViewModel.kt: ukMatch, blackout

def _blackout(g):
    """GuideViewModel.blackout: an English / Scottish league game at 14:45–15:15 UK time on a Saturday."""
    l = g.league
    lg = (l.startswith("soccer/eng.") and l[11:12].isdigit()) or (l.startswith("soccer/sco.") and l[11:12].isdigit())
    if not lg:
        return False
    t = datetime.fromtimestamp(g.start / 1000, _LONDON)
    mins = t.hour * 60 + t.minute
    return t.weekday() == 5 and 14 * 60 + 45 <= mins <= 15 * 60 + 15


def uk_match(http, g, now_ms, uk_listings, cache):
    """GuideViewModel.ukMatch: Sky's guide, then the UK Sport on TV plugin's listings, then the 3pm blackout.
    (TheSportsDB, the app's last resort, is skipped.) Returns (channels, sureness, listing, source) or None."""
    try:
        sky = sky_find(http, g, now_ms, cache)
    except Exception:
        sky = None
    if sky is not None and sky[0]:
        return sky[0], SURE, "%s (%s)" % (sky[1], _hhmm(sky[2])), "Sky TV guide"
    if uk_listings is not None:
        best = None
        for m in uk_listings:
            if abs(m[0] - g.start) > 30 * MIN:
                continue
            s = teams_are_game(m[1], m[2], g)
            if s != NO and (best is None or s > best[1]):
                best = (m, s)
        if best is not None:
            return best[0][3], best[1], "%s v %s" % (best[0][1], best[0][2]), "UK TV listings"
        if g.league.startswith("soccer/") and any(m[0] >= g.start for m in uk_listings):
            return [NOT_ON_UK_TV], SURE, None, "UK TV listings"
    if sky is not None and _blackout(g):
        return [NOT_ON_UK_TV], SURE, None, "Sky TV guide"
    return None


# The UK Sport on TV plugin's own fetch + parse (its plugin.py: SOURCE, DATE_RE, FIXTURE_RE, _field, _day, parse), so
# this plugin needs no other: televised UK football from live-footballontv.com, weeks ahead.
UK_SOURCE = "https://www.live-footballontv.com/"
_UK_DATE_RE = re.compile(r'<div class="fixture-date"[^>]*>(.*?)<', re.S)
_UK_FIXTURE_RE = re.compile(r'<div class="fixture">(.*?)</div></div></div>', re.S)


def _uk_field(block, name):
    m = re.search(r'fixture__%s">(.*?)<' % name, block, re.S)
    return _html.unescape(m.group(1)).replace("\xa0", " ").strip() if m else ""


def _uk_day(text):
    # "Saturday 21st November 2026"
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    try:
        return datetime.strptime("%s %s %s" % (m.group(1), m.group(2), m.group(3)), "%d %B %Y").date()
    except ValueError:
        return None


def parse_uk_listings(page, days_ahead=30, now_s=None):
    """uk_sport_on_tv parse(): [[start ms (UTC), home, away, competition, [channels]], ...] from today to [days_ahead]."""
    out = []
    today = datetime.fromtimestamp(now_s if now_s is not None else _time.time(), _LONDON).date()
    last = today + timedelta(days=days_ahead)
    starts = [(m.start(), m.group(1)) for m in _UK_DATE_RE.finditer(page)]
    for i, (pos, label) in enumerate(starts):
        day = _uk_day(_html.unescape(label))
        if day is None or day < today or day > last:
            continue
        chunk = page[pos: starts[i + 1][0] if i + 1 < len(starts) else len(page)]
        for f in _UK_FIXTURE_RE.findall(chunk):
            teams = re.sub(r"\s*\(.*?\)\s*", " ", _uk_field(f, "teams")).strip()
            if " v " not in teams:
                continue
            home, away = [t.strip() for t in teams.split(" v ", 1)]
            hm = re.match(r"(\d{1,2}):(\d{2})", _uk_field(f, "time"))
            if not hm:
                continue
            local = datetime(day.year, day.month, day.day, int(hm.group(1)), int(hm.group(2)), tzinfo=_LONDON)
            channels = [c for c in (_html.unescape(c).strip() for c in re.findall(r"channel-pill[^>]*>(.*?)<", f)) if c]
            if channels:
                out.append([int(local.timestamp() * 1000), home, away, _uk_field(f, "competition"), channels])
    return out


def fetch_uk_listings(http_get=None, days_ahead=30, now_s=None, cache=None, log=None):
    """The UK listings, fetched at most every 6 hours (kept in [cache]); after a 429 / 503 left alone an hour; a failed
    or empty fetch keeps the last good copy, else gives []. Never raises."""
    cache = cache or DEFAULT_CACHE
    now = int((now_s if now_s is not None else _time.time()) * 1000)
    c = cache.uk
    if c and now - c[0] < cache.UK_TTL:
        return c[1]
    if now < cache.uk_rest:
        return c[1] if c else []
    try:
        code, page = (http_get or _default_http_get)(UK_SOURCE, {"Accept": "text/html"})
    except Exception:
        code, page = 0, ""
    if code in (429, 503):
        cache.uk_rest = now + H
    if not (200 <= (code or 0) < 300):
        if log:
            log("sports: UK listings fetch failed (%s)" % code)
        return c[1] if c else []
    try:
        matches = parse_uk_listings(page, days_ahead, now / 1000)
    except Exception:
        matches = []
    if not matches:  # (the page changed or came back empty: keep what we had)
        if log:
            log("sports: UK listings page had no matches")
        return c[1] if c else []
    cache.uk = (now, matches)
    return matches


# Separate fetchers for the caller (each keeps its answers in [cache] between rounds).

def fetch_headline(http_get=None, now_s=None, cache=None, log=None):
    """ESPN's headline games (kept 2 minutes)."""
    now = int((now_s if now_s is not None else _time.time()) * 1000)
    return headline(_Http(http_get, log), now, cache or DEFAULT_CACHE)


def fetch_follow_games(follow, http_get=None, now_s=None, cache=None, log=None):
    """A follow's ESPN games (kept for games_ttl()). [follow]: one of decode_follows()'s dicts."""
    now = int((now_s if now_s is not None else _time.time()) * 1000)
    with ThreadPoolExecutor(max_workers=6) as pool:
        return follow_games(_Http(http_get, log), follow, now, pool, cache or DEFAULT_CACHE)


def fetch_sky_day(day, http_get=None, now_s=None, cache=None, log=None):
    """Sky's listings for one UTC date (kept 6 hours): [{"channel", "title", "start" ms, "end" ms, "match"}] or None."""
    now = int((now_s if now_s is not None else _time.time()) * 1000)
    return _sky_day(_Http(http_get, log), day, now, cache or DEFAULT_CACHE)


def _uk_listings(raw):
    """GuideViewModel.ukListings: the plugin's [[start ms, home, away, competition, [channels]], ...]."""
    if raw is None:
        return None
    try:
        lst = json.loads(raw) if isinstance(raw, str) else raw
        out = []
        for a in lst:
            try:
                out.append((int(a[0]), str(a[1]), str(a[2]), [str(c) for c in a[4]]))
            except Exception:
                continue
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------------------------
# GamePlacer.kt (normChannel, byName, eventChannel, place) and ClubChannels.kt

_GP_NON_ALNUM = re.compile(r"[^a-z0-9]")
_TBC = re.compile(r"\bTBC\b", re.I)
_FOUR_K = re.compile(r"(?i)\b(ultra\s*hdr|uhd|4k)\b")
_GP_EVENT_SLOT = re.compile(r"\b\d{1,3}\s*:")
_CH_PREFIX = re.compile(r"^[a-z]{2,3}\s*[|:\-]\s*")
_CH_QUALITY = re.compile(r"\b(fhd|uhd|hd|sd|hevc|h265)\b")


def norm_channel(name):
    """GamePlacer.normChannel."""
    s = _CH_PREFIX.sub("", name.lower(), count=1)
    s = s.replace("+1", " plus1 ").replace("+", " plus ")
    s = _CH_QUALITY.sub(" ", s)
    return _GP_NON_ALNUM.sub("", s)


_CC_GROUP = re.compile(r"(?i)\b(efl|epl|spfl|scottish|football|soccer|club|sport|sports|nfl|nba|nhl|mlb|mls|rugby|hockey"
                       r"|basketball|baseball|serie a|laliga|la liga|ligue|bundesliga|eredivisie|afl|nrl|cricket)")
_CC_NOT_TEAM = re.compile(r"(?i)\b(sky|tnt|premier sports|bbc|itv|stv|amazon|prime|dazn|bt sport|s4c|alba|espn|fox|nbc|cbs"
                          r"|abc|peacock|paramount|network|redzone|red zone|zone|channel|league|cup|news|highlights|replay"
                          r"|pass|ticket|event|events|ppv|multi)\b")
_CC_EVENT_SLOT = re.compile(r"\d{1,3}\s*:")
_CC_PREFIX = re.compile(r"^((uk|us|ca|au|ie)\s*\|?\s+)?((spfl|epl|efl|nifl|wsl|swpl|nfl|nba|nhl|mlb|mls|wnba|afl|nrl"
                        r"|serie a|laliga|la liga|ligue 1|bundesliga|eredivisie)\s*[-|:]?\s*)?(\d+\s+)?")
_CC_DROP = {"tv", "official", "hd", "fhd", "uhd", "sd", "live"}
_CC_SUFFIX = {"united", "city", "town", "albion", "and", "hove", "athletic", "rovers", "wanderers", "county", "hotspur",
              "argyle", "alexandra", "stanley"}
_CC_NICK = {"lfc": "liverpool", "mufc": "manchester united", "mcfc": "manchester city", "utd": "united", "st": "saint"}
_CC_SPORT_WORDS = [(re.compile(r"\b(" + "|".join(ws) + r")\b"), sp) for ws, sp in [
    (["nfl", "american football", "ncaaf", "cfl"], "football"),
    (["nba", "wnba", "basketball", "ncaab"], "basketball"),
    (["nhl", "hockey"], "hockey"),
    (["mlb", "baseball"], "baseball"),
    (["rugby", "nrl", "super league"], "rugby"),
    (["afl", "aussie rules"], "australian-football"),
    (["cricket"], "cricket"),
    (["efl", "epl", "spfl", "nifl", "wsl", "swpl", "soccer", "football", "club", "scottish", "serie a", "laliga", "la liga",
      "ligue", "bundesliga", "eredivisie", "mls"], "soccer"),
]]
_INTERNATIONAL = ("uefa.", "fifa.", "concacaf.", "conmebol.", "afc.", "caf.")


def _cc_key(name):
    """ClubChannels.key: a channel's team name, normalised ("SPFL - HIBS FC" -> "hibs")."""
    s = name.lower().replace("&", " and ").replace(".", " ").strip()
    for _ in range(2):
        s = _CC_PREFIX.sub("", s, count=1).strip()
    words = []
    for w in norm(s).split(" "):
        if w.strip() and w not in _CC_DROP:
            words.extend(_CC_NICK.get(w, w).split(" "))
    return " ".join(words)


def _cc_sport(text):
    t = text.lower()
    for rx, sp in _CC_SPORT_WORDS:
        if rx.search(t):
            return sp
    return None


def club_index(channels):
    """ClubChannels.index: team channels by sport, then their team's name (lowest number first)."""
    by_sport = {}
    for c in channels:
        g = c.get("group") or ""
        if not _CC_GROUP.search(g) or _CC_EVENT_SLOT.search(c["name"]) or _CC_NOT_TEAM.search(c["name"]):
            continue
        sp = _cc_sport(c["name"] + " " + g)
        if sp:
            by_sport.setdefault(sp, []).append(c)
    out = {}
    for sp, lst in by_sport.items():
        idx = {}
        for c in sorted(lst, key=lambda c: c["number"] if c.get("number") is not None else float("inf")):
            k = _cc_key(c["name"])
            if len(k) >= 3 and k not in idx:
                idx[k] = c
        out[sp] = idx
    return out


def _cc_find(index, names):
    """ClubChannels.find."""
    al = aliases(names)
    for a in al:
        if a in index:
            return index[a]
    for a in al:
        k = " ".join(_CC_NICK.get(w, w) for w in a.split(" "))
        if k in index:
            return index[k]
    if not al:
        return None
    full = al[0]
    for a in al:
        if len(a) > len(full):
            full = a
    best = None
    for ck, c in index.items():
        if full.startswith(ck + " ") and all(w in _CC_SUFFIX for w in full[len(ck) + 1:].split(" ")):
            if best is None or len(ck) > len(best[0]):
                best = (ck, c)
    return best[1] if best else None


def _cc_covers(g):
    if g.is_event:
        return False
    l = g.league.split("/", 1)[-1]
    if l.startswith(_INTERNATIONAL) or ".w." in l or l.startswith("womens-"):
        return False
    return "women" not in g.home.lower() and "women" not in g.away.lower()


def club_for_game(index, g):
    """ClubChannels.forGame: the home team's own channel, else the away team's."""
    if not _cc_covers(g) or g.state == "post":
        return None
    sp = index.get(g.league.split("/", 1)[0])
    if not sp:
        return None
    return _cc_find(sp, g.home_names or [g.home]) or _cc_find(sp, g.away_names or [g.away])


class Placer:
    """GamePlacer: channel names normalised once per channel list."""

    def __init__(self, channels):
        self.channels = channels
        self.by_norm = [(norm_channel(c["name"]), c) for c in channels]
        self.exact = {}
        for n, c in self.by_norm:  # (Kotlin's toMap(): the last one of a name wins)
            self.exact[n] = c
        self.clubs = club_index(channels)
        self.match_names = [" " + norm(c["name"]) + " " for c in channels]

    def by_name(self, name):
        """GamePlacer.byName."""
        if _TBC.search(name):
            return None
        n = norm_channel(name)
        if _FOUR_K.search(name):
            return self.exact.get(n)
        if len(n) < 3:
            return None
        if n in self.exact:
            return self.exact[n]
        best = None
        for cn, c in self.by_norm:
            if n in cn and "plus1" not in cn and (n + "plus") not in cn and not _GP_EVENT_SLOT.search(c["name"]):
                if best is None or len(cn) < len(best[0]):
                    best = (cn, c)
        if best:
            return best[1]
        return self.exact.get(n + "plus")

    def event_channel(self, g, now_ms):
        """GamePlacer.eventChannel: a channel named after the match, for a game on now or within 6 hours."""
        if g.state == "post" or g.start - now_ms > 6 * H:
            return None
        for i, t in enumerate(self.match_names):
            if norm_text_is_game(t, g) == SURE:
                return self.channels[i]
        return None

    def place(self, g, ch, tv, m, now_ms):
        """GamePlacer.place -> (channel or None, sure 0..2, why, src, other channels named)."""
        raw = tv or []
        named = [n for n in raw if n != NOT_ON_UK_TV]
        not_on_tv = ch is None and NOT_ON_UK_TV in raw
        if ch is not None:
            c, p = ch
            return c, SURE, 'From your TV guide: "%s" on %s, %s–%s' % (
                p["title"], c["name"], _hhmm(p["start"]), _hhmm(p["end"])), "guide", []
        listed = None
        for n in named:
            c = self.by_name(n)
            if c is not None:
                listed = (n, c)
                break
        if listed is not None:
            said, c = listed
            sure = m is None or m[1] == SURE
            src = m[3] if m else "UK TV listings"
            why = "%s: %s" % (src, said)
            if m and m[3] == "Sky TV guide" and m[2] is not None:
                why += ', "%s"' % m[2]
            if said.lower() != c["name"].lower():
                why += " (your channel %s)" % c["name"]
            if not sure:
                why += '. Possible match only: the listing is "%s"' % (m[2] if m else None)
            others = [x for x in (self.by_name(n) for n in named if n != said) if x is not None]
            return c, (SURE if sure else POSSIBLE), why, ("sky" if src == "Sky TV guide" else "plugin"), others
        c = self.event_channel(g, now_ms)
        if c is not None:
            return c, SURE, "An event channel named after the match: %s" % c["name"], "event", []
        c = club_for_game(self.clubs, g)
        if c is not None:
            if not_on_tv:
                pre = "Not on UK TV (the UK listings don't have it). "
            elif named:
                pre = "Listed on %s, not one of your channels. " % ", ".join(named)
            else:
                pre = "Not in your guide or the UK listings. "
            word = "Club" if g.league.startswith("soccer/") else "Team"
            return c, SURE, pre + "%s channel: %s" % (word, c["name"]), "club", []
        if g.state == "post":
            why = ""
        elif not_on_tv:
            why = "Not on UK TV (a Saturday 3pm game, or not in the UK listings for that day)"
        elif named:
            why = "Listed on %s: not one of your channels" % ", ".join(named)
        else:
            why = "Not in your TV guide or the UK listings yet"
        return None, NO, why, None, []


# ---------------------------------------------------------------------------------------------------------------
# GuideViewModel.kt: channelForGame (over the programmes handed in)

class _GuideIndex:
    """The app's searchProgramsEnding over the caller's programmes: titles containing a word (case-insensitive), ending
    in a window, soonest first, at most 60 (the app's SQL LIMIT)."""

    def __init__(self, progs, chan_by_id):
        self.chan = chan_by_id
        self.by_title = {}
        for p in progs:
            self.by_title.setdefault(p["title"].lower(), []).append(p)
        self.titles = list(self.by_title)
        self._word = {}

    def search_ending(self, w, end_from, end_to):
        ts = self._word.get(w)
        if ts is None:
            ts = [t for t in self.titles if w in t]
            self._word[w] = ts
        hits = [p for t in ts for p in self.by_title[t] if end_from <= p["end"] <= end_to]

        def k(p):
            n = (self.chan.get(p["channel_id"]) or {}).get("number")
            return p["start"], n is None, n if n is not None else 0
        hits.sort(key=k)
        return hits[:60]


def channel_for_game(gi, g):
    """GuideViewModel.channelForGame: a programme naming both sides, starting 3 h before to 1 h after kick-off."""
    found, seen = [], set()
    for w in search_words(g):
        for p in gi.search_ending(w, g.start - 3 * H, g.start + 7 * H):
            k = (p["channel_id"], p["start"])
            if k in seen:
                continue
            seen.add(k)
            c = gi.chan.get(p["channel_id"])
            if c is not None:
                found.append((c, p))
    best = None
    for c, p in found:
        if g.start - 3 * H <= p["start"] <= g.start + H and \
                text_is_game(p["title"] + " " + (p.get("sub_title") or ""), g) == SURE:
            if best is None or abs(p["start"] - g.start) < abs(best[1]["start"] - g.start):
                best = (c, p)
    return best


# ---------------------------------------------------------------------------------------------------------------
# GuideViewModel.kt: Live now (liveGroupOn, fillerFor, realSlot, saysLive, liveEventsNow)

_QUALITY_MARK = re.compile(r"(?i)\((?:[^)]*\b)?(uhd|4k|fhd|hd|sd|hevc|raw)\b[^)]*\)|\b(uhd|fhd)\b|\|")
_LIVE_MARK = re.compile(r"(?i)\blive\b|ᴸᶦᵛᵉ|🔴")
_LIVE_START = re.compile(r"(?i)^\W*live\b|\blive (coverage|action|from)\b")
_DATE_SUFFIX = re.compile(r"\s+\d{1,2}-\d{1,2}$")
_EVENT_FEED = re.compile(r"\d{1,3}\s*:|\bevents?\s*\d+|\bppv\s*\d+", re.I)
EVENT_WORDS = re.compile(r"\b(sports?|events?|ppv|pay per view|box office|football|soccer|rugby|ufc|boxing|wwe|nfl|nba|nhl"
                         r"|mlb|f1|motogp|cricket|golf|tennis|darts|racing|premier league|efl|epl|spfl|uefa|matchroom"
                         r"|dazn)\b", re.I)


def live_group_on(name, plugin_groups):
    """GuideViewModel.liveGroupOn, untouched by the user: the plugin's groups and sporty-named ones."""
    k = name.strip().lower()
    return k in plugin_groups or bool(EVENT_WORDS.search(name))


def _bare(t):
    """GuideViewModel.bare: lower case, no date ending, letters and digits only."""
    t = _DATE_SUFFIX.sub("", t.lower())
    return "".join(c for c in t if c.isalpha() or c in "0123456789")


def _filler_for(channel_bare, p):
    """GuideViewModel.fillerFor: a guide entry that only repeats its channel's / stream's name."""
    t = p["title"].strip()
    if t.endswith(":") or _bare(t) == channel_bare:
        return True
    if _QUALITY_MARK.search(t):
        return True
    letters = "".join(c for c in t if c.isalpha())
    return len(letters) >= 4 and letters == letters.upper() and p["end"] - p["start"] >= 5 * H


def _real_slot(p):
    return 1 <= p["end"] - p["start"] <= 9 * H


def _says_live(p):
    return bool(p.get("is_live")) or bool(_LIVE_MARK.search(p["title"])) or \
        bool(p.get("sub_title") and _LIVE_MARK.search(p["sub_title"])) or \
        bool(p.get("description") and _LIVE_START.search(p["description"][:80]))


def live_events_now(now_ms, chans, progs, games, known):
    """GuideViewModel.liveEventsNow. [chans]: the Live now groups' channels; [progs]: their programmes now..+3 h;
    [games]: (Game, channel, guide programme or None) on now / within 3 h; [known]: every game known.
    Returns (on_now, soon): lists of entry dicts."""
    by_id = {c["id"]: c for c in chans}
    grouped = {}
    for p in sorted(progs, key=lambda p: p["start"]):
        if p["channel_id"] in by_id and p["end"] > now_ms - 1 and p["start"] < now_ms + 3 * H:
            grouped.setdefault(p["channel_id"], []).append(p)
    on_now, soon = [], []
    for cid, ps0 in grouped.items():
        c = by_id[cid]
        feed = bool(_EVENT_FEED.search(c["name"]))
        cb = _bare(c["name"])
        ps = [p for p in ps0 if clean(p["title"]) is not None and not _filler_for(cb, p) and _real_slot(p)
              and _says_live(p) and (feed or sport_of(p["title"]) is not None or p.get("is_live"))]
        x = next((p for p in ps if p["start"] <= now_ms < p["end"]), None)
        if x:
            on_now.append((c, x))
        x = next((p for p in ps if p["start"] > now_ms and (not feed or p["start"] - now_ms <= 30 * MIN)), None)
        if x:
            soon.append((c, x))

    def scheduled(p):
        if not known:
            return False
        t = " " + norm(p["title"]) + " "
        return any(norm_text_is_game(t, g) == SURE for g in known)

    def distinct(l):
        out, seen = [], set()
        for c, p in l:
            if scheduled(p):
                continue
            k = (clean(p["title"]) or p["title"]).strip().lower() + "|" + str(p["start"])
            if k not in seen:
                seen.add(k)
                out.append((c, p))
        return out

    now_pairs = distinct(sorted(on_now, key=lambda cp: -cp[1]["start"]))[:60]
    soon_pairs = distinct(sorted(soon, key=lambda cp: cp[1]["start"]))[:30]

    def from_schedule(l, is_soon):
        out, seen = [], set()
        for g, c, p in l:
            if g.id in seen:
                continue
            seen.add(g.id)
            st, en = (p["start"], p["end"]) if p else (g.start, g.start + 3 * H)
            title = "%s %s %s" % (g.home, g.score, g.away) if g.score is not None and not g.is_event else g.title
            out.append({"uuid": c["uuid"], "start": st, "end": en, "title": title,
                        "sport": sport_of("%s %s" % (g.league_name, g.league)), "group": c.get("group"),
                        "pic": g.home_logo, "game": g.id, "score": g.score, "soon": is_soon})
        return out

    def events(l, is_soon):
        out = []
        for c, p in l:
            title = clean(p["title"]) or p["title"]
            out.append({"uuid": c["uuid"], "start": p["start"], "end": p["end"], "title": title,
                        "sport": sport_of("%s %s %s" % (title, c["name"], c.get("group") or "")),
                        "group": c.get("group"), "pic": p.get("image"), "game": None, "score": None, "soon": is_soon})
        return out

    sched_now = from_schedule([x for x in games if x[0].state == "in"], False)
    sched_soon = from_schedule(sorted([x for x in games if x[0].state == "pre" and now_ms <= x[0].start <= now_ms + 3 * H],
                                      key=lambda x: x[0].start), True)
    return (sched_now + events(now_pairs, False))[:60], (sched_soon + events(soon_pairs, True))[:30]


# ---------------------------------------------------------------------------------------------------------------
# compute()

def _merge_windows(ws, gap):
    out = []
    for a, b in sorted(ws):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def compute(now_s, channels, programmes, followed, plugin_groups, http_get=None, log=None, uk_listings=None,
            cache=None):
    """Live now and where every followed game is on, as the app would work them out (see the module's docstring).

    uk_listings (optional): the UK football listings ([[start ms, home, away, competition, [channels]], ...] or that as
      JSON text), e.g. a copy of fetch_uk_listings() the caller keeps; None: fetched here (at most every 6 h via [cache]).
    cache (optional): a Cache kept between rounds (ESPN, Sky, UK listings); None: the module-wide one.
    """
    cache = cache or DEFAULT_CACHE
    t0 = _time.time()
    now = int(now_s) * 1000
    http = _Http(http_get, log)
    plugin_groups = {str(g).strip().lower() for g in (plugin_groups or ())}
    if uk_listings is None:
        uk_listings = fetch_uk_listings(http_get, now_s=now_s, cache=cache, log=log)
    ukl = _uk_listings(uk_listings) or None

    # Channels: in number order (the app's channel list), ms-free.
    chans = sorted((dict(c) for c in channels),
                   key=lambda c: (c.get("number") is None, c.get("number") or 0, c.get("id") or 0))
    for c in chans:
        c["name"] = c.get("name") or ""
    chan_by_id = {c["id"]: c for c in chans}

    # 1: games (SportsHub.load step 1 + headline for Live now).
    follows = decode_follows(followed)
    sids = {f["sid"] for f in follows}
    for sid in [k for k in cache.games if k not in sids]:   # (follows nobody has any more)
        cache.games.pop(sid, None)
        cache.live_seen.pop(sid, None)
    win_lo, win_hi = now - 3 * H, now + 3 * DAY
    known, shown_ids = {}, set()
    fol_games = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        def one(f):
            try:
                return f["sid"], follow_games(http, f, now, pool, cache)
            except Exception as e:
                if log:
                    log("sports: %s failed: %s" % (f["sid"], type(e).__name__))
                return f["sid"], []
        with ThreadPoolExecutor(max_workers=4) as outer:
            for sid, gs in outer.map(one, follows):
                fol_games[sid] = gs
        try:
            head = headline(http, now, cache)
        except Exception:
            head = []
    t_games = _time.time()
    order = []  # followed games first (shown lists, then the 3-day window), then headline extras
    for f in follows:
        all_ = fol_games.get(f["sid"], [])
        shown = [g for g in all_ if g.state == "in"] + [g for g in all_ if g.state == "pre" and g.start < now + 60 * DAY][:20] + \
            [g for g in all_ if g.state == "post"][-5:]
        window = [g for g in all_ if g.state == "in" or win_lo <= g.start <= win_hi]
        for g in shown + window:
            if g.id not in known:
                known[g.id] = g
                order.append(g)
            shown_ids.add(g.id)
    for g in head:
        if g.id not in known:
            known[g.id] = g
            order.append(g)
    report = [g for g in order if g.state == "in" or win_lo <= g.start <= win_hi]

    # 2: the programmes: around each game to place (one window per cluster of kick-offs) and Live now's next 3 hours.
    to_find = [g for g in report if g.state != "post" and g.start < now + 7 * DAY]
    live_chans = [c for c in chans if c.get("group") and live_group_on(c["group"], plugin_groups)]
    windows = _merge_windows([(g.start - 3 * H, g.start + 7 * H) for g in to_find], 6 * H)
    progs, seen = [], set()

    def take(ps):
        for p in ps or []:
            if "start_s" not in p and "start" in p:
                d = p  # (already in this shape, times in ms: the plugin's reader hands them so, saving a copy each)
            else:
                try:
                    d = {"channel_id": p["channel_id"], "title": p.get("title") or "", "sub_title": p.get("sub_title"),
                         "description": p.get("description"), "start": int(p["start_s"]) * 1000,
                         "end": int(p["end_s"]) * 1000, "is_live": bool(p.get("is_live")), "image": p.get("image")}
                except Exception:
                    continue
            k = (d["channel_id"], d["start"], d["title"])
            if k not in seen:
                seen.add(k)
                progs.append(d)

    all_ids = [c["id"] for c in chans]
    covered = False
    for a, b in windows:
        take(programmes(all_ids, a // 1000, b // 1000))
        if a <= now and b >= now + 3 * H:
            covered = True
    if not covered and live_chans:
        take(programmes([c["id"] for c in live_chans], now // 1000, (now + 3 * H) // 1000))
    t_progs = _time.time()

    # 3: where each game is on (GamePlacer.place, with the guide and the listings first).
    gi = _GuideIndex(progs, chan_by_id)
    placer = Placer(chans)
    games_out, live_games = {}, []

    def soon(g):
        return g.state == "in" or (g.state == "pre" and g.start < now + 3 * H)

    for g in report:
        ch = None
        if g.state != "post" and g.start < now + 7 * DAY:
            try:
                ch = channel_for_game(gi, g)
            except Exception:
                ch = None
        m = None
        if ch is None and g.state != "post" and g.start < now + 60 * DAY:
            try:
                m = uk_match(http, g, now, ukl, cache)
            except Exception:
                m = None
        c, sure, why, src, others = placer.place(g, ch, m[0] if m else None, m, now)
        uuids = _oset(([c["uuid"]] if c else []) + [o["uuid"] for o in others])
        games_out[g.id] = {"ch": uuids, "sure": sure, "why": why, "src": src, "start": g.start // 1000,
                           "title": g.title, "league": g.league_name, "state": g.state,
                           # (For "record all of a team's games" on the server: server.py matches its team rules.)
                           "home": g.home, "away": g.away,
                           # (For live score alerts, alerts.py: where to fetch this game's live score, and how it stood.)
                           "lp": g.league, "score": g.score, "detail": g.detail}
        if soon(g) and c is not None and sure == SURE:
            live_games.append((g, c, ch[1] if ch else None))

    t_place = _time.time()

    # 4: Live now (SportsHub.liveGames -> GuideViewModel.liveEventsNow).
    live_ids = {c["id"] for c in live_chans}
    on_now, soon_l = live_events_now(now, live_chans, [p for p in progs if p["channel_id"] in live_ids],
                                     live_games, list(known.values()))
    live = sorted(on_now, key=lambda e: e["start"]) + sorted(soon_l, key=lambda e: e["start"])
    for e in live:
        e["start"] //= 1000
        e["end"] //= 1000

    out = {"v": 1, "at": int(now_s), "live": live, "games": games_out}
    _prune(out, now // 1000)
    if log:
        log("sports: %d follows, %d games, %d live, %d programmes, %d requests, %.1fs (ESPN and UK listings %.1fs, "
            "programmes %.1fs, placing incl. Sky schedule %.1fs)" % (
                len(follows), len(games_out), len(live), len(progs), http.count, _time.time() - t0, t_games - t0,
                t_progs - t_games, t_place - t_progs))
    return out


def _prune(out, now_s, limit=100_000):
    """Keeps the JSON under ~100 KB: finished games first, then the least sure furthest ahead, then soon entries."""
    def size():
        return len(json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size() <= limit:
        return
    gs = out["games"]
    victims = sorted(gs, key=lambda k: (gs[k]["state"] != "post", gs[k]["sure"], -abs(gs[k]["start"] - now_s)))
    for k in victims:
        if size() <= limit:
            return
        del gs[k]
    while out["live"] and size() > limit:
        soon_idx = [i for i, e in enumerate(out["live"]) if e["soon"]]
        out["live"].pop(soon_idx[-1] if soon_idx else -1)
