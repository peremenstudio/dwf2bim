"""Survey maps that the colour route cannot read: a map in DWF form, and a plot found by its shape.

Survey offices draw the plot boundary in any colour, and the archive holds many maps as DWF, not
DWFX. Both are answered the same way. The plot's shape is known from the Revit sheet, so the map
is searched for corners that repeat that shape, whatever their colour: one rotation and one
shift, no scale, that puts the plot's corners on corners of the map and its edges on drawn lines.
"""
from __future__ import annotations

import math
import re
import zipfile
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from . import whip, xps

# Israel's grid: anything outside is not a map coordinate
_ITM_X, _ITM_Y = (100000.0, 300000.0), (350000.0, 850000.0)
_UNITS = re.compile(rb"\(Units\s+'[^']*'\s+\(\(([-\d.eE+ ]+)\)\(([-\d.eE+ ]+)\)\(([-\d.eE+ ]+)\)\(([-\d.eE+ ]+)\)\)\)")


def load_dwf(path) -> xps.Sheet | None:
    """A survey map in DWF form, as the sheet the DWFX reader gives, with its lines in map coordinates.

    The W2D stream states its units: logical = drawing * scale + offset. None when the file states none,
    or when what it draws is not on Israel's grid.
    """
    with zipfile.ZipFile(path) as z:
        name = next((n for n in z.namelist() if n.lower().endswith(".w2d")), None)
        if name is None:
            return None
        buf = z.read(name)
    m = _UNITS.search(buf)
    if m is None:
        return None
    rows = [[float(v) for v in m.group(i).split()] for i in range(1, 5)]
    sx, sy, ox, oy = rows[0][0], rows[1][1], rows[3][0], rows[3][1]
    if not sx or not sy:
        return None

    def to_map(q):
        return ((q[0] - ox) / sx, (q[1] - oy) / sy)

    sheet = xps.Sheet(Path(path).name, 0.0, 0.0)
    # the DWFX reader's frame, set so that its page point (x, -y) is the map point (x, y)
    sheet.drawing_transform, sheet.clip, sheet.content_matrix = (1.0, 1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for p in whip.decode(buf).primitives:
        if p.kind not in ("line", "polyline", "polygon", "contour") or len(p.points) < 2:
            continue
        pts = [to_map(q) for q in p.points]
        if all(_ITM_X[0] < x < _ITM_X[1] and _ITM_Y[0] < y < _ITM_Y[1] for x, y in pts):
            stroke = "#{:06X}".format(p.color & 0xFFFFFF)
            sheet.paths.append(xps.PagePath("", [([(x, -y) for x, y in pts], p.kind in ("polygon", "contour"))], stroke, None, 0.0))
    return sheet if sheet.paths else None


def map_segments(sheet) -> list:
    """Every stroked segment of a survey sheet, of any colour, in map coordinates."""
    if sheet.drawing_transform is None or sheet.clip is None or sheet.content_matrix is None:
        return []
    a, d, e, f = sheet.drawing_transform
    x0, y0, _, h = sheet.clip
    m = sheet.content_matrix

    def to_map(p):
        cx, cy = (p[0] - m[4]) / m[0], (p[1] - m[5]) / m[3]
        return ((cx - x0 - e) / a, ((y0 + h) - cy - f) / d)

    out = []
    for path in sheet.paths:
        if not path.stroke:
            continue
        for ring, closed in path.rings:
            pts = [to_map(q) for q in ring] + ([to_map(ring[0])] if closed else [])
            out += [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return out


def sharp_corners(ring, min_turn_deg=20.0) -> list:
    """Indexes of the corners where the ring really turns. The points along a rounded corner are left out:
    a survey draws such a corner its own way."""
    n = len(ring)
    out = []
    for k in range(n):
        a, b, c = ring[k - 1], ring[k], ring[(k + 1) % n]
        turn = math.degrees(math.atan2(c[1] - b[1], c[0] - b[0]) - math.atan2(b[1] - a[1], b[0] - a[0]))
        if abs((turn + 180.0) % 360.0 - 180.0) >= min_turn_deg:
            out.append(k)
    return out


def _cells(points, size):
    """One whole number per point, the same for points in one square of this size."""
    return np.round(points[..., 0] / size).astype(np.int64) * 100_000_000 + np.round(points[..., 1] / size).astype(np.int64)


def _near_a_vertex(points, keys, size):
    """Which points have a map vertex in their square or one beside it."""
    found = np.zeros(points.shape[0], dtype=bool)
    for dx in (-size, 0.0, size):
        for dy in (-size, 0.0, size):
            found |= np.isin(_cells(points + np.array([dx, dy]), size), keys)
    return found


def find_plot_by_shape(model_ring, segments, corner_tol=0.10, min_segment=0.3, min_share=0.8, edge_share=0.55):
    """Where the plot of the Revit sheet lies on the survey map, found by its shape.

    Returns (theta, tx, ty, residuals, corners) with map = R(theta) * model + T, the residuals of the
    matched corners, and the plot's corners on the map: the map's own corner where one was matched.
    None when no place on the map repeats the plot's shape.
    """
    whole = np.array(model_ring, dtype=float)
    kept = sharp_corners(model_ring)
    ring = whole[kept]
    n = len(ring)
    long_ones = [s for s in segments if math.dist(*s) >= min_segment]
    if n < 3 or len(long_ones) < 3:
        return None
    verts = np.unique(np.round(np.array([p for s in long_ones for p in s]), 2), axis=0)
    keys = np.unique(_cells(verts, corner_tol))
    # the two corners farthest apart fix a rotation and a shift most surely; the corner farthest from both sifts the guesses
    gaps = np.linalg.norm(ring[:, None, :] - ring[None, :, :], axis=2)
    i, j = np.unravel_index(np.argmax(gaps), gaps.shape)
    third = int(np.argmax(np.minimum(gaps[i], gaps[j])))
    length = gaps[i, j]
    base = math.atan2(ring[j][1] - ring[i][1], ring[j][0] - ring[i][0])
    off = ring[third] - ring[i]
    need = max(3, math.ceil(min_share * n))
    best = None
    for start in range(0, len(verts), 400):
        chunk = verts[start:start + 400]
        dist = np.linalg.norm(chunk[:, None, :] - verts[None, :, :], axis=2)
        a, b = np.nonzero(np.abs(dist - length) <= corner_tol)
        if not len(a):
            continue
        ps, qs = chunk[a], verts[b]
        thetas = np.arctan2(qs[:, 1] - ps[:, 1], qs[:, 0] - ps[:, 0]) - base
        cs, sn = np.cos(thetas), np.sin(thetas)
        thirds = ps + np.stack([cs * off[0] - sn * off[1], sn * off[0] + cs * off[1]], axis=1)
        for k in np.nonzero(_near_a_vertex(thirds, keys, corner_tol))[0]:
            c, s_ = cs[k], sn[k]
            placed = (np.array([[c, -s_], [s_, c]]) @ ring.T).T
            placed += ps[k] - placed[i]
            near = np.linalg.norm(placed[:, None, :] - verts[None, :, :], axis=2)
            hit = near.argmin(axis=1)
            res = near[np.arange(n), hit]
            matched = res <= corner_tol
            score = (int(matched.sum()), -float(res[matched].max()) if matched.any() else 0.0)
            if matched.sum() >= need and (best is None or score > best[0]):
                best = (score, matched, hit)
    if best is None:
        return None
    _, matched, hit = best
    # least squares over the matched corners
    src, dst = ring[matched], verts[hit[matched]]
    ca, cb = src.mean(0), dst.mean(0)
    u, _, vt = np.linalg.svd((src - ca).T @ (dst - cb))
    r = vt.T @ u.T
    if np.linalg.det(r) < 0:
        vt[1] *= -1
        r = vt.T @ u.T
    t = cb - r @ ca
    placed = (r @ whole.T).T + t
    res = np.linalg.norm(placed[kept][matched] - dst, axis=1)
    # the plot's straight edges must be drawn on the map: corners alone can meet by chance
    lines = [LineString(s) for s in long_ones]
    tree = STRtree(lines)
    on_line, samples = 0, 0
    for at, k in enumerate(kept):
        nxt = kept[(at + 1) % n]
        if nxt != (k + 1) % len(whole):
            continue  # a rounded corner lies between these two
        a, b = placed[k], placed[nxt]
        for f in (0.2, 0.35, 0.5, 0.65, 0.8):
            pt = Point(a + (b - a) * f)
            samples += 1
            on_line += any(lines[m].distance(pt) <= corner_tol for m in tree.query(pt.buffer(corner_tol)))
    if not samples or on_line < edge_share * samples:
        return None
    by_corner = {k: tuple(verts[hit[at]]) for at, k in enumerate(kept) if matched[at]}
    corners = [by_corner.get(k, tuple(placed[k])) for k in range(len(whole))]
    return math.atan2(r[1, 0], r[0, 0]), float(t[0]), float(t[1]), res, [(float(x), float(y)) for x, y in corners]
