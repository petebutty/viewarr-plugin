"""Title logos for the guide, found once for the house (Stage 4 of the server work, 2026-11-17).

Viewarr shows a programme's title logo (its show's or film's logo artwork on TMDB) at the top of the guide. Each TV used
to look every title up itself as you moved along. Here the plugin works through the titles airing in the next day,
a few hundred new ones per round (your main channels first), with the app's own rules (data/media/ProgramArt.kt):

  * the title cleaned the same way ("New: ", "Live: ", "(1995)", "S01E02", the small "ᴸᶦᵛᵉ" letters taken off);
  * a TMDB multi-search, and only an exact name match counts; for a logo, stricter still: a UK / Irish / US show, or the
    only film with that name ("Breakfast": five films and a Philippine show, so no logo);
  * the logo: English first, then no language, PNGs first, best voted.

Hits and misses are remembered (misses for 7 days), so nothing is looked up twice. The map (cleaned, normalised title
→ logo path) goes to every user's app data in parts (viewarr_logos = {"v":1,"at":…,"parts":n}, then viewarr_logos_0…),
each under Dispatcharr's 128 KiB per value.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

TMDB = "https://api.themoviedb.org/3"
PER_ROUND = 250
MISS_DAYS = 7
HIT_DAYS = 60
PART_BYTES = 110_000
INDEX_KEY = "viewarr_logos"

_LEAD = re.compile(r"^(New|Live|Film|Premiere|Movie)\s*:\s*", re.I)
_YEAR = re.compile(r"\s*\(\d{4}\)\s*$")
_SMALL = re.compile("[ᴬ-ᵛ]+")  # the app's [ᴬ-ᵛ] (superscript letters: "ᴸᶦᵛᵉ", "ᴺᵉʷ")
_EP = re.compile(r"\.?\s*S\d+\s*E\d+$", re.I)
_NON = re.compile(r"[^a-z0-9]")


def clean(t):
    """ProgramArt.clean."""
    t = _LEAD.sub("", t or "")
    t = _YEAR.sub("", t)
    t = _SMALL.sub("", t)
    t = _EP.sub("", t)
    return t.strip().rstrip(".")


def norm(s):
    """ProgramArt.norm."""
    return _NON.sub("", (s or "").lower().replace("&", "and"))


def _get(url, key):
    headers = {"User-Agent": "Viewarr-Plugin/0.5 (+https://github.com/petebutty/viewarr-plugin)", "Accept": "application/json"}
    # (A v4 read token is long and dotted; a v3 key goes in the address.)
    if len(key) > 60 and "." in key:
        headers["Authorization"] = "Bearer " + key
    else:
        url += ("&" if "?" in url else "?") + "api_key=" + urllib.parse.quote(key)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


def lookup(title, key):
    """One cleaned title → logo path ("/abc.png") or None (ProgramArt.match + logoFor)."""
    q = urllib.parse.urlencode({"query": title, "include_adult": "false", "language": "en-GB"})
    res = _get(f"{TMDB}/search/multi?{q}", key).get("results") or []
    want = norm(title)
    same = [j for j in res if j.get("media_type") in ("tv", "movie") and norm(j.get("name") or j.get("title") or "") == want]
    tv = next((j for j in same if j.get("media_type") == "tv" and set(j.get("origin_country") or []) & {"GB", "IE", "US"}), None)
    films = [j for j in same if j.get("media_type") == "movie"]
    pick = tv or (films[0] if len(films) == 1 else None)
    if not pick or not pick.get("id"):
        return None
    imgs = _get(f"{TMDB}/{pick['media_type']}/{pick['id']}/images?include_image_language=en,null", key).get("logos") or []

    def rank(j):
        lang = j.get("iso_639_1")
        return (0 if lang == "en" else 1 if lang is None else 2,
                0 if (j.get("file_path") or "").endswith(".png") else 1,
                -(j.get("vote_average") or 0))
    imgs.sort(key=rank)
    return imgs[0].get("file_path") if imgs else None


def _cache_path(data_dir):
    return os.path.join(data_dir, "logo_cache.json")


def _load(data_dir):
    try:
        with open(_cache_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data_dir, cache):
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = _cache_path(data_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(tmp, _cache_path(data_dir))
    except Exception:
        pass


def upcoming_titles(hours=24):
    """Distinct cleaned titles airing in the next [hours], main channels first (lowest channel numbers)."""
    from datetime import datetime, timezone, timedelta
    from apps.channels.models import Channel
    from apps.epg.models import ProgramData

    now = datetime.now(timezone.utc)
    order = {}
    for c in Channel.objects.exclude(epg_data__isnull=True).only("epg_data_id", "channel_number"):
        n = float(c.channel_number) if c.channel_number is not None else 1e9
        if c.epg_data_id not in order or n < order[c.epg_data_id]:
            order[c.epg_data_id] = n
    seen, out = set(), []
    rows = ProgramData.objects.filter(epg_id__in=list(order), end_time__gt=now, start_time__lt=now + timedelta(hours=hours)) \
        .values_list("epg_id", "title")
    for epg_id, title in sorted(rows, key=lambda r: order.get(r[0], 1e9)):
        t = clean(title)
        k = norm(t)
        if len(t) >= 2 and k and k not in seen:
            seen.add(k)
            out.append((k, t))
    return out


def run_round(data_dir, key, log=None):
    """Look up to PER_ROUND new titles; returns the {norm title: logo path} map of everything found so far."""
    cache = _load(data_dir)  # norm → [logo path or "", checked_at]
    now = time.time()
    # Forget old misses / very old hits so they're looked at again.
    cache = {k: v for k, v in cache.items()
             if now - v[1] < (HIT_DAYS if v[0] else MISS_DAYS) * 86400}
    looked = 0
    if key:
        for k, t in upcoming_titles():
            if looked >= PER_ROUND:
                break
            if k in cache:
                continue
            try:
                path = lookup(t, key) or ""
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    if log:
                        log("logos: TMDB refused the key (%s)" % e.code)
                    break
                if e.code == 429:
                    break  # back off until the next round
                path = ""
            except Exception:
                continue  # a network blip: try this one again next round
            cache[k] = [path, now]
            looked += 1
            time.sleep(0.12)  # gentle: well under TMDB's limits
        _save(data_dir, cache)
    if log and looked:
        log("logos: %d titles looked up, %d logos known" % (looked, sum(1 for v in cache.values() if v[0])))
    return {k: v[0] for k, v in cache.items() if v[0]}


def publish(found, log=None):
    """The map to every active user's app data, in parts under the size limit."""
    from django.contrib.auth import get_user_model
    from apps.accounts.models import UserAppData

    parts, cur, size = [], {}, 2
    for k in sorted(found):
        entry = len(json.dumps({k: found[k]}, separators=(",", ":"))) - 1
        if cur and size + entry > PART_BYTES:
            parts.append(cur)
            cur, size = {}, 2
        cur[k] = found[k]
        size += entry
    if cur:
        parts.append(cur)
    index = {"v": 1, "at": int(time.time()), "parts": len(parts), "count": len(found), "base": "https://image.tmdb.org/t/p/w500"}
    users = list(get_user_model().objects.filter(is_active=True).only("id"))
    for user in users:
        for i, part in enumerate(parts):
            UserAppData.objects.update_or_create(
                user=user, key=f"{INDEX_KEY}_{i}",
                defaults={"value": part, "byte_size": len(json.dumps(part, separators=(",", ":")).encode("utf-8"))},
            )
        UserAppData.objects.update_or_create(
            user=user, key=INDEX_KEY, defaults={"value": index, "byte_size": len(json.dumps(index).encode("utf-8"))},
        )
        # Parts left from a bigger map before: gone.
        UserAppData.objects.filter(user=user, key__startswith=INDEX_KEY + "_").exclude(
            key__in=[f"{INDEX_KEY}_{i}" for i in range(len(parts))]).delete()
    return len(parts)
