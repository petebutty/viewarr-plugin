"""What the plugin did last, one line per job, for the Activity page in Viewarr (admins only).

Built from each round's status (server.py) and kept in ``_data/viewarr/activity.json`` so a job that runs less often
than every round (JustWatch, measuring at night) keeps showing its last run. Written to app data key
``viewarr_activity`` for every active admin (Dispatcharr user level 10 or more), only when something in it changed:

    {"v": 1, "at": 1795445820,
     "items": [{"id": "streams", "name": "Best stream first", "at": 1795445820,
                "text": "2 channels reordered (82 in the last 7 days)", "state": "ok"}]}

``state`` is "ok", "problem" (something failed or needs a look) or "off" (switched off in the settings). ``at`` is when
that job last ran (epoch seconds); Viewarr shows it in the TV's own time.
"""
import json
import os
import time

KEY = "viewarr_activity"
STATE_FILE = "activity.json"
ADMIN_LEVEL = 10

# (id, name shown, function switch in server.functions() or None)
JOBS = (
    ("sports", "Live now and Sports", None),
    ("recordings", "Sport recordings", "recordings"),
    ("streams", "Best stream first", None),
    ("reports", "Reports from the TVs", None),
    ("measure", "Measuring streams", None),
    ("timeshift", "Live timeshift", None),
    ("just_added", "Just added (JustWatch)", "just_added"),
    ("watch", "IPTV progress sync", "watch_sync"),
)

_last_written = {"blob": None}


def _n(x, word, plural=None):
    return "%d %s" % (x, word if x == 1 else (plural or word + "s"))


def _load(data_dir):
    try:
        with open(os.path.join(data_dir, STATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data_dir, items):
    path = os.path.join(data_dir, STATE_FILE)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(items, f)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def _lines(st, fn, problems, data_dir, now):
    """{job id: (at or None to keep the last one, text, state)} for what this round knows."""
    out = {}
    at = st.get("at") or now

    # Live now and Sports: every round.
    if problems.get("sports"):
        out["sports"] = (at, "Didn't finish: %s" % problems["sports"], "problem")
    elif not fn.get("live", True) and not fn.get("games", True):
        out["sports"] = (at, "Switched off", "off")
    else:
        src = st.get("sources") or {}
        missing = [label for key, label in (("sports", "sports data"), ("uk_tv", "UK TV listings")) if src and not src.get(key)]
        out["sports"] = (at, "%s on now, %s found; sent to %s (took %s s)" % (
            _n(st.get("live", 0), "live event"), _n(st.get("games", 0), "followed game"),
            _n(st.get("users", 0), "user"), st.get("seconds", "?"))
            + ("; from the guide only, no %s address added" % " or ".join(missing) if missing else ""), "ok")

    # Sport recordings: only a round that made one is news; otherwise the last one stays.
    if not fn.get("recordings", True):
        out["recordings"] = (at, "Switched off", "off")
    elif problems.get("recordings"):
        out["recordings"] = (at, "Failed: %s" % problems["recordings"], "problem")
    elif st.get("recorded"):
        out["recordings"] = (at, "%s made in Dispatcharr" % _n(st["recorded"], "recording"), "ok")

    o = st.get("order")
    if "order" not in st:
        pass  # (a round that failed early: the last line stays)
    elif o is None:
        out["streams"] = (at, "Didn't run this round (see Dispatcharr's log)", "problem")
    elif not fn.get("stream_order", True):
        out["streams"] = (at, "Switched off", "off")
    else:
        out["streams"] = (at, ("%s reordered" % _n(o.get("reordered", 0), "channel") if o.get("reordered")
                               else "Checked, nothing to change") + " (%d in the last 7 days)" % o.get("week", 0), "ok")

    r = st.get("reports")
    if "reports" not in st:
        pass
    elif r is None:
        out["reports"] = (at, "Didn't run this round (see Dispatcharr's log)", "problem")
    elif r.get("handled") or r.get("applied"):
        text = "%s handled, %s made" % (_n(r.get("handled", 0), "report"), _n(r.get("applied", 0), "change"))
        last = _last_report_change(data_dir)
        out["reports"] = (at, text + (": " + last if last else ""), "ok")

    m = st.get("measured")
    if not fn.get("measure", True):
        out["measure"] = (at, "Switched off", "off")
    elif m:
        hour = _local_hour()
        when = "Tonight so far" if hour < 7 else "Last night"
        text = "%s: %s measured, %d failed" % (when, _n(m.get("done_tonight", 0), "stream"), m.get("failed", 0))
        if m.get("left"):
            text += ", %d still to do" % m["left"]
        elif m.get("left") == 0:
            text += ", all done"
        out["measure"] = (None, text, "problem" if m.get("failed") else "ok")

    t = st.get("timeshift")
    if t is not None:
        if t.get("on") is False:
            out["timeshift"] = (at, "Switched off", "off")
        else:
            free, need = t.get("free_gb"), t.get("need_gb")
            where = "its own disk" if t.get("mapped") else "inside Dispatcharr's data folder"
            if free is None:
                out["timeshift"] = (at, "Can't write to /data/timeshift", "problem")
            elif need and free < need:
                out["timeshift"] = (at, "May run short: %d GB free in /data/timeshift, the settings can need about "
                                        "%d GB" % (free, need), "problem")
            elif t.get("refused"):
                out["timeshift"] = (at, "Couldn't buffer: %s" % "; ".join(
                    "%s" % v for v in list(t["refused"].values())[:2]), "problem")
            else:
                out["timeshift"] = (at, "Ready: /data/timeshift (%s, %d GB free), %s buffering now" % (
                    where, free, _n(t.get("buffers", 0), "channel")), "ok")

    ja = st.get("just_added")
    if not fn.get("just_added", True):
        out["just_added"] = (at, "Switched off", "off")
    elif (st.get("sources") or {"justwatch": "?"}).get("justwatch") == "":
        out["just_added"] = (at, "No Just added address added (plugin settings)", "off")
    elif isinstance(ja, dict) and ja.get("error"):
        out["just_added"] = (at, "Problem: %s" % ja["error"], "problem")
    elif isinstance(ja, dict) and "titles" in ja:
        out["just_added"] = (at, "%s from %s" % (_n(ja["titles"], "title"), _n(ja.get("services", 0), "service")), "ok")
    elif problems.get("just_added"):
        out["just_added"] = (at, "Failed: %s" % problems["just_added"], "problem")

    w = st.get("watch")
    if not fn.get("watch_sync", True):
        out["watch"] = (at, "Switched off", "off")
    elif problems.get("watch"):
        out["watch"] = (at, "Failed: %s" % problems["watch"], "problem")
    elif isinstance(w, dict) and w.get("users"):
        out["watch"] = (at, "%s: %d in, %d out" % (_n(w["users"], "user"), w.get("pulled", 0), w.get("pushed", 0)), "ok")
    elif isinstance(w, dict):
        out["watch"] = (at, "Nobody has it switched on", "ok")
    return out


def _local_hour():
    """The house's local hour (clock.py beside this file: Dispatcharr's own processes run in UTC)."""
    try:
        import importlib.util as ilu
        spec = ilu.spec_from_file_location("viewarr_clock_activity",
                                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "clock.py"))
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.local_now().hour
    except Exception:
        return time.localtime().tm_hour


def _last_report_at(data_dir):
    try:
        with open(os.path.join(data_dir, "reports.json"), encoding="utf-8") as f:
            return int(max((c.get("at", 0) for c in json.load(f).get("changes") or []), default=0)) or None
    except Exception:
        return None


def _last_report_change(data_dir):
    """The newest change made from a report, as "<channel>: <what>", or ""."""
    try:
        with open(os.path.join(data_dir, "reports.json"), encoding="utf-8") as f:
            changes = json.load(f).get("changes") or []
        c = max(changes, key=lambda c: c.get("at", 0)) if changes else None
        return ("%s: %s" % (c.get("channel_name") or "", c.get("what") or "")).strip(": ") if c else ""
    except Exception:
        return ""


def update(status, fn, data_dir, problems=None, log=None):
    """Merges this round into the saved lines and writes viewarr_activity to every admin when it changed."""
    now = int(time.time())
    saved = _load(data_dir)
    for job, (at, text, state) in _lines(status or {}, fn or {}, problems or {}, data_dir, now).items():
        prev = saved.get(job) or {}
        saved[job] = {"at": int(at or prev.get("at") or now), "text": text[:200], "state": state}
    # Jobs with nothing to report since the plugin started still get a line, so the page lists every job.
    waiting = {"recordings": "Nothing to record yet", "reports": "No reports yet",
               "just_added": "Waiting for the daily run (after 05:00)"}
    for job, text in waiting.items():
        if job not in saved:
            saved[job] = {"at": now, "text": text, "state": "ok"}
            # (What happened before this page existed, where it can be told.)
            if job == "just_added":
                try:
                    saved[job] = {"at": int(os.path.getmtime(os.path.join(data_dir, "just_added_day.txt"))),
                                  "text": "Updated from JustWatch", "state": "ok"}
                except OSError:
                    pass
            elif job == "reports":
                last = _last_report_change(data_dir)
                if last:
                    saved[job] = {"at": _last_report_at(data_dir) or now, "text": "Last change: " + last, "state": "ok"}
    for job in ("guide", "logos"):     # (removed in 0.6.8)
        saved.pop(job, None)
    _save(data_dir, saved)
    items = [dict(id=job, name=name, **saved[job]) for job, name, _ in JOBS if job in saved]
    blob = json.dumps(items, sort_keys=True)
    if blob == _last_written["blob"]:
        return len(items)
    value = {"v": 1, "at": now, "items": items}
    size = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    from django.contrib.auth import get_user_model
    from apps.accounts.models import UserAppData
    for user in get_user_model().objects.filter(is_active=True, user_level__gte=ADMIN_LEVEL).only("id"):
        UserAppData.objects.update_or_create(user=user, key=KEY, defaults={"value": value, "byte_size": size})
    _last_written["blob"] = blob
    return len(items)
