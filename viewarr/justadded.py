"""What's just arrived on each streaming service in the UK, once a day, for Viewarr's "<service> · Just added" rows.

TMDB (and so AIOMetadata's "New" catalogues) knows which service has a title but not when it arrived, so an older film
newly added, or a new season of an existing show, never comes to the front there. JustWatch tracks the day each title
arrived on each service. This asks JustWatch's own web feed (the one its "What's new" pages use; there's no official
public API) for the last DAYS days, splits it by service, and leaves the lists in every user's app data
(viewarr_just_added), as the sports results are left (2026-11-20).

Light by design: one request per day looked back (all services together, a page of up to 100), once a day. Failures
wait for the next day (no retries).
"""
import json
import os
import time
import urllib.request
from datetime import timedelta


def _load_clock():
    """clock.py beside this file (the house's local time; Dispatcharr's own processes run in UTC)."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("viewarr_clock", os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = _load_clock()

KEY = "viewarr_just_added"
DAYS = 14
PER_SERVICE = 20
URL = ""  # the Just added address the admin added + /graphql (server.apply_sources); empty: not fetched

# Viewarr's service names (as its catalogue rows start) → JustWatch's UK package short names.
SERVICES = [
    ("Netflix", ["nfx", "nfa"]), ("Prime Video", ["amp", "pva"]), ("Disney+", ["dnp"]), ("Apple TV+", ["atp"]),
    ("HBO Max", ["mxx"]), ("NOW", ["ntv", "ntc"]), ("BBC iPlayer", ["bbc"]), ("ITVX", ["itv", "itp"]),
    ("Paramount+", ["pmp", "ppp"]), ("Sky Go", ["skg"]), ("UKTV Play", ["ukt"]), ("My5", ["myf"]),
    ("Discovery+", ["dpe"]), ("Crunchyroll", ["cru"]), ("Shudder", ["shd"]),
]
_BY_SHORT = {s: name for name, shorts in SERVICES for s in shorts}

QUERY = """
query GetNewTitles($country: Country!, $date: Date!, $language: Language!, $filter: TitleFilter, $first: Int!, $after: String) {
  newTitles(country: $country, date: $date, filter: $filter, first: $first, pageType: NEW, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      newOffer(platform: WEB) { package { shortName } }
      node {
        objectType
        ... on MovieOrShowOrSeason {
          content(country: $country, language: $language) { title originalReleaseYear posterUrl externalIds { imdbId tmdbId } }
        }
        ... on Season {
          content(country: $country, language: $language) { seasonNumber }
          show { content(country: $country, language: $language) { title originalReleaseYear posterUrl externalIds { imdbId tmdbId } } }
        }
      }
    }
  }
}"""


def _ask(day, after=None):
    body = json.dumps({"operationName": "GetNewTitles", "query": QUERY, "variables": {
        "country": "GB", "language": "en", "date": day, "first": 100, "after": after,
        "filter": {"packages": [s for _, shorts in SERVICES for s in shorts]},
    }}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Viewarr)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        out = json.load(r)
    if out.get("errors"):
        raise RuntimeError(str(out["errors"])[:200])
    return out["data"]["newTitles"]


def _poster(path):
    if not path:
        return None
    return "https://images.justwatch.com" + path.replace("{profile}", "s332").replace("{format}", "jpg")


def _item(edge, day):
    n = edge.get("node") or {}
    kind = n.get("objectType")
    c = n.get("content") or {}
    if kind == "SHOW_SEASON":
        show = (n.get("show") or {}).get("content") or {}
        ids = show.get("externalIds") or {}
        base, season = show, c.get("seasonNumber")
    elif kind in ("MOVIE", "SHOW"):
        ids = c.get("externalIds") or {}
        base, season = c, None
    else:
        return None
    tmdb = str(ids.get("tmdbId") or "").split(":")[0]
    if not tmdb.isdigit() or not base.get("title"):
        return None
    return {
        "t": "m" if kind == "MOVIE" else "s", "tmdb": int(tmdb), "imdb": ids.get("imdbId"), "title": base["title"],
        "year": base.get("originalReleaseYear"), "season": season, "added": day, "poster": _poster(base.get("posterUrl")),
    }


def build(log=None):
    """The lists: {service name: [items, newest first]} for the last DAYS days."""
    lists = {name: [] for name, _ in SERVICES}
    seen = {name: set() for name, _ in SERVICES}
    today = clock.local_today()
    for back in range(DAYS):
        day = (today - timedelta(days=back)).isoformat()
        after = None
        for _page in range(3):  # (a busy day: up to 300 arrivals across all services)
            page = _ask(day, after)
            for e in page.get("edges") or []:
                short = (((e.get("newOffer") or {}).get("package")) or {}).get("shortName")
                name = _BY_SHORT.get(short)
                it = _item(e, day) if name else None
                if not it:
                    continue
                # One card per film / show per service (its newest arrival: a new season beats an older one).
                k = (it["t"], it["tmdb"])
                if k in seen[name] or sum(1 for x in lists[name] if x["t"] == it["t"]) >= PER_SERVICE:
                    continue
                seen[name].add(k)
                lists[name].append(it)
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            after = info.get("endCursor")
        time.sleep(1.0)  # gentle
    return {name: items for name, items in lists.items() if items}


def run(data_dir, log=None):
    """Once a day (after 05:00): fetch, publish to every active user. Returns a short status, or None when not due."""
    os.makedirs(data_dir, exist_ok=True)
    mark = os.path.join(data_dir, "just_added_day.txt")
    today = clock.local_today().isoformat()
    try:
        if open(mark).read().strip() == today:
            return None
    except Exception:
        pass
    if clock.local_now().hour < 5:
        return None
    if not URL:
        return None  # (no Just added address added)
    # Tried for today whatever happens: a failure waits for tomorrow, never a retry every round.
    with open(mark, "w") as f:
        f.write(today)
    try:
        lists = build(log)
    except Exception as exc:
        if log:
            log("just added: JustWatch didn't answer (%s); trying again tomorrow" % str(exc)[:160])
        return {"error": str(exc)[:160]}
    # Films and shows apart (the Movies and Shows pages each take theirs), each service's newest PER_SERVICE.
    value = {"v": 1, "at": int(time.time()), "services": [
        {"name": name, "movies": [i for i in items if i["t"] == "m"][:PER_SERVICE], "shows": [i for i in items if i["t"] == "s"][:PER_SERVICE]}
        for name, items in lists.items()
    ]}
    # Under Dispatcharr's 128 KB per stored item: fewer per service if a busy fortnight would go over.
    keep = PER_SERVICE
    while keep > 5 and len(json.dumps(value, separators=(",", ":")).encode()) > 120_000:
        keep -= 3
        for sv in value["services"]:
            sv["movies"], sv["shows"] = sv["movies"][:keep], sv["shows"][:keep]
    from django.contrib.auth import get_user_model
    from apps.accounts.models import UserAppData
    data = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    size = len(data.encode("utf-8"))
    users = 0
    for user in get_user_model().objects.filter(is_active=True).only("id"):
        UserAppData.objects.update_or_create(user=user, key=KEY, defaults={"value": value, "byte_size": size})
        users += 1
    n = sum(len(s["movies"]) + len(s["shows"]) for s in value["services"])
    if log:
        log("just added: %d titles across %d services to %d users (%d KB)" % (n, len(value["services"]), users, size // 1024))
    return {"titles": n, "services": len(value["services"]), "bytes": size}


def clear(data_dir):
    """Just added switched off in the settings: every user's stored lists removed (no TV shows an old copy), and
    today's mark forgotten, so switching it back on fetches the same day."""
    from apps.accounts.models import UserAppData
    UserAppData.objects.filter(key=KEY).delete()
    try:
        os.remove(os.path.join(data_dir, "just_added_day.txt"))
    except OSError:
        pass
