"""Modelling conventions that make the model clean to build in Revit, whatever the drawings did.

Each rule is set in config/site.json under "modelling", and what it changed goes in the report.
"""
from __future__ import annotations

import math
from collections import defaultdict

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

from .extract import _slab_outline
from .names import clean_text
from .site import _CFG, _difference

RULES = _CFG["modelling"]


def _is_roof(name) -> bool:
    text = clean_text(name).lower()
    return any(word.lower() in text for word in RULES["roof_level_words"])


def walls_from_level(building) -> dict:
    """A wall starts at its level, not below it. Ground floor and roof walls keep their heights.

    Drawings often run a wall down through the slab under it. In Revit that wall then crosses
    the slab, so it is raised to sit on it. Only a base below the level moves; the top stays.
    """
    levels = building["levels"]
    top = max(levels.values(), key=lambda info: info["z"])["z"] if levels else None
    kept = {lid for lid, info in levels.items()
            if abs(info["z"]) < 0.01 or info["z"] == top or _is_roof(info.get("source_name") or lid)}
    raised = 0
    for rec in building["elements"]:
        if rec["class"] != "wall" or rec["level"] not in levels or rec["level"] in kept:
            continue
        z = levels[rec["level"]]["z"]
        if rec["z0"] < z - 0.005 and rec["z1"] > z + 0.1:
            rec["z0"] = z
            rec["base_from"] = "level"
            raised += 1
    return {"walls_raised_to_level": raised, "levels_kept_as_drawn": sorted(kept)}


def walls_to_level_above(building) -> dict:
    """A wall that stops just under the storey above runs up to that level, to meet the wall standing there.

    Drawings stop a wall under the slab above it. Here walls run floor to floor, and the joins with
    the floors settle the slab. A wall stopping more than the reach below, such as a parapet, keeps its top.
    """
    reach = RULES["wall_top_reach_m"]
    levels = sorted(building["levels"].items(), key=lambda kv: kv[1]["z"])
    raised = 0
    for rec in building["elements"]:
        if rec["class"] != "wall":
            continue
        above = [(lid, info["z"]) for lid, info in levels if 0.005 < info["z"] - rec["z1"] <= reach and info["z"] > rec["z0"] + 0.5]
        if not above:
            continue
        lid, z = above[0]
        rec["z1"], rec["top_level"], rec["top_offset"] = z, lid, 0.0
        rec["top_from"] = "level"
        raised += 1
    return {"walls_raised_to_level_above": raised}


def _direction(g):
    dx, dy = g["p1"][0] - g["p0"][0], g["p1"][1] - g["p0"][1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length else None


def merge_wall_runs(building) -> dict:
    """Straight walls of one kind that continue each other become one wall.

    They must share level, type, thickness, base and top, lie on one line within a centimetre, and
    touch or overlap end to end. Doors and windows move to the wall that stays.
    """
    tol = RULES["wall_merge_m"]
    groups = {}
    for rec in building["elements"]:
        g = rec["geometry"]
        if rec["class"] == "wall" and g.get("kind") == "axis" and g.get("p0") and _direction(g):
            key = (rec["level"], rec.get("source_type"), round(g["width"], 3), round(rec["z0"], 2), round(rec["z1"], 2), rec.get("top_level"))
            groups.setdefault(key, []).append(rec)
    absorbed = {}
    for walls in groups.values():
        changed = True
        while changed:
            changed = False
            for i, a in enumerate(walls):
                if a["id"] in absorbed:
                    continue
                ua = _direction(a["geometry"])
                for b in walls[i + 1:]:
                    if b["id"] in absorbed:
                        continue
                    ub = _direction(b["geometry"])
                    if abs(ua[0] * ub[1] - ua[1] * ub[0]) > 0.01:
                        continue
                    origin = a["geometry"]["p0"]
                    # offset of b's ends from a's line, and their positions along it
                    def along(p):
                        return (p[0] - origin[0]) * ua[0] + (p[1] - origin[1]) * ua[1]

                    def off(p):
                        return abs((p[0] - origin[0]) * ua[1] - (p[1] - origin[1]) * ua[0])

                    pb = (b["geometry"]["p0"], b["geometry"]["p1"])
                    if max(off(p) for p in pb) > tol:
                        continue
                    ta = sorted((0.0, along(a["geometry"]["p1"])))
                    tb = sorted(along(p) for p in pb)
                    if tb[0] > ta[1] + tol or tb[1] < ta[0] - tol:
                        continue
                    lo, hi = min(ta[0], tb[0]), max(ta[1], tb[1])
                    a["geometry"]["p0"] = [round(origin[0] + ua[0] * lo, 4), round(origin[1] + ua[1] * lo, 4)]
                    a["geometry"]["p1"] = [round(origin[0] + ua[0] * hi, 4), round(origin[1] + ua[1] * hi, 4)]
                    a.setdefault("merged_from", []).append(b["id"])
                    absorbed[b["id"]] = a["id"]
                    ua = _direction(a["geometry"])
                    changed = True
    for o in building.get("openings", []):
        while o.get("host") in absorbed:
            o["host"] = absorbed[o["host"]]
    building["elements"] = [r for r in building["elements"] if r["id"] not in absorbed]
    return {"walls_merged_into_others": len(absorbed)}


def _axis_walls(building):
    by_level = defaultdict(list)
    for rec in building["elements"]:
        g = rec["geometry"]
        if rec["class"] == "wall" and g.get("kind") == "axis" and g.get("p0") and _direction(g):
            by_level[rec["level"]].append(rec)
    return by_level


def _alignment(rec, below, limit, min_overlap):
    """How far to move a wall sideways so it stands on a parallel wall below, or None.

    Two exterior walls line up their outside faces, so the facade is flush. Other walls take the
    smallest move that puts axis on axis or a face on a face. A wall already standing on one below does not move.
    """
    g = rec["geometry"]
    u = _direction(g)
    n = (-u[1], u[0])
    length = math.dist(g["p0"], g["p1"])
    faces, axes = [], []
    for other in below:
        h = other["geometry"]
        v = _direction(h)
        if abs(u[0] * v[1] - u[1] * v[0]) > 0.02:
            continue
        rel = [(q[0] - g["p0"][0], q[1] - g["p0"][1]) for q in (h["p0"], h["p1"])]
        t = sorted(q[0] * u[0] + q[1] * u[1] for q in rel)
        overlap = min(length, t[1]) - max(0.0, t[0])
        if overlap < min_overlap * length:
            continue
        d = rel[0][0] * n[0] + rel[0][1] * n[1]
        same_way = 1 if u[0] * v[0] + u[1] * v[1] > 0 else -1
        if rec.get("exterior_side") and other.get("exterior_side") and rec["exterior_side"] == other["exterior_side"] * same_way:
            # two exterior walls: the outside faces line up, so the facade is flush whatever the thicknesses
            shift = d + rec["exterior_side"] * (h["width"] - g["width"]) / 2
            if abs(shift) <= limit:
                faces.append(shift)
            continue
        half = (h["width"] - g["width"]) / 2
        axes += [x for x in (d, d + half, d - half) if abs(x) <= limit]
    pool = faces or axes
    if not pool:
        return None
    shift = min(pool, key=abs)
    if abs(shift) < 0.005:
        return None
    return shift


def _move_wall(rec, shift, neighbours, openings):
    """Move a wall sideways, and take with it the ends of the walls that meet it and its doors and windows."""
    g = rec["geometry"]
    u = _direction(g)
    n = (-u[1], u[0])
    length = math.dist(g["p0"], g["p1"])
    origin = g["p0"]
    for other in neighbours:
        if other is rec:
            continue
        h = other["geometry"]
        v = _direction(h)
        across = v[0] * n[0] + v[1] * n[1]
        if abs(across) < 0.3:
            continue  # parallel: it continues the wall, and is aligned on its own
        for end in ("p0", "p1"):
            e = h[end]
            off = (e[0] - origin[0]) * n[0] + (e[1] - origin[1]) * n[1]
            along = (e[0] - origin[0]) * u[0] + (e[1] - origin[1]) * u[1]
            if abs(off) <= g["width"] / 2 + 0.05 and -0.3 <= along <= length + 0.3:
                k = shift / across
                h[end] = [round(e[0] + v[0] * k, 4), round(e[1] + v[1] * k, 4)]
    g["p0"] = [round(g["p0"][0] + n[0] * shift, 4), round(g["p0"][1] + n[1] * shift, 4)]
    g["p1"] = [round(g["p1"][0] + n[0] * shift, 4), round(g["p1"][1] + n[1] * shift, 4)]
    for o in openings:
        if o.get("host") == rec["id"] and o.get("center"):
            o["center"] = [round(o["center"][0] + n[0] * shift, 4), round(o["center"][1] + n[1] * shift, 4)]


def align_walls_to_below(building) -> dict:
    """A wall drawn a little off the wall under it is moved to stand exactly on it, storey by storey from the ground up.

    Only parallel walls that overlap along most of the upper wall's length count, and only a move up
    to the limit; a real setback is larger and stays. Walls that meet the moved wall follow it.
    """
    limit, min_overlap = RULES["wall_align_max_m"], RULES["wall_align_min_overlap"]
    walls = _axis_walls(building)
    levels = [lid for lid, info in sorted(building["levels"].items(), key=lambda kv: kv[1]["z"]) if info["z"] >= -0.01 and walls.get(lid)]
    moved = []
    for below, above in zip(levels, levels[1:]):
        for rec in walls[above]:
            shift = _alignment(rec, walls[below], limit, min_overlap)
            if shift is not None:
                _move_wall(rec, shift, walls[above], building.get("openings", []))
                rec["geometry"]["aligned_to_wall_below_m"] = round(shift, 3)
                moved.append({"id": rec["id"], "level": above, "moved_m": round(shift, 3)})
    return {"walls_aligned_to_wall_below": moved}


def _wall_shapes(walls, grow=0.0):
    shapes = []
    for rec in walls:
        g = rec["geometry"]
        if g.get("p0") and g.get("p1"):
            shapes.append(LineString([g["p0"], g["p1"]]).buffer(g["width"] / 2 + grow, cap_style=3 if grow else 2, join_style=2))
        else:
            shapes += [make_valid(Polygon(p["outer"])).buffer(grow) for p in g.get("polygons") or [] if len(p["outer"]) >= 3]
    return [s for s in shapes if not s.is_empty]


def _filled(shape):
    return unary_union([Polygon(g.exterior) for g in getattr(shape, "geoms", [shape]) if g.geom_type == "Polygon"])


def fit_arcs(path, tol):
    """A curved wall's centre path as few circular arcs and straight lines as fit it within tol.

    Each piece is [start, end] for a line or [start, middle, end] for an arc through those points.
    """
    pts = [tuple(q) for q in path]
    pieces, i = [], 0
    while i < len(pts) - 1:
        j = len(pts) - 1
        while j > i + 1 and _fits(pts[i:j + 1], tol) is None:
            j -= 1
        kind = _fits(pts[i:j + 1], tol) if j > i + 1 else "line"
        if kind == "line":
            pieces.append([list(pts[i]), list(pts[j])])
        else:
            pieces.append([list(pts[i]), list(pts[(i + j) // 2]), list(pts[j])])
        i = j
    return pieces


def _fits(pts, tol):
    a, b = pts[0], pts[-1]
    chord = math.dist(a, b)
    if chord < 1e-6:
        return None
    if all(abs((p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0])) / chord <= tol for p in pts):
        return "line"
    m = pts[len(pts) // 2]
    d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]))
    if abs(d) < 1e-9:
        return None
    ux = ((a[0] ** 2 + a[1] ** 2) * (m[1] - b[1]) + (m[0] ** 2 + m[1] ** 2) * (b[1] - a[1]) + (b[0] ** 2 + b[1] ** 2) * (a[1] - m[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (b[0] - m[0]) + (m[0] ** 2 + m[1] ** 2) * (a[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (m[0] - a[0])) / d
    r = math.dist((ux, uy), a)
    if all(abs(math.dist((ux, uy), p) - r) <= tol for p in pts):
        return "arc"
    return None


def curved_walls_as_arcs(building) -> dict:
    """A curved wall is built from arcs that follow its centre, not from many short straight walls."""
    tol = RULES["arc_tolerance_m"]
    walls, pieces = 0, 0
    for rec in building["elements"]:
        g = rec["geometry"]
        if rec["class"] == "wall" and g.get("kind") != "axis" and g.get("path") and len(g["path"]) >= 3 and isinstance(g["path"][0][0], (int, float)):
            g["arcs"] = fit_arcs(g["path"], tol)
            walls += 1
            pieces += len(g["arcs"])
    return {"curved_walls": walls, "curved_wall_pieces": pieces}


def fill_ground_floors(building) -> dict:
    """Inside the plot, the ground level has a floor everywhere: each gap between its floors gets one.

    A gap continues the floor it shares the longest edge with, at that floor's height and type.
    A gap mostly closed by walls, such as a shaft, stays open, and so does the place of an
    element that could not be built, such as a ramp; those are listed.
    """
    plot = ((building.get("site") or {}).get("plot") or {}).get("ring") or []
    ground = next((lid for lid, info in building["levels"].items() if abs(info["z"]) < 0.01), None)
    if len(plot) < 3 or ground is None:
        return {"ground_floors_added": [], "ground_left_open": []}
    floors = [r for r in building["elements"] if r["class"] == "floor" and r["level"] == ground and r["geometry"].get("polygons")]
    walls = [r for r in building["elements"] if r["class"] == "wall" and r["level"] == ground]
    stairs = [make_valid(Polygon(t["polygon"])) for r in building["elements"] if r["class"] == "stair" and r["level"] == ground
              for t in r["geometry"].get("treads") or [] if len(t.get("polygon") or []) >= 3]
    unbuilt = [(u, make_valid(Polygon(u["plan_outline"]))) for u in building.get("unresolved", [])
               if u.get("level") == ground and len(u.get("plan_outline") or []) >= 3]
    shapes = {r["id"]: _shape(r) for r in floors}
    wall_area = unary_union(_wall_shapes(walls)) if walls else Polygon()
    built = unary_union([s for s in shapes.values() if s is not None] + [wall_area] + stairs)
    open_ground = _difference(make_valid(Polygon(plot)), built)
    gaps = _difference(open_ground, unary_union([s for _, s in unbuilt])) if unbuilt else open_ground
    added, left = [], []
    for u, s in unbuilt:
        if s.intersection(open_ground).area > RULES["ground_fill_min_m2"]:
            left.append({"where": u["id"], "reason": "an element not built stands here: {} {}".format(u.get("class"), u.get("type"))})
    pieces = [g for g in getattr(gaps, "geoms", [gaps]) if g.geom_type == "Polygon" and g.area >= RULES["ground_fill_min_m2"]]
    for n, gap in enumerate(pieces):
        edge = gap.exterior
        walled = edge.intersection(wall_area.buffer(0.02)).length / edge.length if edge.length else 0.0
        where = [round(c, 2) for c in gap.representative_point().coords[0]]
        if walled >= RULES["ground_fill_walled_share"]:
            left.append({"where": where, "area_m2": round(gap.area, 2), "reason": "closed by walls"})
            continue
        shared = sorted(((edge.intersection(s.buffer(0.02)).length, rid) for rid, s in shapes.items() if s is not None), reverse=True)
        if not shared or shared[0][0] <= 0:
            left.append({"where": where, "area_m2": round(gap.area, 2), "reason": "no floor next to it"})
            continue
        source = next(r for r in floors if r["id"] == shared[0][1])
        rec = {k: v for k, v in source.items() if k not in ("geometry", "ifc_guid", "id")}
        rec.update(id="{}_fill{}".format(source["id"], n), ifc_guid=None, height_source="inferred", cloned_from=source["id"],
                   geometry={"kind": "extrusion", "polygons": _slab_outline(gap), "source": "ground_fill", "slab_role": "ground_fill"})
        building["elements"].append(rec)
        added.append({"id": rec["id"], "continues": source["id"], "type": source.get("source_type"), "area_m2": round(gap.area, 2)})
    return {"ground_floors_added": added, "ground_left_open": left}


def unify_ground_floors(building) -> dict:
    """Outside the building, the ground is one floor per height, running across garden walls to the plot edge.

    Floors whose tops lie within a few centimetres of each other join, bridging the walls and slivers
    between them, and take the type that covers most of them. Raised planters and sunken pits keep
    their own floor. The building's outline, stairs and the place of anything not built stay out.
    """
    plot = ((building.get("site") or {}).get("plot") or {}).get("ring") or []
    ground = next((lid for lid, info in building["levels"].items() if abs(info["z"]) < 0.01), None)
    outline = _building_outline(building)
    if len(plot) < 3 or ground is None or outline is None:
        return {"ground_floors_joined": []}
    plot_shape = make_valid(Polygon(plot))
    floors = [r for r in building["elements"] if r["class"] == "floor" and r["level"] == ground and r["geometry"].get("polygons")]
    shapes = {r["id"]: _shape(r) for r in floors}
    outside = [r for r in floors if shapes[r["id"]] is not None and shapes[r["id"]].difference(outline).area >= 0.5 * shapes[r["id"]].area]
    stairs = [make_valid(Polygon(t["polygon"])) for r in building["elements"] if r["class"] == "stair" and r["level"] == ground
              for t in r["geometry"].get("treads") or [] if len(t.get("polygon") or []) >= 3]
    unbuilt = [make_valid(Polygon(u["plan_outline"])) for u in building.get("unresolved", [])
               if u.get("level") == ground and len(u.get("plan_outline") or []) >= 3]
    groups = []
    for rec in sorted(outside, key=lambda r: r["z1"]):
        if groups and rec["z1"] - groups[-1][-1]["z1"] <= RULES["ground_same_height_m"]:
            groups[-1].append(rec)
        else:
            groups.append([rec])
    groups.sort(key=lambda g: -sum(shapes[r["id"]].area for r in g))
    keep_out = unary_union([outline] + stairs + unbuilt + [shapes[r["id"]] for r in floors if r not in outside and shapes[r["id"]] is not None])
    taken = Polygon()
    bridge = RULES["ground_bridge_m"]
    joined, gone = [], set()
    for group in groups:
        own = unary_union([shapes[r["id"]] for r in group])
        others = unary_union([shapes[r["id"]] for g in groups if g is not group for r in g])
        closed = own.buffer(bridge, join_style=2).buffer(-bridge, join_style=2)
        grown = unary_union([own, _difference(closed, unary_union([keep_out, others]))])
        grown = _difference(grown.intersection(plot_shape), unary_union([taken, outline, others]))
        if grown.is_empty or grown.area < RULES["floor_min_m2"]:
            continue
        taken = unary_union([taken, grown])
        by_type = {}
        for r in group:
            by_type[r.get("source_type")] = by_type.get(r.get("source_type"), 0.0) + shapes[r["id"]].area
        main_type = max(by_type, key=by_type.get)
        kept = max((r for r in group if r.get("source_type") == main_type), key=lambda r: shapes[r["id"]].area)
        kept["geometry"]["polygons"] = _slab_outline(grown)
        kept["geometry"]["slab_role"] = "ground"
        kept["geometry"]["joined_from"] = [r["id"] for r in group if r is not kept]
        gone.update(r["id"] for r in group if r is not kept)
        joined.append({"id": kept["id"], "type": main_type, "top_m": kept["z1"], "floors_joined": len(group), "area_m2": round(grown.area, 1)})
    building["elements"] = [r for r in building["elements"] if r["id"] not in gone]
    return {"ground_floors_joined": joined}


def _band(axis, normal, near, far):
    """The strip beside a wall's axis, from near to far along normal, as long as the axis."""
    (x0, y0), (x1, y1) = axis.coords[0], axis.coords[-1]
    nx, ny = normal
    return Polygon([(x0 + nx * near, y0 + ny * near), (x1 + nx * near, y1 + ny * near),
                    (x1 + nx * far, y1 + ny * far), (x0 + nx * far, y0 + ny * far)])


def extend_slabs_to_wall_core(building) -> dict:
    """A storey's main slab ends at the outer face of the exterior walls' core, as Revit's 'extend into wall (to core)' does.

    Only the stretch of a wall that runs along the slab's edge counts, so a parapet leaving the slab,
    or a wall running past its corner, adds no tab. Where the drawn slab runs past the core by a
    finish's thickness and stops, that strip is trimmed; a slab that carries on, as into a recess,
    stays. The exterior plaster stays outside the slab, and the slab's holes stay.
    """
    gap, trim, probe = RULES["slab_to_core_gap_m"], RULES["slab_overhang_m"], RULES["slab_overhang_probe_m"]
    walls_by_level = {}
    for rec in building["elements"]:
        g = rec["geometry"]
        if rec["class"] == "wall" and rec.get("exterior_side") and g.get("p0") and math.dist(g["p0"], g["p1"]) > 0.01:
            u = _direction(g)
            outward = (-u[1] * rec["exterior_side"], u[0] * rec["exterior_side"])
            walls_by_level.setdefault(rec["level"], []).append((LineString([g["p0"], g["p1"]]), g["width"], outward))
    extended, trimmed = 0, 0
    for rec in building["elements"]:
        if rec["class"] != "floor" or rec["geometry"].get("slab_role") != "main" or rec["level"] not in walls_by_level:
            continue
        slab = _shape(rec)
        if slab is None:
            continue
        add, cut = [], []
        for axis, width, outward in walls_by_level[rec["level"]]:
            near = axis.intersection(slab.buffer(width / 2 + gap))
            if near.is_empty or near.length < 0.5 * axis.length:
                continue
            add.append(near.buffer(width / 2, cap_style=2, join_style=2))
            beyond = _band(axis, outward, width / 2 + trim, width / 2 + probe)
            if slab.intersection(beyond).area < 0.1 * beyond.area:
                cut.append(_band(axis, outward, width / 2, width / 2 + trim))
        if not add:
            continue
        grown = make_valid(unary_union([slab] + add))
        holes = [h for g in getattr(slab, "geoms", [slab]) if g.geom_type == "Polygon" for h in g.interiors]
        outer = unary_union([Polygon(g.exterior) for g in getattr(grown, "geoms", [grown]) if g.geom_type == "Polygon"])
        if cut:
            cut = [_difference(unary_union(cut), unary_union(add))]  # a strip never takes another wall's core
        result = _difference(outer, unary_union([Polygon(h) for h in holes] + cut)) if holes or cut else outer
        result = unary_union([g for g in getattr(result, "geoms", [result]) if g.geom_type == "Polygon" and g.area > 0.05])
        if abs(result.symmetric_difference(slab).area) > 0.05:
            rec["geometry"]["polygons"] = _slab_outline(result)
            grew = _difference(result, slab).area
            lost = _difference(slab, result).area
            rec["geometry"]["extended_to_wall_core_m2"] = round(grew, 2)
            rec["geometry"]["trimmed_past_wall_core_m2"] = round(lost, 2)
            extended += grew > 0.05
            trimmed += lost > 0.05
    return {"slabs_extended_to_wall_core": extended, "slabs_trimmed_to_wall_core": trimmed}


def curtain_wall_infill(building) -> dict:
    """A door or window the DWF hosts in a curtain wall it did not export stands in a gap of a wall line.

    The gap, from the wall's end across the doors to the next wall on that line when it is close,
    gets a curtain wall of the wall's height, and each door becomes a door panel in it. What has
    no wall line beside it stays unresolved.
    """
    walls = _axis_walls(building)
    doors = [u for u in building.get("unresolved", []) if u.get("class") in ("door", "window")
             and str(u.get("host_kind") or "").startswith("Curtain") and len(u.get("plan_outline") or []) >= 3]
    built, used = [], set()

    def on_line(g, outline):
        """Where an outline lies along a wall's line, or None when it is off that line."""
        d = _direction(g)
        n = (-d[1], d[0])
        offs = [abs((q[0] - g["p0"][0]) * n[0] + (q[1] - g["p0"][1]) * n[1]) for q in outline]
        if min(offs) > g["width"] / 2 + RULES["curtain_line_m"]:
            return None
        ts = [(q[0] - g["p0"][0]) * d[0] + (q[1] - g["p0"][1]) * d[1] for q in outline]
        return min(ts), max(ts)

    for level, level_walls in walls.items():
        for w in level_walls:
            g = w["geometry"]
            d = _direction(g)
            length = math.dist(g["p0"], g["p1"])
            spans = [(on_line(g, u["plan_outline"]), u) for u in doors if u["level"] == level and u["id"] not in used]
            spans = [(t, u) for t, u in spans if t is not None]
            ends = []
            for other in level_walls:
                if other is w:
                    continue
                t = on_line(g, [other["geometry"]["p0"], other["geometry"]["p1"]])
                if t is not None and abs(d[0] * _direction(other["geometry"])[1] - d[1] * _direction(other["geometry"])[0]) <= 0.02:
                    ends.append(t)
            for sign in (1, -1):
                # walk from the wall's end across doors that follow each other, to the next wall when it is close
                edge = length if sign == 1 else 0.0
                group = []
                while True:
                    nxt = [(t, u) for t, u in spans if u not in [x[1] for x in group]
                           and (0 <= (t[0] - edge if sign == 1 else edge - t[1]) <= RULES["curtain_gap_m"])]
                    if not nxt:
                        break
                    t, u = min(nxt, key=lambda x: x[0][0] * sign)
                    group.append((t, u))
                    edge = t[1] if sign == 1 else t[0]
                if not group:
                    continue
                stop = [(e[0] if sign == 1 else e[1]) for e in ends]
                stop = [x for x in stop if 0 <= (x - edge) * sign <= RULES["curtain_gap_m"]]
                if stop:
                    edge = min(stop, key=lambda x: abs(x - edge))
                lo, hi = sorted((length if sign == 1 else 0.0, edge))
                if hi - lo < 0.3:
                    continue
                point = lambda t: [round(g["p0"][0] + d[0] * t, 4), round(g["p0"][1] + d[1] * t, 4)]
                first = group[0][1]
                rec = {k: v for k, v in w.items() if k not in ("geometry", "id", "ifc_guid", "merged_from", "exterior_side", "plaster")}
                rec.update(id="{}_curtain".format(first["id"]), ifc_guid=None, family="Curtain Wall", type="Curtain Wall",
                           source_family="Curtain Wall", source_type="Curtain Wall", height_source="inferred", cloned_from=w["id"],
                           geometry={"kind": "axis", "p0": point(lo), "p1": point(hi), "width": g["width"], "source": "curtain_infill",
                                     "curtain": {"panels": [{"id": u["id"], "class": u["class"], "family": u.get("family"), "type": u.get("type"),
                                                             "from": round(t[0] - lo, 3), "to": round(t[1] - lo, 3)} for t, u in group]}})
                building["elements"].append(rec)
                used.update(u["id"] for _, u in group)
                built.append({"id": rec["id"], "level": level, "length_m": round(hi - lo, 2), "panels": [u["id"] for _, u in group]})
        # doors inside a wall's span: the DWF's curtain wall was embedded in that wall, so one is embedded again
        for w in level_walls:
            g = w["geometry"]
            d = _direction(g)
            length = math.dist(g["p0"], g["p1"])
            inside = []
            for u in doors:
                if u["level"] != level or u["id"] in used:
                    continue
                t = on_line(g, u["plan_outline"])
                if t is not None and t[0] >= -0.05 and t[1] <= length + 0.05:
                    inside.append((t, u))
            inside.sort(key=lambda x: x[0][0])
            runs = []
            for t, u in inside:
                if runs and t[0] - runs[-1][-1][0][1] <= 0.1:
                    runs[-1].append((t, u))
                else:
                    runs.append([(t, u)])
            for group in runs:
                lo, hi = max(0.0, group[0][0][0]), min(length, max(t[1] for t, _ in group))
                if hi - lo < 0.3:
                    continue
                point = lambda t: [round(g["p0"][0] + d[0] * t, 4), round(g["p0"][1] + d[1] * t, 4)]
                first = group[0][1]
                rec = {k: v for k, v in w.items() if k not in ("geometry", "id", "ifc_guid", "merged_from", "exterior_side", "plaster")}
                rec.update(id="{}_curtain".format(first["id"]), ifc_guid=None, family="Curtain Wall", type="Curtain Wall",
                           source_family="Curtain Wall", source_type="Curtain Wall", height_source="inferred", cloned_from=w["id"],
                           geometry={"kind": "axis", "p0": point(lo), "p1": point(hi), "width": g["width"], "source": "curtain_infill",
                                     "curtain": {"embedded_in": w["id"], "panels": [{"id": u["id"], "class": u["class"], "family": u.get("family"), "type": u.get("type"),
                                                                                    "from": round(t[0] - lo, 3), "to": round(t[1] - lo, 3)} for t, u in group]}})
                building["elements"].append(rec)
                used.update(u["id"] for _, u in group)
                built.append({"id": rec["id"], "level": level, "length_m": round(hi - lo, 2), "panels": [u["id"] for _, u in group], "embedded_in": w["id"]})
    placed = {pid for r in built for pid in r["panels"]}
    building["unresolved"] = [u for u in building.get("unresolved", []) if u["id"] not in placed]
    return {"curtain_walls_inferred": built}


def _shape(rec):
    parts = [make_valid(Polygon(p["outer"], p.get("holes") or [])) for p in rec["geometry"].get("polygons") or [] if len(p["outer"]) >= 3]
    return unary_union(parts) if parts else None


def remove_floor_overlaps(building) -> dict:
    """Two floors never fill the same space. Where they overlap, the stronger keeps the area.

    A balcony is strongest, so it keeps its full size, then a storey's main slab, then a slab on
    its own, and of equals the larger. A floor left with almost nothing is removed and listed.
    """
    rank = {"balcony": 0, "main": 1}
    floors = [r for r in building["elements"] if r["class"] == "floor" and r["geometry"].get("polygons")]
    shapes = {r["id"]: _shape(r) for r in floors}
    order = sorted((r for r in floors if shapes[r["id"]] is not None),
                   key=lambda r: (rank.get(r["geometry"].get("slab_role"), 2), -shapes[r["id"]].area))
    placed, trimmed, removed = [], 0, []
    for rec in order:
        shape = shapes[rec["id"]]
        above = [shapes[o["id"]] for o in placed if o["z0"] < rec["z1"] - 0.005 and rec["z0"] < o["z1"] - 0.005]
        if above:
            rest = _difference(shape, unary_union(above))
            if shape.area - rest.area > RULES["floor_overlap_m2"]:
                if rest.area < RULES["floor_min_m2"]:
                    removed.append({"id": rec["id"], "level": rec["level"], "type": rec.get("source_type"), "area_m2": round(shape.area, 2)})
                    continue
                rec["geometry"]["polygons"] = _slab_outline(rest)
                rec["geometry"]["trimmed_by_overlap_m2"] = round(shape.area - rest.area, 2)
                shapes[rec["id"]] = rest
                trimmed += 1
        placed.append(rec)
    gone = {r["id"] for r in removed}
    building["elements"] = [r for r in building["elements"] if r["id"] not in gone]
    return {"floors_trimmed": trimmed, "floors_removed_as_covered": removed}


def _building_outline(building):
    """The outer outline of the main slabs of the lowest storey above the ground that has them."""
    levels = building["levels"]
    for lid, info in sorted(levels.items(), key=lambda kv: kv[1]["z"]):
        if info["z"] <= 0.01:
            continue
        main = [_shape(r) for r in building["elements"] if r["class"] == "floor" and r["level"] == lid and r["geometry"].get("slab_role") == "main"]
        main = [m for m in main if m is not None]
        if main:
            return _filled(unary_union(main))
    return None


def mark_exterior_walls(building) -> dict:
    """Which straight walls face outside, and on which side, from the ground floor up.

    A side faces outside when a point just beyond its face lies off the outer outline of the
    storey's main slabs, or outside what the storey's walls close all round, as in a recess for
    laundry that has a slab but is open to the air. Holes in a slab, for shafts and stairs, count as inside.
    The ground level's slabs cover the whole plot, so its outline is the building's: the main
    slabs of the first storey above it. Ground level walls get plaster unless the settings say not.
    The side is +1 for the left of p0 to p1 and -1 for the right. A wall with both sides off the
    slabs, such as a parapet, faces the plot line, or away from the building when there is none.
    """
    levels = building["levels"]
    by_level = {}
    for rec in building["elements"]:
        if rec["class"] == "floor" and rec["geometry"].get("polygons"):
            by_level.setdefault(rec["level"], []).append(rec)
    footprints = {}
    for lid, floors in by_level.items():
        main = [_shape(r) for r in floors if r["geometry"].get("slab_role") == "main"] or [_shape(r) for r in floors]
        main = [s for s in main if s is not None]
        if main:
            # a shaft or stair hole in the slab is inside the building, so only the outer outline counts
            whole = unary_union(main)
            footprints[lid] = unary_union([Polygon(g.exterior) for g in getattr(whole, "geoms", [whole]) if g.geom_type == "Polygon"])
    ground = next((lid for lid, info in levels.items() if abs(info["z"]) < 0.01), None)
    if ground is not None:
        building_outline = _building_outline(building)
        if building_outline is not None:
            footprints[ground] = building_outline
    for rec in building["elements"]:
        rec.pop("exterior_side", None)
        rec.pop("plaster", None)
    plot = (building.get("site") or {}).get("plot") or {}
    plot_line = Polygon(plot["ring"]).exterior if len(plot.get("ring") or []) >= 3 else None
    # what the walls close all round; a recess open to the outside, as for laundry, stays out of it
    walls_by_level = defaultdict(list)
    for rec in building["elements"]:
        if rec["class"] == "wall":
            walls_by_level[rec["level"]].append(rec)
    enclosed = {}
    for lid, walls in walls_by_level.items():
        closed = _filled(unary_union(_wall_shapes(walls, RULES["enclosure_gap_m"] / 2)))
        # trusted only where the walls close nearly the whole storey; a storey drawn in part would read as all outside
        if lid in footprints and closed.intersection(footprints[lid]).area >= 0.8 * footprints[lid].area:
            enclosed[lid] = closed
    marked, skipped = 0, 0
    for rec in building["elements"]:
        if rec["class"] != "wall" or rec["level"] not in levels or levels[rec["level"]]["z"] < -0.01:
            continue
        g = rec["geometry"]
        footprint = footprints.get(rec["level"])
        if footprint is None:
            continue
        if not g.get("p0"):
            skipped += 1
            continue
        (x0, y0), (x1, y1) = g["p0"], g["p1"]
        length = math.dist(g["p0"], g["p1"])
        if length < 0.05:
            continue
        nx, ny = -(y1 - y0) / length, (x1 - x0) / length  # left normal
        reach = g["width"] / 2 + RULES["exterior_probe_m"]
        outside = {}
        for side in (1, -1):
            probes = [Point(x0 + (x1 - x0) * t + side * nx * reach, y0 + (y1 - y0) * t + side * ny * reach) for t in (0.25, 0.5, 0.75)]
            # at ground level the walls stand open to lobbies and gardens, so only the slabs count there
            inside = enclosed.get(rec["level"]) if levels[rec["level"]]["z"] > 0.01 else None
            outside[side] = sum(not footprint.contains(p) or (inside is not None and not inside.contains(p)) for p in probes) >= 2
        if outside[1] == outside[-1]:
            if not outside[1]:
                continue
            middle = Point((x0 + x1) / 2, (y0 + y1) / 2)
            if plot_line is not None:
                # both faces are open, as on a parapet: the outside is the face toward the plot line
                near = [plot_line.distance(Point(middle.x + s * nx * reach, middle.y + s * ny * reach)) for s in (1, -1)]
                side = 1 if near[0] <= near[1] else -1
            else:
                centre = footprint.centroid
                side = 1 if (middle.x - centre.x) * nx + (middle.y - centre.y) * ny > 0 else -1
        else:
            side = 1 if outside[1] else -1
        rec["exterior_side"] = side
        if abs(levels[rec["level"]]["z"]) < 0.01 and not RULES["plaster_on_ground_level"]:
            rec["plaster"] = False
        marked += 1
    return {"exterior_walls": marked, "curved_walls_not_marked": skipped}


def apply(building) -> dict:
    report = {}
    report.update(walls_from_level(building))
    report.update(walls_to_level_above(building))
    report.update(merge_wall_runs(building))
    report.update(curtain_wall_infill(building))
    report.update(curved_walls_as_arcs(building))
    mark_exterior_walls(building)  # first, so exterior walls align face to face
    report.update(align_walls_to_below(building))
    report.update(mark_exterior_walls(building))
    report.update(extend_slabs_to_wall_core(building))
    report.update(remove_floor_overlaps(building))
    report.update(fill_ground_floors(building))
    report.update(unify_ground_floors(building))
    building["modelling"] = report
    return report
