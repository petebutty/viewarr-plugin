"""Server-side sports for Viewarr: the work every TV used to do on its own, done once here for the house.

Every few minutes (on the one Dispatcharr process that holds the plugin's leadership) it reads the channels and guide
straight from Dispatcharr's database, fetches the fixtures and the Sky schedule once, and works out (sports.py, the
app's own rules ported to Python):

  * Live now: what's live in sport now and starting soon;
  * where to watch each game in the leagues and teams anyone in the house follows.

The result goes into every active user's own app storage (Dispatcharr's per-user app data, key ``viewarr_server``),
which each user can read with their own sign-in: Viewarr reads it and shows it at once, and works things out itself
only when it's missing or old (no plugin, or the service stopped).
"""

import importlib.util
import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

# Where the results go (Dispatcharr allows 64 characters for a key and 128 KiB for a value).
APP_KEY = "viewarr_server"
SETTINGS_KEY = "io.github.petebutty.viewarr"
MAX_BYTES = 120_000
INTERVAL_S = 300
VERSION = 1


_MODULES = {}

# Every function that can be switched off in the plugin's settings: the name the TVs read in viewarr_server's
# "functions" map → its setting (all on by default). Off here: off for the whole house, and Viewarr hides it.
FUNCTIONS = {
    "live": "live_now", "games": "followed_games", "recordings": "sport_recordings", "alerts": "score_alerts",
    "event_channels": "event_channels", "just_added": "just_added", "watch_sync": "watch_sync",
    "stream_order": "stream_order", "reports": "reports_auto", "measure": "measure_on", "timeshift": "timeshift",
    "backups": "backups", "guide": "guide", "logos": "guide_logos",
}
# Functions that are off until switched on (the rest are on by default).
DEFAULT_OFF = {"guide", "logos"}
# The functions as the last round found them (the quick score checks between rounds read it).
_FN = {}


def functions(settings):
    """{function: on} from the plugin's settings. Sport recordings and score alerts work from the followed games, so
    they're off whenever those are."""
    settings = settings or {}
    on = {name: settings.get(key, name not in DEFAULT_OFF) is not False for name, key in FUNCTIONS.items()}
    on["guide"] = settings.get("guide") is True
    # (Channel logos is a select: off / missing / all; an older app's plain switch counts as "missing".)
    on["logos"] = settings.get("guide_logos") is True or str(settings.get("guide_logos") or "off") in ("missing", "all")
    if not on["games"]:
        on["recordings"] = on["alerts"] = False
    return on


def _module(name):
    """Another file beside this one (sports.py…), loaded once and kept (its caches last between rounds)."""
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location("viewarr_" + name, os.path.join(_HERE, name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODULES[name] = mod
    return _MODULES[name]


def _own_settings():
    from apps.plugins.models import PluginConfig
    conf = PluginConfig.objects.filter(key="viewarr").first()
    return (conf.settings or {}) if conf else {}


_SPORTS = None


def _sports():
    """sports.py beside this file (loaded by path: Dispatcharr loads plugin.py on its own, not as a package), loaded
    once and kept, so its fetch caches (ESPN, Sky, the UK listings) last from one round to the next."""
    global _SPORTS
    if _SPORTS is None:
        spec = importlib.util.spec_from_file_location("viewarr_sports", os.path.join(_HERE, "sports.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _SPORTS = mod
    return _SPORTS


def _channels():
    from apps.channels.models import Channel
    out = []
    for c in Channel.objects.select_related("channel_group").only(
        "id", "uuid", "name", "channel_number", "tvg_id", "epg_data_id", "channel_group__name"
    ):
        out.append({
            "id": c.id, "uuid": str(c.uuid), "name": c.name or "",
            "number": float(c.channel_number) if c.channel_number is not None else None,
            "group": c.channel_group.name if c.channel_group_id else None,
            "tvg_id": c.tvg_id or None, "_epg": c.epg_data_id,
        })
    return out


# The guide's programmes, kept between rounds (2026-11-20: every round read ~145,000 programmes again, 8-25 s).
# Only what changed is read: a new guide refresh (a source's updated_at / message, or new programme rows) starts the
# copy again; otherwise only a stretch of time not read yet is fetched. Read again in full every FULL_RELOAD_S anyway,
# in case something edited programmes in place.
# Only the guide entries of the house's lineup are kept (channels enabled in a channel profile, radio left out, as
# guide.lineup() decides): a game placed on a channel no profile shows couldn't be watched on any TV. Of each
# description only the start is kept (sports.py only looks at its first 80 characters, for "Live coverage...").
_PC = {"stamp": None, "lo": None, "hi": None, "rows": {}, "at": 0.0, "read": 0}
FULL_RELOAD_S = 3 * 3600
DESC_KEEP = 80


def _guide_stamp():
    from django.db.models import Max
    from apps.epg.models import EPGSource, ProgramData
    src = tuple(EPGSource.objects.order_by("id").values_list("id", "updated_at", "last_message"))
    return (src, ProgramData.objects.aggregate(m=Max("id"))["m"])


def _lineup_ids():
    """The ids of the channels in the house's lineup: enabled in at least one channel profile (every channel when
    there are no profiles), radio left out. None when it can't be worked out (then every channel counts)."""
    try:
        from apps.channels.models import Channel, ChannelProfile, ChannelProfileMembership
        qs = Channel.objects.filter(is_radio=False)
        if ChannelProfile.objects.exists():
            qs = qs.filter(id__in=ChannelProfileMembership.objects.filter(enabled=True).values("channel_id"))
        return set(qs.values_list("id", flat=True))
    except Exception:
        return None


def _read_programmes(epg_ids, **where):
    from apps.epg.models import ProgramData
    rows = ProgramData.objects.filter(epg_id__in=epg_ids, **where).values_list(
        "epg_id", "title", "sub_title", "description", "start_time", "end_time", "custom_properties"
    )
    n = 0
    keep = _PC["rows"]
    for epg_id, title, sub, desc, st, en, cp in rows.iterator(chunk_size=2000):
        cp = cp if isinstance(cp, dict) else {}
        keep.setdefault(epg_id, []).append((
            int(st.timestamp()) * 1000, int(en.timestamp()) * 1000, title or "", sub, (desc or "")[:DESC_KEEP] or None,
            bool(cp.get("live")), cp.get("icon"),
        ))
        n += 1
    _PC["read"] += n


def _programmes_for(channels, lineup=None):
    """The guide reader handed to sports.compute: a channel's programmes between two times (kept between rounds),
    already in sports.py's own shape (times in ms), so they aren't turned into dicts twice.
    [lineup]: the channel ids whose guide entries are read (None: every channel's)."""
    from datetime import datetime, timezone

    epg_of = {c["id"]: c["_epg"] for c in channels if c.get("_epg")}
    if lineup is not None:
        # (A channel outside the lineup sharing a guide entry with one inside costs nothing: it's read anyway.)
        wanted_epg = {e for cid, e in epg_of.items() if cid in lineup}
        epg_of = {cid: e for cid, e in epg_of.items() if e in wanted_epg}
    every_epg = sorted(set(epg_of.values()))
    utc = lambda s: datetime.fromtimestamp(s, timezone.utc)
    _PC["read"] = 0
    try:
        stamp = (_guide_stamp(), tuple(every_epg))
    except Exception:
        stamp = None
    if stamp is None or stamp != _PC["stamp"] or time.time() - _PC["at"] > FULL_RELOAD_S:
        _PC.update(stamp=stamp, lo=None, hi=None, rows={}, at=time.time())

    def programmes(channel_ids, start_s, end_s):
        wanted = {}
        for cid in channel_ids:
            e = epg_of.get(cid)
            if e:
                wanted.setdefault(e, []).append(cid)
        if not wanted:
            return []
        # Fill in the stretch not read yet, for every channel at once (later asks are then answered from memory).
        if _PC["lo"] is None:
            _read_programmes(every_epg, end_time__gt=utc(start_s), start_time__lt=utc(end_s))
            _PC["lo"], _PC["hi"] = start_s, end_s
        else:
            if start_s < _PC["lo"]:
                _read_programmes(every_epg, end_time__gt=utc(start_s), end_time__lte=utc(_PC["lo"]))
                _PC["lo"] = start_s
            if end_s > _PC["hi"]:
                _read_programmes(every_epg, start_time__gte=utc(_PC["hi"]), start_time__lt=utc(end_s))
                _PC["hi"] = end_s
        out = []
        lo, hi = start_s * 1000, end_s * 1000
        for e, cids in wanted.items():
            for (st, en, title, sub, desc, live, image) in _PC["rows"].get(e, ()):
                if en > lo and st < hi:
                    for cid in cids:
                        out.append({
                            "channel_id": cid, "title": title, "sub_title": sub, "description": desc,
                            "start": st, "end": en, "is_live": live, "image": image,
                        })
        return out

    # What's long over is let go (the copy only ever needs from a few hours ago).
    cut = int(time.time()) - 12 * 3600
    if _PC["lo"] is not None and _PC["lo"] < cut:
        for e in list(_PC["rows"]):
            _PC["rows"][e] = [r for r in _PC["rows"][e] if r[1] > cut * 1000]
        _PC["lo"] = cut
    return programmes


def _followed():
    """Every followed team and league in the house: each user's synced Viewarr setup holds theirs (sports_follow)."""
    from apps.accounts.models import UserAppData
    seen, out = set(), []

    def walk(v):
        # (The setup is a JSON object; find the sports_follow value wherever it sits, as text or as a list.)
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "sports_follow":
                    yield x
                else:
                    yield from walk(x)
        elif isinstance(v, list):
            for x in v:
                yield from walk(x)

    for row in UserAppData.objects.filter(key=SETTINGS_KEY).only("value"):
        for raw in walk(row.value):
            items = raw
            if isinstance(raw, str):
                try:
                    items = json.loads(raw)
                except ValueError:
                    continue
            if isinstance(items, dict):
                items = [items]
            for f in items if isinstance(items, list) else []:
                if isinstance(f, dict):
                    key = json.dumps(f, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        out.append(f)
    return out


def _plugin_groups():
    """The groups Live now looks through besides sporty-named ones: this plugin's own "Live now groups" setting, plus
    Event Channel Managarr's event groups if that plugin happens to be installed (it isn't needed)."""
    from apps.plugins.models import PluginConfig
    groups = set()
    try:
        own = PluginConfig.objects.filter(key="viewarr").first()
        text = ((own.settings or {}).get("live_groups") if own else "") or ""
        groups |= {g.strip().lower() for g in str(text).replace("\n", ",").split(",") if g.strip()}
    except Exception:
        pass
    # Groups switched on in Viewarr itself (Settings → Live TV → Live now groups): each person's synced setup lists
    # them as "lngrp:+<group>", so setting it on a TV is enough, no need for this page (as much as
    # possible is set from the app).
    try:
        from apps.accounts.models import UserAppData

        def walk(v):
            if isinstance(v, str):
                if v.startswith("lngrp:+"):
                    yield v[len("lngrp:+"):].strip().lower()
            elif isinstance(v, dict):
                for x in v.values():
                    yield from walk(x)
            elif isinstance(v, list):
                for x in v:
                    yield from walk(x)
        for row in UserAppData.objects.filter(key="io.github.petebutty.viewarr").only("value"):
            groups |= {g for g in walk(row.value) if g}
    except Exception:
        pass
    try:
        conf = PluginConfig.objects.filter(key="event_channel_managarr", enabled=True).first()
        text = ((conf.settings or {}).get("channel_groups") if conf else "") or ""
        groups |= {g.strip().lower() for g in str(text).split(",") if g.strip()}
    except Exception:
        pass
    return groups


def _shrink(result):
    """Under Dispatcharr's limit per value: the least sure and furthest-off games go first, then Live now's tail."""
    def size(r):
        return len(json.dumps(r, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    if size(result) <= MAX_BYTES:
        return result
    games = sorted(result.get("games", {}).items(), key=lambda kv: (-(kv[1].get("sure") or 0), kv[1].get("start") or 0))
    while games and size(result) > MAX_BYTES:
        games = games[: max(0, int(len(games) * 0.85))]
        result["games"] = dict(games)
    while result.get("live") and size(result) > MAX_BYTES:
        result["live"] = result["live"][: int(len(result["live"]) * 0.85)]
    return result


GAME_WATCH_KEY = "viewarr_game_watch"
DONE_KEEP = 300


def record_wanted(result, channels, log=None):
    """Stage 2: "record all of a team's games" and "Record" on games the guide didn't list yet, done on the server.

    Each user's Viewarr keeps them in their app data (key viewarr_game_watch: {"record": [game ids], "teams":
    [team names, lower case], "done": {game id: ms}}). Once a game's channel is known for sure and the guide lists a
    programme at kick-off on it, a Dispatcharr recording is made (as Viewarr makes one: Dispatcharr adds its own padding
    when it runs), unless one already covers that time on that channel; the game is then marked done, so neither the
    server nor a TV records it again. TVs needn't be on.
    """
    from datetime import datetime, timezone, timedelta
    from apps.accounts.models import UserAppData
    from apps.channels.models import Recording
    from apps.epg.models import ProgramData

    now = time.time()
    by_uuid = {c["uuid"]: c for c in channels}
    games = result.get("games", {})
    made = 0
    for row in UserAppData.objects.filter(key=GAME_WATCH_KEY):
        v = row.value if isinstance(row.value, dict) else {}
        record = set(v.get("record") or [])
        teams = {str(t).lower() for t in (v.get("teams") or [])}
        done = dict(v.get("done") or {})
        if not record and not teams:
            continue
        changed = False
        for gid, g in games.items():
            if gid in done or g.get("state") != "pre" or (g.get("sure") or 0) < 2 or not g.get("ch"):
                continue
            wanted = gid in record or (g.get("home") or "").lower() in teams or (g.get("away") or "").lower() in teams
            start = g.get("start") or 0
            if not wanted or not (now < start < now + 3 * 86400):
                continue
            ch = next((by_uuid[u] for u in g["ch"] if u in by_uuid and by_uuid[u].get("_epg")), None)
            if ch is None:
                continue
            kick = datetime.fromtimestamp(start, timezone.utc)
            prog = ProgramData.objects.filter(
                epg_id=ch["_epg"], start_time__lte=kick + timedelta(minutes=15), end_time__gt=kick,
            ).order_by("start_time").first()
            if prog is None:
                continue  # the guide doesn't list it yet: next round
            if not Recording.objects.filter(channel_id=ch["id"], start_time__lt=prog.end_time, end_time__gt=prog.start_time).exists():
                program = {
                    "title": prog.title or g.get("title") or "", "tvg_id": prog.tvg_id or ch.get("tvg_id") or "",
                    "start_time": prog.start_time.isoformat().replace("+00:00", "Z"),
                    "end_time": prog.end_time.isoformat().replace("+00:00", "Z"),
                }
                if prog.sub_title:
                    program["sub_title"] = prog.sub_title
                if prog.description:
                    program["description"] = prog.description
                Recording.objects.create(channel_id=ch["id"], start_time=prog.start_time, end_time=prog.end_time,
                                         custom_properties={"program": program})
                made += 1
                if log:
                    log.info("Viewarr: recording %s on %s (%s's %s)", g.get("title"), ch.get("name"), row.user_id,
                             "team rule" if gid not in record else "request")
            done[gid] = int(now * 1000)
            record.discard(gid)
            changed = True
        if changed:
            _save_game_watch(row.pk, {g: t for g, t in done.items() if g not in (v.get("done") or {})}, log)
    return made


def _save_game_watch(pk, newly_done, log=None):
    """Marks [newly_done] ({game id: ms}) done in a user's viewarr_game_watch, re-read under a row lock just before
    saving: a TV may have added a game or a team since the round read it, and that mustn't be lost. Only the plugin's
    own change is applied to the fresh value (those games done and out of "record"); the rest is kept as it is."""
    from django.db import transaction
    from apps.accounts.models import UserAppData
    with transaction.atomic():
        row = UserAppData.objects.select_for_update().filter(pk=pk).first()
        if row is None:
            return
        fresh = row.value if isinstance(row.value, dict) else {}
        done = dict(fresh.get("done") or {})
        done.update(newly_done)
        kept = dict(sorted(done.items(), key=lambda kv: kv[1], reverse=True)[:DONE_KEEP])
        record = [g for g in (fresh.get("record") or []) if g not in newly_done]
        value = dict(fresh, record=sorted(record), done=kept)
        size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        if size > MAX_BYTES:
            if log:
                log.warning("Viewarr: game watch list of user %s too big to save (%d bytes); left as it was",
                            row.user_id, size)
            return
        row.value = value
        row.byte_size = size
        row.save(update_fields=["value", "byte_size", "updated_at"])


# "Refresh sports now" (plugin.py) leaves this file in the data folder instead of running a round inside the web
# request (where it could overlap the service's own round and make a recording twice); the service loop below looks
# for it at least once a minute.
REFRESH_FLAG = "refresh.flag"
ROUND_LOCK = "sports.lock"


class _Lock:
    """One sports round at a time across processes and threads (a non-blocking lock on a file in the data folder)."""

    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, ROUND_LOCK)
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
            self.f = None


def _take_flag(name):
    try:
        os.remove(os.path.join(_data_dir(), name))
        return True
    except OSError:
        return False


def publish(log=None):
    """One round (see _publish), never two at once: a round already running elsewhere means this one is skipped
    (returns {"skipped": ...}), so the recordings and app data are never written by two rounds together."""
    with _Lock(_data_dir()) as got:
        if not got:
            if log:
                log.info("Viewarr server sports: a round is already running; this one skipped")
            return {"at": int(time.time()), "skipped": "another round is running"}
        return _publish(log)


def _publish(log=None):
    """One round: compute, then write to every active user. Returns a short status for the logs / the status action."""
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections
    from apps.accounts.models import UserAppData

    close_old_connections()
    t0 = time.time()
    try:
        fn = functions(_own_settings())
    except Exception:
        fn = functions({})
    _FN.clear()
    _FN.update(fn)
    channels = _channels()
    if fn["live"] or fn["games"]:
        result = _sports().compute(
            int(time.time()), [{k: v for k, v in c.items() if not k.startswith("_")} for c in channels],
            _programmes_for(channels, _lineup_ids()),
            # Followed games off: no fixtures or UK listings fetched (Live now still places ESPN's headline games).
            _followed() if fn["games"] else [], _plugin_groups(),
            # (sports.py writes plain lines: log("…"); the plugin's logger is an object.)
            log=(lambda msg: log.info("Viewarr sports: %s", msg)) if log else None,
            uk_listings=None if fn["games"] else [],
        )
    else:
        result = {"live": [], "games": {}}
    # A function switched off publishes nothing; the TVs read "functions" and hide it.
    if not fn["live"]:
        result["live"] = []
    if not fn["games"]:
        result["games"] = {}
    result["functions"] = fn
    result["v"] = VERSION
    result["at"] = int(time.time())
    # (Kept for the quick score checks between rounds.)
    _LAST_GAMES.clear()
    if fn["alerts"]:
        _LAST_GAMES.update(result.get("games") or {})
    result = _shrink(result)
    data = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
    size = len(data.encode("utf-8"))
    users = 0
    for user in get_user_model().objects.filter(is_active=True).only("id"):
        UserAppData.objects.update_or_create(user=user, key=APP_KEY, defaults={"value": result, "byte_size": size})
        users += 1
    # What's just arrived on each streaming service (justadded.py, JustWatch): once a day, for Viewarr's
    # "<service> · Just added" rows.
    problems = {}
    just = None
    try:
        ja = _module("justadded")
        if fn["just_added"]:
            just = ja.run(_data_dir(), log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
        else:
            # Off: the stored lists go, so no TV shows an old copy.
            ja.clear(_data_dir())
            just = {"off": True}
    except Exception as exc:
        problems["just_added"] = str(exc)[:160]
        if log:
            log.warning("Viewarr: just added failed: %s", exc)
    # IPTV watch progress kept in step with each user's Emby / Jellyfin (watchsync.py), for users who switched it on.
    watch = None
    try:
        ws = _module("watchsync")
        ws.DATA_DIR = _data_dir()
        if fn["watch_sync"]:
            watch = ws.run(log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
        else:
            watch = {"off": True}
            ws.sweep_indexes(set(), log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
    except Exception as exc:
        problems["watch"] = str(exc)[:160]
        if log:
            log.warning("Viewarr: watch sync failed: %s", exc)
    recorded = 0
    if fn["recordings"]:
        try:
            recorded = record_wanted(result, channels, log)
        except Exception as exc:
            problems["recordings"] = str(exc)[:160]
            if log:
                log.warning("Viewarr: team / game recordings failed: %s", exc)
    # Reports from the TVs (reports.py) and Best stream first (streamorder.py): both house-wide, both their own file.
    house = house_round(log, fn)
    close_old_connections()
    status = {
        "recorded": recorded, "watch": watch, "just_added": just,
        "order": house.get("order"), "reports": house.get("reports"), "measured": house.get("measured"),
        "timeshift": _timeshift_status(), "guide": _guide_status(), "logos": _logos_status(), "off": sorted(k for k, on in fn.items() if not on),
        "at": result["at"], "seconds": round(time.time() - t0, 1), "bytes": size, "users": users,
        "live": len(result.get("live", [])), "games": len(result.get("games", {})),
    }
    if log:
        log.info("Viewarr server sports: %s", status)
    # One line per job for Viewarr's Activity page (admins only); never allowed to spoil the round.
    try:
        _module("activity").update(status, fn, _data_dir(), problems)
    except Exception as exc:
        if log:
            log.warning("Viewarr: activity page not written: %s", exc)
    return status


def _timeshift_status():
    try:
        return _module("timeshift").status()
    except Exception:
        return None


def _guide_status():
    try:
        return _module("guide").status(_data_dir())
    except Exception:
        return None


def _logos_status():
    try:
        return _module("logos").status(_data_dir())
    except Exception:
        return None


def _data_dir():
    return os.path.join(os.path.dirname(_HERE), "_data", "viewarr")


def house_round(log=None, fn=None):
    """Reports from the TVs acted on, then each channel's best stream put first (the dead reports feed it), then the
    house-wide results published (viewarr_house). Each part is caught on its own: none of it may stop the sports."""
    settings = {}
    try:
        settings = _own_settings()
    except Exception:
        pass
    fn = fn or functions(settings)
    plain = (lambda m: log.info("Viewarr: %s", m)) if log else None
    data_dir = _data_dir()
    out = {"order": None, "reports": None}
    rp = so = None
    try:
        rp = _module("reports")
        out["reports"] = rp.run_round(data_dir, log=plain, auto=settings.get("reports_auto", True) is not False)
    except Exception as exc:
        if log:
            log.warning("Viewarr: reports round failed: %s", exc)
    try:
        so = _module("streamorder")
        if settings.get("stream_order", True) is not False:
            # Failures the ranking should know about: dead reports from the TVs, and streams that failed three probes.
            fails = dict(rp.dead_fails(data_dir)) if rp else {}
            try:
                for sid, times in _module("measure").fails(data_dir).items():
                    fails.setdefault(sid, []).extend(times)
            except Exception:
                pass
            out["order"] = so.run_round(data_dir, log=plain, extra_fails=fails)
        else:
            out["order"] = {"reordered": 0, "week": so.reordered_this_week(so.load_state(data_dir), time.time())}
    except Exception as exc:
        if log:
            log.warning("Viewarr: best stream first failed: %s", exc)
    try:
        if rp:
            # (Event channels off: no quiet channels sent, so no TV hides any.)
            rp.publish(rp.load_state(data_dir), (out["order"] or {}).get("week", 0), quiet=fn["event_channels"])
    except Exception as exc:
        if log:
            log.warning("Viewarr: house publish failed: %s", exc)
    try:
        out["measured"] = _module("measure").status(data_dir)
    except Exception:
        out["measured"] = None
    return out


_LAST_GAMES = {}
QUICK_S = 45


def service(stop_event, log=None, status_path=None):
    """The persistent service: a full round now, then every INTERVAL_S until Dispatcharr stops it; in between, while a
    game in the results is live (or about to kick off), a quick score check every QUICK_S for alerts (alerts.py)."""
    last_full = 0.0
    # Measuring streams at night (measure.py) runs in its own thread: a probe can take 20 s, and this loop must keep
    # its five-minute rhythm. It watches the same stop event and the plugin settings (window, on / off).
    try:
        _module("measure").start(stop_event, _data_dir(), _own_settings,
                                 log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
    except Exception as exc:
        if log:
            log.warning("Viewarr: measuring thread didn't start: %s", exc)
    # Timeshift (timeshift.py): a buffer per channel being watched, so any TV can pause and rewind live TV. Its own
    # thread, watching the TVs' requests every second; it stops every buffer when the service stops.
    try:
        _module("timeshift").start(stop_event, _data_dir(), _own_settings,
                                   log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
    except Exception as exc:
        if log:
            log.warning("Viewarr: timeshift thread didn't start: %s", exc)
    # TV guide (guide.py): listings from Sky twice a day, written as an XMLTV guide source. Its own thread too (a
    # fetch takes a minute or so); it does nothing while the "guide" setting is off.
    try:
        _module("guide").start(stop_event, _data_dir(), _own_settings,
                               log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
    except Exception as exc:
        if log:
            log.warning("Viewarr: guide thread didn't start: %s", exc)
    # Channel logos (logos.py): from Sky (the guide's matches) and tv-logos, once a day and after changes. Its own
    # thread, separate from the guide, so it works with the guide off; it does nothing while "guide_logos" is off.
    try:
        _module("logos").start(stop_event, _data_dir(), _own_settings,
                               log=(lambda m: log.info("Viewarr: %s", m)) if log else None)
    except Exception as exc:
        if log:
            log.warning("Viewarr: logos thread didn't start: %s", exc)
    while not stop_event.is_set():
        asked = _take_flag(REFRESH_FLAG)   # ("Refresh sports now")
        if asked or time.time() - last_full >= INTERVAL_S:
            last_full = time.time()
            try:
                # A long-lived thread: Django's database connection can go stale between rounds ("round failed" in
                # _channels, 2026-11-18); a fresh one each round.
                try:
                    from django.db import close_old_connections
                    close_old_connections()
                except Exception:
                    pass
                status = publish(log)
            except Exception as exc:  # one bad round (a fixtures site down) mustn't end the service
                status = {"at": int(time.time()), "error": str(exc)[:300]}
                if log:
                    log.exception("Viewarr server sports round failed")
                try:
                    _module("activity").update({"at": status["at"]}, dict(_FN), _data_dir(), {"sports": status["error"]})
                except Exception:
                    pass
            if status_path and not status.get("skipped"):
                try:
                    with open(status_path, "w", encoding="utf-8") as f:
                        json.dump(status, f)
                except Exception:
                    pass
        try:
            al = _module("alerts")
            if _FN.get("alerts", True):
                if al.check(_LAST_GAMES, log=(lambda m: log.info("Viewarr: %s", m)) if log else None):
                    al.publish(log)
            else:
                # Off: the stored alerts are emptied once, so no TV shows an old one.
                al.clear(log)
        except Exception as exc:
            if log:
                log.warning("Viewarr: score alerts check failed: %s", exc)
        stop_event.wait(QUICK_S if _module("alerts").watch_list(_LAST_GAMES, time.time()) else 60)
