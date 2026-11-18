"""Live score alerts for Viewarr (Stage 6, 2026-11-17).

While a game in the house's results (followed teams and leagues, and the ESPN headline games) is live or about to
kick off, the service checks just those leagues' ESPN scoreboards every ~45 seconds (instead of every 5 minutes) and
notes what changed: kick-off, a goal / score change, half-time, full time. The last half hour's alerts go to every
user's app data (key ``viewarr_alerts``); Viewarr polls it while one of its user's teams or leagues is playing and shows
a small banner on whichever TV is on. Each TV keeps only the alerts for what its user follows.
"""

import json
import time
import urllib.request

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
KEY = "viewarr_alerts"
KEEP_S = 30 * 60

_prev = {}      # game id → (state, home score, away score, short detail)
_events = []    # newest last: {"id","game","at","kind","text","home","away","league"}
_live = {}      # game id → its score now, for the ticker's Live scores: {"text","detail","lp","teams","at"}


def _fetch(lp):
    req = urllib.request.Request(f"{ESPN}/{lp}/scoreboard", headers={"User-Agent": "Viewarr-Plugin/0.5 (+https://github.com/petebutty/viewarr-plugin)", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _side(comp, where):
    for c in comp.get("competitors") or []:
        if c.get("homeAway") == where:
            t = c.get("team") or {}
            return (t.get("shortDisplayName") or t.get("displayName") or t.get("name") or ""), str(c.get("score") or "0")
    return "", "0"


def watch_list(games, now):
    """The games worth checking quickly now: live, or kicking off within 10 minutes."""
    return {gid: g for gid, g in games.items()
            if g.get("lp") and (g.get("state") == "in" or (g.get("state") == "pre" and 0 <= (g.get("start") or 0) - now < 600))}


def check(games, log=None):
    """One quick look at the live games' scoreboards; returns True when there are new alerts."""
    now = time.time()
    live = watch_list(games, now)
    if not live:
        return False
    new = False
    for lp in sorted({g["lp"] for g in live.values()}):
        try:
            board = _fetch(lp)
        except Exception as exc:
            if log:
                log("alerts: %s scoreboard failed: %s" % (lp, type(exc).__name__))
            continue
        for ev in board.get("events") or []:
            gid = str(ev.get("id") or "")
            if gid not in live:
                continue
            comp = (ev.get("competitions") or [{}])[0]
            st = ((comp.get("status") or ev.get("status") or {}).get("type") or {})
            state, short = st.get("state") or "", st.get("shortDetail") or st.get("detail") or ""
            home, hs = _side(comp, "home")
            away, aw = _side(comp, "away")
            before = _prev.get(gid)
            _prev[gid] = (state, hs, aw, short)
            # (The score as it stands, for the ticker: live games only; a finished one stays 30 minutes.)
            if state == "in" or (state == "post" and before is not None and before[0] == "in"):
                _live[gid] = {"text": "%s %s–%s %s" % (home, hs, aw, away), "detail": short, "lp": live[gid].get("lp") or "",
                              "teams": [live[gid].get("home") or "", live[gid].get("away") or ""], "at": int(now), "state": state}
                new = True
            if before is None:
                continue  # first sight this run: nothing to compare with
            b_state, b_hs, b_aw, b_short = before
            score = "%s %s–%s %s" % (home, hs, aw, away)
            kind = text = None
            # By sport (no "GOAL!" alert for every basket): goals for football and
            # hockey; touchdowns / field goals for American football; runs for baseball; no scoring alerts at all for
            # basketball (it scores every minute), just the start, half-time and the result.
            sport = (live[gid].get("lp") or "").split("/")[0]
            start_word = {"basketball": "Tip-off", "hockey": "Puck drop", "baseball": "First pitch"}.get(sport, "Kick-off")
            end_word = "Full time" if sport in ("soccer", "rugby", "rugby-league") else "Final"
            half = short.upper().startswith("HT") or short.lower().startswith("halftime")
            b_half = b_short.upper().startswith("HT") or b_short.lower().startswith("halftime")
            if b_state == "pre" and state == "in":
                kind, text = "kickoff", "%s: %s v %s" % (start_word, home, away)
            elif state == "in" and (hs, aw) != (b_hs, b_aw) and sport != "basketball":
                scorer = home if hs != b_hs else away
                try:
                    gain = (int(hs) - int(b_hs)) if hs != b_hs else (int(aw) - int(b_aw))
                except (TypeError, ValueError):
                    gain = 0
                if sport in ("soccer", "hockey"):
                    word = "GOAL!"
                elif sport == "football":
                    word = "TOUCHDOWN!" if gain >= 6 else "Field goal" if gain == 3 else "Score"
                elif sport == "baseball":
                    word = "Run%s" % ("s" if gain > 1 else "")
                else:
                    word = "Score"
                kind, text = "goal", "%s %s · %s%s" % (word, scorer, score, (" (%s)" % short) if short else "")
            elif state == "in" and half and not b_half:
                kind, text = "ht", "Half-time: " + score
            elif b_state == "in" and state == "post":
                kind, text = "ft", "%s: %s" % (end_word, score)
            if kind:
                _events.append({"id": "%s-%d" % (gid, int(now)), "game": gid, "at": int(now), "kind": kind, "text": text,
                                "home": home, "away": away, "league": live[gid].get("league") or "",
                                "lp": live[gid].get("lp") or "", "teams": [live[gid].get("home") or "", live[gid].get("away") or ""]})
                new = True
                if log:
                    log("alerts: " + text)
    return new


def publish(log=None):
    """The last half hour's alerts to every active user."""
    from django.contrib.auth import get_user_model
    from django.db import close_old_connections
    from apps.accounts.models import UserAppData

    now = time.time()
    _events[:] = [e for e in _events if now - e["at"] < KEEP_S][-40:]
    for gid in [g for g, v in _live.items() if v.get("state") == "post" and now - v["at"] > KEEP_S or now - v["at"] > 4 * 3600]:
        _live.pop(gid, None)
    value = {"v": 1, "at": int(now), "events": list(_events), "live": [dict(v, game=g) for g, v in _live.items()]}
    size = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    close_old_connections()
    for user in get_user_model().objects.filter(is_active=True).only("id"):
        UserAppData.objects.update_or_create(user=user, key=KEY, defaults={"value": value, "byte_size": size})
    close_old_connections()
