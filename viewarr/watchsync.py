"""IPTV watch progress kept in step with Emby / Jellyfin / Plex, without Trakt.

Viewarr keeps each Dispatcharr user's watch progress for IPTV and addon films and episodes in their app data (key
``viewarr_vod_progress``: {"tmdb_movie_693134": {"pos": 2415, "dur": 9960, "status": "watching", "updated_at": s},
"tmdb_tv_94997_s02_e04": {"status": "watched", "updated_at": s}, …}). A user who switches on "Sync IPTV progress with my
Emby / Jellyfin" in Viewarr also leaves their server's address, user id and token in their own app data (key
``viewarr_watch_servers``: {"servers": [{"kind": "emby"|"jellyfin", "url", "user_id", "token", "name"}]}), readable only by
them and admins. Every few minutes, for each such user and server:

  * Emby / Jellyfin → Viewarr: films and episodes played or part played there (with a TMDB id) go into the Viewarr list
    when newer than what it has;
  * Viewarr → Emby / Jellyfin: what's newer in Viewarr is marked there (played, unplayed, or the resume point), found
    in the library by TMDB id (episodes by their show's TMDB id, season and number).

The newer change wins (5 s leeway between clocks), as Viewarr's own sync between TVs does.
"""

import calendar
import json
import time
import urllib.error
import urllib.parse
import urllib.request

PROGRESS_KEY = "viewarr_vod_progress"
SERVERS_KEY = "viewarr_watch_servers"
MAX_BYTES = 120_000           # Dispatcharr allows 128 KiB per stored value
LEEWAY = 5
INDEX_TTL = 3600
TICKS = 10_000_000

_index = {}  # (url, user) → (at, {("movie", tmdb): item id}, {tmdb series: series item id})


def _req(srv, method, path, body=None, timeout=20):
    url = srv["url"].rstrip("/") + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-Emby-Token": srv["token"], "Accept": "application/json", "User-Agent": "Viewarr-Plugin/0.5 (+https://github.com/petebutty/viewarr-plugin)"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw.decode("utf-8")) if raw else None


def _items(srv, params):
    q = urllib.parse.urlencode(params)
    return (_req(srv, "GET", f"/Users/{srv['user_id']}/Items?{q}") or {}).get("Items") or []


def _tmdb(item):
    p = item.get("ProviderIds") or {}
    v = p.get("Tmdb") or p.get("tmdb") or p.get("TMDB")
    try:
        return int(v) if v else None
    except ValueError:
        return None


def _when(item):
    """The item's last played time (epoch s) from its UserData, or 0."""
    s = ((item.get("UserData") or {}).get("LastPlayedDate") or "")[:19]
    try:
        return int(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))) if s else 0
    except ValueError:
        return 0


def server_state(srv):
    """What's played / part played on the server: {viewarr key: (status, pos s, dur s, when s, item id, title)}."""
    fields = "ProviderIds,UserData,SeriesId,ParentIndexNumber,IndexNumber,SeriesName,RunTimeTicks"
    got = {}
    for flt in ("IsPlayed", "IsResumable"):
        got.update({i["Id"]: i for i in _items(srv, {"Recursive": "true", "IncludeItemTypes": "Movie,Episode",
                                                         "Filters": flt, "Fields": fields, "Limit": 5000})})
    series_ids = sorted({i.get("SeriesId") for i in got.values() if i.get("Type") == "Episode" and i.get("SeriesId")})
    series_tmdb = {}
    for n in range(0, len(series_ids), 100):
        for s in _items(srv, {"Ids": ",".join(series_ids[n:n + 100]), "Fields": "ProviderIds"}):
            series_tmdb[s["Id"]] = _tmdb(s)
    out = {}
    for i in got.values():
        ud = i.get("UserData") or {}
        dur = int((i.get("RunTimeTicks") or 0) / TICKS)
        pos = int((ud.get("PlaybackPositionTicks") or 0) / TICKS)
        status = "watched" if ud.get("Played") else ("watching" if pos > 0 else None)
        if not status:
            continue
        if i.get("Type") == "Movie":
            t = _tmdb(i)
            if t:
                out[f"tmdb_movie_{t}"] = (status, pos, dur, _when(i), i["Id"], i.get("Name") or "")
        else:
            t = series_tmdb.get(i.get("SeriesId"))
            s, e = i.get("ParentIndexNumber"), i.get("IndexNumber")
            if t and s is not None and e is not None:
                out[f"tmdb_tv_{t}_s{int(s):02d}_e{int(e):02d}"] = (
                    status, pos, dur, _when(i), i["Id"], "%s S%02dE%02d" % (i.get("SeriesName") or "", int(s), int(e)))
    return out


def _library_index(srv):
    """Every film and show in the library by TMDB id (kept an hour): to mark something not played there yet."""
    key = (srv["url"], srv["user_id"])
    hit = _index.get(key)
    if hit and time.time() - hit[0] < INDEX_TTL:
        return hit[1], hit[2]
    movies, shows = {}, {}
    for i in _items(srv, {"Recursive": "true", "IncludeItemTypes": "Movie,Series", "Fields": "ProviderIds", "Limit": 20000}):
        t = _tmdb(i)
        if t:
            (movies if i.get("Type") == "Movie" else shows)[t] = i["Id"]
    _index[key] = (time.time(), movies, shows)
    return movies, shows


def _find(srv, vkey):
    """A Viewarr key → the server's item id, or None (not in that library)."""
    movies, shows = _library_index(srv)
    parts = vkey.split("_")
    try:
        if vkey.startswith("tmdb_movie_"):
            return movies.get(int(parts[2]))
        if vkey.startswith("tmdb_tv_"):
            series = shows.get(int(parts[2]))
            if not series:
                return None
            season, ep = int(parts[3][1:]), int(parts[4][1:])
            q = urllib.parse.urlencode({"season": season, "userId": srv["user_id"]})
            for e in (_req(srv, "GET", f"/Shows/{series}/Episodes?{q}") or {}).get("Items") or []:
                if e.get("IndexNumber") == ep:
                    return e["Id"]
    except (ValueError, IndexError):
        return None
    return None


def _mark(srv, item_id, status, pos):
    uid = srv["user_id"]
    if status == "watched":
        _req(srv, "POST", f"/Users/{uid}/PlayedItems/{item_id}")
    elif status == "unwatched":
        _req(srv, "DELETE", f"/Users/{uid}/PlayedItems/{item_id}")
    else:
        _req(srv, "POST", f"/Users/{uid}/Items/{item_id}/UserData", {"PlaybackPositionTicks": int(pos * TICKS), "Played": False})


# ---- Plex (its own API: tmdb:// guids, scrobble / unscrobble, the view offset for a resume point)

def _plex(srv, path, params=None):
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    req = urllib.request.Request(srv["url"].rstrip("/") + path + q, headers={
        "X-Plex-Token": srv["token"], "Accept": "application/json", "User-Agent": "Viewarr-Plugin/0.5 (+https://github.com/petebutty/viewarr-plugin)",
        "X-Plex-Client-Identifier": "viewarr-plugin", "X-Plex-Product": "Viewarr plugin"})
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
        return (json.loads(raw.decode("utf-8")) if raw else {}).get("MediaContainer") or {}


def _plex_all(srv, path, params):
    """Every item of a Plex list, 500 at a time (a whole big library in one answer took longer than the timeout,
    through an outside address and a VPN)."""
    out, start = [], 0
    while True:
        page = _plex(srv, path, dict(params, **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": 500}))
        items = page.get("Metadata") or []
        out += items
        start += len(items)
        total = page.get("totalSize") or page.get("size") or 0
        if not items or start >= total or len(items) < 500:
            return out


def _plex_tmdb(item):
    for g in item.get("Guid") or []:
        gid = g.get("id") or ""
        if gid.startswith("tmdb://"):
            try:
                return int(gid[7:])
            except ValueError:
                return None
    return None


def _plex_sections(srv):
    return [(d["key"], d.get("type")) for d in (_plex(srv, "/library/sections").get("Directory") or []) if d.get("type") in ("movie", "show")]


DATA_DIR = None  # set by server.py: where the big Plex lookup lists are kept between restarts
PLEX_INDEX_TTL = 24 * 3600


def _index_name(srv):
    """The file a Plex server's lookup lists are kept in (in DATA_DIR)."""
    import hashlib
    key = (srv.get("url"), (srv.get("token") or "")[-6:])
    return "plex_index_%s.json" % hashlib.sha1(("%s|%s" % key).encode()).hexdigest()[:12]


def sweep_indexes(keep, log=None):
    """Deletes the Plex lookup files no user's watch sync uses any more ([keep]: the file names still in use)."""
    import os
    if not DATA_DIR or not os.path.isdir(DATA_DIR):
        return 0
    n = 0
    for name in os.listdir(DATA_DIR):
        if name.startswith("plex_index_") and name.endswith(".json") and name not in keep:
            try:
                os.remove(os.path.join(DATA_DIR, name))
                n += 1
            except OSError:
                pass
    if n and log:
        log("watch sync: %d Plex lookup file(s) no longer used deleted" % n)
    return n


def _plex_index(srv):
    """Films and shows by TMDB id: {tmdb: ratingKey} each. Only needed to mark something not yet played on Plex; a big
    library (80,000 titles) takes minutes to list, so it's kept 24 hours, on disk too."""
    import os
    key = (srv["url"], srv["token"][-6:])
    hit = _index.get(key)
    if hit and time.time() - hit[0] < PLEX_INDEX_TTL:
        return hit[1], hit[2]
    path = os.path.join(DATA_DIR, _index_name(srv)) if DATA_DIR else None
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                at, mv, sh = json.load(f)
            if time.time() - at < PLEX_INDEX_TTL:
                movies = {int(k): v for k, v in mv.items()}
                shows = {int(k): v for k, v in sh.items()}
                _index[key] = (at, movies, shows)
                return movies, shows
        except Exception:
            pass
    movies, shows = {}, {}
    for k, kind in _plex_sections(srv):
        for i in _plex_all(srv, f"/library/sections/{k}/all", {"type": 1 if kind == "movie" else 2, "includeGuids": 1}):
            t = _plex_tmdb(i)
            if t:
                (movies if kind == "movie" else shows)[t] = i["ratingKey"]
    _index[key] = (time.time(), movies, shows)
    if path:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([time.time(), movies, shows], f, separators=(",", ":"))
        except Exception:
            pass
    return movies, shows


def plex_state(srv):
    """Watched / part watched on Plex in the last 30 days (and anything on deck): {viewarr key: (status, pos, dur, when,
    ratingKey, title)}."""
    since = int(time.time()) - 30 * 86400
    items = {}
    for k, kind in _plex_sections(srv):
        for i in _plex_all(srv, f"/library/sections/{k}/all", {"type": 1 if kind == "movie" else 4, "includeGuids": 1,
                                                               "lastViewedAt>>": since}):
            items[i["ratingKey"]] = i
    for i in _plex(srv, "/library/onDeck").get("Metadata") or []:
        items.setdefault(i["ratingKey"], i)
    # The shows of the episodes here, looked up directly (just these, not the whole library).
    show_tmdb = {}
    need = sorted({str(i.get("grandparentRatingKey")) for i in items.values() if i.get("type") == "episode" and i.get("grandparentRatingKey")})
    for n in range(0, len(need), 50):
        for sh in _plex(srv, "/library/metadata/" + ",".join(need[n:n + 50]), {"includeGuids": 1}).get("Metadata") or []:
            show_tmdb[str(sh.get("ratingKey"))] = _plex_tmdb(sh)
    out = {}
    for i in items.values():
        dur = int((i.get("duration") or 0) / 1000)
        pos = int((i.get("viewOffset") or 0) / 1000)
        status = "watching" if pos > 0 else ("watched" if (i.get("viewCount") or 0) > 0 else None)
        if not status:
            continue
        when = int(i.get("lastViewedAt") or 0)
        if i.get("type") == "movie":
            t = _plex_tmdb(i)
            if t:
                out[f"tmdb_movie_{t}"] = (status, pos, dur, when, i["ratingKey"], i.get("title") or "")
        elif i.get("type") == "episode":
            t = show_tmdb.get(str(i.get("grandparentRatingKey")))
            s, e = i.get("parentIndex"), i.get("index")
            if t and s is not None and e is not None:
                out[f"tmdb_tv_{t}_s{int(s):02d}_e{int(e):02d}"] = (
                    status, pos, dur, when, i["ratingKey"], "%s S%02dE%02d" % (i.get("grandparentTitle") or "", int(s), int(e)))
    return out


def plex_find(srv, vkey):
    movies, shows = _plex_index(srv)
    parts = vkey.split("_")
    try:
        if vkey.startswith("tmdb_movie_"):
            return movies.get(int(parts[2]))
        if vkey.startswith("tmdb_tv_"):
            show = shows.get(int(parts[2]))
            if not show:
                return None
            season, ep = int(parts[3][1:]), int(parts[4][1:])
            for e in _plex_all(srv, f"/library/metadata/{show}/allLeaves", {}):
                if e.get("parentIndex") == season and e.get("index") == ep:
                    return e["ratingKey"]
    except (ValueError, IndexError):
        return None
    return None


def plex_mark(srv, rk, status, pos):
    base = {"key": rk, "identifier": "com.plexapp.plugins.library"}
    if status == "watched":
        _plex(srv, "/:/scrobble", base)
    elif status == "unwatched":
        _plex(srv, "/:/unscrobble", base)
    else:
        _plex(srv, "/:/progress", dict(base, time=int(pos * 1000), state="stopped"))


def sync_user(progress, servers, log=None):
    """One user's sync with each of their servers. Returns (the updated Viewarr list, changed?, counts)."""
    prog = dict(progress or {})
    changed = False
    pulled = pushed = 0
    now = int(time.time())
    for srv in servers:
        if not (srv.get("url") and srv.get("token") and (srv.get("user_id") or srv.get("kind") == "plex")):
            continue
        name = srv.get("name") or srv.get("kind") or "server"
        plex = srv.get("kind") == "plex"
        try:
            remote = plex_state(srv) if plex else server_state(srv)
        except Exception as exc:
            if log:
                log("watch sync: %s unreachable (%s)" % (name, type(exc).__name__))
            continue
        # Server → Viewarr: newer there, for titles Viewarr already has (or another episode of a show it has). A show
        # only ever watched on the server stays there: Viewarr shows that server's own rows (otherwise
        # server watching turned up as blank IPTV cards).
        shows_here = {k.rsplit("_s", 1)[0] for k in prog if k.startswith("tmdb_tv_")}
        for k, (status, pos, dur, when, _iid, title) in remote.items():
            if k not in prog and not (k.startswith("tmdb_tv_") and k.rsplit("_s", 1)[0] in shows_here):
                continue
            mine = prog.get(k) or {}
            if when and when > int(mine.get("updated_at") or 0) + LEEWAY and \
                    (mine.get("status") != status or (status == "watching" and abs(int(mine.get("pos") or 0) - pos) > 30)):
                entry = {"status": status, "updated_at": when, "title": mine.get("title") or title}
                if status == "watching":
                    entry.update(pos=pos, dur=dur or int(mine.get("dur") or 0))
                if not mine:
                    entry["src"] = srv.get("kind") or "server"  # (taken from the server, never played in Viewarr)
                for extra in ("key", "type", "poster", "src"):
                    if extra in mine:
                        entry[extra] = mine[extra]
                prog[k] = entry
                changed = True
                pulled += 1
        # Viewarr → server: newer here (or not there at all).
        for k, v in list(prog.items()):
            if not k.startswith("tmdb_"):
                continue
            status = v.get("status")
            mine_at = int(v.get("updated_at") or 0)
            theirs = remote.get(k)
            if theirs and theirs[3] + LEEWAY >= mine_at:
                continue
            if theirs and theirs[0] == status and (status != "watching" or abs(theirs[1] - int(v.get("pos") or 0)) <= 30):
                continue
            if not theirs and status == "unwatched":
                continue  # not played there either
            if now - mine_at > 30 * 86400 and not theirs:
                continue  # (old history: not pushed into a library that never had it)
            try:
                iid = theirs[4] if theirs else (plex_find(srv, k) if plex else _find(srv, k))
                if not iid:
                    continue
                (plex_mark if plex else _mark)(srv, iid, status, int(v.get("pos") or 0))
                pushed += 1
            except Exception as exc:
                if log:
                    log("watch sync: couldn't mark %s on %s (%s)" % (k, name, type(exc).__name__))
    return prog, changed, (pulled, pushed)


def run(log=None):
    """Every user who switched it on: synced. Returns a short status."""
    from apps.accounts.models import UserAppData
    from django.db import close_old_connections

    close_old_connections()
    users = pulled = pushed = too_big = 0
    in_use = set()
    for row in UserAppData.objects.filter(key=SERVERS_KEY):
        servers = [s for s in ((row.value or {}).get("servers") or []) if isinstance(s, dict)]
        if not servers:
            continue
        in_use |= {_index_name(s) for s in servers if s.get("kind") == "plex" and s.get("url") and s.get("token")}
        prog_row = UserAppData.objects.filter(user_id=row.user_id, key=PROGRESS_KEY).first()
        progress = prog_row.value if prog_row and isinstance(prog_row.value, dict) else {}
        new, changed, (a, b) = sync_user(progress, servers, log)
        users += 1
        pulled += a
        pushed += b
        if changed:
            ours = {k: e for k, e in new.items() if progress.get(k) != e}
            if not _save_progress(row.user_id, progress, ours, log):
                too_big += 1
    close_old_connections()
    sweep_indexes(in_use, log)
    out = {"users": users, "pulled": pulled, "pushed": pushed}
    if too_big:
        out["too_big"] = too_big
    return out


def _save_progress(user_id, before, ours, log=None):
    """The entries this sync took from the servers ([ours]) into the user's viewarr_vod_progress, re-read under a row
    lock just before saving: the servers can take a while to answer, and a TV may have saved its own progress in the
    meantime. An entry is taken only when the TV hasn't changed it since the sync read it ([before]), or when ours is
    newer. Returns False when the result is over Dispatcharr's limit (not saved, and said so in the log)."""
    from django.db import transaction
    from apps.accounts.models import UserAppData
    with transaction.atomic():
        row = UserAppData.objects.select_for_update().filter(user_id=user_id, key=PROGRESS_KEY).first()
        fresh = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
        applied = 0
        for k, e in ours.items():
            cur = fresh.get(k)
            if cur == before.get(k) or int((cur or {}).get("updated_at") or 0) < int(e.get("updated_at") or 0):
                fresh[k] = e
                applied += 1
        if not applied:
            return True
        size = len(json.dumps(fresh, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if size > MAX_BYTES:
            if log:
                log("watch sync: user %s's watch progress is too big to save (%d KB, the limit is %d KB); %d update(s) "
                    "from the servers not saved" % (user_id, size // 1024, MAX_BYTES // 1024, applied))
            return False
        if row is None:
            UserAppData.objects.create(user_id=user_id, key=PROGRESS_KEY, value=fresh, byte_size=size)
        else:
            row.value = fresh
            row.byte_size = size
            row.save(update_fields=["value", "byte_size", "updated_at"])
    return True
