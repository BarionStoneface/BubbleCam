"""Per night numbers: used by the Activity tab and the morning notification."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .db import DB
from .presence import time_out

# A minute counts as "he was moving" when the watch zones moved for at least this many seconds.
ACTIVE_MINUTE_S = 5
# How long after feeding we look for him to come out.
FEED_WINDOW_S = 8 * 3600


def night_bounds(day: datetime, night_start: str, night_end: str) -> tuple[float, float]:
    """The night that *ends* on `day`'s date."""
    end = datetime.combine(day.date(), datetime.strptime(night_end, "%H:%M").time())
    start = datetime.combine(day.date(), datetime.strptime(night_start, "%H:%M").time())
    if start >= end:
        start -= timedelta(days=1)
    return start.timestamp(), end.timestamp()


def summarize(db: DB, start_ts: float, end_ts: float) -> dict:
    rows = db.query("SELECT minute_ts, zone_moving_s FROM activity WHERE minute_ts >= ? AND minute_ts < ?",
                    (start_ts, end_ts))
    moving = sum((r["zone_moving_s"] or 0) for r in rows)
    active = [r["minute_ts"] for r in rows if (r["zone_moving_s"] or 0) >= ACTIVE_MINUTE_S]
    visits = db.one("SELECT COUNT(*) AS n FROM events WHERE kind='zone' AND start_ts >= ? AND start_ts < ?",
                    (start_ts, end_ts))["n"]
    bursts = db.one("SELECT COUNT(*) AS n FROM events WHERE kind='burst' AND start_ts >= ? AND start_ts < ?",
                    (start_ts, end_ts))["n"]
    best = db.one("""SELECT id, thumb FROM events WHERE kind='zone' AND thumb IS NOT NULL
                     AND start_ts >= ? AND start_ts < ? ORDER BY peak DESC LIMIT 1""", (start_ts, end_ts))
    out_s, outings = time_out(db, start_ts, end_ts)
    return {
        "start": start_ts, "end": end_ts,
        "moving_minutes": moving / 60,
        "first": min(active) if active else None,
        "last": max(active) if active else None,
        "visits": visits, "bursts": bursts,
        "out_minutes": out_s / 60, "outings": outings,
        "best_event": best["id"] if best else None,
        "best_thumb": best["thumb"] if best else None,
        "has_data": bool(rows),
    }


def recent_nights(db: DB, days: int, night_start: str, night_end: str) -> list[dict]:
    out = []
    now = datetime.now()
    for back in range(-1, days + 1):  # -1: tonight, if it has started
        start, end = night_bounds(now - timedelta(days=back), night_start, night_end)
        if start > time.time():
            continue
        s = summarize(db, start, end)
        if s["has_data"]:
            s["night"] = datetime.fromtimestamp(start).strftime("%a %d %b")
            s["in_progress"] = end > time.time()
            s.pop("best_thumb")
            out.append(s)
    return out


def feedings(db: DB, days: int) -> list[dict]:
    since = time.time() - days * 86400
    rows = db.query("SELECT * FROM feedings WHERE ts >= ? ORDER BY ts DESC", (since,))
    for r in rows:
        r["emerged_after_s"] = emerged_after(db, r["ts"])
    return rows


def emerged_after(db: DB, ts: float) -> float | None:
    """Seconds from feeding until he came out: a 'left the cave' change or a pleco zone event,
    whichever is first. None if he hasn't yet (or was already out)."""
    limit = ts + FEED_WINDOW_S
    state = db.one("SELECT state FROM presence WHERE ts <= ? ORDER BY ts DESC LIMIT 1", (ts,))
    if state and state["state"] == "away":
        return None
    candidates = []
    p = db.one("SELECT ts FROM presence WHERE state='away' AND ts > ? AND ts < ? ORDER BY ts LIMIT 1", (ts, limit))
    if p:
        candidates.append(p["ts"])
    e = db.one("SELECT start_ts FROM events WHERE kind='zone' AND start_ts > ? AND start_ts < ? ORDER BY start_ts LIMIT 1",
               (ts, limit))
    if e:
        candidates.append(e["start_ts"])
    return min(candidates) - ts if candidates else None


def fmt_minutes(m: float) -> str:
    m = round(m)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def morning_text(s: dict) -> tuple[str, str]:
    """Title and body for the morning notification."""
    if not s["has_data"]:
        return "No footage last night", "The camera didn't record anything overnight. Worth checking on it."
    if s["moving_minutes"] < 1 and not s["visits"] and not s["outings"]:
        return "Quiet night", "He didn't show himself last night."
    clock = lambda ts: datetime.fromtimestamp(ts).strftime("%H:%M")
    if s["outings"]:
        title = f"Out {s['outings']} time{'s' if s['outings'] != 1 else ''}, {fmt_minutes(s['out_minutes'])} total"
    else:
        title = f"{s['visits']} visit{'s' if s['visits'] != 1 else ''}, {fmt_minutes(s['moving_minutes'])} moving"
    bits = []
    if s["first"]:
        bits.append(f"First seen {clock(s['first'])}, last seen {clock(s['last'])}.")
    if s["outings"]:
        bits.append(f"Moving for {fmt_minutes(s['moving_minutes'])}.")
    if s["bursts"]:
        bits.append(f"{s['bursts']} burst{'s' if s['bursts'] != 1 else ''} of tank activity.")
    bits.append("Timelapse is in the app.")
    return title, " ".join(bits)
