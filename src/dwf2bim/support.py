"""What each element stands on, and a verdict on the whole model.

A person building a model puts every element on something: a wall on a floor or on the wall
below, a floor on walls, a railing on a floor's edge, a window in a wall. A rebuilt model can pass
every schema check and still hold a wall in mid air. This stage finds those elements. It changes
nothing: it lists them in `building["support"]`, the report and the preview show them, and
`quality()` turns them, with the other counts a reviewer starts from, into one verdict.
"""
from __future__ import annotations

import collections

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from .site import _CFG, area_plan_is_sure

RULES = _CFG["support"]
QUALITY = _CFG["quality"]


def footprint(rec):
    """An element's plan outline, or None."""
    g = rec["geometry"]
    if g.get("kind") == "axis":
        if g["p0"] == g["p1"]:
            return None
        return LineString([g["p0"], g["p1"]]).buffer(g["width"] / 2, cap_style=2)
    if g.get("kind") == "treads":
        parts = [make_valid(Polygon(t["polygon"])) for t in g["treads"] if len(t["polygon"]) >= 3]
    else:
        parts = [make_valid(Polygon(p["outer"], p.get("holes") or [])) for p in g.get("polygons") or [] if len(p["outer"]) >= 3]
    shape = unary_union(parts) if parts else None
    return None if shape is None or shape.is_empty else shape


class _Index:
    def __init__(self, records):
        self.records = records
        self.tree = STRtree([r["_fp"] for r in records]) if records else None

    def near(self, shape, reach):
        if self.tree is None:
            return []
        return [self.records[i] for i in self.tree.query(shape.buffer(reach))]


def _share(shape, others):
    if not others or shape.area <= 0:
        return 0.0
    return shape.intersection(unary_union(others)).area / shape.area


def check(building) -> dict:
    """List the elements that stand on nothing, each with what was looked for."""
    tol, reach, need = RULES["height_tolerance_m"], RULES["plan_reach_m"], RULES["min_share"]
    ground = RULES["ground_reach_m"]
    records = []
    for rec in building["elements"]:
        shape = footprint(rec)
        if shape is not None:
            records.append(dict(rec, _fp=shape))
    walls = _Index([r for r in records if r["class"] == "wall"])
    floors = _Index([r for r in records if r["class"] == "floor"])
    found = []

    def flag(rec, why):
        found.append({"id": rec["id"], "class": rec["class"], "level": rec.get("level"), "type": rec.get("type"),
                      "z0": round(rec["z0"], 3), "z1": round(rec["z1"], 3), "why": why})

    for rec in records:
        fp, z0, z1 = rec["_fp"], rec["z0"], rec["z1"]
        if rec["class"] == "wall":
            if z0 <= ground:
                continue  # stands in the ground, or on a foundation that is not drawn
            under = [f["_fp"] for f in floors.near(fp, 0.0) if f["z0"] - tol <= z0 <= f["z1"] + tol]
            # a wall below that reaches this wall's base carries it, also when it runs on past the base
            under += [w["_fp"] for w in walls.near(fp, 0.0) if w["id"] != rec["id"] and w["z0"] < z0 - tol and w["z1"] >= z0 - tol]
            if _share(fp, under) >= need:
                continue
            # a short wall under a slab is a beam or a lintel: it hangs from the slab
            over = [f["_fp"] for f in floors.near(fp, 0.0) if f["z0"] - tol <= z1 <= f["z1"] + tol]
            if z1 - z0 <= RULES["hung_wall_max_m"] and _share(fp, over) >= need:
                continue
            flag(rec, "no floor and no wall under it")
        elif rec["class"] == "floor":
            if z0 <= ground:
                continue
            carried = any(w["z0"] < z0 - tol and w["z1"] >= z0 - tol and w["_fp"].distance(fp) <= reach for w in walls.near(fp, reach))
            beside = any(f["id"] != rec["id"] and abs(f["z1"] - z1) <= RULES["neighbour_height_m"] and f["_fp"].distance(fp) <= reach
                         for f in floors.near(fp, reach))
            if not carried and not beside:
                flag(rec, "no wall under it and no floor beside it")
        elif z0 > ground:
            base = any(abs(f["z1"] - z0) <= RULES["on_floor_m"] and f["_fp"].distance(fp) <= RULES["beside_m"] for f in floors.near(fp, RULES["beside_m"]))
            base = base or any(w["z0"] - tol <= z0 <= w["z1"] + tol and w["_fp"].distance(fp) <= RULES["beside_m"] for w in walls.near(fp, RULES["beside_m"]))
            if not base:
                flag(rec, "no floor and no wall at its base")

    by_id = {r["id"]: r for r in records}
    levels = building["levels"]
    for op in building.get("openings", []):
        host = by_id.get(op.get("host"))
        top = op["z0"] + op.get("height", 0.0)
        if host is None:
            why = "no host wall"
        elif op["z0"] < host["z0"] - tol or top > host["z1"] + tol:
            why = "outside the height of its wall"
        else:
            continue
        found.append({"id": op["id"], "class": op["class"], "level": op.get("level"), "type": op.get("type"),
                      "z0": round(op["z0"], 3), "z1": round(top, 3), "why": why,
                      "level_z": levels.get(op.get("level"), {}).get("z")})
    counts = collections.Counter(r["class"] for r in found)
    out = {"unsupported": found, "counts": dict(sorted(counts.items())), "checked": len(records) + len(building.get("openings", []))}
    building["support"] = out
    return out


def quality(building, checks=None) -> dict:
    """One verdict for the model, from the counts a reviewer starts from.

    `ready`: nothing passes its limit. `review`: a person should look before the model is used.
    `failed`: the model should not be used as it is. The limits are in config/site.json.
    """
    elements = building["elements"]
    by_class = collections.Counter(r["class"] for r in elements)
    structural = ("wall", "floor", "stair", "door", "window", "railing")
    unresolved = collections.Counter(u["class"] for u in building.get("unresolved", []))
    support = building.get("support") or {"counts": {}}
    openings = building.get("openings", [])
    site = building.get("site") or {}
    areas = building.get("areas") or []
    floors = [r for r in elements if r["class"] == "floor"]

    def share(part, whole):
        return round(part / whole, 3) if whole else 0.0

    built = sum(by_class[c] for c in structural) + len(openings)
    missing = sum(unresolved[c] for c in structural)
    hanging = sum(n for c, n in support["counts"].items() if c not in ("door", "window"))
    metrics = {
        "unresolved_share": share(missing, built + missing),
        "unsupported_share": share(hanging, len(elements)),
        "openings_not_in_a_wall_share": share(sum(n for c, n in support["counts"].items() if c in ("door", "window")), len(openings)),
        "inferred_share": share(sum(1 for r in elements + openings if r.get("cloned_from") or r.get("inferred")), len(elements) + len(openings)),
        "floors_off_their_area_share": share(sum(1 for r in floors if r["geometry"].get("area_error", 0) > 0.15), len(floors)),
        "weak_plan_links": sum(1 for i in building.get("issues", []) if i.startswith("Weak plan alignment")),
        "unclassified_families": len((building.get("stats") or {}).get("unclassified_families", {})),
        "placed_on_the_survey": bool(site.get("placement")),
        "area_plan_levels": sum(len(a["levels"]) for a in areas),
        "area_plan_worst_fit": min((a.get("walls_inside_share", 0) for a in areas), default=None),
        "area_plan_levels_not_placed_surely": [lid for a in areas if not area_plan_is_sure(a, QUALITY["area_plan_min_fit"]) for lid in a["levels"]],
    }
    reasons = {"failed": [], "review": []}
    for name, limits in QUALITY["limits"].items():
        value = metrics.get(name)
        if value is None:
            continue
        for verdict in ("failed", "review"):
            if verdict in limits and value > limits[verdict]:
                reasons[verdict].append(f"{name} is {value}, over {limits[verdict]}")
                break
    if not metrics["placed_on_the_survey"]:
        reasons["review"].append("the model is not placed on the survey")
    if not areas:
        reasons["review"].append("there is no area plan")
    elif metrics["area_plan_levels_not_placed_surely"]:
        reasons["review"].append("the area plan is not placed surely on: " + ", ".join(metrics["area_plan_levels_not_placed_surely"]))
    verdict = "failed" if reasons["failed"] else "review" if reasons["review"] else "ready"
    out = {"verdict": verdict, "reasons": reasons["failed"] + reasons["review"], "metrics": metrics,
           "unresolved_by_class": dict(sorted(unresolved.items())), "unsupported_by_class": support["counts"]}
    building["quality"] = out
    return out
