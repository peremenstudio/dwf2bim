"""Floor outlines per storey, read from the plan: the main floor and each balcony.

The drawn slabs of a permit set are often in pieces, overlap, or run past the walls. What the
plan does show reliably is the walls. So each storey above the ground gets two outlines first:

- the main floor: what the storey's full-height walls close all round, to the outer face of
  their core, with the slab's shaft and stair holes kept;
- the balconies: what the drawn floors and the area plan's balcony cells cover outside it.

Both are written into the plan DXF as layers, and the storey's floors are then built from them:
one main floor and one floor per balcony, each taking the type of the drawn floor it covers most.
"""
from __future__ import annotations

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union
from shapely.validation import make_valid

from .extract import _slab_outline
from .site import _CFG, _difference

RULES = _CFG["contours"]
BALCONY_CODES = set(_CFG["balcony_codes"])
NO_SLAB_CODES = set(_CFG["no_slab_codes"])


def _shape(rec):
    parts = [make_valid(Polygon(p["outer"], p.get("holes") or [])) for p in rec["geometry"].get("polygons") or [] if len(p["outer"]) >= 3]
    return unary_union(parts) if parts else None


def _polygons(shape, min_area):
    return [g for g in getattr(shape, "geoms", [shape]) if g.geom_type == "Polygon" and g.area >= min_area]


def _square(shape, r):
    """Closing or opening with square corners kept: positive r closes gaps, negative r removes thin parts."""
    return shape.buffer(r, join_style=2).buffer(-r, join_style=2)


def _wall_bodies(walls):
    out = []
    for rec in walls:
        g = rec["geometry"]
        if g.get("p0") and g.get("p1"):
            out.append(LineString([g["p0"], g["p1"]]).buffer(g["width"] / 2, cap_style=3, join_style=2))
        else:
            out += [make_valid(Polygon(p["outer"])) for p in g.get("polygons") or [] if len(p["outer"]) >= 3]
    return [s for s in out if not s.is_empty]


def main_outline(walls, gap, stub):
    """What full-height walls close all round, to their outer faces, without the stubs that stick out."""
    bodies = unary_union(_wall_bodies(walls))
    if bodies.is_empty:
        return None
    closed = _square(bodies, gap)
    filled = unary_union([Polygon(g.exterior) for g in _polygons(closed, 1.0)])
    if filled.is_empty:
        return None
    # a wall standing out of the building, as between two balconies, leaves no tab of floor
    return _square(filled, -stub)


def storey_outlines(building, lid):
    """The main floor and balcony outlines of one storey, or None when its walls close nothing."""
    levels = building["levels"]
    z = levels[lid]["z"]
    tall = [r for r in building["elements"] if r["class"] == "wall" and r["level"] == lid
            and r["z1"] - r["z0"] >= RULES["full_height_wall_m"] and abs(r["z0"] - z) < 0.6]
    source = "walls of the storey"
    if len(tall) < RULES["min_walls"]:
        # a roof: the walls of the storey below carry it
        tall = [r for r in building["elements"] if r["class"] == "wall" and abs(r["z1"] - z) < 0.6
                and r["z1"] - r["z0"] >= RULES["full_height_wall_m"]]
        source = "walls of the storey below"
    if len(tall) < RULES["min_walls"]:
        return None
    walled = main_outline(tall, RULES["close_gaps_m"], RULES["stub_m"])
    if walled is None:
        return None
    floors = [r for r in building["elements"] if r["class"] == "floor" and r["level"] == lid and r["geometry"].get("polygons")]
    shapes = {r["id"]: _shape(r) for r in floors}
    # the area plan's cells count only where the plan sits well on the walls
    cells = [c for a in building.get("areas", []) if lid in a["levels"] and a.get("walls_inside_share", 0) >= RULES["trusted_area_fit"]
             for c in a["cells"]]
    balcony_cells = [make_valid(Polygon(c["ring"])) for c in cells if c["c"] in BALCONY_CODES]
    room_cells = [make_valid(Polygon(c["ring"])) for c in cells if c["c"] not in BALCONY_CODES and c["c"] not in NO_SLAB_CODES]
    drawn_main = [shapes[r["id"]] for r in floors if r["geometry"].get("slab_role") in ("main", None) and shapes[r["id"]] is not None]
    # what the walls close, with the drawn main floor and the rooms where a wall line is open, as at a wide glazing
    fitted_main = [shapes[r["id"]] for r in floors if r["geometry"].get("slab_role") == "main" and shapes[r["id"]] is not None]
    support = [g for g in fitted_main + room_cells if g.intersects(walled)]
    whole = _square(unary_union([walled] + support), RULES["close_gaps_m"]) if support else walled
    filled = _square(unary_union([Polygon(g.exterior) for g in _polygons(whole, 1.0)]), -RULES["stub_m"])
    # the slabs and cells add only a room the walls leave open; a strip of them along a facade, drawn
    # to the plaster or the cladding, does not push the outline past the walls
    extra = _difference(filled, walled)
    if not extra.is_empty:
        extra = _square(extra, -RULES["support_min_width_m"] / 2)
        filled = unary_union([walled] + _polygons(extra, 1.0))
        filled = unary_union([Polygon(g.exterior) for g in _polygons(filled, 1.0)])
    # a balcony cell marks a balcony inside the walls, as a recessed one, only where it covers more than a strip:
    # the cells sit a little off the walls, so a thin overlap means nothing
    inside_cells = Polygon()
    if balcony_cells:
        inside_cells = _square(unary_union(balcony_cells).intersection(filled), -RULES["support_min_width_m"] / 2)
        inside_cells = unary_union(_polygons(inside_cells, RULES["min_balcony_m2"])) if not inside_cells.is_empty else Polygon()
    main = _difference(filled, inside_cells) if not inside_cells.is_empty else filled
    # a hole stays only where no floor covers it: a shaft or a stair, not a lower bathroom floor
    covered = unary_union([v for v in shapes.values() if v is not None]) if shapes else Polygon()
    holes = []
    for g in drawn_main:
        for part in _polygons(g, 0.0):
            for ring in part.interiors:
                hole = Polygon(ring)
                if hole.area >= RULES["min_hole_m2"] and hole.within(main.buffer(0.01)) and hole.intersection(covered).area < 0.5 * hole.area:
                    holes.append(hole)
    if holes:
        main = _difference(main, unary_union(holes))
    # outside the walls a balcony is what the drawn floors cover; the cells there only told which slab is which
    others = unary_union([v for v in shapes.values() if v is not None]) if shapes else Polygon()
    outside = _difference(others, filled.buffer(0.01, join_style=2))
    balconies = unary_union([outside, inside_cells]) if not inside_cells.is_empty else outside
    plot = ((building.get("site") or {}).get("plot") or {}).get("ring") or []
    if len(plot) >= 3:
        balconies = balconies.intersection(make_valid(Polygon(plot)).buffer(RULES["beyond_plot_m"], join_style=2))
    balconies = _square(balconies, -RULES["sliver_m"])
    balconies = [b for b in _polygons(balconies, RULES["min_balcony_m2"]) if b.distance(main) < 0.05]
    return {"main": main, "balconies": balconies, "holes": len(holes), "source": source, "walls": len(tall)}


def _carrier(candidates, shapes, piece, fallback):
    best = max(candidates, key=lambda r: shapes[r["id"]].intersection(piece).area if shapes[r["id"]] is not None else 0.0, default=None)
    if best is None or shapes[best["id"]] is None or shapes[best["id"]].intersection(piece).area <= 0:
        return fallback
    return best


def apply(building) -> dict:
    """Outline every storey above the ground and rebuild its main floor and balconies from the outlines."""
    report = {"storeys": [], "floors_replaced": []}
    contours = {}
    for lid, info in sorted(building["levels"].items(), key=lambda kv: kv[1]["z"]):
        if info["z"] <= 0.01:
            continue
        out = storey_outlines(building, lid)
        if out is None:
            continue
        floors = [r for r in building["elements"] if r["class"] == "floor" and r["level"] == lid and r["geometry"].get("polygons")]
        shapes = {r["id"]: _shape(r) for r in floors}
        mains = [r for r in floors if r["geometry"].get("slab_role") in ("main", None)]
        balconies = [r for r in floors if r["geometry"].get("slab_role") == "balcony"]
        if not mains:
            continue
        # the main floor is one element, exactly the outline, of the type that covers most of it. The floors
        # drawn inside it, room by room (finishes, a shelter, a bathroom), are not elements of their own: they
        # join it rather than cut it apart. Its holes, for the stair and the lift, are shafts in Revit.
        carrier = max(mains, key=lambda r: shapes[r["id"]].intersection(out["main"]).area if shapes[r["id"]] is not None else 0.0)
        inside = [r for r in floors if r is not carrier and r not in balconies and shapes[r["id"]] is not None
                  and shapes[r["id"]].intersection(out["main"]).area >= 0.5 * shapes[r["id"]].area]
        outline = _slab_outline(out["main"])
        # a new dict: a storey copied from another must not share its floor's geometry with it
        carrier["geometry"] = dict(carrier["geometry"], polygons=[{"outer": p["outer"], "holes": []} for p in outline],
                                   shafts=[h for p in outline for h in p.get("holes") or []], slab_role="main", from_contour=True)
        gone = {r["id"] for r in inside}
        # each balcony outline is one floor, of the balcony slab it covers most
        used = set()
        new = []
        for n, piece in enumerate(out["balconies"]):
            source = _carrier(balconies + [r for r in floors if r not in mains and r not in balconies], shapes, piece, balconies[0] if balconies else None)
            if source is None:
                continue
            if source["id"] in used:
                rec = {k: v for k, v in source.items() if k not in ("geometry", "id", "ifc_guid")}
                rec.update(id="{}_b{}".format(source["id"], n), ifc_guid=None, cloned_from=source["id"], height_source="inferred",
                           geometry=dict(source["geometry"]))
                new.append(rec)
            else:
                rec = source
                used.add(source["id"])
            rec["geometry"] = {k: v for k, v in rec["geometry"].items() if k != "polygons"}
            rec["geometry"].update(polygons=_slab_outline(piece), slab_role="balcony", from_contour=True)
        gone |= {r["id"] for r in balconies if r["id"] not in used}
        building["elements"] = [r for r in building["elements"] if r["id"] not in gone] + new
        contours[lid] = {"main": _slab_outline(out["main"]), "balconies": [_slab_outline(b) for b in out["balconies"]]}
        report["storeys"].append({"level": lid, "main_m2": round(out["main"].area, 1), "balconies": len(out["balconies"]),
                                  "balconies_m2": round(sum(b.area for b in out["balconies"]), 1), "holes": out["holes"],
                                  "from": out["source"], "walls": out["walls"], "floors_joined_into_it": len(inside),
                                  "shafts": len(carrier["geometry"]["shafts"])})
        report["floors_replaced"] += sorted(gone)
    building["contours"] = contours
    return report
