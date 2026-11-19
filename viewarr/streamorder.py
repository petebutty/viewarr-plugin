"""Best stream first: each channel's best stream put first in Dispatcharr, for every app and TV in the house.

Dispatcharr plays a channel's streams in their saved order and only moves on when one fails. Every round this ranks
the streams of each channel that has more than one, and puts the best first:

  * tier from the measured resolution (Stream.stream_stats) when Dispatcharr has one, else from the name: FHD / 1080
    or no tag at all → 3, HD / 720 → 2, SD / 576 / 480 → 1, 4K / UHD / 2160 → 0.5 (the house line can't carry 4K, so
    it goes last). Measuring streams at night (measure.py) fills stream_stats, so names matter less over time;
  * health from what the TVs saw: each TV keeps the failures and last good minute of every stream it played in its
    user's app data (key ``viewarr_stream_health``), merged across the house. A failure in the last day, or three in
    a week, costs 10; a good minute in the last week earns 1; a stream Dispatcharr marks stale costs 20. "Dead" reports
    from the TVs (reports.py) count as failures too;
  * score = health + tier × 2; ties keep the current order.

A channel is only reordered when its best stream beats the current first by a whole point, and at most once an hour.
The order each channel had before the first change is kept in streamorder.json, so "Put stream order back" can undo
everything. Channels in Event Channel Managarr's groups are left alone, as are channels listed in the state file's
"keep" list (or carrying ``viewarr_keep_order`` in their custom properties, on Dispatcharr versions that have them).
"""

import json
import os
import re
import time

HEALTH_KEY = "viewarr_stream_health"
STATE_FILE = "streamorder.json"
HOLD_S = 3600
DAY_S = 86400
WEEK_S = 7 * 86400
MIN_GAIN = 1.0

_UHD = re.compile(r"\b(4K|UHD|2160[PI]?)\b", re.I)
_FHD = re.compile(r"\b(FHD|1080[PI]?)\b", re.I)
_SD = re.compile(r"\b(SD|576[PI]?|480[PI]?)\b", re.I)
_HD = re.compile(r"\b(HD|720[PI]?)\b", re.I)


# ---- Ranking

def tier_from_name(name):
    """The quality tier a stream's name claims (word-boundary tags only, so "HD" inside a word doesn't count).

    A name with no tag at all counts as full HD, not HD: the main provider's streams are often untagged and are
    usually 1080p, and moving them behind a backup's "FHD" on a tag alone would swap a two-connection line for a
    one-connection one on most channels. The measured picture (stream_stats, once a stream has played) overrides
    the name either way, and a plain "HD" tag still counts as 720p.
    """
    if _UHD.search(name or ""):
        return 0.5
    if _FHD.search(name or ""):
        return 3
    if _SD.search(name or ""):
        return 1
    if _HD.search(name or ""):
        return 2
    return 3


def tier_from_height(height):
    """The tier for a measured picture height (1080 → 3, 720 → 2, 576 → 1, 2160 → 0.5)."""
    if height >= 1800:
        return 0.5
    if height >= 1000:
        return 3
    if height >= 700:
        return 2
    return 1


def tier(stream):
    """Measured resolution first ("1920x1080" in stream_stats), the name otherwise."""
    stats = stream.stream_stats if isinstance(stream.stream_stats, dict) else {}
    res = str(stats.get("resolution") or "")
    m = re.match(r"^\s*(\d{3,4})\s*x\s*(\d{3,4})\s*$", res, re.I)
    if m:
        return tier_from_height(int(m.group(2)))
    return tier_from_name(stream.name)


def health_score(entry, now, stale):
    """-10 for a stream that failed recently (once in a day or three times in a week), +1 for a good minute this
    week, -20 when Dispatcharr marks it stale."""
    fails = [t for t in (entry or {}).get("f", []) if now - t < WEEK_S]
    score = 0
    if any(now - t < DAY_S for t in fails) or len(fails) >= 3:
        score -= 10
    if now - ((entry or {}).get("ok") or 0) < WEEK_S:
        score += 1
    if stale:
        score -= 20
    return score


def score(stream, entry, now):
    return health_score(entry, now, bool(stream.is_stale)) + tier(stream) * 2


# ---- Health from the TVs

def add_fail(bucket, when):
    """One failure time into a stream's health entry, ignoring repeats within a minute."""
    fails = bucket.setdefault("f", [])
    if all(abs(when - t) >= 60 for t in fails):
        fails.append(when)
        fails.sort()
        del fails[:-50]


def merge_health(values, now, extra_fails=None):
    """Every user's viewarr_stream_health value → {stream id: {"f": [times], "ok": last good}} for the house."""
    merged = {}
    for value in values:
        streams = (value or {}).get("streams") if isinstance(value, dict) else None
        for sid, h in (streams or {}).items():
            if not isinstance(h, dict):
                continue
            try:
                bucket = merged.setdefault(int(sid), {"f": [], "ok": 0})
            except (TypeError, ValueError):
                continue
            for t in h.get("f") or []:
                if isinstance(t, (int, float)) and now - t < WEEK_S:
                    add_fail(bucket, int(t))
            ok = h.get("ok") or 0
            if isinstance(ok, (int, float)):
                bucket["ok"] = max(bucket["ok"], int(ok))
    for sid, times in (extra_fails or {}).items():
        bucket = merged.setdefault(int(sid), {"f": [], "ok": 0})
        for t in times:
            if now - t < WEEK_S:
                add_fail(bucket, int(t))
    return merged


def house_health(now, extra_fails=None):
    from apps.accounts.models import UserAppData
    values = [row.value for row in UserAppData.objects.filter(key=HEALTH_KEY).only("value")]
    return merge_health(values, now, extra_fails)


# ---- What to leave alone

def managed_groups():
    """Event Channel Managarr's groups (lower case): it reorders and hides event channels itself."""
    try:
        from apps.plugins.models import PluginConfig
        conf = PluginConfig.objects.filter(key="event_channel_managarr", enabled=True).first()
        text = ((conf.settings or {}).get("channel_groups") if conf else "") or ""
        return {g.strip().lower() for g in str(text).split(",") if g.strip()}
    except Exception:
        return set()


def quiet_groups():
    """
    Groups whose channels are off air most of the time by nature (the owner, 2026-11-19): Event Channel Managarr's
    event groups, plus the plugin setting "Groups never reported" (default "Club TV"). Their streams are never marked
    as failing, never measured, and dead-stream reports about them are logged only; the TVs get the list too
    (viewarr_house "quiet_channels") so they don't report them in the first place. Lower case, stripped.
    """
    names = set(managed_groups())
    try:
        from apps.plugins.models import PluginConfig
        conf = PluginConfig.objects.filter(key="viewarr").first()
        text = ((conf.settings or {}).get("quiet_groups") if conf else None)
        if text is None:
            text = "Club TV"
        names |= {g.strip().lower() for g in str(text).replace("\n", ",").split(",") if g.strip()}
    except Exception:
        names.add("club tv")
    return names


def quiet_channel_ids(groups=None):
    """The ids of every channel in the quiet groups."""
    from apps.channels.models import Channel
    groups = quiet_groups() if groups is None else groups
    if not groups:
        return []
    return [c.id for c in Channel.objects.select_related("channel_group").only("id", "channel_group__name")
            if (getattr(c.channel_group, "name", "") or "").strip().lower() in groups]


def keep_order(channel, state):
    """True when the owner asked for this channel's order to stay as it is."""
    if channel.id in set(state.get("keep") or []):
        return True
    props = getattr(channel, "custom_properties", None)
    return isinstance(props, dict) and bool(props.get("viewarr_keep_order"))


# ---- State file

def _path(data_dir):
    return os.path.join(data_dir, STATE_FILE)


def load_state(data_dir):
    if not data_dir:
        return {}
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(data_dir, state):
    if not data_dir:
        return
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, _path(data_dir))


def reordered_this_week(state, now):
    times = [t for t in state.get("changes") or [] if now - t < WEEK_S]
    state["changes"] = times
    return len(times)


# ---- The plan

def _links_by_channel():
    """{channel: [ChannelStream in current order]} for every channel with two or more streams."""
    from django.db.models import Count
    from apps.channels.models import Channel, ChannelStream
    channels = list(Channel.objects.annotate(n=Count("streams")).filter(n__gte=2).select_related("channel_group"))
    links = {}
    rows = ChannelStream.objects.filter(channel__in=channels).select_related("stream").order_by("channel_id", "order", "id")
    for cs in rows:
        links.setdefault(cs.channel_id, []).append(cs)
    return [(c, links.get(c.id, [])) for c in channels if len(links.get(c.id, [])) >= 2]


def measured(stream):
    """True when Dispatcharr has a measured picture size for the stream (the proxy's stats, or the night measuring)."""
    stats = stream.stream_stats if isinstance(stream.stream_stats, dict) else {}
    return bool(stats.get("resolution"))


UNMEASURED_PENALTY = 5


def rank(links, health, now):
    """
    The channel's links best first; ties keep their current order. Returns [(score, link)].

    Measured beats assumed: once any of the channel's streams has a measured picture size, a stream that hasn't been
    measured yet drops below the measured ones (its tier is only what its name claims), so a main provider's
    measured 576p copy isn't swapped for a backup's untagged copy nobody has looked at. Once the backup is measured
    too, the better picture wins on the facts.
    """
    any_measured = any(measured(cs.stream) for cs in links)
    scored = []
    for cs in links:
        sc = score(cs.stream, health.get(cs.stream_id), now)
        if any_measured and not measured(cs.stream):
            sc -= UNMEASURED_PENALTY
        scored.append((sc, cs))
    return sorted(scored, key=lambda x: (-x[0], x[1].order, x[1].id))


def spread(ranked):
    """
    Second place goes to the best stream from a provider other than the first's (the rest keep their rank), so a
    client bumped off the first stream's provider when its line is busy lands on the best copy elsewhere, not on
    another copy of the same busy provider. Only Viewarr asks live which lines are free; Dispatcharr's own failover
    and plain M3U players go down this list in order.
    """
    if len(ranked) < 3:
        return ranked
    first_acc = ranked[0][1].stream.m3u_account_id
    for i in range(1, len(ranked)):
        if ranked[i][1].stream.m3u_account_id != first_acc:
            return ranked[:1] + [ranked[i]] + ranked[1:i] + ranked[i + 1:]
    return ranked


def _proposal(channel, links, health, now, state):
    """What to do with one channel: a dict when it should be reordered, else None."""
    ranked = spread(rank(links, health, now))
    best_score, best = ranked[0]
    current = links[0]
    touched = (state.get("channels") or {}).get(str(channel.id)) or {}
    if now - (touched.get("changed_at") or 0) < HOLD_S:
        return None
    if best.id == current.id:
        # First place right already: second place still moves when it should be another provider's best (spread).
        if len(ranked) < 3 or ranked[1][1].id == links[1].id:
            return None
        return {
            "channel_id": channel.id, "name": channel.name,
            "current_first": current.stream.name, "proposed_first": best.stream.name,
            "second": ranked[1][1].stream.name, "gain": 0,
            "order": [cs.stream_id for _, cs in ranked], "was": [cs.stream_id for cs in links],
            "links": [cs for _, cs in ranked],
        }
    current_score = next(s for s, cs in ranked if cs.id == current.id)
    if best_score - current_score < MIN_GAIN:
        return None
    return {
        "channel_id": channel.id, "name": channel.name,
        "current_first": current.stream.name, "proposed_first": best.stream.name,
        "gain": round(best_score - current_score, 1),
        "order": [cs.stream_id for _, cs in ranked], "was": [cs.stream_id for cs in links],
        "links": [cs for _, cs in ranked],
    }


def plan(data_dir=None, now=None, extra_fails=None):
    """The channels that would be reordered now, without changing anything (also the dry-run entry point)."""
    now = now or int(time.time())
    state = load_state(data_dir)
    health = house_health(now, extra_fails)
    skip_groups = managed_groups()
    quiet = quiet_groups()
    out = []
    for channel, links in _links_by_channel():
        group = (channel.channel_group.name if channel.channel_group_id else "") or ""
        # (Stripped: a Dispatcharr group name can carry a trailing space, the settings text can't.)
        if group.strip().lower() in skip_groups or keep_order(channel, state):
            continue
        # A quiet group (club TV): ordered by quality only; being off air isn't a fail.
        p = _proposal(channel, links, {} if group.strip().lower() in quiet else health, now, state)
        if p:
            out.append(p)
    return out


# ---- Applying and undoing

def _write_order(links, order):
    """ChannelStream.order = 0..n in the given stream order (the same bulk update Dispatcharr's own API uses)."""
    from apps.channels.models import ChannelStream
    by_stream = {cs.stream_id: cs for cs in links}
    to_update = []
    for i, sid in enumerate(order):
        cs = by_stream.get(sid)
        if cs is not None and cs.order != i:
            cs.order = i
            to_update.append(cs)
    if to_update:
        ChannelStream.objects.bulk_update(to_update, ["order"])


def apply(data_dir, log=None, extra_fails=None):
    """One round: reorder what the plan says and remember each channel's original order. Returns the count."""
    now = int(time.time())
    proposals = plan(data_dir, now, extra_fails)
    if not proposals:
        return 0
    state = load_state(data_dir)
    channels = state.setdefault("channels", {})
    for p in proposals:
        _write_order(p["links"], p["order"])
        entry = channels.setdefault(str(p["channel_id"]), {"original": p["was"], "name": p["name"]})
        entry["changed_at"] = now
        entry["first"] = p["proposed_first"]
        state.setdefault("changes", []).append(now)
    reordered_this_week(state, now)
    save_state(data_dir, state)
    if log:
        log("Best stream first: %d channels reordered" % len(proposals))
    return len(proposals)


def reset(data_dir):
    """Put every touched channel back to the order it had before the plugin first changed it; forget the state."""
    from apps.channels.models import ChannelStream
    state = load_state(data_dir)
    restored = 0
    for cid, entry in (state.get("channels") or {}).items():
        links = list(ChannelStream.objects.filter(channel_id=int(cid)))
        original = [sid for sid in entry.get("original") or [] if any(cs.stream_id == sid for cs in links)]
        # Streams added since go after the original ones, in their current order.
        original += [cs.stream_id for cs in sorted(links, key=lambda x: x.order) if cs.stream_id not in original]
        _write_order(links, original)
        restored += 1
    save_state(data_dir, {"keep": state.get("keep") or []})
    return restored


def run_round(data_dir, log=None, extra_fails=None):
    """Called by the service each round. Returns {"reordered": this round, "week": the last seven days}."""
    n = apply(data_dir, log, extra_fails)
    state = load_state(data_dir)
    week = reordered_this_week(state, int(time.time()))
    return {"reordered": n, "week": week}
