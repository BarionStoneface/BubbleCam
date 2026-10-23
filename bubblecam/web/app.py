"""Web UI and API. Reached over Tailscale (or on the Pi's own screen), never the open internet."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import pi
from ..update import Updater
from ..core import App, encode_view
from ..media import FrameReader, readable
from ..recorder import act_path, read_act
from ..summary import feedings, night_bounds, nights, tank_summary, trends
from ..timelapse import TimelapseParams

STATIC = Path(__file__).parent / "static"
TAG_MAX = 40


class Rect(BaseModel):
    x: float
    y: float
    w: float
    h: float


class SpotLayout(BaseModel):
    id: int
    areas: list[Rect] = []
    home: Rect | None = None


class LayoutIn(BaseModel):
    ignore: list[Rect] = []
    bubbles: Rect | None = None
    spotlights: list[SpotLayout] = []


class ModeIn(BaseModel):
    mode: str


class FreeIn(BaseModel):
    name: str = ""


class TankIn(BaseModel):
    name: str = ""


class TankPatch(BaseModel):
    name: str | None = None
    settings: dict | None = None


class NameIn(BaseModel):
    name: str


class SpotPatch(BaseModel):
    name: str | None = None
    still_label: str | None = None
    moving_label: str | None = None


class TimelapseIn(BaseModel):
    tank_id: int | None = None
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
    T = cfg.timelapse

    def tank_or_404(tank_id: int):
        tank = core.tanks.get(tank_id)
        if tank is None:
            raise HTTPException(404, "no such tank")
        return tank

    def bad(e: Exception):
        raise HTTPException(400, str(e))

    @api.get("/")
    def index():
        # Stamp the script and style links with their file times, so a phone that kept an old copy
        # picks up the new one straight after an update.
        html = (STATIC / "index.html").read_text()
        for name in ("app.js", "style.css"):
            v = int((STATIC / name).stat().st_mtime)
            html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @api.get("/sw.js")
    def service_worker():
        return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    updater = Updater()

    @api.get("/api/update")
    async def update_check(force: bool = False):
        return await run_in_threadpool(updater.check, force)

    @api.post("/api/update")
    async def update_apply():
        r = await run_in_threadpool(updater.apply)
        if not r["ok"]:
            raise HTTPException(409, r["error"])
        return r

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
                jpg = await run_in_threadpool(encode_view, frame, scores, core.layout() if overlay else None,
                                              width, overlay)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(period)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @api.get("/api/snapshot.jpg")
    def snapshot(overlay: bool = False, width: int | None = None):
        jpg = core.snapshot_jpeg(width, overlay)
        if jpg is None:
            raise HTTPException(503, "no picture yet")
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    def spot_live(tank) -> list[dict]:
        s = core.scores
        out = []
        for spot in tank.spotlights:
            value = s.spots.get(spot.id, 0.0)
            mode = core.stats.modes.get(spot.id, "quiet")
            out.append({"id": spot.id, "name": spot.name, "moving": value >= tank.zone_threshold,
                        "value": value, "presence": core.presence.status(spot.id), "has_home": spot.home is not None,
                        "has_areas": bool(spot.areas), "mode": mode,
                        "doing": spot.still_label if mode == "still" else spot.moving_label if mode == "moving" else None})
        return out

    def perf() -> dict:
        import os
        try:
            load = os.getloadavg()[0] / (os.cpu_count() or 1)
        except (OSError, AttributeError):
            load = None
        from ..health import cpu_temp
        return {"loop_ms": round(core.loop_ms, 1), "analyze_ms": round(core.analyze_ms, 1),
                "budget_ms": round(1000 / max(1, cfg.camera.fps), 1), "stride": core.stride,
                "cpu_load": round(load, 2) if load is not None else None, "temp_c": cpu_temp(),
                "drop_rate": round(core.drop_rate(), 3)}

    @api.get("/api/status")
    def status():
        disk = shutil.disk_usage(core.data)
        tank = core.tank
        return {
            "fps": round(core.fps, 1),
            "perf": perf(),
            "health": core.health.status(),
            "view": core.view.status(),
            "tank": {"id": tank.id, "name": tank.name} if tank else None,
            "spotlights": spot_live(tank) if tank else [],
            "bubbles": core.danger.bubble_status(tank) if tank else None,
            "tank_motion": core.scores.tank,
            "settling": core.scores.settling,
            "maintenance_until": core.maintenance_until(core.tank_id),
            "mode": core.mode,
            "version": updater.current["version"],
            "update_available": bool(updater.info.get("available")),
            "free": core.free.status(),
            "recording": core.active_recorder() is not None,
            "encoder": core.recorder.encoder if core.recorder else None,
            "disk_free_gb": round(disk.free / 1e9, 1),
            "disk_total_gb": round(disk.total / 1e9, 1),
            "tanks": [{"id": t.id, "name": t.name} for t in core.tanks.all()],
        }

    # Tanks

    @api.get("/api/tanks")
    def tanks():
        start, end = night_bounds(datetime.now(), T.night_start, T.night_end)
        out = []
        for t in core.tanks.all():
            s = tank_summary(db, t, start, end)
            s.pop("best_thumb")
            out.append({"id": t.id, "name": t.name, "live": t.id == core.tank_id,
                        "spotlights": spot_live(t) if t.id == core.tank_id else
                        [{"id": sp.id, "name": sp.name} for sp in t.spotlights],
                        "last_night": s, "maintenance_until": core.maintenance_until(t.id)})
        return out

    @api.post("/api/tanks")
    def new_tank(body: TankIn):
        try:
            return core.new_tank_here(body.name).to_dict()
        except ValueError as e:
            bad(e)

    @api.patch("/api/tanks/{tank_id}")
    def patch_tank(tank_id: int, body: TankPatch):
        tank_or_404(tank_id)
        try:
            if body.name is not None:
                core.tanks.rename(tank_id, body.name)
            if body.settings:
                settings = {k: v for k, v in body.settings.items() if k not in ("ignore", "bubbles")}
                core.tanks.update_settings(tank_id, settings)
        except ValueError as e:
            bad(e)
        return core.tanks.get(tank_id).to_dict()

    @api.delete("/api/tanks/{tank_id}")
    def delete_tank(tank_id: int):
        tank_or_404(tank_id)
        core.tanks.delete(tank_id)
        core.view.forget(tank_id)
        return {"ok": True}

    @api.post("/api/tanks/{tank_id}/this-is")
    def this_is(tank_id: int):
        try:
            core.this_is(tank_id)
        except ValueError as e:
            bad(e)
        return {"ok": True}

    @api.get("/api/tanks/{tank_id}/thumb.jpg")
    def tank_thumb(tank_id: int):
        p = core.data / "tanks" / str(tank_id) / "last.jpg"
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @api.get("/api/tanks/{tank_id}/layout")
    def get_layout(tank_id: int):
        tank = tank_or_404(tank_id)
        return core.layout() if tank_id == core.tank_id else tank.to_dict()

    @api.put("/api/tanks/{tank_id}/layout")
    def put_layout(tank_id: int, body: LayoutIn):
        tank = tank_or_404(tank_id)
        if tank_id != core.tank_id:
            raise HTTPException(409, "the camera isn't looking at this tank right now")
        core.rebase()
        known = {s.id for s in tank.spotlights}
        for s in body.spotlights:
            if s.id in known:
                core.tanks.update_spotlight(s.id, areas=[r.model_dump() for r in s.areas],
                                            home=s.home.model_dump() if s.home else None)
        core.tanks.update_settings(tank_id, {"ignore": [r.model_dump() for r in body.ignore],
                                             "bubbles": body.bubbles.model_dump() if body.bubbles else None})
        return core.layout()

    @api.post("/api/tanks/{tank_id}/spotlights")
    def add_spotlight(tank_id: int, body: NameIn):
        tank_or_404(tank_id)
        try:
            return core.add_spotlight(tank_id, body.name).to_dict()
        except ValueError as e:
            bad(e)

    @api.patch("/api/spotlights/{spot_id}")
    def patch_spotlight(spot_id: int, body: SpotPatch):
        core.tanks.update_spotlight(spot_id, name=body.name, still_label=body.still_label,
                                    moving_label=body.moving_label)
        return {"ok": True}

    @api.delete("/api/spotlights/{spot_id}")
    def delete_spotlight(spot_id: int):
        core.tanks.delete_spotlight(spot_id)
        core.presence.clear(spot_id)
        return {"ok": True}

    @api.post("/api/spotlights/{spot_id}/teach")
    def teach(spot_id: int, label: str):
        tank = core.tank
        if tank is None or tank.spotlight(spot_id) is None:
            raise HTTPException(409, "the camera isn't looking at this animal's tank right now")
        if core.latest is None:
            raise HTTPException(503, "no picture yet")
        try:
            core.presence.add_sample(tank.id, spot_id, core.latest, label)
        except ValueError as e:
            bad(e)
        return core.presence.status(spot_id)

    @api.delete("/api/spotlights/{spot_id}/examples")
    def forget_examples(spot_id: int):
        core.presence.clear(spot_id)
        return core.presence.status(spot_id)

    # A tank's day: everything the day bar draws

    @api.get("/api/tanks/{tank_id}/day")
    def day(tank_id: int, start: float, end: float):
        tank = tank_or_404(tank_id)
        names = {s.id: s.name for s in tank.spotlights}
        evs = db.query("""SELECT id, kind, spot_id, detail, start_ts, end_ts, wrong, starred, thumb IS NOT NULL AS has_thumb
                          FROM events WHERE tank_id=? AND start_ts < ? AND COALESCE(end_ts, ?) > ?
                          ORDER BY start_ts""", (tank_id, end, time.time(), start))
        for e in evs:
            e["spot_name"] = names.get(e["spot_id"])
        presence = {}
        for s in tank.spotlights:
            if not s.home:
                continue
            before = db.one("SELECT ts, state FROM presence WHERE spot_id=? AND ts < ? ORDER BY ts DESC LIMIT 1",
                            (s.id, start))
            rows = db.query("SELECT ts, state FROM presence WHERE spot_id=? AND ts >= ? AND ts < ? ORDER BY ts",
                            (s.id, start, end))
            presence[s.id] = ([before] if before else []) + rows
        return {
            "spotlights": [{"id": s.id, "name": s.name, "slot": s.slot} for s in tank.spotlights],
            "events": evs,
            "presence": presence,
            "feedings": db.query("SELECT id, ts, source FROM feedings WHERE tank_id=? AND ts >= ? AND ts < ?",
                                 (tank_id, start, end)),
            "maintenance": db.query("""SELECT start_ts, end_ts FROM maintenance WHERE tank_id=?
                                       AND end_ts > ? AND start_ts < ?""", (tank_id, start, end)),
            "watched": db.tank_intervals(tank_id, start, min(end, time.time())),
            "footage_from": db.value("SELECT MIN(start_ts) FROM segments"),
        }

    @api.get("/api/frame.jpg")
    def frame_at(ts: float, width: int = 960):
        seg = db.one("""SELECT * FROM segments WHERE start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?)
                        ORDER BY start_ts DESC LIMIT 1""", (ts, ts))
        if not seg or not Path(seg["path"]).exists():
            raise HTTPException(404, "no footage at that time")
        act = read_act(act_path(Path(seg["path"])))
        if not len(act):
            raise HTTPException(404, "no footage at that time")
        idx = int(min(np.searchsorted(act["ts"], ts), len(act) - 1))
        frame = None
        for candidate in (idx, max(0, idx - int(seg["fps"] * 3))):   # the newest seconds may not be on disk yet
            reader = FrameReader(Path(seg["path"]), seg["fps"])
            try:
                frame = reader.get(candidate)
            finally:
                reader.close()
            if frame is not None:
                break
        if frame is None:
            raise HTTPException(404, "no footage at that time")
        small = cv2.resize(frame, (width, round(frame.shape[0] * width / frame.shape[1])), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @api.get("/api/footage.mp4")
    async def footage(start: float, end: float):
        if end <= start or end - start > 3600:
            raise HTTPException(400, "pick up to an hour")
        p = await run_in_threadpool(core.storage.footage, start, end)
        if p is None:
            raise HTTPException(404, "no footage at that time")
        return FileResponse(p, media_type="video/mp4")

    # Feeding and maintenance

    @api.post("/api/tanks/{tank_id}/fed")
    def fed(tank_id: int):
        tank_or_404(tank_id)
        now = time.time()
        # An automatic feeding detected moments ago is the same feeding: confirm it instead.
        auto = db.one("SELECT id FROM feedings WHERE tank_id=? AND source='auto' AND ts > ?", (tank_id, now - 900))
        if auto:
            db.execute("UPDATE feedings SET source='button' WHERE id=?", (auto["id"],))
            return {"id": auto["id"], "merged": True}
        return {"id": db.execute("INSERT INTO feedings(tank_id, ts, source) VALUES (?,?, 'button')",
                                 (tank_id, now)).lastrowid}

    @api.get("/api/tanks/{tank_id}/feedings")
    def get_feedings(tank_id: int, days: int = 14):
        return feedings(db, tank_or_404(tank_id), days)

    @api.delete("/api/feedings/{fid}")
    def delete_feeding(fid: int):
        row = db.one("SELECT event_id FROM feedings WHERE id=?", (fid,))
        if row and row["event_id"]:
            db.execute("UPDATE events SET wrong=1 WHERE id=?", (row["event_id"],))
        db.execute("DELETE FROM feedings WHERE id=?", (fid,))
        return {"ok": True}

    @api.post("/api/tanks/{tank_id}/maintenance")
    def maintenance(tank_id: int, minutes: float = 60):
        tank_or_404(tank_id)
        return {"until": core.start_maintenance(tank_id, max(5, min(minutes, 480)))}

    @api.delete("/api/tanks/{tank_id}/maintenance")
    def end_maintenance(tank_id: int):
        core.stop_maintenance(tank_id)
        return {"ok": True}

    # Nights and trends

    @api.get("/api/tanks/{tank_id}/nights")
    def tank_nights(tank_id: int, days: int = 14):
        rows = nights(db, tank_or_404(tank_id), days, T.night_start, T.night_end)
        for n in rows:
            date = datetime.fromtimestamp(n["start"]).strftime("%Y-%m-%d")
            n["date"] = date
            n["trails"] = "ready" if core.trails.path(tank_id, date).exists() else \
                "none" if core.trails.path(tank_id, date).with_suffix(".none").exists() else "pending"
        return rows

    @api.get("/api/tanks/{tank_id}/heatmap.png")
    def heatmap(tank_id: int, period: str = "night", days: int = 7):
        tank_or_404(tank_id)
        if period not in ("day", "night"):
            raise HTTPException(400, "period is day or night")
        bg = core.latest if tank_id == core.tank_id else None
        if bg is None:
            p = core.data / "tanks" / str(tank_id) / "last.jpg"
            bg = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) if p.exists() else None
        png = core.heat.render(tank_id, period, min(days, 90), bg)
        if png is None:
            raise HTTPException(404, "no movement recorded yet")
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=300"})

    @api.get("/api/tanks/{tank_id}/trails.png")
    def trails(tank_id: int, date: str):
        tank_or_404(tank_id)
        datetime.strptime(date, "%Y-%m-%d")
        p = core.trails.path(tank_id, date)
        if not p.exists():
            if p.with_suffix(".none").exists():
                raise HTTPException(404, "not enough footage that night")
            core.trails.request(tank_id, date)
            raise HTTPException(202, "being drawn; try again in a minute")
        return FileResponse(p, media_type="image/png", headers={"Cache-Control": "max-age=86400"})

    @api.get("/api/tanks/{tank_id}/trends")
    def tank_trends(tank_id: int, days: int = 30):
        return trends(db, tank_or_404(tank_id), min(days, 365), T.night_start, T.night_end)

    # Moments

    def moment_rows(where: str, args: tuple, limit: int) -> list[dict]:
        rows = db.query(f"""SELECT e.*, s.name AS spot_name, t.name AS tank_name FROM events e
                            LEFT JOIN spotlights s ON s.id = e.spot_id LEFT JOIN tanks t ON t.id = e.tank_id
                            WHERE {where} ORDER BY e.start_ts DESC LIMIT ?""", (*args, limit))
        tags: dict[int, list[str]] = {}
        if rows:
            ids = [r["id"] for r in rows]
            for t in db.query(f"SELECT * FROM event_tags WHERE event_id IN ({','.join('?' * len(ids))})", tuple(ids)):
                tags.setdefault(t["event_id"], []).append(t["tag"])
        for r in rows:
            r["has_thumb"] = bool(r.pop("thumb"))
            r["has_clip"] = bool(r.pop("clip"))
            r["tags"] = sorted(tags.get(r["id"], []))
        return rows

    @api.get("/api/moments")
    def moments(tank_id: int | None = None, kind: str | None = None, spot_id: int | None = None,
                tag: str | None = None, starred: bool = False, wrong: bool = False, night_only: bool = False,
                before: float | None = None, limit: int = 40):
        where, args = ["e.wrong = ?"], [1 if wrong else 0]
        if tank_id is not None:
            where.append("e.tank_id = ?")
            args.append(tank_id)
        if kind:
            where.append("e.kind = ?")
            args.append(kind)
        if spot_id is not None:
            where.append("e.spot_id = ?")
            args.append(spot_id)
        if tag:
            where.append("EXISTS (SELECT 1 FROM event_tags g WHERE g.event_id = e.id AND g.tag = ?)")
            args.append(tag)
        if starred:
            where.append("e.starred = 1")
        if night_only:
            where.append("(CAST(strftime('%H', e.start_ts, 'unixepoch', 'localtime') AS INTEGER) >= 21 "
                         "OR CAST(strftime('%H', e.start_ts, 'unixepoch', 'localtime') AS INTEGER) < 6)")
        if before:
            where.append("e.start_ts < ?")
            args.append(before)
        return moment_rows(" AND ".join(where), tuple(args), min(limit, 200))

    def moment(ev_id: int) -> dict:
        ev = db.one("SELECT * FROM events WHERE id=?", (ev_id,))
        if not ev:
            raise HTTPException(404)
        return ev

    @api.get("/api/moments/{ev_id}/thumb.jpg")
    def moment_thumb(ev_id: int):
        ev = moment(ev_id)
        if not ev["thumb"] or not Path(ev["thumb"]).exists():
            raise HTTPException(404)
        return FileResponse(ev["thumb"], media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @api.get("/api/moments/{ev_id}/clip.mp4")
    async def moment_clip(ev_id: int):
        ev = moment(ev_id)
        name = f"bubblecam_{datetime.fromtimestamp(ev['start_ts']).strftime('%Y-%m-%d_%H%M%S')}_{ev['kind']}.mp4"
        clip = Path(ev["clip"]) if ev["clip"] else None
        if clip and clip.exists():
            if await run_in_threadpool(readable, clip):
                return FileResponse(clip, media_type="video/mp4", filename=name, content_disposition_type="inline")
            # Cut from footage that can't be read: cut it again (from the proxy if need be).
            clip.unlink(missing_ok=True)
            db.execute("UPDATE events SET clip=NULL WHERE id=?", (ev_id,))
        # Not cut yet (still happening, or just ended): cut it from the raw footage now.
        p = await run_in_threadpool(core.storage.footage, ev["start_ts"], ev["end_ts"] or time.time())
        if p is None:
            raise HTTPException(410, "The recording for this moment has been cleared out to make room.")
        return FileResponse(p, media_type="video/mp4", filename=name, content_disposition_type="inline")

    @api.post("/api/moments/{ev_id}/star")
    def star(ev_id: int, on: bool = True):
        moment(ev_id)
        db.execute("UPDATE events SET starred=? WHERE id=?", (int(on), ev_id))
        return {"ok": True}

    @api.post("/api/moments/{ev_id}/wrong")
    def wrong(ev_id: int, on: bool = True):
        ev = moment(ev_id)
        db.execute("UPDATE events SET wrong=? WHERE id=?", (int(on), ev_id))
        if ev["kind"] == "feeding" and on:
            db.execute("DELETE FROM feedings WHERE event_id=? AND source='auto'", (ev_id,))
        return {"ok": True}

    @api.put("/api/moments/{ev_id}/tags")
    def set_tags(ev_id: int, tags: list[str]):
        moment(ev_id)
        clean = sorted({t.strip().lower()[:TAG_MAX] for t in tags if t.strip()})
        db.execute("DELETE FROM event_tags WHERE event_id=?", (ev_id,))
        for t in clean:
            db.execute("INSERT INTO event_tags VALUES (?, ?)", (ev_id, t))
        return {"tags": clean}

    @api.get("/api/tags")
    def all_tags():
        return db.query("SELECT tag, COUNT(*) AS n FROM event_tags GROUP BY tag ORDER BY n DESC, tag")

    @api.delete("/api/moments/{ev_id}")
    def delete_moment(ev_id: int):
        ev = moment(ev_id)
        for p in (ev["thumb"], ev["clip"]):
            if p:
                Path(p).unlink(missing_ok=True)
        db.execute("DELETE FROM event_tags WHERE event_id=?", (ev_id,))
        db.execute("DELETE FROM events WHERE id=?", (ev_id,))
        return {"ok": True}

    # Timelapses

    @api.get("/api/timelapses")
    def timelapses(tank_id: int | None = None):
        sql, args = ("SELECT tl.*, t.name AS tank_name FROM timelapses tl LEFT JOIN tanks t ON t.id = tl.tank_id"
                     " WHERE tl.out_path IS NULL"), ()
        if tank_id is not None:
            sql += " AND tl.tank_id = ?"
            args = (tank_id,)
        rows = db.query(sql + " ORDER BY tl.start_ts DESC, tl.id DESC LIMIT 100", args)
        for r in rows:
            r["has_file"] = bool(r["path"]) and Path(r["path"]).exists()
            r.pop("path")
            r["params"] = json.loads(r["params"] or "{}")
        return rows

    @api.post("/api/timelapses")
    def make_timelapse(req: TimelapseIn):
        if req.end_ts <= req.start_ts:
            raise HTTPException(400, "the end has to be after the start")
        tank = core.tanks.get(req.tank_id) if req.tank_id is not None else None
        p = core.timelapses.default_params()
        for f in ("max_speed", "min_speed", "ease_seconds", "driver"):
            value = getattr(req, f)
            if value is not None:
                setattr(p, f, value)
        p.overlay = req.overlay
        if not (p.driver in ("spots", "tank", "both") or p.driver.startswith("spot:")):
            raise HTTPException(400, "unknown slow down choice")
        if p.driver != "tank" and (tank is None or not tank.spotlights):
            p.driver = "tank"
        return {"id": core.timelapses.queue(tank.id if tank else None, req.start_ts, req.end_ts, p)}

    @api.get("/api/timelapses/{tl_id}.mp4")
    def timelapse_file(tl_id: int, download: bool = False):
        row = db.one("SELECT path FROM timelapses WHERE id=?", (tl_id,))
        if not row or not row["path"] or not Path(row["path"]).exists():
            raise HTTPException(404)
        return FileResponse(row["path"], media_type="video/mp4",
                            filename=Path(row["path"]).name if download else None)

    @api.post("/api/timelapses/{tl_id}/retry")
    def retry_timelapse(tl_id: int):
        if not db.one("SELECT id FROM timelapses WHERE id=?", (tl_id,)):
            raise HTTPException(404, "no such timelapse")
        db.execute("UPDATE timelapses SET status='queued', error=NULL, progress=0 WHERE id=?", (tl_id,))
        core.timelapses.wake.set()
        return {"ok": True}

    @api.post("/api/timelapses/{tl_id}/star")
    def star_timelapse(tl_id: int, on: bool = True):
        db.execute("UPDATE timelapses SET starred=? WHERE id=?", (int(on), tl_id))
        return {"ok": True}

    @api.delete("/api/timelapses/{tl_id}")
    def delete_timelapse(tl_id: int):
        row = db.one("SELECT path, status FROM timelapses WHERE id=?", (tl_id,))
        if not row:
            raise HTTPException(404)
        if row["status"] == "rendering":
            raise HTTPException(409, "still being made")
        if row["path"]:
            Path(row["path"]).unlink(missing_ok=True)
        db.execute("DELETE FROM timelapses WHERE id=?", (tl_id,))
        return {"ok": True}

    @api.get("/api/defaults")
    def defaults():
        return {"max_speed": T.max_speed, "min_speed": T.min_speed, "ease_seconds": T.ease_seconds,
                "driver": T.driver, "night_start": T.night_start, "night_end": T.night_end}

    # Notifications

    @api.get("/api/notify")
    def notify_settings():
        return core.notifier.settings()

    @api.put("/api/notify")
    def notify_update(changes: dict):
        try:
            return core.notifier.update(changes)
        except ValueError as e:
            bad(e)

    @api.post("/api/notify/test")
    def notify_test():
        core.notifier.test()
        return {"ok": True}

    @api.post("/api/notify/morning")
    def notify_morning():
        core.notifier.morning_summary(force=True)
        return {"ok": True}

    # Advanced

    @api.get("/api/advanced")
    def advanced():
        def folder_gb(p: Path) -> float:
            return round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e9, 2) if p.exists() else 0.0

        false_alarms = db.query("""SELECT t.name AS tank, e.kind, SUM(e.wrong) AS wrong, COUNT(*) AS total
                                   FROM events e LEFT JOIN tanks t ON t.id = e.tank_id
                                   WHERE e.start_ts > ? GROUP BY e.tank_id, e.kind""", (time.time() - 30 * 86400,))
        return {
            "storage": {"raw_gb": folder_gb(core.data / "rec"), "moments_gb": folder_gb(core.data / "clips"),
                        "timelapses_gb": folder_gb(core.data / "timelapses"),
                        "oldest_raw": db.value("SELECT MIN(start_ts) FROM segments"),
                        "clip_days": cfg.storage.clip_days, "timelapse_days": cfg.storage.timelapse_days,
                        "min_free_gb": cfg.storage.min_free_gb},
            "view": core.view.status(),
            "false_alarms": false_alarms,
            "health_log": db.query("SELECT * FROM health_log ORDER BY start_ts DESC LIMIT 50"),
            "camera": {"device": cfg.camera.device, "size": f"{cfg.camera.width}x{cfg.camera.height}",
                       "fps": cfg.camera.fps, "exposure": cfg.camera.exposure, "gain": cfg.camera.gain},
            "encoder": core.recorder.encoder if core.recorder else None,
            "dropped_frames": core.recorder.dropped if core.recorder else 0,
            "perf": perf(),
        }

    # Remembered box sizes: "Cheeks' home is about this big", so after moving the camera a box can be
    # dropped with one tap and nudged into place. Keyed by what the box is for and the animal's name.

    @api.get("/api/shapes")
    def get_shapes():
        return json.loads(db.get("shapes", "{}"))

    @api.put("/api/shapes")
    def put_shapes(body: dict):
        shapes = json.loads(db.get("shapes", "{}"))
        for key, v in body.items():
            try:
                w, h = float(v["w"]), float(v["h"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0.005 < w <= 1 and 0.005 < h <= 1 and len(key) <= 80:
                shapes[key] = {"w": w, "h": h}
        db.set("shapes", json.dumps(shapes))
        return shapes

    # Modes and free recording

    @api.get("/api/mode")
    def get_mode():
        return {"mode": core.mode}

    @api.put("/api/mode")
    def put_mode(body: ModeIn):
        if body.mode not in ("tank", "free"):
            raise HTTPException(400, "mode is tank or free")
        core.set_mode(body.mode)
        return {"mode": core.mode}

    @api.get("/api/free")
    def free_list():
        return {"status": core.free.status(), "mode": core.mode, "sessions": core.free.sessions()}

    @api.post("/api/free/start")
    def free_start(body: FreeIn | None = None):
        if core.mode != "free":
            core.set_mode("free")
        return core.free.start(body.name if body else "")

    @api.post("/api/free/stop")
    def free_stop():
        return core.free.stop()

    @api.get("/api/free/{sid}/part/{n}.mp4")
    def free_part(sid: int, n: int):
        p = core.free.part_path(sid, n)
        if not p:
            raise HTTPException(404, "no such part")
        return FileResponse(p, media_type="video/mp4", filename=f"{p.parent.name}_part{n + 1}.mp4")

    @api.get("/api/free/{sid}/timelapse.mp4")
    def free_timelapse(sid: int):
        p = core.free.timelapse_path(sid)
        if not p:
            raise HTTPException(404, "no timelapse yet")
        return FileResponse(p, media_type="video/mp4", filename=f"{p.parent.name}_timelapse.mp4")

    @api.post("/api/free/{sid}/timelapse")
    def free_make_timelapse(sid: int):
        tl = core.free.make_timelapse(sid)
        if tl is None:
            raise HTTPException(400, "too short for a timelapse, or still recording")
        return {"timelapse_id": tl}

    @api.patch("/api/free/{sid}")
    def free_rename(sid: int, body: FreeIn):
        core.free.rename(sid, body.name)
        return {"ok": True}

    @api.delete("/api/free/{sid}")
    def free_delete(sid: int):
        if not core.free.delete(sid):
            raise HTTPException(404, "no such recording")
        return {"ok": True}

    @api.get("/api/pi")
    async def pi_info():
        info = await run_in_threadpool(pi.tailscale, cfg.web.port)
        if cfg.web.host in ("127.0.0.1", "localhost", "::1"):
            info["lan_url"] = None      # only reachable through Tailscale's https address, by design
        info["qr"] = pi.qr_svg(info["url"]) if info["url"] and info["serving"] else None
        return info

    @api.post("/api/pi/phone-access")
    async def pi_phone_access():
        ok, out = await run_in_threadpool(pi.serve, cfg.web.port)
        if not ok:
            raise HTTPException(500, out or "tailscale serve failed")
        return {"ok": True}

    @api.post("/api/pi/{action}")
    async def pi_action(action: str):
        ok, out = await run_in_threadpool(pi.act, action)
        if not ok:
            raise HTTPException(500, out or f"{action} failed")
        return {"ok": True}

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api
