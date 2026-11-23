"""The house's local time, for everything the plugin does "at 04:30" or shows as "Last round 07:15".

Dispatcharr's processes run with Django's time zone (UTC), so time.localtime() and datetime.now() give UTC, not the
time on the wall. The time zone comes from Dispatcharr's own setting (Settings → System → Time zone), then from
the DISPATCHARR_DISPLAY_TZ setting (taken from the container's TZ before Django changes it), then UTC. It is looked up
again every ten minutes, so a change in Dispatcharr's settings is picked up without a restart.

Stdlib only (Django is used when present). The other files load it by path, as Dispatcharr loads the plugin's
files one by one rather than as a package.
"""

import os
import threading
import time
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # (no tz database: everything stays UTC)
    ZoneInfo = None

_RECHECK_S = 600
_cache = {"tz": None, "at": 0.0, "name": "UTC"}
_lock = threading.Lock()


def _zone(name):
    if not name or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(str(name))
    except Exception:
        return None


def _names():
    try:
        from core.models import CoreSettings
        yield CoreSettings.get_system_time_zone()
    except Exception:
        pass
    try:
        from django.conf import settings
        yield getattr(settings, "DISPATCHARR_DISPLAY_TZ", None)
    except Exception:
        pass
    yield os.environ.get("DISPATCHARR_TIME_ZONE")
    yield os.environ.get("TZ")


def local_tz():
    """The house's time zone (a tzinfo); UTC when nothing better is known."""
    now = time.time()
    with _lock:
        if _cache["tz"] is not None and now - _cache["at"] < _RECHECK_S:
            return _cache["tz"]
    tz, name = None, "UTC"
    for n in _names():
        tz = _zone(n)
        if tz is not None:
            name = str(n)
            break
    tz = tz or timezone.utc
    with _lock:
        _cache.update(tz=tz, at=now, name=name)
    return tz


def tz_name():
    local_tz()
    return _cache["name"]


def local_now(ts=None):
    """An aware datetime in the house's time zone (now, or at the epoch seconds [ts])."""
    return datetime.fromtimestamp(time.time() if ts is None else ts, local_tz())


def local_today(ts=None):
    """Today's date on the house's wall calendar."""
    return local_now(ts).date()


def strftime(fmt, ts=None):
    """time.strftime(fmt, time.localtime(ts)) in the house's time zone."""
    return local_now(ts).strftime(fmt)


def local_epoch(day, hour, minute):
    """Epoch seconds of [hour]:[minute] on [day] (a date) in the house's time zone."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=local_tz()).timestamp()

