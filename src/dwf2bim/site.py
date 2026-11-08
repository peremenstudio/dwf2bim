"""Stage 4b - survey map and area plan: placement, plot and building lines, slab limits.

The Revit sheet already holds the plot and building lines as Property elements. The survey
gives the plot corners in ITM, so fitting one to the other places the model in the world.
The area plan divides each floor into numbered cells, which gives the building's envelope
including balconies.
"""
from __future__ import annotations

import collections
import json
import math
import re

import numpy as np
from shapely.affinity import translate
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely import set_precision
from shapely.ops import polygonize, unary_union
from shapely.validation import make_valid

from .names import CONFIG_DIR, level_id

_CFG = json.loads((CONFIG_DIR / "site.json").read_text(encoding="utf8"))
FIT_LIMIT_M = 0.10
ENVELOPE_WALL_SHARE = 0.10  # more walls than this outside the area plan: do not trust it as an envelope
TRUSTED_FIT_SHARE = 0.90  # a panel holding this share of its plan's walls anchors the others
WIDE_FIT_SHARE = 0.80  # a wider search is accepted from this share
COLLINEAR_SIN = 0.02
PANEL_GRID = 0.02  # page units, under 4 mm on the sheet's scale
TABLE_WIDTH = 215.0  # page units from a table's first column to its right edge
UNIT_COLUMN = (181.0, TABLE_WIDTH)  # page units from a table's first column to its apartment column
UNIT_ROW_REACH = 3.2  # page units, half a table row


# ---- reading the sheets --------------------------------------------------------
def is_area_sheet(sheet) -> bool:
    return any(_is_header(t, sheet.texts) for t in sheet.texts)


def _same_row(sheet_texts, y, tol=0.2):
    return [t for t in sheet_texts if abs(t.y - y) < tol]


def _is_header(t, texts):
    if not re.fullmatch(r"\d+\.\d\d?", t.text):
        return False
    row = _same_row(texts, t.y)
    return any(abs(o.x - t.x + 18) < 1 for o in row) and any(abs(o.x - t.x - 50) < 2 for o in row)


def _survey_segments(sheet, stroke):
    """Stroked segments of one colour, in ITM."""
    if sheet.drawing_transform is None or sheet.clip is None or sheet.content_matrix is None:
        return []
    a, d, e, f = sheet.drawing_transform
    x0, y0, _, h = sheet.clip
    m = sheet.content_matrix

    def to_itm(p):
        cx, cy = (p[0] - m[4]) / m[0], (p[1] - m[5]) / m[3]
        return ((cx - x0 - e) / a, ((y0 + h) - cy - f) / d)

    out = []
    for path in sheet.paths:
        if path.stroke != stroke:
            continue
        for ring, closed in path.rings:
            pts = [to_itm(q) for q in ring] + ([to_itm(ring[0])] if closed else [])
            out += [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return out


def survey_corners(sheet) -> list:
    """The plot ring in ITM, from the map's plot boundary line, every corner kept."""
    segments = [s for s in _survey_segments(sheet, _CFG["survey_plot_stroke"]) if math.dist(*s) > 0.3]
    if len(segments) < 3:
        return []
    return _walk_ring(segments)


def _walk_ring(segments, join=0.02):
    """Follow segments end to end into one closed ring. Empty if they do not close."""
    remaining = list(segments)
    first = remaining.pop(0)
    ring = [first[0], first[1]]
    while remaining:
        tip = ring[-1]
        hit = None
        for s in remaining:
            for near, far in ((s[0], s[1]), (s[1], s[0])):
                if math.dist(tip, near) <= join:
                    hit = (s, far)
                    break
            if hit:
                break
        if hit is None:
            break
        remaining.remove(hit[0])
        if math.dist(hit[1], ring[0]) <= join:
            return ring
        ring.append(hit[1])
    return []


def survey_building_line(sheet, plot_ring) -> list:
    """The building line in ITM. It is drawn as dashes inside the plot, between dimension lines
    and parcel extensions of the same colour, which the dash length and the plot keep out."""
    cfg = _CFG["survey_building_line"]
    plot = Polygon(plot_ring)
    dashes = [
        s for s in _survey_segments(sheet, cfg["stroke"])
        if cfg["dash_min_m"] <= math.dist(*s) <= cfg["dash_max_m"] and plot.contains(LineString(s))
    ]
    edges = [e for e in _merge_collinear(dashes, offset_tol=0.05) if math.dist(*e) >= cfg["edge_min_m"]]
    return _chain_edges(edges, True) if len(edges) >= 3 else []


def closed_lines_only(lines) -> tuple:
    """A building line encloses an area. Open pieces beside a closed line are left out, by id. With no closed line, all stay."""
    closed = [b for b in lines if b["closed"]]
    if not closed:
        return list(lines), []
    return closed, [b["id"] for b in lines if not b["closed"]]


def drop_collinear(ring, limit=COLLINEAR_SIN):
    """A corner lying on a straight edge says nothing about the plot's shape."""
    out = list(ring)
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            a, b, c = out[i - 1], out[i], out[(i + 1) % len(out)]
            cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            if abs(cross) / (math.dist(a, b) * math.dist(b, c)) < limit:
                out.pop(i)
                changed = True
                break
    return out


def fit_rigid(model, world):
    """Best rotation and translation from a model ring to a world ring of the same size.

    Rings are tried at every starting corner. Returns (theta, tx, ty, residuals) with
    world = R(theta) * model + T, or None when the rings differ in size.
    """
    if len(model) != len(world):
        return None
    a = np.array(_ccw(model))
    w = _ccw(world)
    best = None
    for shift in range(len(w)):
        b = np.array(w[shift:] + w[:shift])
        ca, cb = a.mean(0), b.mean(0)
        u, _, vt = np.linalg.svd((a - ca).T @ (b - cb))
        r = vt.T @ u.T
        if np.linalg.det(r) < 0:
            vt[1] *= -1
            r = vt.T @ u.T
        t = cb - r @ ca
        res = np.linalg.norm((r @ a.T).T + t - b, axis=1)
        if best is None or res.max() < best[3].max():
            best = (math.atan2(r[1, 0], r[0, 0]), float(t[0]), float(t[1]), res)
    return best


def _ccw(ring):
    return list(ring) if Polygon(ring).exterior.is_ccw else list(ring)[::-1]


def _unit_labels(texts, hx, row_y):
    """Apartment labels from a table's apartment column, each given to the nearest row."""
    labels = {}
    for o in texts:
        if row_y and hx + UNIT_COLUMN[0] <= o.x < hx + UNIT_COLUMN[1] and o.text.strip():
            n = min(row_y, key=lambda k: abs(row_y[k] - o.y))
            if abs(row_y[n] - o.y) < UNIT_ROW_REACH:
                labels[n] = o.text.strip()
    return labels


def read_area_panels(sheet) -> list:
    """Floor panels from the last row of the area sheet, which numbers every polygon."""
    headers = [t for t in sheet.texts if _is_header(t, sheet.texts)]
    if not headers:
        return []
    last = max(t.y for t in headers)
    texts = [t for t in sheet.texts if t.y >= last - 25]
    panels = []
    previous = None
    for t in sorted((h for h in headers if abs(h.y - last) < 0.5), key=lambda h: h.x):
        row = _same_row(texts, t.y)
        building = next(o for o in row if abs(o.x - t.x + 18) < 1)
        floor = next((o.text for o in row if abs(o.x - t.x - 66) < 3), None)
        hx = building.x
        # a panel's plan fills the space between the previous table and its own
        x_lo = previous + TABLE_WIDTH if previous is not None else hx - 104.5
        previous = hx
        rows = {}
        row_y = {}
        for o in texts:
            if abs(o.x - hx) < 1.5 and o.y > t.y + 5 and re.fullmatch(r"\d+", o.text):
                same = _same_row(texts, o.y)
                code = [q for q in same if 15 < q.x - o.x < 30]
                area = [q for q in same if 135 < q.x - o.x < 150]
                height = [q for q in same if 122 < q.x - o.x < 135]
                if code and area:
                    rows[o.text] = {"n": o.text, "c": code[0].text, "t": float(area[0].text), "h": height[0].text if height else None}
                    row_y[o.text] = o.y
        for n, label in _unit_labels(texts, hx, row_y).items():
            rows[n]["u"] = label
        labels = {o.text: (o.x, o.y) for o in texts if x_lo <= o.x < hx - 5 and re.fullmatch(r"\d+", o.text)}
        lines = []
        shapes = []
        for path in sheet.paths:
            rings, pieces = [], []
            for ring, closed in path.rings:
                pts = ring + ([ring[0]] if closed else [])
                if len(pts) > 1 and all(x_lo - 1 <= q[0] <= hx - 1 and t.y - 25 <= q[1] <= t.y + 420 for q in pts):
                    lines.append(pts)
                    if closed and len(ring) >= 3:
                        rings.append(ring)
                    elif not closed:
                        pieces.append(list(ring))
            rings += _chain_pieces(pieces)
            if rings:
                shapes.append(_path_polygon(rings))
        panels.append({
            "floors": [int(v) for v in re.findall(r"-?\d+", floor or "")],
            "height": float(row_value(row, t, 50)),
            "total": float(t.text),
            "rows": rows,
            "labels": labels,
            "lines": lines,
            "shapes": [g for g in shapes if g is not None],
        })
    return panels


def _chain_pieces(pieces, join=0.15):
    """A cell with a curved side is drawn as several open pieces of one path: join them into rings."""
    remaining = [list(p) for p in pieces]
    rings = []
    while remaining:
        chain = remaining.pop(0)
        grown = True
        while grown and math.dist(chain[0], chain[-1]) > join:
            grown = False
            for piece in remaining:
                if math.dist(chain[-1], piece[0]) <= join:
                    chain += piece[1:]
                elif math.dist(chain[-1], piece[-1]) <= join:
                    chain += piece[-2::-1]
                else:
                    continue
                remaining.remove(piece)
                grown = True
                break
        if len(chain) >= 4 and math.dist(chain[0], chain[-1]) <= join:
            rings.append(chain[:-1])
    return rings


def _path_polygon(rings):
    """One drawn path: its largest ring, with the others as holes."""
    polys = sorted((make_valid(Polygon(r)) for r in rings), key=lambda g: -g.area)
    outer = polys[0]
    if outer.geom_type != "Polygon" or outer.area == 0:
        return None
    holes = [g for g in polys[1:] if g.geom_type == "Polygon" and outer.contains(g.representative_point())]
    return outer.difference(unary_union(holes)) if holes else outer


def row_value(row, t, dx):
    return next(o.text for o in row if abs(o.x - t.x - dx) < 2)


def _bridge_gaps(lines, limit):
    """Join a line end that stops just short of another line, or of another free end."""
    pieces = list(getattr(lines, "geoms", [lines]))
    ends = collections.Counter()
    for piece in pieces:
        ends[piece.coords[0]] += 1
        ends[piece.coords[-1]] += 1
    free = [e for e, n in ends.items() if n == 1]
    bridges = []
    for i, piece in enumerate(pieces):
        for end in (piece.coords[0], piece.coords[-1]):
            if ends[end] != 1:
                continue
            point = Point(end)
            others = unary_union(pieces[:i] + pieces[i + 1:]) if len(pieces) > 1 else None
            target = None
            if others is not None and 0 < others.distance(point) <= limit:
                target = others.interpolate(others.project(point)).coords[0]
            near = [e for e in free if e != end and math.dist(e, end) <= limit]
            if near:
                closest = min(near, key=lambda e: math.dist(e, end))
                if target is None or math.dist(closest, end) <= math.dist(target, end):
                    target = closest
            if target is not None:
                bridges.append(LineString([end, target]))
    return unary_union([lines] + bridges) if bridges else lines


def label_cells(panel):
    """The shape around each number label.

    The area plan draws each cell as its own closed path, so that is read directly: of the
    shapes holding the label, the one nearest the table's area. A label with none falls back
    to the faces of the line network, where a small gap may be closed only if the face still
    matches the table, or the gap has let it leak into a neighbour.
    """
    shapes = panel.get("shapes") or []
    rows = panel["rows"]
    holding = {}
    for n, xy in panel["labels"].items():
        if n in rows:
            holding[n] = [g for g in shapes if g.contains(Point(xy))]
    smallest = sorted(rows[n]["t"] / min(g.area for g in gs) for n, gs in holding.items() if gs and rows[n]["t"] > 2)
    page_m2 = smallest[len(smallest) // 2] if smallest else None
    found = {}
    if page_m2:
        for n, gs in holding.items():
            if gs:
                best = min(gs, key=lambda g: abs(g.area * page_m2 - rows[n]["t"]))
                if abs(best.area * page_m2 / rows[n]["t"] - 1) <= 0.15:
                    found[n] = best
    missing = [n for n in panel["labels"] if n in rows and n not in found]
    if missing:
        lines = unary_union([set_precision(LineString(q), PANEL_GRID, mode="pointwise") for q in panel["lines"]])
        faces = list(polygonize(lines))
        if not page_m2:
            ratios = sorted(rows[n]["t"] / c.area for n in missing for c in faces if c.contains(Point(panel["labels"][n])) and rows[n]["t"] > 2)
            page_m2 = ratios[len(ratios) // 2] if ratios else None
        for gap in (0.0, 0.03, 0.08, 0.2, 0.5):
            if not page_m2 or not missing:
                break
            repaired = faces if gap == 0.0 else list(polygonize(_bridge_gaps(lines, gap)))
            for n in list(missing):
                face = next((c for c in repaired if c.contains(Point(panel["labels"][n]))), None)
                if face is not None and abs(face.area * page_m2 / rows[n]["t"] - 1) <= 0.15:
                    found[n] = face
                    missing.remove(n)
    return found


def cell_edges(cells):
    """The boundaries of the numbered cells. A border drawn by both of its cells comes out once."""
    rings = []
    for cell in cells:
        for ring in [cell.exterior] + list(cell.interiors):
            rings.append(set_precision(LineString(ring.coords), PANEL_GRID, mode="pointwise"))
    if not rings:
        return []
    merged = unary_union(rings)
    edges = []
    for piece in getattr(merged, "geoms", [merged]):
        coords = list(piece.coords)
        edges += [(a, b) for a, b in zip(coords, coords[1:]) if a != b]
    return edges


def panel_scale(panel):
    """Metres per page unit, from cell areas against the area table."""
    cells = label_cells(panel)
    ratios = [panel["rows"][n]["t"] / c.area for n, c in cells.items() if n in panel["rows"] and panel["rows"][n]["t"] > 2 and c.area > 0]
    return (math.sqrt(sorted(ratios)[len(ratios) // 2]) if ratios else None), cells


def vote_translations(a_pts, b_pts, top=25, bin_m=0.02):
    """Candidate shifts from a to b: the same corners seen in both collect peaks of votes."""
    votes = collections.Counter()
    for ax, ay in a_pts:
        for bx, by in b_pts:
            votes[(round((bx - ax) / bin_m), round((by - ay) / bin_m))] += 1
    return [(i * bin_m, j * bin_m, n) for (i, j), n in votes.most_common(top)]


def _match_shelters(apartments, near) -> dict:
    """Give as many apartments as possible a shelter of their own. near lists each apartment's shelters, best first."""
    taken = {}

    def give(apartment, tried):
        for shelter in near[apartment]:
            if shelter in tried:
                continue
            tried.add(shelter)
            if shelter not in taken or give(taken[shelter], tried):
                taken[shelter] = apartment
                return True
        return False

    for apartment in sorted(apartments, key=lambda a: len(near[a])):
        give(apartment, set())
    return taken


def _infer_apartments(floor) -> tuple:
    """Who owns which cell, when the area table does not say. Returns the owners and the parts left with none.

    A large residential cell is an apartment. It usually has one shelter, so shelters are shared out
    one each first, and only then does a spare one go to the apartment beside it. A shelter's walls go
    where the shelter goes. Every balcony belongs to an apartment, the nearest however far. Nothing
    passes on through another part, or a shelter would follow its walls into the next apartment.
    """
    reach = _CFG["apartment_reach_m"]
    cells = {c["n"]: c for c in floor["cells"]}
    shapes = {n: make_valid(Polygon(c["ring"])) for n, c in cells.items()}
    residential = set(_CFG["apartment_codes"])
    heads = [n for n, c in cells.items() if c["c"] in residential and c["t"] >= _CFG["apartment_min_m2"]]

    def of(codes):
        return [n for n, c in cells.items() if c["c"] in codes and n not in heads]

    def closest(n, targets, limit=None):
        """The target nearest to this cell, and of equals the one it borders most. None when beyond the limit."""
        grown = shapes[n].buffer(reach, join_style=2, mitre_limit=1.5)
        found = [(round(shapes[n].distance(shapes[t]), 1), -grown.intersection(shapes[t]).area, t) for t in targets]
        found = [f for f in found if limit is None or f[0] <= limit]
        return min(found)[2] if found else None

    owner = {n: n for n in heads}
    left = []
    if not heads:
        return owner, left

    def join(n, target):
        if target is None:
            left.append(n)
        else:
            owner[n] = owner[target]

    shelters = of(_CFG["apartment_shelter_codes"])
    near = {a: [t[1] for t in sorted((round(shapes[a].distance(shapes[n]), 1), n) for n in shelters if shapes[a].distance(shapes[n]) <= reach)] for a in heads}
    matched = _match_shelters(heads, near)
    for n in shelters:
        join(n, matched.get(n) or closest(n, heads, reach))
    placed = [n for n in shelters if n in owner]
    for n in of(_CFG["apartment_shelter_wall_codes"]):
        join(n, closest(n, placed, reach) or closest(n, heads, reach))
    for n in of(_CFG["balcony_codes"]):
        join(n, closest(n, heads))
    for n in of(set(_CFG["apartment_part_codes"]) | residential):
        join(n, closest(n, heads, reach))
    return owner, left


def assign_apartments(building):
    """Number the apartments, through the building from the bottom up.

    The area plan's apartment column decides when it is filled in, and otherwise they are inferred.
    A floor with no residential cell has no apartments, whatever its table says, since a basement's
    shelter walls may be listed under one. Every other cell gets 0.
    """
    apartment_codes = set(_CFG["apartment_codes"])
    shelter_codes = set(_CFG["apartment_shelter_codes"])
    from_table = any(c.get("u") for floor in building.get("areas", []) for c in floor["cells"])
    unassigned = []
    for floor in building.get("areas", []):
        if from_table:
            residential = any(c["c"] in apartment_codes for c in floor["cells"])
            floor["apartment_of_cell"] = {c["n"]: c["u"] for c in floor["cells"] if c.get("u") and residential}
            continue
        floor["apartment_of_cell"], left = _infer_apartments(floor)
        unassigned += [{"floors": floor["floors"], "cell": n} for n in left]

    def number_key(label):
        digits = re.sub(r"\D", "", str(label))
        return (int(digits) if digits else 0, str(label))

    levels = sorted(
        ((building["levels"][lid]["z"], lid, floor) for floor in building.get("areas", []) for lid in floor["levels"] if lid in building["levels"]),
        key=lambda row: row[0],
    )
    running = 0
    table = []
    for _, lid, floor in levels:
        owner = floor["apartment_of_cell"]
        ids = {}
        for head in sorted(set(owner.values()), key=number_key):
            running += 1
            ids[head] = running
            cells = [c for c in floor["cells"] if owner.get(c["n"]) == head]
            table.append({"unit": running, "level": lid, "cells": [c["n"] for c in cells], "area_m2": round(sum(c["t"] for c in cells), 2),
                          "shelters": sum(c["c"] in shelter_codes for c in cells)})
        floor.setdefault("unit_ids", {})[lid] = {n: ids[o] for n, o in owner.items()}
    building["apartments"] = {"source": "area_table" if from_table else "inferred", "units": table, "parts_with_no_apartment": unassigned}


def merge_storey_slabs(building):
    """A storey's slab is one floor: slabs within 5 cm of each other join, across the walls between them.

    This applies from the level in from_level upwards, and only to slabs fitted inside the
    building's outer walls. Balconies, slabs that stand on their own, and lower slabs stay
    separate, and so does everything on the ground floor and in the basements. The result
    takes the type that covers most of the floor.
    """
    from .extract import _slab_outline

    rule = _CFG["storey_slab"]
    no_slab_codes = set(_CFG["no_slab_codes"])
    walls = [r for r in building["elements"] if r["class"] == "wall"]
    floors_by_level = {lid: floor for floor in building.get("areas", []) for lid in floor["levels"]}
    merged_away = set()
    report = {}
    lowest = building["levels"].get(rule["from_level"], {}).get("z")
    for lid, info in building["levels"].items():
        if lowest is None or info["z"] < lowest:
            continue
        slabs = [
            r for r in building["elements"]
            if r["class"] == "floor" and r["level"] == lid and r["geometry"].get("polygons")
            and r["geometry"].get("slab_role") == "main" and r["geometry"].get("fitted_to_area_plan")
        ]
        groups = []
        for rec in sorted(slabs, key=lambda r: -_outline(r).area):
            group = next((g for g in groups if abs(g[0]["z1"] - rec["z1"]) <= rule["same_height_m"]), None)
            if group is None:
                groups.append([rec])
            else:
                group.append(rec)
        for group in groups:
            if len(group) < 2:
                continue
            shapes = [_outline(r) for r in group]
            union = unary_union(shapes)
            z = group[0]["z1"]
            near = [fp for fp in (_wall_outline(w) for w in walls if w["z0"] < z + 1.0 < w["z1"]) if fp is not None and fp.distance(union) < 0.05]
            floor = floors_by_level.get(lid)
            joined = union
            if floor:
                # walls within the building, up to its outer faces: the area plan's cells and the walls hold it
                cells = [make_valid(Polygon(c["ring"])) for c in floor["cells"] if c["c"] not in set(_CFG["balcony_codes"])]
                body = unary_union(cells + near)
                joined = unary_union([union] + [fp.intersection(body) for fp in near])
                joined = joined.buffer(rule["bridge_m"], join_style=2, mitre_limit=1.5).buffer(-rule["bridge_m"], join_style=2, mitre_limit=1.5).intersection(body)
                voids = [make_valid(Polygon(c["ring"])) for c in floor["cells"] if c["c"] in no_slab_codes]
                if voids:
                    joined = joined.difference(unary_union(voids).difference(union))
            else:
                joined = joined.buffer(rule["bridge_m"], join_style=2, mitre_limit=1.5).buffer(-rule["bridge_m"], join_style=2, mitre_limit=1.5)
            by_type = collections.Counter()
            for r, g in zip(group, shapes):
                by_type[r.get("source_type")] += g.area
            main_type = by_type.most_common(1)[0][0]
            same_type = [r for r in group if r.get("source_type") == main_type]
            model = max(same_type, key=lambda r: _outline(r).area)
            thickness = model["z1"] - model["z0"]
            depths = sorted({round(r["z1"] - r["z0"], 2) for r in group})
            keeper = group[0]
            keeper.update(
                source_type=main_type, type=model.get("type"), material=model.get("material"),
                z1=round(z, 4), z0=round(z - thickness, 4),
            )
            keeper["geometry"]["polygons"] = _slab_outline(joined)
            keeper["geometry"]["merged_ids"] = [r["id"] for r in group if r is not keeper]
            keeper["geometry"]["merged_depths_m"] = depths
            merged_away.update(id(r) for r in group if r is not keeper)
            report.setdefault(lid, []).append({
                "slabs_joined": len(group), "area_m2": round(sum(Polygon(q["outer"], q.get("holes")).area for q in keeper["geometry"]["polygons"]), 1),
                "type": model.get("type"), "thickness_m": round(thickness, 3), "depths_in_source_m": depths,
            })
    building["elements"] = [r for r in building["elements"] if id(r) not in merged_away]
    building["site"]["storey_slabs"] = report


def keep_plate_masses(building):
    """A mass stays only if it reads as a floor: a thin horizontal plate that is not already a slab.

    A post built as a floor stands in front of windows, so anything tall, tiny or on top of a
    real slab is left out and listed. A plate that stays belongs to the level nearest its top,
    as a slab does.
    """
    rule = _CFG["mass_as_floor"]
    slabs = [(r, _outline(r)) for r in building["elements"] if r["class"] == "floor"]
    kept, left_out = [], []
    for rec in building["elements"]:
        if rec["class"] not in ("generic", "mullion", "curtain_panel"):
            kept.append(rec)
            continue
        shape = _outline(rec)
        thickness = rec["z1"] - rec["z0"]
        reason = None
        if thickness > rule["max_thickness_m"]:
            reason = "taller than a slab"
        elif shape is None or shape.area < rule["min_area_m2"]:
            reason = "too small to be a slab"
        else:
            on_slabs = sum(g.intersection(shape).area for r, g in slabs if g is not None and r["z0"] < rec["z1"] and rec["z0"] < r["z1"])
            if on_slabs > rule["max_share_on_a_slab"] * shape.area:
                reason = "already covered by a slab"
        if reason:
            left_out.append({"id": rec["id"], "class": rec["class"], "family": rec.get("family"), "thickness_m": round(thickness, 2), "area_m2": round(shape.area, 2) if shape is not None else 0.0, "reason": reason})
        else:
            if not rec.get("level") and building.get("levels"):
                rec["level"] = min(building["levels"], key=lambda lid: abs(building["levels"][lid]["z"] - rec["z1"]))
            kept.append(rec)
    building["elements"] = kept
    building["masses_left_out"] = left_out


def _outline(rec):
    polys = rec["geometry"].get("polygons") or []
    if not polys:
        return None
    return unary_union([make_valid(Polygon(q["outer"], q.get("holes"))) for q in polys])


# ---- attaching to the model ----------------------------------------------------
class SiteBuilder:
    def __init__(self, extractor, building):
        self.ex = extractor
        self.b = building
        self.report = {}

    def _plan(self):
        return next(iter(v for v in self.ex.plans.values() if v.offset is not None and v.level and "Landscape" not in v.key), None)

    def property_lines(self):
        """Plot and building lines from Property elements drawn in a plan."""
        view = self._plan()
        names = set(_CFG["building_line_names"])
        plot, building = None, []
        seen = set()
        for el in self.ex.elements.values():
            if el.family != "Property" or view.key not in self.ex.prims[el.id]:
                continue
            prims = self.ex.prims[el.id][view.key]
            area = _num(el.props.get("Area"))
            if (el.props.get("Name") or "").strip() in names:
                key = round(area or 0, 2), len(prims), prims[0].points[0]
                if key not in seen:
                    seen.add(key)
                    building.append(_building_line([[view.to_m(q) for q in p.points] for p in prims if len(p.points) > 1], area, el.id))
            elif area and area > 20 and len(prims) == 1 and len(prims[0].points) >= 4 and (plot is None or area > plot["area"]):
                ring = [view.to_m(q) for q in prims[0].points]
                if math.dist(ring[0], ring[-1]) < 1e-3:
                    ring = ring[:-1]
                plot = {"ring": [[round(a, 4), round(b, 4)] for a, b in ring], "area": area, "id": el.id}
        return plot, building

    def place(self, survey_sheet):
        plot, building_lines = self.property_lines()
        kept, left_out = closed_lines_only(building_lines)
        site = {"plot": plot, "building_lines": kept, "building_lines_left_out": left_out, "placement": None, "datum_elevation": self.b.get("datum_elevation")}
        self.b["site"] = site
        if plot is None:
            self.b["issues"].append("No plot line on the Revit sheet: the model cannot be placed from the survey.")
            return
        if survey_sheet is None:
            self.b["issues"].append("No survey map: the plot and building lines are built, but the model is not placed.")
            return
        corners = survey_corners(survey_sheet)
        if not corners:
            self.b["issues"].append("Survey map read, but its plot boundary was not found (survey_plot_stroke in config/site.json): the model is not placed.")
            return
        site["survey_corners_itm"] = [[round(a, 3), round(b, 3)] for a, b in corners]
        site["survey_area"] = round(Polygon(corners).area, 2)
        fit = fit_rigid(drop_collinear(plot["ring"]), drop_collinear(corners))
        if fit is None:
            self.b["issues"].append("Plot corners differ between the Revit sheet and the survey: the model is not placed.")
            return
        theta, tx, ty, res = fit
        worst = float(res.max())
        site["placement"] = {
            "rotation_deg": round(math.degrees(theta), 5),
            "easting": round(tx, 4),
            "northing": round(ty, 4),
            "fit_max_m": round(worst, 3),
            "base_point_model": plot["ring"][0],
        }
        if worst > FIT_LIMIT_M:
            self.b["issues"].append("Plot fit to survey is off by {:.2f} m: check the placement.".format(worst))

        # from here on the lines are the survey's, brought into the model by the fit
        cos_t, sin_t = math.cos(theta), math.sin(theta)

        def to_model(q):
            dx, dy = q[0] - tx, q[1] - ty
            return [round(cos_t * dx + sin_t * dy, 4), round(-sin_t * dx + cos_t * dy, 4)]

        sheet_plot, sheet_lines = plot, building_lines
        site["revit_sheet_plot_area"] = sheet_plot["area"]
        site["plot"] = {"ring": [to_model(q) for q in corners], "area": round(Polygon(corners).area, 2), "id": sheet_plot["id"], "source": "survey"}
        drawn = survey_building_line(survey_sheet, corners)
        if drawn:
            area = round(Polygon(drawn).area, 2)
            closed_sheet = next((b for b in sheet_lines if b["closed"]), None)
            site["survey_building_line_itm"] = [[round(a, 3), round(b, 3)] for a, b in drawn]
            site["building_lines"] = [{"id": "survey", "closed": True, "points": [to_model(q) for q in drawn], "property_area": area, "source": "survey"}]
            site["building_lines_left_out"] = [b["id"] for b in sheet_lines]
            if closed_sheet is not None:
                site["building_line_area_on_revit_sheet"] = closed_sheet["property_area"]
                site["building_line_vs_revit_sheet_m"] = round(max(Polygon(closed_sheet["points"]).exterior.distance(Point(to_model(q))) for q in drawn), 3)
        else:
            self.b["issues"].append("The survey's building line was not found (survey_building_line in config/site.json): using the line on the Revit sheet.")
        site["placement"]["base_point_model"] = site["plot"]["ring"][0]

    def areas(self, area_sheet):
        panels = read_area_panels(area_sheet) if area_sheet is not None else []
        out = []
        neighbour = None  # panels run floor by floor, so the last one placed is the closest match
        for panel in panels:
            done = self._fit_panel(panel)
            if done and done["walls_inside_share"] < TRUSTED_FIT_SHARE:
                wide = self._fit_panel(panel, top=400)
                if wide["walls_inside_share"] >= WIDE_FIT_SHARE:
                    done = wide
                    done["fit"] = "walls_wide_search"
                elif neighbour:
                    # the core (lift, stairs) repeats from floor to floor
                    done = self._fit_panel(panel, neighbour)
                    done["fit"] = "anchored_to_the_next_floor"
            if done:
                out.append(done)
                neighbour = {tuple(q) for c in done["cells"] for q in c["ring"]}
        self.b["areas"] = out

    def _level_for_floor(self, number):
        for name in self.ex.levels:
            if name.strip().lstrip("‏") == "{:02d}".format(abs(number)) and number >= 0:
                return name
            if number < 0 and name.strip() == "-{:02d}".format(abs(number)):
                return name
            if number == 0 and re.match(r"^0(\.0+)?(=|$)", name.strip()):
                return name
        return None

    def _fit_panel(self, panel, anchor_pts=None, top=25):
        levels = [self._level_for_floor(n) for n in panel["floors"]]
        known = [n for n in levels if n]
        plan = next((self.ex.plans[n] for n in known if n in self.ex.plans and self.ex.plans[n].offset is not None), None)
        k, cells = panel_scale(panel)
        if plan is None or k is None:
            self.b["issues"].append("Area panel for floors {} has no matching plan or scale.".format(panel["floors"]))
            return None
        wall_pts, wall_shapes = [], []
        for eid, by in self.ex.prims.items():
            el = self.ex.elements.get(eid)
            if el is not None and el.family == "Basic Wall" and plan.key in by:
                polys = self.ex._contours(plan, by[plan.key])
                wall_shapes += polys
                for p in by[plan.key]:
                    if p.kind == "contour":
                        wall_pts += [plan.to_m(q) for q in p.points]

        def flip(q):
            return (q[0] * k, -q[1] * k)

        verts = {q for c in cells.values() for q in c.exterior.coords}
        walls = unary_union(wall_shapes)
        faces = unary_union([make_valid(Polygon([flip(q) for q in c.exterior.coords])) for c in cells.values()])
        if anchor_pts:
            dx, dy, votes = vote_translations([flip(q) for q in verts], list(anchor_pts), top=1, bin_m=0.05)[0]
        else:
            candidates = vote_translations([flip(q) for q in verts], wall_pts[:6000], top=top)
            # the plan's frame is the one that holds the walls, and a repeated corner pattern does not
            dx, dy, votes = max(candidates, key=lambda c: translate(faces, c[0], c[1]).intersection(walls).area)
        walls_inside = translate(faces, dx, dy).intersection(walls).area / walls.area if walls.area else 0.0

        def to_model(q):
            x, y = flip(q)
            return (x + dx, y + dy)

        out_cells = []
        unplaced = []
        not_drawn = unary_union([LineString(q) for q in panel["lines"]]).length - sum(math.dist(a, b) for a, b in cell_edges(cells.values()))
        for n, row in panel["rows"].items():
            label = panel["labels"].get(n)
            cell = cells.get(n)
            if cell is None or label is None:
                unplaced.append(n)
                continue
            ring = [to_model(q) for q in cell.exterior.coords[:-1]]
            holes = [[to_model(q) for q in r.coords[:-1]] for r in cell.interiors]
            out_cells.append({
                "n": n, "c": row["c"], "name": _CFG["area_codes"].get(row["c"], row["c"]), "t": row["t"], "u": row.get("u"),
                "ring": [[round(a, 4), round(b, 4)] for a, b in ring],
                "holes": [[[round(a, 4), round(b, 4)] for a, b in r] for r in holes],
                "point": [round(v, 4) for v in to_model(label)],
                "model_area": round(Polygon(ring, holes).area, 3),
            })
        return {
            "floors": panel["floors"],
            "levels": [level_id(n) for n in known],
            "height": panel["height"],
            "total": panel["total"],
            "scale": round(k, 5),
            "fit_votes": votes,
            "walls_inside_share": round(walls_inside, 3),
            "cells": out_cells,
            "rows_without_a_cell": unplaced,
            "segments": [[[round(v, 4) for v in to_model(a)], [round(v, 4) for v in to_model(b)]] for a, b in cell_edges(cells.values())],
            "lines_left_out_m": round(max(0.0, not_drawn) * k, 1),
        }

    def clip_floors(self):
        """Fit slabs to the area plan, so that a main slab stops at the building's outer walls.

        A slab mostly inside the building is a main slab: it is cut at the interior cells and
        their walls, so it passes neither the walls nor into a balcony. A slab mostly in a
        balcony cell may fill that cell. Any other slab stands on its own and stays as drawn.
        A level is skipped when many of its walls lie outside the plan, as on a ground floor
        with site walls: there the plan is not the level's envelope.
        """
        from .extract import _slab_outline

        walls = [r for r in self.b["elements"] if r["class"] == "wall"]
        no_slab_codes = set(_CFG["no_slab_codes"])
        balcony_codes = set(_CFG["balcony_codes"])
        stats = collections.Counter()
        for floor in self.b.get("areas", []):
            if not floor["cells"]:
                continue
            everything = unary_union([make_valid(Polygon(c["ring"])) for c in floor["cells"]])
            everything = everything.buffer(0.03, join_style=2).buffer(-0.03, join_style=2)
            interior_cells = [make_valid(Polygon(c["ring"])) for c in floor["cells"] if c["c"] not in balcony_codes]
            interior = unary_union(interior_cells).buffer(0.03, join_style=2).buffer(-0.03, join_style=2) if interior_cells else Polygon()
            for lid in floor["levels"]:
                z = self.b["levels"][lid]["z"]
                shapes = [fp for fp in (_wall_outline(w) for w in walls if w["z0"] < z + 1.0 < w["z1"]) if fp is not None]
                total = sum(fp.area for fp in shapes)
                outside = sum(fp.difference(everything).area for fp in shapes)
                if total and outside / total > ENVELOPE_WALL_SHARE:
                    stats["levels_skipped"] += 1
                    self.b["issues"].append("Slabs on {} not fitted to the area plan: {:.0f}% of its walls lie outside the plan.".format(lid, 100 * outside / total))
                    continue
                body = unary_union([interior] + [fp for fp in shapes if fp.distance(interior) < 0.05])
                whole = unary_union([everything, body])
                slabs = [r for r in self.b["elements"] if r["class"] == "floor" and r["level"] == lid and r["geometry"].get("polygons")]
                current = {}
                for rec in slabs:
                    stats["checked"] += 1
                    shape = unary_union([make_valid(Polygon(q["outer"], q.get("holes"))) for q in rec["geometry"]["polygons"]])
                    in_body = shape.intersection(body).area
                    if in_body >= 0.5 * shape.area:
                        kept = shape.intersection(body)
                        stats["main_slabs"] += 1
                        rec["geometry"]["slab_role"] = "main"
                    elif shape.intersection(whole).area >= 0.5 * shape.area:
                        kept = shape.intersection(whole)
                        stats["balcony_slabs"] += 1
                        rec["geometry"]["slab_role"] = "balcony"
                    else:
                        rec["geometry"]["slab_role"] = "independent"
                        rec["geometry"]["envelope"] = "stands_on_its_own"
                        stats["independent_slabs_kept"] += 1
                        continue
                    if shape.area - kept.area > 0.02:
                        stats["clipped"] += 1
                        stats["removed_m2"] += shape.area - kept.area
                    current[rec["id"]] = kept
                # rooms: what the plan calls floor space and no slab reached (hidden by a bed, a cupboard)
                for cell in floor["cells"]:
                    if cell["c"] in no_slab_codes:
                        continue
                    room = make_valid(Polygon(cell["ring"], cell.get("holes")))
                    best = max(current, key=lambda i: current[i].intersection(room).area, default=None)
                    if best is None or current[best].intersection(room).area < 0.3 * room.area:
                        continue
                    others = unary_union([v for i, v in current.items() if i != best])
                    missing = room.difference(current[best]).difference(others)
                    if missing.area > 0.02:
                        current[best] = current[best].union(missing)
                        stats["filled_m2"] += missing.area
                for rec in slabs:
                    if rec["id"] in current:
                        rec["geometry"]["polygons"] = _slab_outline(current[rec["id"]])
                        rec["geometry"]["fitted_to_area_plan"] = True
        self.b["site"]["slab_clip"] = {k: round(v, 1) for k, v in stats.items()}


def _building_line(dash_groups, area, element_id):
    """Dashes of one line collapse to its edges. A closed line becomes a ring, an open one a path."""
    segs = [(g[i], g[i + 1]) for g in dash_groups for i in range(len(g) - 1) if math.dist(g[i], g[i + 1]) > 1e-3]
    edges = _merge_collinear(segs)
    closed = bool(area and area > 1.0)
    ring = _chain_edges(edges, closed)
    return {"id": element_id, "closed": closed, "points": [[round(a, 4), round(b, 4)] for a, b in ring], "property_area": area}


def _merge_collinear(segs, offset_tol=0.02, angle_tol=1.5):
    """Pieces on one straight line become one edge, from the first piece to the last."""
    usable = sorted(((math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180, a, b) for a, b in segs if math.dist(a, b) >= 0.2), key=lambda r: r[0])
    groups = []
    for row in usable:
        if groups and row[0] - groups[-1][-1][0] <= angle_tol:
            groups[-1].append(row)
        else:
            groups.append([row])
    if len(groups) > 1 and groups[0][0][0] + 180 - groups[-1][-1][0] <= angle_tol:
        groups[0] = groups.pop() + groups[0]
    out = []
    for items in groups:
        lead = max(items, key=lambda r: math.dist(r[1], r[2]))
        length = math.dist(lead[1], lead[2])
        ux, uy = (lead[2][0] - lead[1][0]) / length, (lead[2][1] - lead[1][1]) / length
        nx, ny = -uy, ux
        rows = sorted(((a[0] + b[0]) * nx / 2 + (a[1] + b[1]) * ny / 2, min(a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy), max(a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy)) for _, a, b in items)
        group = [rows[0]]
        for r in rows[1:] + [None]:
            if r is not None and r[0] - group[-1][0] <= offset_tol:
                group.append(r)
                continue
            offset = sum(g[0] for g in group) / len(group)
            lo, hi = min(g[1] for g in group), max(g[2] for g in group)
            out.append(((ux * lo + nx * offset, uy * lo + ny * offset), (ux * hi + nx * offset, uy * hi + ny * offset)))
            group = [r]
    return out


def _chain_edges(edges, closed):
    """Order edges end to end. Neighbouring edges meet at their line intersection."""
    if not edges:
        return []
    remaining = list(edges)
    chain = [remaining.pop(0)]
    while remaining:
        tip = chain[-1][1]
        best, flip_it, best_d = None, False, math.inf
        for e in remaining:
            for rev, p in ((False, e[0]), (True, e[1])):
                d = math.dist(tip, p)
                if d < best_d:
                    best, flip_it, best_d = e, rev, d
        remaining.remove(best)
        chain.append((best[1], best[0]) if flip_it else best)
    points = [chain[0][0]] if not closed else []
    for i in range(len(chain) - (0 if closed else 1)):
        a, b = chain[i], chain[(i + 1) % len(chain)]
        points.append(_meet_lines(a, b))
    if not closed:
        points.append(chain[-1][1])
    return points


def _meet_lines(a, b):
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    den = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(den) < 1e-9:
        return x2, y2
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / den
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1)


def _wall_outline(rec):
    g = rec["geometry"]
    if g["kind"] == "axis":
        return LineString([g["p0"], g["p1"]]).buffer(g["width"] / 2, cap_style=2)
    return None


def _num(v):
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(v)) if v else None
    return float(m.group(1)) if m else None
