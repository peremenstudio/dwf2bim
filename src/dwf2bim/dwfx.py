"""Read a Revit DWFX as building sheets, in the same form a DWF gives.

A DWFX page is XPS markup. Its paths are drawn in W2D logical units under one scaling canvas,
with y pointing down. The W2X companion lists, in drawing order, which element (Object_Node)
and which view (Viewport) each named path belongs to, and the object definition holds the
Revit properties. Together they make the primitives, views and nodes that a W2D stream holds.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
import zipfile
from bisect import bisect_right

from . import whip, xps

# W2X records that name a path, and the primitive each makes
KINDS = {"Contour_Set": "contour", "Polygon": "polygon", "Filled_Ellipse": "polygon", "Outline_Ellipse": "polyline", "Text": "text"}
# W2X records that carry no geometry of the building
NO_GEOMETRY = {"Named_View", "Units", "PNG_Group4_Image", "Embed", "View"}
MIN_OBJECTS = 100  # a building sheet set has thousands of Revit objects; area and survey sheets have a handful


class DwfxError(Exception):
    pass


def object_count(path) -> int:
    """How many Revit objects the package describes."""
    with zipfile.ZipFile(path) as z:
        return sum(z.read(n).count(b"<Instance ") for n in z.namelist() if n.endswith(".xml") and _is_object_definition(z, n))


def is_building(path) -> bool:
    try:
        return object_count(path) >= MIN_OBJECTS
    except (zipfile.BadZipFile, OSError):
        return False


def _is_object_definition(z, name) -> bool:
    with z.open(name) as fh:
        return b"<PageObjectDefinition" in fh.read(300)


def _suffix(name) -> int | None:
    m = re.search(r"_(\d+)$", name or "")
    return int(m.group(1)) if m else None


def read_w2x(root) -> tuple:
    """Walk the W2X in order. Returns the views, the nodes, and for each named path its state and records.

    The state of a path is the element and view in force where its name first appears. A path the
    W2X does not name takes the state of the nearest name before it.
    """
    views, nodes = [], {}
    view_refs = {}  # view index -> name of the canvas that clips it
    node, view = -1, -1
    state, records, order = {}, {}, []
    for child in root:
        ref = child.get("refName")
        if child.tag == "RenditionSync":
            for sub in child:
                if sub.tag == "Object_Node":
                    node = int(sub.get("Number"))
                    nodes[node] = sub.get("Name") or ""
                elif sub.tag == "Viewport":
                    upm = float((sub.get("Transform") or "1").split(",")[0])
                    views.append(whip.Viewport(html.unescape(sub.get("Name") or ""), [], upm))
                    view = len(views) - 1
                    view_refs[view] = ref
        elif child.tag in KINDS:
            count = int(child.get("Contours") or 1) if child.tag == "Contour_Set" else 1
            records.setdefault(ref, []).extend([KINDS[child.tag]] * count)
        elif child.tag not in NO_GEOMETRY:
            raise DwfxError(f"Unknown W2X record <{child.tag}>: the reader would lose track of elements and views after it")
        if ref is not None and ref not in state:
            state[ref] = (node, view)
            number = _suffix(ref)
            if number is not None:
                order.append((number, node, view))
    order.sort()
    return views, nodes, view_refs, state, records, order


def _page_items(root):
    """Each named path and text of the page, with its points in page units."""
    items = []

    def walk(el, matrix):
        for child in el:
            tag = child.tag.replace(xps.NS, "")
            if tag == "Canvas":
                m = xps._mul(matrix, xps._matrix(child.get("RenderTransform")))
                items.append(("canvas", child.get("Name"), child.get("Clip"), m, None))
                walk(child, m)
            elif tag == "Path" and child.get("Data"):
                m = xps._mul(matrix, xps._matrix(child.get("RenderTransform")))
                rings = [([xps._apply(m, *q) for q in pts], closed) for pts, closed in xps.parse_path_data(child.get("Data"))]
                items.append(("path", child.get("Name"), rings, m, child))
            elif tag == "Glyphs":
                m = xps._mul(matrix, xps._matrix(child.get("RenderTransform")))
                origin = xps._apply(m, float(child.get("OriginX") or 0), float(child.get("OriginY") or 0))
                items.append(("text", child.get("Name"), origin, m, child))

    walk(root, (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
    return items


def page_to_whip(page_root, w2x_root) -> whip.WhipData:
    views, nodes, view_refs, state, records, order = read_w2x(w2x_root)
    named = w2x_root.find("Named_View")
    if named is None:
        raise DwfxError("The W2X has no Named_View, so the page's logical size is unknown")
    _, _, width, height = (float(v) for v in named.get("Area").split())
    scale = width / float(page_root.get("Width"))  # logical units per page unit

    def logical(p):
        return (round(p[0] * scale), round(height - p[1] * scale))

    keys = [o[0] for o in order]

    def state_of(name):
        if name in state:
            return state[name]
        number = _suffix(name)
        i = bisect_right(keys, number) - 1 if number is not None else -1
        return (order[i][1], order[i][2]) if i >= 0 else (-1, -1)

    data = whip.WhipData(version="DWFX", nodes=nodes, views=views)
    clips = {}
    for kind, name, payload, matrix, element in _page_items(page_root):
        if kind == "canvas":
            if payload:
                rings = xps.parse_path_data(payload)
                if rings:
                    clips[name] = [logical(xps._apply(matrix, *q)) for q in rings[0][0]]
            continue
        node, view = state_of(name)
        if kind == "text":
            text = element.get("UnicodeString") or ""
            if text:
                data.primitives.append(whip.Primitive("text", [logical(payload)], node, view, 0, 0, 0, text=text))
            continue
        filled = bool(element.get("Fill"))
        weight = round(float(element.get("StrokeThickness") or 0))
        planned = list(records.get(name, []))
        for points, closed in payload:
            pts = [logical(q) for q in points]
            kind_of = planned.pop(0) if planned else ("polygon" if filled and closed else "polyline")
            if kind_of == "text":
                continue
            if kind_of == "polyline" and closed and pts[0] != pts[-1]:
                pts.append(pts[0])
            if kind_of == "polyline" and len(pts) == 2:
                kind_of = "line"
            data.primitives.append(whip.Primitive(kind_of, pts, node, view, 0, 0, weight, fill=kind_of in ("contour", "polygon")))
    for index, ref in view_refs.items():
        views[index].contour = clips.get(ref, [])
    return data


def read(path) -> list:
    """Every page of the package: (title, WhipData, object definition bytes or None)."""
    pages = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        for page in (n for n in names if n.lower().endswith(".fpage")):
            folder = page.rsplit("/", 1)[0]
            w2x = objects = None
            title = folder.rsplit("/", 1)[-1]
            for n in names:
                if not n.startswith(folder + "/"):
                    continue
                if n.endswith("descriptor.xml"):
                    m = re.search(rb'<ePlot:Page [^>]*name="([^"]*)"', z.read(n))
                    if m:
                        title = html.unescape(m.group(1).decode("utf8", "replace"))
                elif n.endswith(".xml"):
                    with z.open(n) as fh:
                        start = fh.read(300)
                    if start.lstrip()[:4] == b"<W2X":
                        candidate = ET.fromstring(z.read(n))
                        if candidate.find("RenditionSync") is not None:
                            w2x = candidate
                    elif b"<PageObjectDefinition" in start:
                        objects = z.read(n)
            if w2x is None:
                continue
            pages.append((title, page_to_whip(ET.fromstring(z.read(page)), w2x), objects))
    return pages
