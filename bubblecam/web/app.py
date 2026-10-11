"""Web UI and API. Meant to be reached over Tailscale (or on the Pi's own screen), not the open internet."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..core import App, encode_view
from ..media import extract_clip
from ..summary import feedings, recent_nights
from ..timelapse import TimelapseParams

STATIC = Path(__file__).parent / "static"


class ZoneIn(BaseModel):
    name: str
    kind: str
    x: float
    y: float
    w: float
    h: float


class TimelapseIn(BaseModel):
    start_ts: float
    end_ts: float
    max_speed: float | None = None
    min_speed: float | None = None
    ease_seconds: float | None = None
    driver: str | None = None
    overlay: bool = True


def create_app(core: App) -> FastAPI:
    api = FastAPI(title="BubbleCam")
    cfg = core.cfg
    db = core.db

    @api.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @api.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    # Live

    @api.get("/stream.mjpg")
    async def stream(overlay: bool = False, width: int | None = None):
        width = width or cfg.web.stream_width
        period = 1.0 / cfg.web.stream_fps

        async def gen():
            last = 0.0
            while True:
                frame, ts, scores = await run_in_threadpool(core.wait_frame, last)
                if frame is None or ts == last:
                    await asyncio.sleep(0.5)
                    continue
                last = ts
                jpg = await run_in_threadpool(encode_view, frame, scores, core.zones, width, overlay)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(period)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @api.get("/api/snapshot.jpg")
    def snapshot(overlay: bool = False, width: int | None = None):
        jpg = core.snapshot_jpeg(width, overlay)
        if jpg is None:
            raise HTTPException(503, "no frame yet")
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @api.get("/api/status")
    def status():
        s = core.scores
        disk = shutil.disk_usage(core.data)
        oldest = db.one("SELECT MIN(start_ts) AS t FROM segments")
        return {
            "fps": round(core.fps, 1),
            "zone": s.zone, "zones": s.zones, "tank": s.tank, "settling": s.settling,
            "zone_threshold": cfg.zone_events.threshold,
            "recording": core.recorder is not None,
            "encoder": core.recorder.encoder if core.recorder else None,
            "dropped": core.recorder.dropped if core.recorder else 0,
            "disk_free_gb": round(disk.free / 1e9, 1),
            "disk_total_gb": round(disk.total / 1e9, 1),
            "oldest_recording": oldest["t"] if oldest else None,
            "open_events": [k for k in core.events.open_ids],
            "presence": core.presence.status(),
        }

    # Zones

    @api.get("/api/zones")
    def get_zones():
        return core.zones

    @api.put("/api/zones")
    def put_zones(zones: list[ZoneIn]):
        try:
            core.update_zones([z.model_dump() for z in zones])
        except ValueError as e:
            raise HTTPException(400, str(e))
        return core.zones

    # Events

    @api.get("/api/events")
    def events(kind: str | None = None, starred: bool = False, before: float | None = None, limit: int = 50):
        sql = "SELECT * FROM events WHERE 1=1"
        args: list = []
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if starred:
            sql += " AND starred=1"
        if before:
            sql += " AND start_ts < ?"
            args.append(before)
        sql += " ORDER BY start_ts DESC LIMIT ?"
        args.append(min(limit, 500))
        rows = db.query(sql, tuple(args))
        for r in rows:
            r["has_thumb"] = bool(r.pop("thumb"))
        return rows

    def _event(ev_id: int) -> dict:
        ev = db.one("SELECT * FROM events WHERE id=?", (ev_id,))
        if not ev:
            raise HTTPException(404)
        return ev

    @api.post("/api/events/{ev_id}/star")
    def star(ev_id: int, on: bool = True):
        _event(ev_id)
        db.execute("UPDATE events SET starred=? WHERE id=?", (1 if on else 0, ev_id))
        return {"ok": True}

    @api.delete("/api/events/{ev_id}")
    def delete_event(ev_id: int):
        ev = _event(ev_id)
        for p in (ev["thumb"], str(core.data / "clips" / f"{ev_id}.mp4")):
            if p:
                Path(p).unlink(missing_ok=True)
        db.execute("DELETE FROM events WHERE id=?", (ev_id,))
        return {"ok": True}

    @api.get("/api/events/{ev_id}/thumb.jpg")
    def thumb(ev_id: int):
        ev = _event(ev_id)
        if not ev["thumb"] or not Path(ev["thumb"]).exists():
            raise HTTPException(404)
        return FileResponse(ev["thumb"], media_type="image/jpeg")

    @api.get("/api/events/{ev_id}/clip.mp4")
    async def clip(ev_id: int):
        ev = _event(ev_id)
        end = ev["end_ts"] or time.time()
        path = core.data / "clips" / f"{ev_id}.mp4"
        finished = ev["end_ts"] is not None and ev["end_ts"] < time.time() - 2
        if not (finished and path.exists()):
            ok = await run_in_threadpool(extract_clip, db, ev["start_ts"], end, path)
            if not ok:
                raise HTTPException(404, "footage not available")
        return FileResponse(path, media_type="video/mp4")

    # Activity

    @api.get("/api/activity")
    def activity(hours: float = 24):
        since = time.time() - hours * 3600
        return db.query("SELECT * FROM activity WHERE minute_ts >= ? ORDER BY minute_ts", (since,))

    @api.get("/api/nights")
    def nights(days: int = 14):
        t = cfg.timelapse
        return recent_nights(db, days, t.night_start, t.night_end)

    # Is he home?

    @api.get("/api/presence")
    def presence():
        return core.presence.status()

    @api.post("/api/presence/sample")
    def presence_sample(label: str):
        if core.latest is None:
            raise HTTPException(503, "no frame yet")
        try:
            core.presence.add_sample(core.latest, label)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return core.presence.status()

    @api.delete("/api/presence/samples")
    def presence_clear():
        core.presence.clear()
        return core.presence.status()

    @api.get("/api/presence/history")
    def presence_history(hours: float = 24):
        since = time.time() - hours * 3600
        before = db.one("SELECT ts, state FROM presence WHERE ts < ? ORDER BY ts DESC LIMIT 1", (since,))
        rows = db.query("SELECT ts, state FROM presence WHERE ts >= ? ORDER BY ts", (since,))
        return ([before] if before else []) + rows

    # Feedings

    @api.post("/api/feedings")
    def fed_now():
        return {"id": db.execute("INSERT INTO feedings(ts) VALUES (?)", (time.time(),)).lastrowid}

    @api.get("/api/feedings")
    def get_feedings(days: int = 14):
        return feedings(db, days)

    @api.delete("/api/feedings/{fid}")
    def delete_feeding(fid: int):
        db.execute("DELETE FROM feedings WHERE id=?", (fid,))
        return {"ok": True}

    # Notifications

    @api.get("/api/notify")
    def notify_settings():
        return core.notifier.settings()

    @api.put("/api/notify")
    def notify_update(changes: dict):
        return core.notifier.update(changes)

    @api.post("/api/notify/test")
    def notify_test():
        core.notifier.test()
        return {"ok": True}

    @api.post("/api/notify/morning")
    def notify_morning():
        core.notifier.morning_summary(force=True)
        return {"ok": True}

    # Timelapses

    @api.get("/api/timelapses")
    def timelapses():
        rows = db.query("SELECT * FROM timelapses ORDER BY id DESC LIMIT 100")
        for r in rows:
            r["has_file"] = bool(r["path"]) and Path(r["path"]).exists()
            r.pop("path")
        return rows

    @api.post("/api/timelapses")
    def make_timelapse(req: TimelapseIn):
        if req.end_ts <= req.start_ts:
            raise HTTPException(400, "end must be after start")
        p = core.timelapses.default_params()
        for field in ("max_speed", "min_speed", "ease_seconds", "driver"):
            value = getattr(req, field)
            if value is not None:
                setattr(p, field, value)
        p.overlay = req.overlay
        if p.driver not in ("zone", "tank", "both"):
            raise HTTPException(400, "driver must be zone, tank or both")
        return {"id": core.timelapses.queue(req.start_ts, req.end_ts, p)}

    @api.get("/api/timelapses/{tl_id}.mp4")
    def timelapse_file(tl_id: int):
        row = db.one("SELECT path FROM timelapses WHERE id=?", (tl_id,))
        if not row or not row["path"] or not Path(row["path"]).exists():
            raise HTTPException(404)
        return FileResponse(row["path"], media_type="video/mp4", filename=Path(row["path"]).name)

    @api.delete("/api/timelapses/{tl_id}")
    def delete_timelapse(tl_id: int):
        row = db.one("SELECT path, status FROM timelapses WHERE id=?", (tl_id,))
        if not row:
            raise HTTPException(404)
        if row["status"] == "rendering":
            raise HTTPException(409, "still rendering")
        if row["path"]:
            Path(row["path"]).unlink(missing_ok=True)
        db.execute("DELETE FROM timelapses WHERE id=?", (tl_id,))
        return {"ok": True}

    @api.get("/api/defaults")
    def defaults():
        t = cfg.timelapse
        return {"max_speed": t.max_speed, "min_speed": t.min_speed, "ease_seconds": t.ease_seconds,
                "driver": t.driver, "night_start": t.night_start, "night_end": t.night_end}

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api
