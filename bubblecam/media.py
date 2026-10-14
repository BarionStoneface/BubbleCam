"""Reading frames back out of recordings, and cutting event clips."""

from __future__ import annotations

import functools
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .db import DB
from .recorder import act_path, read_act


@functools.lru_cache(maxsize=256)
def probe_size(path: str) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


class FrameReader:
    """Forward reading of grey frames by index. Seeks (cheap, keyframe every second) when asked
    to jump ahead more than `seek_gap` frames, otherwise decodes straight through."""

    def __init__(self, path: Path, fps: float, seek_gap: int = 90):
        self.path = path
        self.fps = fps
        self.w, self.h = probe_size(str(path))
        self.size = self.w * self.h
        self.seek_gap = seek_gap
        self.proc: subprocess.Popen | None = None
        self.pos = 0

    def _open(self, idx: int) -> None:
        self.close()
        # Seek half a frame early: ffmpeg returns the first frame at or after the seek time.
        ss = max(0.0, (idx - 0.5) / self.fps)
        self.proc = subprocess.Popen(
            ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{ss:.4f}", "-i", str(self.path),
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            stdout=subprocess.PIPE)
        self.pos = idx

    def _read(self) -> np.ndarray | None:
        buf = bytearray(self.size)
        view = memoryview(buf)
        got = 0
        while got < self.size:
            n = self.proc.stdout.readinto(view[got:])
            if not n:
                return None
            got += n
        self.pos += 1
        return np.frombuffer(buf, np.uint8).reshape(self.h, self.w)

    def get(self, idx: int) -> np.ndarray | None:
        if self.proc is None or idx < self.pos or idx - self.pos > self.seek_gap:
            self._open(idx)
        while self.pos < idx:
            if self._read() is None:
                return None
        return self._read()

    def close(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None


def extract_clip(db: DB, start_ts: float, end_ts: float, out_path: Path) -> bool:
    """Cut [start_ts, end_ts] out of the real time recording without re-encoding."""
    lines = []
    for seg in db.segments_between(start_ts, end_ts):
        video = Path(seg["path"])
        act = read_act(act_path(video))
        if not video.exists() or len(act) == 0:
            continue
        i0 = int(np.searchsorted(act["ts"], start_ts))
        i1 = int(np.searchsorted(act["ts"], end_ts))
        if i1 <= i0:
            continue
        lines.append(f"file '{video.resolve()}'")
        if i0 > 0:
            lines.append(f"inpoint {i0 / seg['fps']:.3f}")
        if i1 < len(act):
            lines.append(f"outpoint {i1 / seg['fps']:.3f}")
    if not lines:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("\n".join(lines) + "\n")
        listing = f.name
    tmp = out_path.with_suffix(".tmp.mp4")
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", listing, "-c", "copy", "-movflags", "+faststart", str(tmp)],
                           capture_output=True, timeout=300)
        if r.returncode != 0 or not tmp.exists():
            return False
        tmp.replace(out_path)
        return True
    finally:
        Path(listing).unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
