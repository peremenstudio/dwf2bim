"""Stage 6 - verify the reconstruction and produce view exports and previews."""
from __future__ import annotations

import collections
import math

import ezdxf
import ifcopenshell.geom
from PIL import Image, ImageDraw
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .names import level_id, slug

_SHADE = {
    "wall": (205, 205, 200),
    "floor": (170, 180, 190),
    "stair": (225, 195, 140),
    "railing": (100, 110, 125),
    "mullion": (85, 95, 110),
    "curtain_panel": (150, 200, 230),
    "generic": (165, 145, 120),
    "door": (160, 100, 60),
    "window": (90, 160, 220),
}


# ---- views -------------------------------------------------------------------
def render_view(view, path, width=2400):
    xs = [c[0] for c in view.contour]
    ys = [c[1] for c in view.contour]
    if not xs or max(xs) == min(xs):
        return
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    k = width / (x1 - x0)
    im = Image.new("RGB", (width, max(1, int((y1 - y0) * k))), "white")
    d = ImageDraw.Draw(im)
    def tr(q):
        return ((q[0] - x0) * k, (y1 - q[1]) * k)
    for p in view.prims:
        if p.kind in ("contour", "polygon") and len(p.points) > 2:
            d.polygon([tr(q) for q in p.points], fill=(225, 232, 240), outline=(60, 60, 60))
        elif p.kind in ("line", "polyline") and len(p.points) > 1:
            d.line([tr(q) for q in p.points], fill=(40, 40, 40))
    im.save(path)


def _contour_layers(doc, msp, contours):
    """The storey's floor outlines as closed red polylines, the main floor and the balconies on their own layers."""
    from .site import _CFG

    spec = _CFG["contours"]
    for key in ("main", "balcony"):
        name = spec["dxf_layers"][key]
        if name not in doc.layers:
            doc.layers.add(name, color=spec["dxf_color"])
    groups = [("main", [contours["main"]])] + [("balcony", [b]) for b in contours["balconies"]]
    for key, polys in groups:
        for poly_set in polys:
            for poly in poly_set:
                for ring in [poly["outer"]] + list(poly.get("holes") or []):
                    msp.add_lwpolyline([tuple(q) for q in ring], close=True, dxfattribs={"layer": spec["dxf_layers"][key], "color": spec["dxf_color"]})


def export_contours_dxf(contours, path):
    """One storey's floor outlines alone, in model coordinates."""
    doc = ezdxf.new("R2018", setup=False)
    doc.units = ezdxf.units.M
    _contour_layers(doc, doc.modelspace(), contours)
    doc.saveas(path)


def export_dxf(view, path, contours=None):
    """One DXF per view, in metres at model scale, with its annotation and text.

    A plan aligned to the others is in the model's coordinates, so Revit can place it origin to
    origin under the model. Any other view is local to itself.
    """
    from .views import in_model_frame

    doc = ezdxf.new("R2018", setup=False)
    doc.units = ezdxf.units.M
    msp = doc.modelspace()
    k = 1.0 / (view.upm * 1000.0)
    if in_model_frame(view):
        place = view.to_m
    else:
        def place(q):
            return (q[0] * k, q[1] * k)
    text_height = 0.0025 * (view.scale or 100)
    for layer, prims in (("MODEL", view.prims), ("ANNOTATION", view.annotation)):
        for p in prims:
            pts = [place(q) for q in p.points]
            if not pts:
                continue
            if p.kind == "text":
                if p.text and p.text.strip():
                    msp.add_text(p.text.strip(), height=text_height, dxfattribs={"layer": layer + "_TEXT", "insert": pts[0]})
            elif p.kind in ("contour", "polygon") and len(pts) > 2:
                msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer + "_CUT" if layer == "MODEL" else layer})
            elif p.kind in ("line", "polyline") and len(pts) > 1:
                msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
            elif p.kind == "circle" and p.radius:
                msp.add_circle(pts[0], p.radius[0] * k, dxfattribs={"layer": layer})
    if contours and in_model_frame(view):
        _contour_layers(doc, msp, contours)
    doc.saveas(path)


# ---- verification ------------------------------------------------------------
def overlay_plan(view, building, level, path, width=2400):
    """Source plan in grey with the reconstructed elements of that level on top."""
    pts = [view.to_m(q) for p in view.prims for q in p.points if p.kind != "text"]
    if not pts:
        return
    x0 = min(p[0] for p in pts)
    x1 = max(p[0] for p in pts)
    y0 = min(p[1] for p in pts)
    y1 = max(p[1] for p in pts)
    k = width / (x1 - x0)
    im = Image.new("RGB", (width, int((y1 - y0) * k) + 1), "white")
    d = ImageDraw.Draw(im)
    def tr(q):
        return ((q[0] - x0) * k, (y1 - q[1]) * k)
    for p in view.prims:
        if p.kind != "text" and len(p.points) > 1:
            d.line([tr(view.to_m(q)) for q in p.points], fill=(190, 190, 190))
    for rec in building["elements"]:
        if rec.get("level") != level or rec["class"] not in ("wall", "floor", "railing"):
            continue
        g = rec["geometry"]
        outline = {"wall": (40, 150, 70), "floor": (120, 170, 230), "railing": (150, 40, 200)}[rec["class"]]
        if rec["class"] == "railing":
            for route in g.get("path", []):
                d.line([tr(c) for c in route], fill=outline, width=3)
            continue
        if rec.get("cloned_from"):
            outline = (230, 140, 30)
        for poly in _footprint(g):
            ring = [tr(c) for c in poly]
            d.line(ring + [ring[0]], fill=outline, width=2)
    for rec in building["openings"]:
        if rec.get("level") != level:
            continue
        c = tr(rec["center"])
        col = (230, 40, 40) if rec.get("host") is None else (200, 50, 180)
        r = 5
        d.ellipse((c[0] - r, c[1] - r, c[0] + r, c[1] + r), outline=col, width=2)
    im.save(path)


def render_contours(view, outline, path, width=2400):
    """The storey's plan in grey, its main floor outline in red and its balconies in orange."""
    pts = [q for poly in outline["main"] for q in poly["outer"]] + [q for b in outline["balconies"] for poly in b for q in poly["outer"]]
    if not pts:
        return
    x0, x1 = min(p[0] for p in pts) - 2, max(p[0] for p in pts) + 2
    y0, y1 = min(p[1] for p in pts) - 2, max(p[1] for p in pts) + 2
    k = width / (x1 - x0)
    im = Image.new("RGB", (width, int((y1 - y0) * k) + 1), "white")
    d = ImageDraw.Draw(im)

    def tr(q):
        return ((q[0] - x0) * k, (y1 - q[1]) * k)

    if view is not None:
        for p in view.prims:
            if p.kind != "text" and len(p.points) > 1:
                d.line([tr(view.to_m(q)) for q in p.points], fill=(200, 200, 200))
    for colour, polys in [((230, 0, 0), outline["main"])] + [((255, 140, 0), b) for b in outline["balconies"]]:
        for poly in polys:
            for ring in [poly["outer"]] + list(poly.get("holes") or []):
                r = [tr(q) for q in ring]
                d.line(r + [r[0]], fill=colour, width=4)
    im.save(path)


def render_site(building, path, width=1800):
    """Plot, building lines, ground floor walls and area cells of the lowest residential floor."""
    site = building["site"]
    rings = [site["plot"]["ring"]] + [b["points"] for b in site["building_lines"]]
    xs = [q[0] for r in rings for q in r]
    ys = [q[1] for r in rings for q in r]
    margin = 3.0
    x0, x1, y0, y1 = min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin
    k = width / (x1 - x0)
    im = Image.new("RGB", (width, int((y1 - y0) * k)), "white")
    d = ImageDraw.Draw(im)

    def tr(q):
        return ((q[0] - x0) * k, (y1 - q[1]) * k)

    for rec in building["elements"]:
        if rec["class"] == "wall" and rec["level"] == "L01":
            for ring in _footprint(rec["geometry"]):
                d.polygon([tr(c) for c in ring], fill=(210, 210, 210), outline=(120, 120, 120))
    plot = [tr(c) for c in site["plot"]["ring"]]
    d.line(plot + [plot[0]], fill=(30, 60, 220), width=4)
    for line in site["building_lines"]:
        pts = [tr(c) for c in line["points"]]
        if line["closed"]:
            pts.append(pts[0])
        d.line(pts, fill=(220, 40, 40), width=3)
    im.save(path)


def render_area_plan(floor, path, width=1300, revit_lines=None):
    """The DWF's cells; with Revit's lines on top when given: green fits, red is extra, orange is missing."""
    pts = [q for seg in floor["segments"] for q in seg] + [q for seg in (revit_lines or []) for q in seg]
    x0, x1 = min(q[0] for q in pts) - 1.5, max(q[0] for q in pts) + 1.5
    y0, y1 = min(q[1] for q in pts) - 1.5, max(q[1] for q in pts) + 1.5
    k = width / (x1 - x0)
    im = Image.new("RGB", (width, int((y1 - y0) * k)), "white")
    d = ImageDraw.Draw(im)

    def tr(q):
        return ((q[0] - x0) * k, (y1 - q[1]) * k)

    if revit_lines is None:
        for a, b in floor["segments"]:
            d.line([tr(a), tr(b)], fill=(40, 40, 40), width=2)
    else:
        extra, missing = compare_lines(floor["segments"], revit_lines)
        for a, b in floor["segments"]:
            d.line([tr(a), tr(b)], fill=(40, 170, 80), width=2)
        for a, b in missing:
            d.line([tr(a), tr(b)], fill=(240, 150, 0), width=4)
        for a, b in extra:
            d.line([tr(a), tr(b)], fill=(230, 0, 0), width=4)
    for c in floor["cells"]:
        d.text(tr(c["point"]), f"{c['n']}:{c['c']}", fill=(0, 0, 190))
    im.save(path)


def compare_lines(dwf, revit, tol=0.03):
    """Lines of one set not covered by the other: (in Revit only, in the DWF only)."""
    def uncovered(lines, other):
        shape = unary_union([LineString(q) for q in other]).buffer(tol, cap_style=2) if other else None
        out = []
        for a, b in lines:
            line = LineString([a, b])
            rest = line if shape is None else line.difference(shape)
            for piece in getattr(rest, "geoms", [rest]):
                if not piece.is_empty and piece.length > tol * 2:
                    out.append(list(piece.coords))
        return out

    return uncovered(revit, dwf), uncovered(dwf, revit)


def compare_areas(building, result):
    """Revit's area plans against the DWF, level by level."""
    rows = []
    for floor in building.get("areas", []):
        for lid in floor["levels"]:
            level = result.get(lid)
            if level is None:
                rows.append({"level": lid, "missing": True})
                continue
            extra, missing = compare_lines(floor["segments"], level["lines"])
            enclosed = [a for a in level["areas"] if a["area"] > 1e-6]
            off = [a for a in level["areas"] if abs(a["area"] - a["table"]) > max(0.1, 0.01 * a["table"])]
            rows.append({
                "level": lid,
                "cells": len(floor["cells"]),
                "areas": len(level["areas"]),
                "enclosed": len(enclosed),
                "off_table": off,
                "table_m2": round(sum(a["table"] for a in level["areas"]), 2),
                "revit_m2": round(sum(a["area"] for a in level["areas"]), 2),
                "lines_extra_m": round(sum(LineString(q).length for q in extra), 2),
                "lines_missing_m": round(sum(LineString(q).length for q in missing), 2),
            })
    return rows


def write_area_comparison(path, rows):
    out = ["# Area plans: Revit against the DWF", ""]
    out += ["| Level | Cells | Areas | Enclosed | Table m2 | Revit m2 | Off the table | Lines only in Revit (m) | Lines only in the DWF (m) |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        if r.get("missing"):
            out.append(f"| {r['level']} | no area plan in the Revit result | | | | | | | |")
            continue
        out.append(f"| {r['level']} | {r['cells']} | {r['areas']} | {r['enclosed']} | {r['table_m2']} | {r['revit_m2']} | {len(r['off_table'])} | {r['lines_extra_m']} | {r['lines_missing_m']} |")
    out.append("")
    bad = [(r["level"], a) for r in rows if not r.get("missing") for a in r["off_table"]]
    if bad:
        out += ["## Areas off the table", "", "| Level | Number | Table m2 | Revit m2 |", "|---|---|---:|---:|"]
        out += [f"| {lid} | {a['number']} | {a['table']} | {a['area']} |" for lid, a in bad]
        out.append("")
    with open(path, "w", encoding="utf8") as fh:
        fh.write("\n".join(out))


def _footprint(g):
    if g["kind"] == "axis":
        (x0, y0), (x1, y1) = g["p0"], g["p1"]
        length = math.dist(g["p0"], g["p1"])
        if length < 1e-6:
            return []
        nx, ny = -(y1 - y0) / length * g["width"] / 2, (x1 - x0) / length * g["width"] / 2
        return [[(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)]]
    if g["kind"] == "treads":
        return [t["polygon"] for t in g["treads"]]
    return [p["outer"] for p in g["polygons"]]


def check_against_sources(building, extractor):
    """Compare the rebuilt model with properties and with section views."""
    out = {}
    length_err, volume_err, floor_err, z_err = [], [], [], []
    by_source = collections.Counter()
    for rec in building["elements"]:
        el = extractor.elements.get(rec["id"])
        p = el.props if el is not None else {}
        g = rec["geometry"]
        by_source[(rec["class"], "cloned" if rec.get("cloned_from") else g.get("source", ""))] += 1
        if rec["class"] == "wall":
            prop_len = extractor._length(p.get("Length"))
            if g["kind"] == "axis" and prop_len:
                length_err.append(abs(math.dist(g["p0"], g["p1"]) - prop_len))
            vol = _num(p.get("Volume"))
            if g["kind"] == "axis" and vol and rec["z1"] > rec["z0"]:
                model_vol = math.dist(g["p0"], g["p1"]) * g["width"] * (rec["z1"] - rec["z0"])
                volume_err.append(model_vol / vol)
            zr = extractor.z_range(rec["id"])
            if zr and rec["height_source"] == "properties":
                z_err.append(max(rec["z0"] - zr[0], zr[1] - rec["z1"], 0.0))  # drawn outside the modelled range
        if rec["class"] == "floor" and g["kind"] == "extrusion" and _num(p.get("Area")):
            area = sum(Polygon(q["outer"], q.get("holes")).area for q in g["polygons"])
            floor_err.append(area / _num(p.get("Area")))
    out["wall_length_abs_error_m"] = _summary(length_err)
    out["walls_more_than_0.5m_off_revit_length"] = sum(1 for e in length_err if e > 0.5)
    out["wall_volume_ratio_model_over_revit"] = _summary(volume_err)
    out["floor_area_ratio_model_over_revit"] = _summary(floor_err)
    out["railing_path_length_over_revit"] = _summary([
        r["geometry"]["path_length_ratio"] for r in building["elements"]
        if r["class"] == "railing" and r["geometry"].get("path_length_ratio") is not None
    ])
    out["wall_drawn_outside_modelled_height_m"] = _summary(z_err)
    out["geometry_source_counts"] = {f"{k[0]}/{k[1]}": v for k, v in sorted(by_source.items())}
    return out


def _num(v):
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError):
        return None


def _summary(values):
    if not values:
        return {"count": 0}
    s = sorted(values)
    q = lambda f: round(s[min(len(s) - 1, int(f * len(s)))], 3)
    return {"count": len(s), "p05": q(0.05), "median": q(0.5), "p95": q(0.95), "max": round(s[-1], 3)}


def level_summary(building, extractor):
    """Height and slab area per level, modelled against the Revit Area property."""
    order = list(building["levels"].items())
    rows = []
    for i, (lid, lv) in enumerate(order):
        above = order[i + 1][1]["z"] if i + 1 < len(order) else None
        modelled = sum(
            Polygon(q["outer"], q.get("holes")).area
            for e in building["elements"]
            if e["class"] == "floor" and e["level"] == lid
            for q in e["geometry"]["polygons"]
        )
        revit, slabs, built = 0.0, 0, 0
        built_ids = {e["id"] for e in building["elements"]}
        built_ids |= {i for e in building["elements"] for i in e["geometry"].get("merged_ids", [])}
        for el in extractor.elements.values():
            if extractor.rules.classify(el.family, el.props.get("Type Name")) != "floor":
                continue
            if level_id(el.props.get("Level")) == lid:
                revit += _num(el.props.get("Area")) or 0.0
                slabs += 1
                built += el.id in built_ids
        rows.append({
            "level": lid,
            "z": lv["z"],
            "storey_height": round(above - lv["z"], 3) if above is not None else None,
            "source": lv["source"],
            "slabs": slabs,
            "slabs_built": built,
            "slab_area_revit": round(revit, 1),
            "slab_area_model": round(modelled, 1),
        })
    return rows


def check_geometry(ifc_file):
    """Build every element's solid, voids included. Returns (built, [failed element names])."""
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    built, failed = 0, []
    for product in ifc_file.by_type("IfcElement"):
        if product.is_a("IfcOpeningElement"):
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            if not shape.geometry.verts:
                raise RuntimeError("empty")
            built += 1
        except Exception:
            failed.append(product.Name)
    return built, failed


# ---- 3D preview --------------------------------------------------------------
def render_3d(building, path, azimuth=-55.0, elevation=32.0, size=(2000, 1500), cut_z=None):
    az, el = math.radians(azimuth), math.radians(elevation)
    ca, sa, ce, se = math.cos(az), math.sin(az), math.cos(el), math.sin(el)

    def project(x, y, z):
        rx = x * ca - y * sa
        ry = x * sa + y * ca
        return (rx, ry * se + z * ce, ry * ce - z * se)

    light = (0.45, -0.55, 0.7)
    n = math.sqrt(sum(c * c for c in light))
    light = tuple(c / n for c in light)
    faces = []

    def add_prism(ring, z0, z1, color):
        if cut_z is not None:
            if z0 >= cut_z:
                return
            z1 = min(z1, cut_z)
        if len(ring) < 3 or z1 <= z0:
            return
        area = sum(ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1] for i in range(len(ring)))
        pts = ring if area > 0 else ring[::-1]
        for i in range(len(pts)):
            a, b = pts[i], pts[(i + 1) % len(pts)]
            dx, dy = b[0] - a[0], b[1] - a[1]
            lg = math.hypot(dx, dy) or 1.0
            normal = (dy / lg, -dx / lg, 0.0)
            faces.append(([(a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)], normal, color))
        faces.append(([(p[0], p[1], z1) for p in pts], (0.0, 0.0, 1.0), color))

    for rec in building["elements"]:
        color = _SHADE.get(rec["class"], (180, 180, 180))
        g = rec["geometry"]
        if g["kind"] == "treads":
            for t in g["treads"]:
                add_prism([tuple(c) for c in t["polygon"]], t["z_top"] - 0.3, t["z_top"], color)
        else:
            for ring in _footprint(g):
                add_prism([tuple(c) for c in ring], rec["z0"], rec["z1"], color)
    for rec in building["openings"]:
        cx, cy = rec["center"]
        ux, uy = rec["direction"]
        w, d = rec["width"] / 2, rec.get("depth", 0.2) / 2 + 0.01
        nx, ny = -uy, ux
        ring = [
            (cx - ux * w + nx * d, cy - uy * w + ny * d),
            (cx + ux * w + nx * d, cy + uy * w + ny * d),
            (cx + ux * w - nx * d, cy + uy * w - ny * d),
            (cx - ux * w - nx * d, cy - uy * w - ny * d),
        ]
        add_prism(ring, rec["z0"], rec["z0"] + rec["height"], _SHADE.get(rec["class"]))

    projected = []
    view_dir = (sa * ce, ca * ce, -se)  # direction from model towards the viewer's opposite
    for pts, normal, color in faces:
        facing = normal[0] * view_dir[0] + normal[1] * view_dir[1] + normal[2] * view_dir[2]
        if facing > 0.02:
            continue
        pp = [project(*p) for p in pts]
        depth = sum(p[2] for p in pp) / len(pp)
        lit = 0.55 + 0.45 * max(0.0, normal[0] * light[0] + normal[1] * light[1] + normal[2] * light[2])
        projected.append((depth, pp, tuple(min(255, int(c * lit)) for c in color)))
    if not projected:
        return
    xs = [p[0] for _, pp, _ in projected for p in pp]
    ys = [p[1] for _, pp, _ in projected for p in pp]
    k = min((size[0] - 80) / (max(xs) - min(xs)), (size[1] - 80) / (max(ys) - min(ys)))
    ox, oy = 40 - min(xs) * k, size[1] - 40 + min(ys) * k
    im = Image.new("RGB", size, (245, 246, 248))
    d = ImageDraw.Draw(im)
    for _, pp, color in sorted(projected, key=lambda t: -t[0]):
        d.polygon([(p[0] * k + ox, oy - p[1] * k) for p in pp], fill=color, outline=tuple(int(c * 0.72) for c in color))
    im.save(path)


# ---- report ------------------------------------------------------------------
def write_report(path, source_name, building, checks, ifc_counts, manifest, validation, levels_table):
    b = building
    out = [f"# Build report - {source_name}", ""]
    out += ["## Result", "", "| Class | In DWF | Modelled |", "|---|---:|---:|"]
    by_class = b["stats"]["elements_by_class"]
    for cls in sorted(ifc_counts):
        out.append(f"| {cls} | {by_class.get(cls, 0)} | {ifc_counts[cls]} |")
    out += ["", f"Unresolved elements (no usable geometry): **{len(b['unresolved'])}**", ""]
    cnt = collections.Counter((u["class"], u["level"]) for u in b["unresolved"])
    if cnt:
        out += ["| Class | Level | Count |", "|---|---|---:|"]
        out += [f"| {k[0]} | {k[1]} | {v} |" for k, v in sorted(cnt.items(), key=str)]
        out.append("")
    for info in b.get("typical_floors", {}).values():
        out += [f"## Typical floor {info['level']}", ""]
        out += [f"Exact copy of {info['copied_from']}: {info['copied']}. Walls already standing there, rising from another level: {info['walls_already_standing']}."]
        twins = info["source_elements_without_a_twin"]
        out += [f"Source elements of this level with no same-size element on {info['copied_from']}, left out so the floors stay identical: {len(twins)}."]
        counts = collections.Counter(t["class"] for t in twins)
        if counts:
            out += ["", "| Class | Count |", "|---|---:|"] + [f"| {k} | {v} |" for k, v in sorted(counts.items())]
        out.append("")
    out += ["## Levels, heights and slab areas", ""]
    out += ["| Level | Elevation (m) | Storey height (m) | Slabs built | Slab area, Revit (m2) | Slab area, model (m2) | Height source |", "|---|---:|---:|---:|---:|---:|---|"]
    for r in reversed(levels_table):
        storey = f"{r['storey_height']:.2f}" if r["storey_height"] is not None else ""
        out.append(f"| {r['level']} | {r['z']:.2f} | {storey} | {r['slabs_built']} of {r['slabs']} | {r['slab_area_revit']:.1f} | {r['slab_area_model']:.1f} | {r['source']} |")
    tops = [e["z1"] for e in b["elements"] if e["class"] in ("wall", "floor")]
    out.append("")
    if tops:
        out.append(f"Highest wall or slab top: {max(tops):.2f} m above project zero.")
    if b.get("datum_elevation") is not None:
        out.append(f"Project zero = absolute {b['datum_elevation']} m.")
    out.append("Slab area is the sum of slab outlines per level. It is not a regulatory floor area, which needs the area plan.")
    out += ["", "## Plan alignment", "", "| Link | Votes | Peak margin | Common elements | Coinciding |", "|---|---:|---:|---:|---:|"]
    out += [f"| {k} | {v['votes']} | {v['margin']} | {v['common_elements']} | {v['coinciding_elements']} |" for k, v in b["plan_alignment"].items()]
    floors = [e for e in b["elements"] if e["class"] == "floor"]
    cloned = [e for e in b["elements"] + b["openings"] if e.get("cloned_from")]
    out += ["", "## Inferred geometry (review first)", ""]
    out += [f"- Copied from another level, typical floors included: {len(cloned)}"]
    out += [f"- Floors whose outline misses the Revit area by more than 15%: {sum(1 for e in floors if e['geometry'].get('area_error', 0) > 0.15)} of {len(floors)}"]
    out += [f"- Openings without a host wall (not cut): {sum(1 for o in b['openings'] if not o.get('host'))}"]
    stairs = [e for e in b["elements"] if e["class"] == "stair"]
    out += [f"- Stairs: {len(stairs)}, way up read from the plans on {sum(1 for e in stairs if e['geometry'].get('direction_checked'))}. Each is rebuilt in Revit from flights; the IFC shows only the treads visible in plan"]
    out += ["", "## Checks against source", ""]
    for k, v in checks.items():
        out.append(f"- `{k}`: {v}")
    out += ["", "## Excluded by configuration", ""]
    out += [f"- {k}: {v}" for k, v in sorted(b["stats"]["excluded_by_class"].items())] or ["- none"]
    if b["stats"]["unclassified_families"]:
        out += ["", "## Unclassified families (not modelled - add to config/classification.json)", ""]
        out += [f"- {k}: {v}" for k, v in sorted(b["stats"]["unclassified_families"].items())]
    out += ["", "## IFC validation", "", f"{validation}", ""]
    site = b.get("site")
    if site:
        out += ["## Site", ""]
        if site.get("plot"):
            out += [f"- Plot line from the {site['plot'].get('source', 'Revit sheet')}: {site['plot']['area']} m2, on the Revit sheet: {site.get('revit_sheet_plot_area', site['plot']['area'])} m2."]
        for line in site.get("building_lines", []):
            kind = "closed" if line["closed"] else "open"
            out += [f"- Building line from the {line.get('source', 'Revit sheet')}: {kind}, {len(line['points'])} points, area {line['property_area']}."]
        if site.get("building_line_vs_revit_sheet_m") is not None:
            out += [f"- Survey building line against the Revit sheet: worst corner {site['building_line_vs_revit_sheet_m']} m, areas {site['building_lines'][0]['property_area']} and {site.get('building_line_area_on_revit_sheet')} m2."]
        placement = site.get("placement")
        if placement:
            out += [f"- Placement: rotation {placement['rotation_deg']} deg, internal origin at E {placement['easting']}, N {placement['northing']}, elevation {site.get('datum_elevation')}. Worst corner against the survey: {placement['fit_max_m']} m."]
        else:
            out += ["- The model is not placed."]
        if site.get("slab_clip"):
            out += [f"- Slabs fitted to the area plan: {site['slab_clip']}."]
        out.append("")
    rules = b.get("modelling")
    if rules:
        out += ["## Modelling rules", "",
                f"- Walls raised to start at their level: {rules['walls_raised_to_level']}. Levels kept as drawn: {', '.join(rules['levels_kept_as_drawn'])}",
                f"- Walls run up to the level above, to meet the wall there: {rules['walls_raised_to_level_above']}",
                f"- Walls merged into the wall they continue: {rules['walls_merged_into_others']}",
                f"- Wall pieces joined along their line: {rules.get('walls_joined_along_their_line', 0)}, of which took another type: {len(rules.get('walls_that_took_another_type', []))}; shelter walls kept apart: {rules.get('shelter_walls_kept_apart', 0)}",
                f"- Main slabs extended to the outer face of the exterior walls' core: {rules['slabs_extended_to_wall_core']}",
                f"- Floors trimmed where they overlapped a stronger floor: {rules['floors_trimmed']}",
                f"- Floors removed as covered by another: {len(rules['floors_removed_as_covered'])}",
                f"- Walls moved to stand on the wall below (inferred): {len(rules.get('walls_aligned_to_wall_below', []))}",
                f"- Walls left with no length after the walls they meet moved, so not built: {[r['id'] for r in rules.get('walls_left_without_length', [])]}",
                f"- Curved walls built from arcs: {rules.get('curved_walls', 0)}, in {rules.get('curved_wall_pieces', 0)} pieces",
                f"- Exterior walls, to get exterior plaster in Revit: {rules['exterior_walls']} (curved walls not marked: {rules['curved_walls_not_marked']}). Ground level walls get no plaster.",
                f"- Ground floors added inside the plot (inferred): {len(rules.get('ground_floors_added', []))}, "
                f"{round(sum(r['area_m2'] for r in rules.get('ground_floors_added', [])), 1)} m2. Only stairs stay open.", ""]
        if rules.get("ground_filled_with_care"):
            out += ["Ground floors filled where the drawing has none, to check:", "", "| Floor | At | Area m2 | Why |", "|---|---|---:|---|"]
            out += [f"| {r['id']} | {r['where']} | {r['area_m2']} | {r['reason']} |" for r in rules["ground_filled_with_care"]]
            out.append("")
        moved = [r for r in rules.get("walls_aligned_to_wall_below", []) if abs(r["moved_m"]) >= 0.3]
        if moved:
            out += ["Walls moved 30 cm or more to stand on the wall below; check these:", "", "| Wall | Level | Moved m |", "|---|---|---:|"]
            out += [f"| {r['id']} | {r['level']} | {r['moved_m']} |" for r in moved]
            out.append("")
        if rules["floors_removed_as_covered"]:
            out += ["| Floor | Level | Type | Area m2 |", "|---|---|---|---:|"]
            out += [f"| {r['id']} | {r['level']} | {r['type']} | {r['area_m2']} |" for r in rules["floors_removed_as_covered"]]
            out.append("")
    outlines = (b.get("modelling") or {}).get("floor_contours")
    if outlines and outlines.get("storeys"):
        out += ["## Floor outlines", "",
                "Each storey's main floor and balconies are outlined first, and its floors are built from the outlines. "
                "See `qa/contour_<level>.png` (main floor red, balconies orange) and the layers in the plan DXFs and `views/CONTOUR_<level>.dxf`.", "",
                "| Level | Main floor m2 | Shafts | Balconies | Balconies m2 | From | Drawn floors joined into the main floor |", "|---|---:|---:|---:|---:|---|---:|"]
        out += [f"| {x['level']} | {x['main_m2']} | {x['shafts']} | {x['balconies']} | {x['balconies_m2']} | {x['from']} | {x['floors_joined_into_it']} |"
                for x in outlines["storeys"]]
        out += ["", f"Drawn floors replaced by the outlines: {len(outlines['floors_replaced'])}", ""]
    if b.get("sections"):
        out += ["## Section lines", "", "| Section | Drawing | From | To | Looks | Side from |", "|---|---|---|---|---|---|"]
        out += [f"| {x['name']} | {x['view']} | {x['p0']} | {x['p1']} | {x['look']} | {x['look_source']} |" for x in b["sections"]]
        out.append("")
    if site and site.get("building_lines_left_out"):
        out += [f"Building lines on the Revit sheet not used, as the closed line stands alone: {site['building_lines_left_out']}", ""]
    if b.get("apartments"):
        apartments = b["apartments"]
        if apartments["source"] == "inferred":
            out += ["## Apartments (inferred)", "", "The area plan's apartment column is empty. Apartments are inferred from residential cells and what borders them. Check this list.", ""]
        else:
            out += ["## Apartments (from the area table)", ""]
        out += ["| IL_UnitID | Level | Cells | Area m2 | Shelters |", "|---:|---|---|---:|---:|"]
        out += [f"| {u['unit']} | {u['level']} | {', '.join(u['cells'])} | {u['area_m2']} | {u['shelters']} |" for u in apartments["units"]]
        out.append("")
        odd = [u["unit"] for u in apartments["units"] if u["shelters"] != 1]
        if odd:
            out += [f"Apartments without exactly one shelter, to check: {odd}", ""]
        if apartments["parts_with_no_apartment"]:
            out += [f"Parts with no apartment next to them, given 0: {apartments['parts_with_no_apartment']}", ""]
    if site and site.get("storey_slabs"):
        out += ["## Storey slabs", "", "From level 01 up, slabs inside the outer walls and within 5 cm of each other became one floor. Balconies, lower slabs, the ground floor and the basements stay separate.", ""]
        out += ["| Level | Slabs joined | Area m2 | Type | Thickness m | Thicknesses in the source m |", "|---|---:|---:|---|---:|---|"]
        for lid, rows in site["storey_slabs"].items():
            out += [f"| {lid} | {r['slabs_joined']} | {r['area_m2']} | {r['type']} | {r['thickness_m']} | {', '.join(map(str, r['depths_in_source_m']))} |" for r in rows]
        out.append("")
    if b.get("masses_left_out"):
        counts = collections.Counter(m["reason"] for m in b["masses_left_out"])
        out += ["## Masses left out", "", "Only thin horizontal plates are built, as floors.", ""]
        out += [f"- {reason}: {n}" for reason, n in counts.most_common()]
        out += ["", "| Id | Family | Thickness m | Area m2 | Reason |", "|---|---|---:|---:|---|"]
        out += [f"| {m['id']} | {m['family']} | {m['thickness_m']} | {m['area_m2']} | {m['reason']} |" for m in b["masses_left_out"]]
        out.append("")
    if b.get("areas"):
        out += ["## Area plan", "", "| Floors | Levels | Table total m2 | Cells placed | Rows with no cell | Cell area vs table | Walls inside cells | Lines in no cell, left out (m) |", "|---|---|---:|---:|---:|---:|---:|---:|"]
        for floor in b["areas"]:
            tot = sum(c["model_area"] for c in floor["cells"]) or 1
            ref = sum(c["t"] for c in floor["cells"]) or 1
            out.append(f"| {', '.join(map(str, floor['floors']))} | {', '.join(floor['levels'])} | {floor['total']} | {len(floor['cells'])} | {len(floor['rows_without_a_cell'])} | {tot / ref:.3f} | {floor['walls_inside_share']:.0%} | {floor.get('lines_left_out_m', 0)} |")
        out.append("")
    if b["issues"]:
        out += ["## Issues", ""] + [f"- {i}" for i in b["issues"]] + [""]
    out += ["## Views", "", "| Key | Kind | Scale | Level | Primary | Height calibrated |", "|---|---|---:|---|---|---|"]
    for m in manifest:
        out.append(f"| {m['key']} | {m['kind']} | 1:{m['scale']} | {m['level'] or ''} | {'yes' if m['primary_plan'] else ''} | {'yes' if m['height_calibrated'] else ''} |")
    out.append("")
    with open(path, "w", encoding="utf8") as fh:
        fh.write("\n".join(out))
