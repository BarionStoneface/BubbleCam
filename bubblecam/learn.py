"""Smart ignore: watch the tank for a few seconds and offer every patch of movement as a box to
ignore (swaying plants, filter flow, bubbles).

Boxes that strongly overlap become one patch. Boxes that only touch stay separate, so a diagonal
sweep of a leaf gives a chain of boxes, not one big square over everything. Which ones to keep, and
whether a big box swallows the small ones inside it, is chosen on the drawing screen."""

from __future__ import annotations

import time

MIN_AREA = 0.0002        # smaller than this (a few pixels at analysis size) is noise
MERGE_IOU = 0.3          # how much two boxes must overlap to count as the same patch
MAX_PATCHES = 40


def iou(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if inter <= 0:
        return 0.0
    area = lambda r: (r[2] - r[0]) * (r[3] - r[1])
    return inter / (area(a) + area(b) - inter)


def patches(samples: list[list[tuple]], min_hits: int = 2) -> list[dict]:
    """samples: per analysed frame, the moving boxes as (x, y, w, h) fractions of the picture.
    Returns patches as {x, y, w, h, hits}, busiest first."""
    clusters: list[list] = []          # [x0, y0, x1, y1, hits]
    for frame in samples:
        for x, y, w, h in (b[:4] for b in frame):
            if w * h < MIN_AREA:
                continue
            r = (x, y, x + w, y + h)
            best, best_i = None, MERGE_IOU
            for c in clusters:
                v = iou(r, c)
                if v >= best_i:
                    best, best_i = c, v
            if best is None:
                clusters.append([*r, 1])
            else:
                best[0], best[1] = min(best[0], r[0]), min(best[1], r[1])
                best[2], best[3] = max(best[2], r[2]), max(best[3], r[3])
                best[4] += 1
    # A second pass joins patches that ended up mostly on top of each other.
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if iou(a, b) >= 0.5:
                    a[0], a[1], a[2], a[3] = min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])
                    a[4] += b[4]
                    clusters.pop(j)
                    merged = True
                    break
            if merged:
                break
    out = [{"x": round(c[0], 4), "y": round(c[1], 4), "w": round(c[2] - c[0], 4), "h": round(c[3] - c[1], 4), "hits": c[4]}
           for c in clusters if c[4] >= min_hits]
    out.sort(key=lambda p: -p["hits"])
    return out[:MAX_PATCHES]


def watch(core, seconds: float) -> list[dict]:
    """Collect moving boxes from the live analysis for `seconds`, then group them."""
    samples, last_ts, seen = [], 0.0, None
    end = time.time() + seconds
    while time.time() < end:
        frame, ts, scores = core.wait_frame(last_ts, timeout=1.0)
        if ts > last_ts:
            last_ts = ts
            if scores is not seen and scores is not None:
                seen = scores
                samples.append(list(scores.boxes))
    # A fish swims through a spot once; a plant or the bubbles move in the same place again and
    # again. Only offer patches that kept moving for a good part of the time.
    return patches(samples, min_hits=max(2, int(len(samples) * 0.08)))
