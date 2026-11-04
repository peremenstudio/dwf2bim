"""Reader for DWFX packages: the sheet is XPS page markup (FixedPage) with a W2X companion.

Curves are flattened to polylines. Coordinates are returned in page units, after all
canvas transforms. W2X supplies the transform from W2D logical units to drawing units.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

NS = "{http://schemas.microsoft.com/xps/2005/06}"
_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


class XpsError(Exception):
    pass


@dataclass
class PagePath:
    name: str
    rings: list  # [(points, closed)]
    stroke: str | None
    fill: str | None
    thickness: float


@dataclass
class PageText:
    name: str
    text: str
    x: float
    y: float
    size: float
    angle: float


@dataclass
class Sheet:
    source_dwg: str
    width: float
    height: float
    paths: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    drawing_transform: tuple | None = None  # (a, d, e, f): logical = (x*a+e, y*d+f)
    logical_area: tuple | None = None  # W2X view area in logical units
    clip: tuple | None = None  # (x0, y0, w, h) of the content canvas, in canvas units
    content_matrix: tuple | None = None  # page matrix of the canvas that holds the clip
    w2x: object | None = None


def _matrix(text):
    if not text:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return tuple(float(v) for v in text.split(","))


def _mul(p, q):
    """Apply q first, then p (both as a, b, c, d, e, f)."""
    a, b, c, d, e, f = q
    A, B, C, D, E, F = p
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _apply(m, x, y):
    return (x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5])


def _arc_points(p0, rx, ry, phi, large, sweep, p1, step=math.radians(10)):
    """SVG-style elliptical arc to a polyline."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1 = cos_p * dx + sin_p * dy
    y1 = -sin_p * dx + cos_p * dy
    rx, ry = abs(rx), abs(ry)
    scale = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if scale > 1:
        rx *= math.sqrt(scale)
        ry *= math.sqrt(scale)
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    k = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        k = -k
    cx1, cy1 = k * rx * y1 / ry, -k * ry * x1 / rx
    cx = cos_p * cx1 - sin_p * cy1 + (p0[0] + p1[0]) / 2
    cy = sin_p * cx1 + cos_p * cy1 + (p0[1] + p1[1]) / 2
    a0 = math.atan2((y1 - cy1) / ry, (x1 - cx1) / rx)
    a1 = math.atan2((-y1 - cy1) / ry, (-x1 - cx1) / rx)
    delta = a1 - a0
    if sweep and delta < 0:
        delta += 2 * math.pi
    elif not sweep and delta > 0:
        delta -= 2 * math.pi
    n = max(1, int(abs(delta) / step))
    out = []
    for i in range(1, n + 1):
        t = a0 + delta * i / n
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        out.append((cos_p * ex - sin_p * ey + cx, sin_p * ex + cos_p * ey + cy))
    out[-1] = p1
    return out


def parse_path_data(data: str) -> list:
    """XPS abbreviated geometry to [(points, closed)]."""
    tokens = _TOKEN.findall(data)
    i = 0
    rings = []
    points = []
    x = y = 0.0
    cmd = None
    last_ctrl = None

    def num():
        nonlocal i
        i += 1
        return float(tokens[i - 1])

    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "zZ":
                if points:
                    rings.append((points, True))
                    x, y = points[0]
                    points = [(x, y)]
                continue
            if cmd == "F":  # fill rule
                num()
                continue
            if cmd not in "MmLlHhVvCcSsQqAa":
                raise XpsError(f"Unknown path command {cmd!r}")
        elif cmd is None:
            raise XpsError("Path data starts with a number")
        if cmd in "Mm":
            nx, ny = num(), num()
            if cmd == "m":
                nx, ny = x + nx, y + ny
            if len(points) > 1:
                rings.append((points, False))
            x, y = nx, ny
            points = [(x, y)]
            cmd = "L" if cmd == "M" else "l"
        elif cmd in "Ll":
            nx, ny = num(), num()
            x, y = (x + nx, y + ny) if cmd == "l" else (nx, ny)
            points.append((x, y))
        elif cmd in "Hh":
            v = num()
            x = x + v if cmd == "h" else v
            points.append((x, y))
        elif cmd in "Vv":
            v = num()
            y = y + v if cmd == "v" else v
            points.append((x, y))
        elif cmd in "CcSsQq":
            rel = cmd.islower()
            if cmd in "Cc":
                c1 = (num(), num())
                if rel:
                    c1 = (x + c1[0], y + c1[1])
            elif cmd in "Ss":
                c1 = (2 * x - last_ctrl[0], 2 * y - last_ctrl[1]) if last_ctrl else (x, y)
            if cmd in "Qq":
                q = (num(), num())
                if rel:
                    q = (x + q[0], y + q[1])
                c1 = (x + 2 * (q[0] - x) / 3, y + 2 * (q[1] - y) / 3)
                end = (num(), num())
                if rel:
                    end = (x + end[0], y + end[1])
                c2 = (end[0] + 2 * (q[0] - end[0]) / 3, end[1] + 2 * (q[1] - end[1]) / 3)
            else:
                c2 = (num(), num())
                if rel:
                    c2 = (x + c2[0], y + c2[1])
                end = (num(), num())
                if rel:
                    end = (x + end[0], y + end[1])
            for k in range(1, 9):
                t_ = k / 8
                u_ = 1 - t_
                points.append((
                    u_ ** 3 * x + 3 * u_ * u_ * t_ * c1[0] + 3 * u_ * t_ * t_ * c2[0] + t_ ** 3 * end[0],
                    u_ ** 3 * y + 3 * u_ * u_ * t_ * c1[1] + 3 * u_ * t_ * t_ * c2[1] + t_ ** 3 * end[1],
                ))
            last_ctrl = c2
            x, y = end
            continue
        elif cmd in "Aa":
            rx, ry, phi, large, sweep = num(), num(), math.radians(num()), int(num()), int(num())
            nx, ny = num(), num()
            if cmd == "a":
                nx, ny = x + nx, y + ny
            points.extend(_arc_points((x, y), rx, ry, phi, large, sweep, (nx, ny)))
            x, y = nx, ny
        last_ctrl = None
    if len(points) > 1:
        rings.append((points, False))
    return rings


def _walk(el, matrix, sheet):
    for child in el:
        tag = child.tag.replace(NS, "")
        if tag == "Canvas":
            m = _mul(matrix, _matrix(child.get("RenderTransform")))
            clip = child.get("Clip")
            if clip and sheet.clip is None:
                nums = [float(v) for v in re.findall(r"[-+]?[\d.]+", clip)][:4]
                if len(nums) == 4:
                    sheet.clip = tuple(nums)
                    sheet.content_matrix = m
            _walk(child, m, sheet)
        elif tag == "Path" and child.get("Data"):
            m = _mul(matrix, _matrix(child.get("RenderTransform")))
            rings = [([_apply(m, *q) for q in pts], closed) for pts, closed in parse_path_data(child.get("Data"))]
            scale = math.hypot(m[0], m[1])
            sheet.paths.append(PagePath(child.get("Name") or "", rings, child.get("Stroke"), child.get("Fill"), float(child.get("StrokeThickness") or 1.0) * scale))
        elif tag == "Glyphs":
            m = _mul(matrix, _matrix(child.get("RenderTransform")))
            ox, oy = _apply(m, float(child.get("OriginX") or 0), float(child.get("OriginY") or 0))
            sheet.texts.append(PageText(child.get("Name") or "", child.get("UnicodeString") or "", ox, oy,
                                        float(child.get("FontRenderingEmSize") or 0) * math.hypot(m[0], m[1]),
                                        math.degrees(math.atan2(m[1], m[0]))))


def load(path: str | Path) -> list:
    sheets = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for page in (n for n in names if n.lower().endswith(".fpage")):
            root = ET.fromstring(z.read(page))
            folder = page.rsplit("/", 1)[0]
            dwg = ""
            w2x = None
            for n in names:
                if n.startswith(folder + "/") and n.endswith(".xml") and z.getinfo(n).file_size > 1000:
                    blob = z.read(n)
                    if blob.lstrip()[:4] == b"<W2X":
                        candidate = ET.fromstring(blob)
                        if candidate.find("Units") is not None:
                            w2x = candidate
            sheet = Sheet("", float(root.get("Width")), float(root.get("Height")), w2x=w2x)
            if w2x is not None:
                embed = w2x.find("Embed")
                sheet.source_dwg = embed.get("Filename", "") if embed is not None else ""
                units = w2x.find("Units")
                if units is not None:
                    t = [float(v) for v in units.get("Transform").split(",")]
                    sheet.drawing_transform = (t[0], t[5], t[12], t[13])
                view = w2x.find("View")
                if view is not None:
                    sheet.logical_area = tuple(float(v) for v in view.get("Area").split(","))
            _walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), sheet)
            sheets.append(sheet)
    if not sheets:
        raise XpsError(f"{path} has no pages")
    return sheets
