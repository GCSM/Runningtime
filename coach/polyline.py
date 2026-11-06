"""Draw a run's route without any map service: decode Google's encoded-polyline format (what Strava uses) and project the
points onto a small SVG. Nothing leaves the server, and no tiles are fetched."""
from __future__ import annotations

import math


def decode(encoded: str) -> list[tuple[float, float]]:
    """Google encoded polyline -> [(lat, lon), ...] (precision 5)."""
    points, index, lat, lon = [], 0, 0, 0
    n = len(encoded)
    while index < n:
        pair = []
        for _ in range(2):
            shift = result = 0
            while True:
                if index >= n:
                    return points              # truncated input: keep what decoded cleanly
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            pair.append(~(result >> 1) if result & 1 else result >> 1)
        lat += pair[0]
        lon += pair[1]
        points.append((lat / 1e5, lon / 1e5))
    return points


def to_svg(points: list[tuple[float, float]], width: int = 320, height: int = 200, pad: int = 16, max_points: int = 400) -> dict | None:
    """Fit the route into width x height (aspect preserved). Returns {'d', 'start', 'end', 'width', 'height'} or None if it can't be drawn."""
    if len(points) < 2:
        return None
    if len(points) > max_points:                  # thin very dense routes; keep both ends
        step = len(points) / max_points
        points = [points[int(i * step)] for i in range(max_points)] + [points[-1]]
    mean_lat = math.radians(sum(p[0] for p in points) / len(points))
    xs = [p[1] * math.cos(mean_lat) for p in points]      # equirectangular: shrink longitude by cos(latitude)
    ys = [p[0] for p in points]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    if span_x == 0 and span_y == 0:
        return None
    scale = min((width - 2 * pad) / span_x if span_x else math.inf, (height - 2 * pad) / span_y if span_y else math.inf)
    ox = (width - span_x * scale) / 2
    oy = (height - span_y * scale) / 2
    proj = [(round(ox + (x - min(xs)) * scale, 1), round(height - (oy + (y - min(ys)) * scale), 1)) for x, y in zip(xs, ys)]   # north is up
    d = "M" + " L".join(f"{x} {y}" for x, y in proj)
    return {"d": d, "start": proj[0], "end": proj[-1], "width": width, "height": height}
