"""Per night numbers and long term trends. Facts only: the app reports, it never advises."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .db import DB
from .presence import time_out

# A minute counts as "moving" for an animal when its areas moved for at least this many seconds.
ACTIVE_MINUTE_S = 5
# How long after feeding we look for a spotlighted animal to show up.
FEED_WINDOW_S = 8 * 3600


def night_bounds(day: datetime, night_start: str, night_end: str) -> tuple[float, float]:
    """The night that *ends* on `day`'s date."""
    end = datetime.combine(day.date(), datetime.strptime(night_end, "%H:%M").time())
    start = datetime.combine(day.date(), datetime.strptime(night_start, "%H:%M").time())
    if start >= end:
        start -= timedelta(days=1)
    return start.timestamp(), end.timestamp()


def fmt_minutes(m: float) -> str:
    m = round(m)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min" if m % 60 else f"{m // 60} h"


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def spot_summary(db: DB, spot, start: float, end: float) -> dict:
    rows = db.query("""SELECT minute_ts, moving_s FROM spot_activity
                       WHERE spot_id=? AND minute_ts >= ? AND minute_ts < ?""", (spot.id, start, end))
    active = [r["minute_ts"] for r in rows if (r["moving_s"] or 0) >= ACTIVE_MINUTE_S]
    visits = db.value("""SELECT COUNT(*) FROM events WHERE spot_id=? AND wrong=0
                         AND start_ts >= ? AND start_ts < ?""", (spot.id, start, end))
    out_s, outings = time_out(db, spot.id, start, end) if spot.home else (0.0, 0)
    return {"id": spot.id, "name": spot.name, "has_home": spot.home is not None,
            "moving_minutes": sum((r["moving_s"] or 0) for r in rows) / 60,
            "first": min(active) if active else None, "last": max(active) if active else None,
            "visits": visits, "out_minutes": out_s / 60, "outings": outings}


def tank_summary(db: DB, tank, start: float, end: float) -> dict:
    watched = sum(b - a for a, b in db.tank_intervals(tank.id, start, end))
    count = lambda kind: db.value("""SELECT COUNT(*) FROM events WHERE tank_id=? AND kind=? AND wrong=0
                                      AND start_ts >= ? AND start_ts < ?""", (tank.id, kind, start, end))
    best = db.one("""SELECT id, thumb FROM events WHERE tank_id=? AND wrong=0 AND thumb IS NOT NULL
                     AND start_ts >= ? AND start_ts < ?
                     ORDER BY (kind='spot') DESC, peak DESC LIMIT 1""", (tank.id, start, end))
    maint = db.value("""SELECT COALESCE(SUM(MIN(end_ts, ?) - MAX(start_ts, ?)), 0) FROM maintenance
                        WHERE tank_id=? AND end_ts > ? AND start_ts < ?""", (end, start, tank.id, start, end))
    return {"tank_id": tank.id, "name": tank.name, "watched_hours": watched / 3600,
            "spots": [spot_summary(db, s, start, end) for s in tank.spotlights],
            "bursts": count("burst"), "feedings": count("feeding"),
            "maintenance_minutes": (maint or 0) / 60,
            "best_event": best["id"] if best else None, "best_thumb": best["thumb"] if best else None}


def incidents(db: DB, start: float, end: float) -> list[dict]:
    return db.query("""SELECT kind, message, severity, start_ts, end_ts FROM health_log
                       WHERE start_ts < ? AND COALESCE(end_ts, ?) > ? ORDER BY start_ts""",
                    (end, time.time(), start))


def spot_line(s: dict) -> str:
    if s["outings"]:
        times = f"{s['outings']} time{'s' if s['outings'] != 1 else ''}"
        line = f"{s['name']} came out {times}, {fmt_minutes(s['out_minutes'])} in all"
    elif s["moving_minutes"] >= 1:
        line = f"{s['name']} was moving for {fmt_minutes(s['moving_minutes'])}"
    else:
        return f"{s['name']} stayed out of sight"
    if s["first"]:
        line += f" (first seen {clock(s['first'])}, last {clock(s['last'])})"
    return line


def morning_text(tanks: list[dict], problems: list[dict]) -> tuple[str, str, str | None]:
    """Title, body and best picture for the morning notification."""
    watched = [t for t in tanks if t["watched_hours"] >= 0.5]
    lines = []
    for t in watched:
        prefix = f"{t['name']}: " if len(watched) > 1 else ""
        bits = [spot_line(s) for s in t["spots"]]
        extra = []
        if t["feedings"]:
            extra.append(f"{t['feedings']} feeding{'s' if t['feedings'] != 1 else ''}")
        if t["bursts"]:
            extra.append(f"{t['bursts']} burst{'s' if t['bursts'] != 1 else ''} of activity")
        if extra:
            bits.append(", ".join(extra).capitalize())
        if not bits:
            bits.append(f"Watched for {t['watched_hours']:.0f} h")
        lines.append(prefix + ". ".join(bits) + ".")
    for p in problems:
        if p["severity"] == "problem":
            when = clock(p["start_ts"]) + (f" to {clock(p['end_ts'])}" if p["end_ts"] else ", still going")
            lines.append(f"{p['message']} ({when}).")
    if not watched:
        title = "No footage last night"
        if not lines:
            lines.append("The camera didn't record anything overnight.")
    else:
        first = watched[0]
        title = spot_line(first["spots"][0]) if first["spots"] else f"Last night in {first['name']}"
    image = next((t["best_thumb"] for t in watched if t["best_thumb"]), None)
    return title, " ".join(lines), image


def nights(db: DB, tank, days: int, night_start: str, night_end: str) -> list[dict]:
    out = []
    now = datetime.now()
    for back in range(-1, days + 1):  # -1: tonight, if it has started
        start, end = night_bounds(now - timedelta(days=back), night_start, night_end)
        if start > time.time():
            continue
        s = tank_summary(db, tank, start, end)
        if s["watched_hours"] > 0:
            s.pop("best_thumb")
            s["night"] = datetime.fromtimestamp(start).strftime("%a %d %b")
            s["start"], s["end"] = start, end
            s["in_progress"] = end > time.time()
            out.append(s)
    return out


def feedings(db: DB, tank, days: int) -> list[dict]:
    since = time.time() - days * 86400
    rows = db.query("""SELECT f.* FROM feedings f LEFT JOIN events e ON e.id = f.event_id
                       WHERE f.tank_id=? AND f.ts >= ? AND COALESCE(e.wrong, 0) = 0
                       ORDER BY f.ts DESC""", (tank.id, since))
    for r in rows:
        r["response"] = feeding_response(db, tank.id, r["ts"])
        r["emerged"] = emerged_after(db, tank, r["ts"])
    return rows


def feeding_response(db: DB, tank_id: int, ts: float) -> float | None:
    """How much busier the tank got in the two minutes after feeding than in the ten before (x times)."""
    m = int(ts // 60) * 60
    before = db.value("""SELECT AVG(tank_mean) FROM activity WHERE tank_id=? AND minute_ts >= ? AND minute_ts < ?""",
                      (tank_id, m - 600, m))
    after = db.value("""SELECT AVG(tank_mean) FROM activity WHERE tank_id=? AND minute_ts >= ? AND minute_ts <= ?""",
                     (tank_id, m, m + 120))
    if not before or after is None:
        return None
    return round(after / max(before, 1e-4), 1)


def emerged_after(db: DB, tank, ts: float) -> dict | None:
    """First spotlighted animal to show up after feeding: who, and how many seconds later."""
    limit = ts + FEED_WINDOW_S
    best = None
    for spot in tank.spotlights:
        times = []
        if spot.home:
            state = db.one("SELECT state FROM presence WHERE spot_id=? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                           (spot.id, ts))
            if state and state["state"] == "away":
                continue    # already out when fed
            p = db.value("""SELECT MIN(ts) FROM presence WHERE spot_id=? AND state='away'
                            AND ts > ? AND ts < ?""", (spot.id, ts, limit))
            if p:
                times.append(p)
        e = db.value("""SELECT MIN(start_ts) FROM events WHERE spot_id=? AND wrong=0
                        AND start_ts > ? AND start_ts < ?""", (spot.id, ts, limit))
        if e:
            times.append(e)
        if times and (best is None or min(times) - ts < best["after_s"]):
            best = {"name": spot.name, "after_s": min(times) - ts}
    return best


def trends(db: DB, tank, days: int, night_start: str, night_end: str) -> dict:
    """One point per night for the charts. Nights with maintenance or little footage are marked, not hidden."""
    points = []
    now = datetime.now()
    for back in range(days, 0, -1):
        start, end = night_bounds(now - timedelta(days=back - 1), night_start, night_end)
        if end > time.time():
            continue
        watched = sum(b - a for a, b in db.tank_intervals(tank.id, start, end))
        if watched < 3600:
            continue
        act = db.value("""SELECT AVG(tank_mean) FROM activity WHERE tank_id=? AND maintenance=0
                          AND minute_ts >= ? AND minute_ts < ?""", (tank.id, start, end))
        point = {"night": datetime.fromtimestamp(start).strftime("%d %b"), "start": start,
                 "tank_activity": act, "spots": {}}
        for spot in tank.spotlights:
            s = spot_summary(db, spot, start, end)
            point["spots"][spot.id] = {"moving_minutes": round(s["moving_minutes"], 1),
                                       "out_minutes": round(s["out_minutes"], 1) if spot.home else None}
        maint = db.value("SELECT COUNT(*) FROM maintenance WHERE tank_id=? AND end_ts > ? AND start_ts < ?",
                         (tank.id, start, end))
        point["maintenance"] = bool(maint)
        points.append(point)
    return {"spotlights": [{"id": s.id, "name": s.name, "has_home": s.home is not None} for s in tank.spotlights],
            "nights": points}
