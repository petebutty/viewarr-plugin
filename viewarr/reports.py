"""Reports from the TVs, acted on for the house.

Anyone watching can report a channel from Viewarr: the guide is for another channel, the guide times are out, the
stream is dead, the logo or the name is wrong. Each TV leaves its user's reports in their app data (key
``viewarr_reports``: {"updated_at": s, "items": [{"id", "at", "channel_id", "kind", "epg_data_id", "offset_min",
"name", "note", "stream_id"}]}). Every round this reads everyone's, and acts on each new one straight away:

  * guide_wrong: with an epg_data_id, the channel's guide is set to it; without one, a guide entry with the same name
    in another source is used when there is exactly one such match, else the report goes on the "needs a pick" list;
  * guide_times: a house-wide offset for that channel, published to the TVs (nothing changes in Dispatcharr);
  * dead: a failure for that stream, which Best stream first (streamorder.py) uses at once;
  * logo: the logo of another of the channel's streams, when one differs;
  * name: the channel's name;
  * other: noted in the log only.

Every change is kept in a change log with enough to reverse it (the last 200, in reports.json); admins undo one with
the Undo action, from Dispatcharr or from Viewarr. The house-wide results go to every user's app data (key
``viewarr_house``: offsets, the recent changes without their undo payloads, how many channels were reordered this
week, and the reports that still need someone to pick a guide entry).
"""

import importlib.util
import json
import os
import re
import time


def _load_clock():
    """clock.py beside this file (the house's local time; Dispatcharr's own processes run in UTC)."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("viewarr_clock", os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


clock = _load_clock()


def _streamorder():
    spec = importlib.util.spec_from_file_location("viewarr_streamorder", os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamorder.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

REPORTS_KEY = "viewarr_reports"
HOUSE_KEY = "viewarr_house"
STATE_FILE = "reports.json"
KINDS = {"guide_wrong", "guide_times", "dead", "logo", "name", "other"}
KEEP_CHANGES = 200
PUBLISH_CHANGES = 30
KEEP_PICKS = 20
PICK_DAYS = 14
ITEM_DAYS = 30
WEEK_S = 7 * 86400
MAX_BYTES = 64_000

_DECOR = re.compile(r"\b(UK|US|IE|HD|FHD|UHD|SD|4K|HEVC|H265|RAW|VIP|1080[PI]?|720[PI]?|576[PI]?)\b", re.I)
_NON = re.compile(r"[^a-z0-9]+")


# ---- State file

def _path(data_dir):
    return os.path.join(data_dir, STATE_FILE)


def load_state(data_dir):
    try:
        with open(_path(data_dir), encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st = st if isinstance(st, dict) else {}
    for key, empty in (("handled", {}), ("changes", []), ("offsets", {}), ("dead", {}), ("needs_pick", [])):
        if not isinstance(st.get(key), type(empty)):
            st[key] = empty
    return st


def save_state(data_dir, state):
    os.makedirs(data_dir, exist_ok=True)
    tmp = _path(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, _path(data_dir))


def prune(state, now):
    state["handled"] = {k: v for k, v in state["handled"].items() if now - v < ITEM_DAYS * 86400}
    state["changes"] = state["changes"][-KEEP_CHANGES:]
    state["dead"] = {k: [t for t in v if now - t < WEEK_S] for k, v in state["dead"].items()}
    state["dead"] = {k: v for k, v in state["dead"].items() if v}
    state["needs_pick"] = [p for p in state["needs_pick"] if now - p.get("at", 0) < PICK_DAYS * 86400][-KEEP_PICKS:]


def dead_fails(data_dir):
    """{stream id: [failure times]} from the TVs' dead reports, for Best stream first."""
    return {int(k): v for k, v in load_state(data_dir)["dead"].items() if str(k).isdigit()}


# ---- Reading the TVs' reports

def _valid(item):
    """A report item with the shape the TVs write, or None."""
    if not isinstance(item, dict) or item.get("kind") not in KINDS:
        return None
    rid, cid, at = item.get("id"), item.get("channel_id"), item.get("at")
    if not isinstance(rid, str) or not 1 <= len(rid) <= 64 or not isinstance(cid, int) or not isinstance(at, (int, float)):
        return None
    return item


def pending(state, now):
    """Every user's reports not handled yet, oldest first: [(username, item)]."""
    from apps.accounts.models import UserAppData
    out = []
    for row in UserAppData.objects.filter(key=REPORTS_KEY).select_related("user"):
        items = (row.value or {}).get("items") if isinstance(row.value, dict) else None
        for raw in items or []:
            item = _valid(raw)
            if item and item["id"] not in state["handled"] and now - item["at"] < ITEM_DAYS * 86400:
                out.append((row.user.username, item))
    return sorted(out, key=lambda x: x[1]["at"])


# ---- Acting on one report

def _change(state, item, by, channel, what, undo):
    entry = {"id": item["id"], "at": int(time.time()), "by": by, "channel_id": channel.id if channel else item["channel_id"],
             "channel_name": channel.name if channel else "", "what": what, "undo": undo, "undone": False}
    state["changes"].append(entry)
    return entry


def norm_name(name):
    """A channel or guide name without its decorations: "UK - BBC ONE SCOTLAND FHD" and "BBC One Scotland HD" agree."""
    return _NON.sub("", _DECOR.sub(" ", name or "").lower())


def match_guide(channel):
    """The one guide entry in another source whose name matches the channel's, or None when there isn't exactly one."""
    from apps.epg.models import EPGData
    want = norm_name(channel.name)
    if not want:
        return None
    current_source = channel.epg_data.epg_source_id if channel.epg_data_id and channel.epg_data else None
    found = []
    for e in EPGData.objects.select_related("epg_source").only("id", "name", "epg_source__name", "epg_source__is_active"):
        if e.epg_source_id == current_source or not (e.epg_source and e.epg_source.is_active):
            continue
        if norm_name(e.name) == want and has_listings(e.id):
            found.append(e)
    return found[0] if len(found) == 1 else None


def has_listings(epg_id):
    """True when the guide entry has programmes in the coming day (an empty entry is no fix for a wrong guide)."""
    from datetime import timedelta
    from django.utils import timezone
    from apps.epg.models import ProgramData
    now = timezone.now()
    return ProgramData.objects.filter(epg_id=epg_id, end_time__gt=now, start_time__lt=now + timedelta(hours=24)).exists()


def _set_guide(state, item, by, channel, epg, log):
    from apps.epg.models import EPGData
    prev = channel.epg_data_id
    channel.epg_data_id = epg.id
    channel.save(update_fields=["epg_data"])
    source = epg.epg_source.name if epg.epg_source_id else "no source"
    _change(state, item, by, channel, "Guide changed to %s (%s)" % (epg.name, source), {"kind": "epg", "channel_id": channel.id, "prev": prev})
    state["needs_pick"] = [p for p in state["needs_pick"] if p.get("channel_id") != channel.id]
    if log:
        log("reports: %s → guide %s (%s), reported by %s" % (channel.name, epg.name, source, by))


def _guide_wrong(state, item, by, channel, log):
    from apps.epg.models import EPGData
    epg = None
    if isinstance(item.get("epg_data_id"), int):
        epg = EPGData.objects.select_related("epg_source").filter(id=item["epg_data_id"]).first()
    elif item.get("epg_data_id") is None:
        epg = match_guide(channel)
    if epg is not None:
        _set_guide(state, item, by, channel, epg, log)
        return True
    state["needs_pick"] = [p for p in state["needs_pick"] if p.get("report_id") != item["id"]]
    state["needs_pick"].append({"report_id": item["id"], "at": int(item["at"]), "channel_id": channel.id,
                                "channel_name": channel.name, "by": by})
    if log:
        log("reports: no confident guide match for %s (reported by %s); needs a pick" % (channel.name, by))
    return False


def _guide_times(state, item, by, channel, log):
    mins = item.get("offset_min")
    if not isinstance(mins, int):
        return False
    key = str(channel.id)
    prev = state["offsets"].get(key)
    if mins:
        state["offsets"][key] = mins
    else:
        state["offsets"].pop(key, None)
    _change(state, item, by, channel, "Guide times moved %+d min" % mins, {"kind": "offset", "channel_id": channel.id, "prev": prev})
    return True


def _dead(state, item, by, channel, log):
    from apps.channels.models import Stream, ChannelStream
    sid = item.get("stream_id")
    if not isinstance(sid, int):
        # The TV couldn't tell which stream it was on (it never started, so Dispatcharr's status had none): the
        # channel's first stream is what Dispatcharr would have served.
        first = ChannelStream.objects.filter(channel=channel).order_by("order", "id").values_list("stream_id", flat=True).first()
        if first is None:
            return False
        sid = int(first)
    so = _streamorder()
    group = (getattr(channel.channel_group, "name", "") or "").strip().lower()
    if group in so.quiet_groups() or channel.id in so.tunarr_channel_ids():
        if log:
            log("reports: %s: %s is a quiet or Tunarr channel: not playing isn't the stream's fault, nothing changed" % (by, channel.name))
        return False
    stream = Stream.objects.filter(id=sid).only("name").first()
    at = int(item["at"])
    state["dead"].setdefault(str(sid), []).append(at)
    _change(state, item, by, channel, "Stream %s marked as failing" % (stream.name if stream else sid),
            {"kind": "dead", "stream_id": sid, "at": at})
    return True


def _logo(state, item, by, channel, log):
    from apps.channels.models import Logo
    current = channel.logo.url if channel.logo_id and channel.logo else None
    other = next((s.logo_url for s in channel.streams.all().order_by("channelstream__order")
                  if s.logo_url and s.logo_url != current), None)
    if not other:
        if log:
            log("reports: no other logo for %s (reported by %s)" % (channel.name, by))
        return False
    logo, _ = Logo.objects.get_or_create(url=other, defaults={"name": channel.name})
    prev = channel.logo_id
    channel.logo = logo
    channel.save(update_fields=["logo"])
    _change(state, item, by, channel, "Logo changed", {"kind": "logo", "channel_id": channel.id, "prev": prev})
    return True


def _name(state, item, by, channel, log):
    new = str(item.get("name") or "").strip()[:512]
    if not new or new == channel.name:
        return False
    prev = channel.name
    channel.name = new
    channel.save(update_fields=["name"])
    _change(state, item, by, channel, "Renamed from %s to %s" % (prev, new), {"kind": "name", "channel_id": channel.id, "prev": prev})
    return True


_HANDLERS = {"guide_wrong": _guide_wrong, "guide_times": _guide_times, "dead": _dead, "logo": _logo, "name": _name}


def handle(state, item, by, log=None):
    """One report → True when something was changed."""
    from apps.channels.models import Channel
    channel = Channel.objects.select_related("epg_data", "logo", "channel_group").filter(id=item["channel_id"]).first()
    if channel is None:
        return False
    handler = _HANDLERS.get(item["kind"])
    if handler is None:
        if log:
            log("reports: %s says about %s: %s" % (by, channel.name, (item.get("note") or "")[:200]))
        return False
    return handler(state, item, by, channel, log)


# ---- Undo

def _undo_payload(undo):
    from apps.channels.models import Channel
    kind = undo.get("kind")
    if kind == "dead":
        return "dead", None
    if kind == "offset":
        return "offset", None
    channel = Channel.objects.filter(id=undo.get("channel_id")).first()
    if channel is None:
        return None, "That channel is gone."
    if kind == "epg":
        channel.epg_data_id = undo.get("prev")
        channel.save(update_fields=["epg_data"])
    elif kind == "logo":
        channel.logo_id = undo.get("prev")
        channel.save(update_fields=["logo"])
    elif kind == "name":
        channel.name = undo.get("prev") or channel.name
        channel.save(update_fields=["name"])
    else:
        return None, "Nothing to undo for that change."
    return kind, None


def undo(data_dir, change_id):
    """Reverse one change from the log; returns (ok, message)."""
    state = load_state(data_dir)
    entry = next((c for c in state["changes"] if c.get("id") == change_id), None)
    if entry is None:
        return False, "No change with that id."
    if entry.get("undone"):
        return False, "Already undone."
    kind, err = _undo_payload(entry.get("undo") or {})
    if err:
        return False, err
    u = entry["undo"]
    if kind == "dead":
        times = state["dead"].get(str(u.get("stream_id")), [])
        state["dead"][str(u.get("stream_id"))] = [t for t in times if t != u.get("at")]
    elif kind == "offset":
        key = str(u.get("channel_id"))
        if u.get("prev"):
            state["offsets"][key] = u["prev"]
        else:
            state["offsets"].pop(key, None)
    entry["undone"] = True
    save_state(data_dir, state)
    return True, "Undone: %s (%s)" % (entry.get("what"), entry.get("channel_name"))


# ---- Publishing to the TVs

def house_value(state, order_week, now, quiet_on=True):
    changes = [{k: v for k, v in c.items() if k != "undo"} for c in state["changes"][-PUBLISH_CHANGES:]]
    try:
        quiet = _streamorder().quiet_channel_ids() if quiet_on else []
    except Exception:
        quiet = []
    value = {"updated_at": now, "offsets": dict(state["offsets"]), "changes": changes,
             "order": {"reordered": order_week}, "needs_pick": list(state["needs_pick"][-KEEP_PICKS:]),
             "quiet_channels": quiet}
    while _size(value) > MAX_BYTES and value["changes"]:
        value["changes"] = value["changes"][1:]
    return value


def _size(value):
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def publish(state, order_week, quiet=True):
    """viewarr_house to every active user (quiet False: no quiet channels, the event channels setting is off)."""
    from django.contrib.auth import get_user_model
    from apps.accounts.models import UserAppData
    value = house_value(state, order_week, int(time.time()), quiet_on=quiet)
    size = _size(value)
    for user in get_user_model().objects.filter(is_active=True).only("id"):
        UserAppData.objects.update_or_create(user=user, key=HOUSE_KEY, defaults={"value": value, "byte_size": size})


# ---- The round

def run_round(data_dir, log=None, auto=True):
    """Act on new reports (when auto is on) and remember them handled. Returns {"handled": n, "applied": n}."""
    now = int(time.time())
    state = load_state(data_dir)
    prune(state, now)
    handled = applied = 0
    if auto:
        for by, item in pending(state, now):
            try:
                if handle(state, item, by, log):
                    applied += 1
            except Exception as exc:
                if log:
                    log("reports: %s report on channel %s failed: %s" % (item["kind"], item["channel_id"], exc))
            state["handled"][item["id"]] = now
            handled += 1
    save_state(data_dir, state)
    if log and handled:
        log("Reports from the TVs: %d handled, %d changes made" % (handled, applied))
    return {"handled": handled, "applied": applied}


def status_text(data_dir):
    """The last ten changes, newest first, one per line."""
    state = load_state(data_dir)
    lines = []
    for c in reversed(state["changes"][-10:]):
        when = clock.strftime("%d %b %H:%M", c.get("at", 0))
        lines.append("%s  %s: %s (%s)%s  id %s" % (when, c.get("channel_name"), c.get("what"), c.get("by"),
                                                   "  [undone]" if c.get("undone") else "", c.get("id")))
    picks = len(state["needs_pick"])
    if picks:
        lines.append("%d report(s) still need someone to pick a guide entry." % picks)
    return "\n".join(lines) if lines else "No changes from reports yet."
