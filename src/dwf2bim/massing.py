"""A model from the area plan alone, for a building whose drawings hold no Revit object data.

Many permit sets are drawn in AutoCAD: lines with no elements behind them. Their area plan still
states what a planning check reads: each storey, its height, and its cells with their uses. So the
building is built from that: levels from the heights, one main floor and its balconies per storey
from the cells, openings where the plan marks a void, and exterior walls round each main floor.
There are no interior walls, doors or windows, and the model says so (`kind: massing`).
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

from . import site
from .extract import _slab_outline
from .site import _CFG

RULES = _CFG["massing"]
VOID_CODES = set(_CFG["contours"]["void_codes"])


def _is_outside(code) -> bool:
    """Balconies, roof terraces and technical roofs: the area tables number them from 2000."""
    try:
        return int(code) >= RULES["outside_codes_from"]
    except ValueError:
        return False


def _level_name(number) -> str:
    return "{:03d}".format(number) if number < 0 else "{:02d}".format(number)


def _scaled(panel):
    """A panel's cells in metres, y up, still where the sheet drew them."""
    k, cells = site.panel_scale(panel)
    if k is None or not cells:
        return None
    out = {}
    for n, cell in cells.items():
        if n in panel["rows"]:
            shape = make_valid(Polygon([(x * k, -y * k) for x, y in cell.exterior.coords],
                                       [[(x * k, -y * k) for x, y in r.coords] for r in cell.interiors]))
            if not shape.is_empty:
                out[n] = shape
    return {"panel": panel, "k": k, "cells": out} if out else None


def _corners(item):
    return [(round(x, 3), round(y, 3)) for c in item["cells"].values() for g in getattr(c, "geoms", [c]) if g.geom_type == "Polygon"
            for x, y in g.exterior.coords[:-1]]


def stack_panels(items) -> list:
    """Put the storeys over each other: each takes the shift that lays most of its corners on the storey beside it.

    The stair, the lift and the outer walls repeat from storey to storey, so their corners agree. Returns the
    notes on storeys that share few corners with their neighbour.
    """
    notes = []
    order = sorted(items, key=lambda it: min(it["panel"]["floors"]))
    start = next((i for i, it in enumerate(order) if 0 in it["panel"]["floors"]), 0)
    order[start]["shift"] = (0.0, 0.0)
    for step, rng in ((1, range(start + 1, len(order))), (-1, range(start - 1, -1, -1))):
        for i in rng:
            near, here = order[i - step], order[i]
            sx, sy = near["shift"]
            target = [(x + sx, y + sy) for x, y in _corners(near)]
            dx, dy, votes = site.vote_translations(_corners(here), target, top=1, bin_m=0.05)[0]
            here["shift"], here["votes"] = (dx, dy), votes
            if votes < RULES["min_shared_corners"]:
                notes.append("Storey {} shares only {} corners with the storey beside it: its place over it is not sure.".format(
                    here["panel"]["floors"], votes))
    return notes


def _heights(items) -> dict:
    """Each storey's floor level, with the ground floor at zero."""
    height = {n: it["panel"]["height"] for it in items for n in it["panel"]["floors"]}
    numbers = sorted(height)
    z = {}
    ground = 0 if 0 in height else min((n for n in numbers if n >= 0), default=numbers[0])
    z[ground] = 0.0
    for n in [m for m in numbers if m > ground]:
        below = max(m for m in z if m < n)
        z[n] = z[below] + (height[below] or RULES["default_storey_m"]) * (n - below)
    for n in sorted((m for m in numbers if m < ground), reverse=True):
        above = min(m for m in z if m > n)
        z[n] = z[above] - (height[n] or RULES["default_storey_m"]) * (above - n)
    return {n: (round(z[n], 4), height[n] or RULES["default_storey_m"]) for n in numbers}


def _walls(outline, lid, z0, z1, thickness, tag):
    """Exterior walls round a main floor: their outer face is the outline."""
    out = []
    inner = outline.buffer(-thickness / 2, join_style=2)
    for part in getattr(inner, "geoms", [inner]):
        if part.geom_type != "Polygon" or part.is_empty:
            continue
        ring = site.drop_collinear(list(part.exterior.coords[:-1]))
        for i, (a, b) in enumerate(zip(ring, ring[1:] + ring[:1])):
            if math.dist(a, b) < RULES["min_wall_m"]:
                continue
            out.append({"id": "{}_w{}".format(tag, len(out)), "class": "wall", "ifc_guid": None, "family": "Basic Wall",
                        "type": "Concrete {:.0f}".format(thickness * 100), "source_family": "Basic Wall",
                        "source_type": "בטון {:.0f}".format(thickness * 100), "material": None, "level": lid,
                        "z0": z0, "z1": z1, "height_source": "area_plan", "cloned_from": None, "inferred": True, "exterior": True,
                        "geometry": {"kind": "axis", "p0": [round(a[0], 4), round(a[1], 4)], "p1": [round(b[0], 4), round(b[1], 4)],
                                     "width": thickness, "source": "area_plan_outline"}})
    return out


def _floor(tag, lid, z, shape, role, thickness, shafts=()):
    return {"id": tag, "class": "floor", "ifc_guid": None, "family": "Floor", "type": "Concrete {:.0f}".format(thickness * 100),
            "source_family": "Floor", "source_type": "בטון {:.0f}".format(thickness * 100), "material": None, "level": lid,
            "z0": round(z - thickness, 4), "z1": z, "height_source": "area_plan", "cloned_from": None, "inferred": True,
            "exterior": role != "main",
            "geometry": {"kind": "extrusion", "polygons": [{"outer": p["outer"], "holes": []} for p in _slab_outline(shape)],
                         "shafts": [list(map(list, h)) for h in shafts], "slab_role": role, "source": "area_plan_cells", "from_contour": True}}


def build(area_sheet) -> dict:
    """The neutral model of a building, from its area plan. Raises ValueError when the plan gives no storey."""
    issues = ["Built from the area plan alone: the architecture file holds no Revit object data. "
              "The model has storeys, floors, balconies, voids and exterior walls, and no interior walls, doors or windows."]
    items, seen = [], {}
    for panel in site.read_area_panels(area_sheet):
        item = _scaled(panel)
        if item is None:
            issues.append("Area panel for floors {} has no cells or no scale, left out.".format(panel["floors"]))
            continue
        key = tuple(panel["floors"])
        if key in seen:
            # two tables for one storey: another building on the plot, or an alternative. The larger one is built
            keep, drop = (item, seen[key]) if panel["total"] > seen[key]["panel"]["total"] else (seen[key], item)
            issues.append("Two area panels for floors {}: the one of {} m2 is built, the one of {} m2 is left out. "
                          "A second building on the plot is not modelled.".format(list(key), keep["panel"]["total"], drop["panel"]["total"]))
            items = [it for it in items if it is not drop]
            if keep is item:
                items.append(item)
            seen[key] = keep
            continue
        seen[key] = item
        items.append(item)
    if not items:
        raise ValueError("the area plan gives no storey")
    issues += stack_panels(items)
    levels_z = _heights(items)
    # the model's origin: the ground storey's lower left corner
    ground = next((it for it in items if 0 in it["panel"]["floors"]), items[0])
    gx, gy = unary_union(list(ground["cells"].values())).bounds[:2]
    ox, oy = ground["shift"][0] + gx, ground["shift"][1] + gy

    slab, wall_t = RULES["slab_m"], RULES["exterior_wall_m"]
    elements, areas, contours, storeys = [], [], {}, []
    levels = {}
    for item in items:
        panel = item["panel"]
        sx, sy = item["shift"][0] - ox, item["shift"][1] - oy

        def move(shape):
            from shapely.affinity import translate
            return translate(shape, sx, sy)

        cells = {n: move(c) for n, c in item["cells"].items()}
        code = {n: panel["rows"][n]["c"] for n in cells}
        inside = [c for n, c in cells.items() if not _is_outside(code[n]) and code[n] not in VOID_CODES]
        voids = [c for n, c in cells.items() if code[n] in VOID_CODES]
        outside = {n: c for n, c in cells.items() if _is_outside(code[n])}
        footprint = unary_union(inside + voids) if inside else None
        if footprint is not None:
            footprint = footprint.buffer(RULES["close_m"], join_style=2).buffer(-RULES["close_m"], join_style=2)
            footprint = unary_union([Polygon(g.exterior) for g in getattr(footprint, "geoms", [footprint]) if g.geom_type == "Polygon"])
        # a void on the outline is a recess open to the sky; one inside is an opening in the floor
        open_cells = [v for v in voids if footprint is not None and v.buffer(-0.05).within(footprint.buffer(-0.3))]
        if footprint is not None and voids:
            recess = [v for v in voids if v not in open_cells]
            if recess:
                footprint = footprint.difference(unary_union(recess).buffer(0.01, join_style=2))
                footprint = unary_union([g for g in getattr(footprint, "geoms", [footprint]) if g.geom_type == "Polygon" and g.area > 1.0])
        for number in panel["floors"]:
            z, height = levels_z[number]
            lid = "L{}".format(_level_name(number))
            levels[lid] = {"z": z, "source": "area_plan", "source_name": _level_name(number)}
            tag = "AP{}".format(_level_name(number))
            balconies = []
            if footprint is not None and not footprint.is_empty:
                shafts = [list(v.exterior.coords[:-1]) for v in open_cells]
                main = footprint.difference(unary_union(open_cells)) if open_cells else footprint
                elements.append(_floor(tag + "_main", lid, z, footprint, "main", slab, shafts))
                for part in getattr(footprint, "geoms", [footprint]):
                    elements += _walls(part, lid, z, round(z + height, 4), wall_t, "{}_{}".format(tag, len(elements)))
            else:
                main = Polygon()
            for n, cell in outside.items():
                rec = _floor("{}_out{}".format(tag, n), lid, z, cell, "balcony", slab)
                rec["area_code"] = code[n]
                elements.append(rec)
                balconies.append(cell)
            contours[lid] = {"main": _slab_outline(main) if not main.is_empty else [], "balconies": [_slab_outline(b) for b in balconies]}
            plan_m2 = round(sum(c.area for n, c in cells.items() if code[n] not in VOID_CODES), 1)
            built = main.area + sum(b.area for b in balconies)
            storeys.append({"level": lid, "main_m2": round(main.area, 1), "balconies": len(balconies),
                            "balconies_m2": round(sum(b.area for b in balconies), 1), "holes": len(open_cells), "shafts": len(open_cells),
                            "shaft_reasons": ["area plan: void"] * len(open_cells), "holes_closed": [], "from": "area plan cells",
                            "walls": 0, "floors_joined_into_it": 0, "area_plan_m2": plan_m2,
                            "floors_over_area_plan": round(built / plan_m2, 3) if plan_m2 else None})
        out_cells = []
        for n, cell in cells.items():
            row, label = panel["rows"][n], panel["labels"].get(n)
            ring = [[round(x, 4), round(y, 4)] for x, y in cell.exterior.coords[:-1]] if cell.geom_type == "Polygon" else []
            if not ring:
                continue
            point = cell.representative_point()
            out_cells.append({"n": n, "c": row["c"], "name": _CFG["area_codes"].get(row["c"], row["c"]), "t": row["t"], "u": row.get("u"),
                              "ring": ring, "holes": [[[round(x, 4), round(y, 4)] for x, y in r.coords[:-1]] for r in cell.interiors],
                              "point": [round(point.x, 4), round(point.y, 4)] if label is not None else [round(point.x, 4), round(point.y, 4)],
                              "model_area": round(cell.area, 3)})
        edges = site.cell_edges([c for c in cells.values() if c.geom_type == "Polygon"])
        areas.append({"floors": panel["floors"], "levels": ["L{}".format(_level_name(n)) for n in panel["floors"]], "height": panel["height"],
                      "total": panel["total"], "scale": round(item["k"], 5), "fit_votes": item.get("votes", 0), "walls_inside_share": 1.0,
                      "anchor_votes": item.get("votes", 0), "fit": "stacked_on_the_storey_beside_it", "cells": out_cells,
                      "rows_without_a_cell": [n for n in panel["rows"] if n not in cells],
                      "segments": [[[round(v, 4) for v in a], [round(v, 4) for v in b]] for a, b in edges], "lines_left_out_m": 0})
    # what stands on the top storey needs a roof over it
    top = max(storeys, key=lambda s: levels[s["level"]]["z"])
    top_main = next((e for e in elements if e["id"].endswith("_main") and e["level"] == top["level"]), None)
    if top_main is not None:
        z = round(levels[top["level"]]["z"] + levels_z[max(levels_z)][1], 4)
        levels["ROOF"] = {"z": z, "source": "area_plan", "source_name": "Roof"}
        roof = dict(top_main, id="AP_roof", level="ROOF", z0=round(z - slab, 4), z1=z, exterior=True,
                    geometry=dict(top_main["geometry"], shafts=[], slab_role="main"))
        elements.append(roof)
    building = {
        "schema": "dwf2bim/1", "kind": "massing", "unit": "m", "datum_elevation": None, "display_unit_m": 0.01,
        "levels": dict(sorted(levels.items(), key=lambda kv: kv[1]["z"])), "plan_alignment": {}, "elements": elements, "openings": [],
        "unresolved": [], "typical_floors": {}, "sections": [], "shaft_hints": [], "ground_cover": [],
        "stats": {"elements_by_class": {}, "excluded_by_class": {}, "unclassified_families": {}}, "issues": issues,
        "areas": sorted(areas, key=lambda a: -max(a["floors"])), "contours": contours,
        "site": {"plot": None, "building_lines": [], "building_lines_left_out": [], "placement": None, "datum_elevation": None},
        "modelling": {"floor_contours": {"storeys": sorted(storeys, key=lambda s: levels[s["level"]]["z"]), "floors_replaced": []}},
    }
    site.assign_apartments(building)
    return building


def place(building, survey_sheet, drawing=None) -> None:
    """Put the model on the survey map, through the site plan of its drawing sheet.

    An area plan does not draw the plot. The drawing sheet does, in its site plan, with the building inside it.
    The survey's plot is found there by its shape, and the ground storey's outline inside it.
    """
    from . import survey

    spot, issues = building["site"], building["issues"]
    if survey_sheet is None:
        issues.append("No survey map: the model is not placed.")
        return
    by_colour = site.survey_corners(survey_sheet)
    rings = [by_colour] if by_colour else survey.plot_candidates(survey_sheet)
    if not rings:
        issues.append("Survey map read, but no closed ring of a plot's size was found on it: the model is not placed.")
        return
    ground = next((e for e in building["elements"] if e["class"] == "floor" and e["geometry"].get("slab_role") == "main"
                   and abs(e["z1"]) < 0.01 and e["geometry"]["polygons"]), None)
    segments = survey.drawing_segments(drawing) if drawing is not None else []
    if ground is None or not segments:
        issues.append("The plot is known only on the survey map, and there is no drawing sheet with a site plan to find it on: the model is not placed.")
        return
    # the largest part of the ground storey: a small detached part, as a parking bay, is a plain rectangle that fits anywhere
    largest = max(ground["geometry"]["polygons"], key=lambda q: Polygon(q["outer"]).area)
    outline = site.drop_collinear(largest["outer"])
    corners = [tuple(q) for a in building["areas"] if ground["level"] in a["levels"] for c in a["cells"] for q in c["ring"]]
    found = None
    for ring in rings:
        found = survey.place_by_site_plan(site.drop_collinear(ring), outline, corners, segments, RULES["sheet_units_m"])
        if found:
            break
    if not found:
        issues.append("The plot and the ground storey's outline were not found together on the drawing sheet: the model is not placed.")
        return
    theta, tx, ty = found["rotation"], found["tx"], found["ty"]
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    # a building stands inside its plot: a place that puts the ground storey across the boundary is a false match
    on_map = Polygon([(cos_t * x - sin_t * y + tx, sin_t * x + cos_t * y + ty) for x, y in outline])
    inside = on_map.intersection(Polygon(ring).buffer(0.2)).area / on_map.area
    if inside < RULES["min_share_inside_plot"]:
        issues.append("A place was found on the drawing sheet that puts {:.0%} of the ground storey outside the plot. "
                      "It is taken as a false match: the model is not placed.".format(1 - inside))
        return

    def to_model(q):
        dx, dy = q[0] - tx, q[1] - ty
        return [round(cos_t * dx + sin_t * dy, 4), round(-sin_t * dx + cos_t * dy, 4)]

    spot["survey_corners_itm"] = [[round(a, 3), round(b, 3)] for a, b in ring]
    spot["survey_area"] = round(Polygon(ring).area, 2)
    spot["plot"] = {"ring": [to_model(q) for q in ring], "area": spot["survey_area"], "id": "survey", "source": "survey"}
    spot["plot_found_by"] = "its shape on the drawing sheet's site plan" + ("" if by_colour else ", among the survey's closed rings")
    spot["placement"] = {"rotation_deg": round(math.degrees(theta), 5), "easting": round(tx, 4), "northing": round(ty, 4),
                         "fit_max_m": round(max(found["plot_fit_m"], found["outline_fit_m"]), 3), "base_point_model": spot["plot"]["ring"][0],
                         "sheet_unit_m": found["unit"], "through": "site plan"}
    if not found["sure"]:
        issues.append("The ground storey's outline fits the site plan as well when turned half round: check which way the building faces.")
    geo = site.itm_to_wgs84(ring[0][0], ring[0][1])
    if geo is not None:
        spot["placement"]["latitude"], spot["placement"]["longitude"] = round(geo[1], 7), round(geo[0], 7)
    drawn = site.survey_building_line(survey_sheet, ring)
    if drawn:
        spot["survey_building_line_itm"] = [[round(a, 3), round(b, 3)] for a, b in drawn]
        spot["building_lines"] = [{"id": "survey", "closed": True, "points": [to_model(q) for q in drawn],
                                   "property_area": round(Polygon(drawn).area, 2), "source": "survey"}]
    else:
        issues.append("The survey's building line was not found: the model has the plot line only.")
