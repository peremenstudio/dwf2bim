"""Stage 4 - reconstruct building elements from views and Revit properties.

Output is a neutral dictionary (building.json). All lengths are metres, heights are
relative to the project zero level. Every element records where its geometry came
from, so the QA report can state what was measured and what was inferred.
"""
from __future__ import annotations

import collections
import copy
import json
import math
import re

from shapely.geometry import LineString, MultiPoint, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from .names import CONFIG_DIR, absolute_datum, english, level_id
from .views import alignable_plans, align_plans, calibrate_heights, select_primary_plans

_UNIT_CANDIDATES = (0.001, 0.01, 0.0254, 0.3048, 1.0)


def _num(v):
    if v is None:
        return None
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", str(v))
    return float(m.group(1)) if m else None


class Rules:
    def __init__(self):
        cfg = json.loads((CONFIG_DIR / "classification.json").read_text(encoding="utf8"))
        self.enabled = set(cfg["enabled_classes"])
        self.rules = [(re.compile(r["match"]), r["class"]) for r in cfg["rules"]]
        self.type_rules = [(re.compile(r["match"]), r["class"]) for r in cfg.get("type_rules", [])]

    def classify(self, family: str, type_name: str | None = None) -> str:
        for rx, cls in self.type_rules:
            if type_name and rx.search(type_name):
                return cls
        for rx, cls in self.rules:
            if rx.search(family):
                return cls
        return "unclassified"


class Extractor:
    def __init__(self, sheets, views):
        self.sheets = sheets
        self.views = views
        self.rules = Rules()
        self.elements = {}
        self.issues = []
        self.unit = 0.01
        self._occluder_cache = {}
        self.prims = collections.defaultdict(lambda: collections.defaultdict(list))  # element -> view -> prims
        for key, v in views.items():
            sheet = sheets[v.sheet]
            for p in v.prims:
                eid = sheet.node_to_element.get(p.node)
                if eid is not None:
                    self.prims[eid][key].append(p)
        for s in sheets:
            self.elements.update(s.elements)

    # ---- units, levels ---------------------------------------------------------
    def _length(self, v):
        n = _num(v)
        if n is None:
            return None
        s = str(v)
        if s.rstrip().endswith("mm"):
            return n / 1000.0
        return n * self.unit

    def detect_units(self):
        """Compare the 'Length' property with measured wall lengths to find the display unit."""
        ratios = []
        for eid, el in self.elements.items():
            if el.family != "Basic Wall":
                continue
            length = _num(el.props.get("Length"))
            for key, prims in self.prims[eid].items():
                v = self.views[key]
                if v.kind != "plan" or not length:
                    continue
                pts = [q for p in prims if p.kind == "contour" for q in p.points]
                if len(pts) >= 4:
                    k = 1.0 / (v.upm * 1000.0)
                    r = MultiPoint([(x * k, y * k) for x, y in pts]).minimum_rotated_rectangle
                    if r.geom_type == "Polygon":
                        c = list(r.exterior.coords)
                        ratios.append(max(math.dist(c[0], c[1]), math.dist(c[1], c[2])) / length)
                break
        ratios.sort()
        median = ratios[len(ratios) // 2] if ratios else 0.01
        self.unit = min(_UNIT_CANDIDATES, key=lambda u: abs(math.log(max(median, 1e-9) / u)))
        return self.unit

    def build_levels(self):
        levels = {}
        for el in self.elements.values():
            if el.family == "Level" and el.props.get("Name") and _num(el.props.get("Elevation")) is not None:
                levels[el.props["Name"]] = {"z": _num(el.props["Elevation"]) * self.unit, "source": "level_property"}
        self.levels = levels
        return levels

    def resolve_missing_levels(self):
        """Levels referenced by elements but absent from the sheet are measured in sections."""
        samples = collections.defaultdict(list)
        for el in self.elements.values():
            if el.family != "Basic Wall":
                continue
            name = el.props.get("Base Constraint")
            if not name or name in self.levels:
                continue
            zr = self.z_range(el.id)
            if zr:
                samples[name].append(zr[0] - (self._length(el.props.get("Base Offset")) or 0.0))
        for name, vals in samples.items():
            vals.sort()
            self.levels[name] = {"z": vals[len(vals) // 2], "source": "measured_from_sections"}
            self.issues.append(f"Level {level_id(name)} is not on the sheet; height measured from sections.")

    def z_range(self, eid):
        lo, hi = math.inf, -math.inf
        for key, prims in self.prims[eid].items():
            v = self.views[key]
            if v.kind in ("section", "elevation") and v.z_offset is not None:
                for p in prims:
                    for _, y in p.points:
                        z = v.z_of(y)
                        lo, hi = min(lo, z), max(hi, z)
        return (lo, hi) if hi > lo else None

    # ---- plan helpers ---------------------------------------------------------
    def plan_prims(self, eid, level=None):
        """Primitives in the plan for this level, else any primary plan, else none."""
        if level in self.plans and self.plans[level].offset is not None and self.plans[level].key in self.prims[eid]:
            return self.plans[level], self.prims[eid][self.plans[level].key]
        for v in self.aligned.values():
            if v.offset is not None and v.key in self.prims[eid]:
                return v, self.prims[eid][v.key]
        return None, []

    @staticmethod
    def _contours(view, prims):
        polys = []
        for p in prims:
            if p.kind in ("contour", "polygon") and len(p.points) >= 3:
                g = make_valid(Polygon([view.to_m(q) for q in p.points]))
                if g.area > 1e-6:
                    polys.append(g)
        return polys

    @staticmethod
    def _points(view, prims):
        return [view.to_m(q) for p in prims if p.kind != "text" for q in p.points]

    # ---- walls ----------------------------------------------------------------
    def wall_geometry(self, el):
        base = el.props.get("Base Constraint")
        view, prims = self.plan_prims(el.id, base)
        if view is None or not prims or view.offset is None:
            return None
        polys = self._contours(view, prims)
        source = "plan_cut"
        if polys:
            shape = unary_union(polys)
        else:
            pts = self._points(view, prims)
            if len(pts) < 3:
                return None
            shape = MultiPoint(pts).minimum_rotated_rectangle
            source = "plan_projection"
        # a crop region can cut the wall short: merge its cuts from the other plans
        prop_len = self._length(el.props.get("Length"))
        if prop_len and _long_side(shape) < prop_len * 0.9:
            extra = [g for v in self.aligned.values() if v.offset is not None and v.key in self.prims[el.id] for g in self._contours(v, self.prims[el.id][v.key])]
            if extra:
                merged = unary_union(extra + [shape])
                if _long_side(merged) <= prop_len * 1.1:
                    shape = merged
                    source = "plan_cut_merged"
        rect = shape.minimum_rotated_rectangle
        if rect.geom_type != "Polygon":
            return None
        c = list(rect.exterior.coords)
        a, b = math.dist(c[0], c[1]), math.dist(c[1], c[2])
        if a >= b:
            p0 = ((c[0][0] + c[3][0]) / 2, (c[0][1] + c[3][1]) / 2)
            p1 = ((c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2)
            length, measured = a, b
        else:
            p0 = ((c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2)
            p1 = ((c[3][0] + c[2][0]) / 2, (c[3][1] + c[2][1]) / 2)
            length, measured = b, a
        width = self._length(el.props.get("Width")) or measured
        out = {"source": source, "source_view": view.key}
        if measured <= width * 1.25 + 0.03:
            out.update(kind="axis", p0=_round2(p0), p1=_round2(p1), width=round(width, 4))
        else:
            # curved or bent wall: follow its centre, so it can still be built from real wall segments
            drawn = shape if polys else unary_union([LineString([view.to_m(q) for q in p.points]) for p in prims if len(p.points) > 1])
            path = _slice_centerline(drawn, p0, p1, length)
            path_len = _path_length(path) if path else 0.0
            if path and (not prop_len or 0.7 <= path_len / prop_len <= 1.3):
                body = LineString(path).buffer(width / 2, cap_style=2, join_style=2, mitre_limit=1.5)
                out.update(kind="extrusion", polygons=_polys_out(body, 0.005), path=[_round2(q) for q in path], width=round(width, 4))
            else:
                out.update(kind="extrusion", polygons=_polys_out(_close(shape, width * 0.75), 0.005))
            if not out["polygons"]:
                return None
        return out

    def wall_heights(self, el):
        base = self.levels.get(el.props.get("Base Constraint"))
        if base is None:
            zr = self.z_range(el.id)
            return (zr[0], zr[1], "section") if zr else None
        z0 = base["z"] + (self._length(el.props.get("Base Offset")) or 0.0)
        h = self._length(el.props.get("Unconnected Height"))
        if h:
            return (z0, z0 + h, "properties")
        top = self.levels.get(el.props.get("Top Constraint"))
        if top:
            return (z0, top["z"] + (self._length(el.props.get("Top Offset")) or 0.0), "properties")
        zr = self.z_range(el.id)
        return (zr[0], zr[1], "section") if zr else None

    # ---- floors ---------------------------------------------------------------
    def floor_geometry(self, el):
        target = _num(el.props.get("Area"))
        best = None
        first, _ = self.plan_prims(el.id, el.props.get("Level"))
        candidates = ([first] if first is not None else []) + [v for v in self.aligned.values() if v is not first and v.offset is not None and v.key in self.prims[el.id]]
        for view in candidates:
            found = self._floor_shape(view, self.prims[el.id][view.key], target)
            if found and (best is None or found[1] < best[1]):
                best = found + (view,)
            if best and best[1] < 0.05:
                break
        if best is None or (target and best[1] > 0.5):
            return None
        return {
            "kind": "extrusion",
            "polygons": _slab_outline(best[0]),
            "source": best[2],
            "source_view": best[3].key,
            "area_error": round(best[1], 3),
        }

    def _occluders(self, view):
        """Outlines of elements drawn over slabs in this plan: furniture, fixtures and wall cuts."""
        if view.key not in self._occluder_cache:
            found = []
            for eid, by_view in self.prims.items():
                prims = by_view.get(view.key)
                el = self.elements.get(eid)
                if not prims or el is None:
                    continue
                cls = self.rules.classify(el.family, el.props.get("Type Name"))
                if cls == "wall":
                    found += self._contours(view, prims)
                elif cls in ("furniture", "plumbing", "equipment", "generic", "door", "window"):
                    pts = self._points(view, prims)
                    if len(pts) >= 3:
                        hull = MultiPoint(pts).convex_hull
                        if hull.geom_type == "Polygon":
                            found.append(hull)
            self._occluder_cache[view.key] = (found, STRtree(found) if found else None)
        return self._occluder_cache[view.key]

    def _floor_shape(self, view, prims, target):
        polys = self._contours(view, prims)
        lines = [LineString([view.to_m(q) for q in p.points]) for p in prims if p.kind in ("line", "polyline") and len(p.points) > 1]
        if polys:
            shape, source = unary_union(polys), "plan_cut"
        elif lines:
            found = list(polygonize(unary_union(lines)))
            if found:
                shape, source = unary_union(found), "plan_lines"
            else:
                # open outline: thicken, keep the outer rings, thin back
                thick = unary_union(lines).buffer(0.05, join_style=2, mitre_limit=1.5)
                rings = [Polygon(g.exterior) for g in getattr(thick, "geoms", [thick]) if g.geom_type == "Polygon"]
                if not rings:
                    return None
                shape, source = unary_union(rings).buffer(-0.05, join_style=2, mitre_limit=1.5), "plan_lines"
        else:
            return None

        def error(g):
            return abs(g.area - target) / target if target else 1.0

        best, best_err = shape, error(shape)
        # furniture and walls hide the slab in plan: add what touches it, close gaps, keep the best area match
        items, tree = self._occluders(view)
        starts = [shape]
        if tree is not None and shape.area > 0:
            touching = [items[i] for i in tree.query(shape.buffer(0.02), predicate="intersects")]
            furnished = [g for g in touching if g.area < 15.0]
            if furnished:
                starts.append(unary_union([shape] + furnished))
        for start in starts:
            for r in (0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.45, 0.6):
                g = _close(start, r) if r else start
                if error(g) < best_err:
                    best, best_err = g, error(g)
        return best, best_err, source

    def floor_heights(self, el):
        p = el.props
        level = self.levels.get(p.get("Level"))
        area, volume = _num(p.get("Area")), _num(p.get("Volume"))
        thick = volume / area if area and volume else 0.3
        top = self._length(p.get("Elevation at Top"))
        if top is None and level is not None:
            top = level["z"] + (self._length(p.get("Height Offset From Level")) or 0.0)
        if top is None:
            zr = self.z_range(el.id)
            return (zr[0], zr[1], "section") if zr else None
        return (top - thick, top, "properties")

    # ---- elements with a plan outline and a height range -----------------------
    def box_geometry(self, el, level):
        view, prims = self.plan_prims(el.id, level)
        if view is None or not prims:
            return None
        pts = self._points(view, prims)
        if len(pts) < 3:
            return None
        hull = MultiPoint(pts).convex_hull
        if hull.geom_type != "Polygon" or hull.area < 1e-5:
            hull = hull.buffer(0.01)
        return {"kind": "extrusion", "polygons": _polys_out(hull, 0.003), "source": "plan_outline", "source_view": view.key}

    def box_heights(self, el, level):
        zr = self.z_range(el.id)
        if zr and zr[1] - zr[0] > 0.01:
            return (zr[0], zr[1], "section")
        lv = self.levels.get(level)
        h = self._length(el.props.get("Height")) or 0.3
        return (lv["z"], lv["z"] + h, "assumed") if lv else None

    def railing_geometry(self, el):
        """A railing becomes its centre path, kept as a simple mass 10 cm wide."""
        view, prims = self.plan_prims(el.id, el.props.get("Base Level"))
        if view is None:
            return None
        segs, polylines = [], []
        for p in prims:
            pts = [view.to_m(q) for q in p.points]
            if p.kind == "contour":
                pts.append(pts[0])
            if len(pts) > 1:
                segs += [(pts[i], pts[i + 1]) for i in range(len(pts) - 1) if math.dist(pts[i], pts[i + 1]) > 1e-3]
                if p.kind == "polyline" and len(pts) > 2:
                    polylines.append(pts)
        if not segs:
            return None
        prop_len = self._length(el.props.get("Length"))
        paths = _railing_paths(segs, polylines, prop_len)
        total = sum(_path_length(path) for path in paths)
        shape = unary_union([LineString(path) for path in paths]).buffer(0.05, join_style=2, cap_style=2, mitre_limit=1.5)
        return {
            "kind": "extrusion",
            "polygons": _polys_out(shape, 0.005),
            "path": [[_round2(q) for q in path] for path in paths],
            "path_length_ratio": round(total / prop_len, 3) if prop_len else None,
            "source": "plan_path",
            "source_view": view.key,
        }

    # ---- stairs ---------------------------------------------------------------
    def stair_geometry(self, el):
        segs = []
        views_used = []
        for v in self.plans.values():
            if v.offset is None or v.key not in self.prims[el.id]:
                continue
            views_used.append(v.key)
            for p in self.prims[el.id][v.key]:
                if p.kind == "line" or (p.kind == "polyline" and len(p.points) == 2):
                    a, b = v.to_m(p.points[0]), v.to_m(p.points[-1])
                    if 0.5 <= math.dist(a, b) <= 3.5:
                        segs.append((a, b))
        if not segs:
            return None
        base = self.levels.get(el.props.get("Base Level"))
        risers = int(_num(el.props.get("Actual Number of Risers")) or 0)
        riser = self._length(el.props.get("Actual Riser Height"))
        if base is None or not risers or not riser:
            return None
        runs = _tread_runs(segs)
        if not runs:
            return None
        treads = _stack_treads(runs, base["z"] + (self._length(el.props.get("Base Offset")) or 0.0), riser, risers)
        flights, checked = self._stair_flights(el, risers)
        return {
            "kind": "treads", "treads": treads, "source": "plan_treads", "source_view": ",".join(views_used), "runs": len(runs),
            "flights": flights, "direction_checked": checked, "risers": risers,
        }

    def _stair_flights(self, el, risers):
        """Flights as start, end and width. The plan of the base level shows the lower treads, so the
        treads seen there, against those seen from the floor above, give the way up."""
        base_view = self.plans.get(el.props.get("Base Level"))
        bands = []
        for v in self.aligned.values():
            if v.offset is None or v.key not in self.prims[el.id]:
                continue
            segs = []
            for p in self.prims[el.id][v.key]:
                if p.kind == "line" or (p.kind == "polyline" and len(p.points) == 2):
                    a, b = v.to_m(p.points[0]), v.to_m(p.points[-1])
                    if 0.5 <= math.dist(a, b) <= 3.5:
                        segs.append((a, b))
            for u, n, rows in _tread_runs(segs):
                centre = sum((r[1] + r[2]) / 2 for r in rows) / len(rows)
                width = sorted(r[2] - r[1] for r in rows)[len(rows) // 2]
                band = next((x for x in bands if x["u"] == u and abs(x["centre"] - centre) < 0.15), None)
                if band is None:
                    band = {"u": u, "n": n, "centre": centre, "width": width, "base": [], "other": []}
                    bands.append(band)
                band["base" if base_view is not None and v.key == base_view.key else "other"] += [r[0] for r in rows]
        if not bands:
            return [], False
        bands.sort(key=lambda x: -(len(x["base"]) + len(x["other"])))
        depths = []
        for band in bands:
            rows = sorted(set(round(v, 3) for v in band["base"] + band["other"]))
            depths += [q - p for p, q in zip(rows, rows[1:]) if q - p < 0.5]
        tread = sorted(depths)[len(depths) // 2] if depths else 0.28

        def point(band, s):
            return _round2((band["u"][0] * band["centre"] + band["n"][0] * s, band["u"][1] * band["centre"] + band["n"][1] * s))

        def extent(band):
            rows = band["base"] + band["other"]
            return min(rows), max(rows)

        if len(bands) == 1:
            band = bands[0]
            lo, hi = extent(band)
            checked = bool(band["base"] and band["other"])
            rising = not checked or (sum(band["base"]) / len(band["base"]) <= (lo + hi) / 2)
            start = lo if rising else hi
            end = start + (risers - 1) * tread * (1 if rising else -1)
            return [{"start": point(band, start), "end": point(band, end), "width": round(band["width"], 3), "risers": risers}], checked

        first = next((b for b in bands if b["base"]), bands[0])
        second = next(b for b in bands if b is not first)
        checked = bool(first["base"]) and bool(second["other"]) and not second["base"] and first["u"] == second["u"]
        a0, a1 = extent(first)
        b0, b1 = extent(second)
        # in a U stair the first flight starts where the second ends, on the same side of the landing
        low_side = abs(a0 - b0) <= abs(a1 - b1)
        # two flights seen as one wide band: the second flight sits beside the first, at its width
        if second["width"] > 1.4 * first["width"]:
            side = 1 if second["centre"] > first["centre"] else -1
            second = dict(second, width=first["width"], centre=first["centre"] + side * (first["width"] + STAIR_WELL_M))
            checked = False
        away = 1 if low_side else -1
        lower = (risers + 1) // 2
        upper = risers - lower
        a_start = a0 if low_side else a1
        b_end = b0 if low_side else b1
        return [
            {"start": point(first, a_start), "end": point(first, a_start + away * (lower - 1) * tread), "width": round(first["width"], 3), "risers": lower},
            {"start": point(second, b_end + away * (upper - 1) * tread), "end": point(second, b_end), "width": round(second["width"], 3), "risers": upper},
        ], checked

    def _borrow_stair_layout(self, built):
        """A stair drawn in one plan only takes the layout of the stair above or below it in the same core.

        Stairs of one core repeat, so one running against the others also takes their layout.
        """
        stairs = [r for r in built.values() if r["class"] == "stair"]
        for rec in stairs:
            g = rec["geometry"]
            if not g.get("direction_checked") or len(g.get("flights") or []) < 2:
                continue
            mine = g["flights"][0]
            core = [o for o in stairs if o["geometry"].get("direction_checked") and o["geometry"].get("risers") == g["risers"] and len(o["geometry"].get("flights") or []) == len(g["flights"])
                    and LineString([o["geometry"]["flights"][0]["start"], o["geometry"]["flights"][0]["end"]]).distance(LineString([mine["start"], mine["end"]])) < 0.1]

            def heading(o):
                f = o["geometry"]["flights"][0]
                return (f["end"][0] - f["start"][0]) * (mine["end"][0] - mine["start"][0]) + (f["end"][1] - f["start"][1]) * (mine["end"][1] - mine["start"][1])

            against = [o for o in core if heading(o) < 0]
            if len(against) > len(core) - len(against):
                g["flights"] = copy.deepcopy(against[0]["geometry"]["flights"])
                g["layout_from"] = against[0]["id"]
        checked = [r for r in stairs if r["geometry"].get("direction_checked") and r["geometry"].get("flights")]
        for rec in stairs:
            g = rec["geometry"]
            if g.get("direction_checked") or not g.get("flights"):
                continue
            mine = g["flights"][0]
            for other in checked:
                f = other["geometry"]["flights"]
                if other["geometry"]["risers"] == g["risers"] and any(
                    LineString([x["start"], x["end"]]).distance(LineString([mine["start"], mine["end"]])) < 0.1 for x in f
                ):
                    g["flights"] = copy.deepcopy(f)
                    g["direction_checked"] = True
                    g["layout_from"] = other["id"]
                    break

    # ---- main -----------------------------------------------------------------
    def run(self):
        self.detect_units()
        self.build_levels()
        calibrate_heights(self.views, self.sheets, self.levels)
        self.resolve_missing_levels()
        self.plans = select_primary_plans(self.views, self.sheets)
        self.aligned = alignable_plans(self.views)
        self.alignment = align_plans(self.aligned, self.sheets)
        self._shift_to_origin()
        for link, info in self.alignment.items():
            if info["common_elements"] == 0 or info["coinciding_elements"] < 0.5 * info["common_elements"]:
                self.issues.append(f"Weak plan alignment on {link}: {info['coinciding_elements']} of {info['common_elements']} common elements coincide.")

        self.typical = self._typical_levels()
        built, pending = {}, []
        classes = collections.Counter()
        excluded = collections.Counter()
        unclassified = collections.Counter()
        for el in self.elements.values():
            cls = self.rules.classify(el.family, el.props.get("Type Name"))
            classes[cls] += 1
            if cls == "unclassified":
                unclassified[english(el.family)] += 1
            if cls not in self.rules.enabled:
                if cls != "annotation":
                    excluded[cls] += 1
                continue
            if cls in ("door", "window") or (cls != "stair" and _level_of(el) in self.typical):
                pending.append((el, cls))
                continue
            rec = self._build_one(el, cls)
            if rec:
                built[el.id] = rec
            else:
                pending.append((el, cls))

        # copy geometry from a matching element on another level (typical floors)
        self._borrow_stair_layout(built)
        self._clone_missing(built, [p for p in pending if p[1] not in ("door", "window")])
        placed, unplaced = self._place_openings(built, [p for p in pending if p[1] in ("door", "window")])
        self._clone_openings(built, placed, unplaced)
        typical_info = self._apply_typical(built, placed, pending)

        claimed = {r.get("source_element") for r in list(built.values()) + list(placed.values()) if r.get("source_element")}
        left = [(el, cls) for el, cls in pending if el.id not in built and el.id not in placed and el.id not in claimed]
        missing = [
            {"id": el.id, "class": cls, "family": english(el.family), "type": english(el.props.get("Type Name")), "level": level_id(_level_of(el))}
            for el, cls in left
            if _level_of(el) not in self.typical
        ]
        for info in typical_info.values():
            info["source_elements_without_a_twin"] = [
                {"id": el.id, "class": cls, "type": english(el.props.get("Type Name"))}
                for el, cls in left
                if level_id(_level_of(el)) == info["level"]
            ]
        return {
            "schema": "dwf2bim/1",
            "unit": "m",
            "datum_elevation": absolute_datum(self.levels),
            "display_unit_m": self.unit,
            "levels": {
                level_id(k): {"z": round(v["z"], 4), "source": v["source"], "source_name": k}
                for k, v in sorted(self.levels.items(), key=lambda kv: kv[1]["z"])
            },
            "plan_alignment": self.alignment,
            "elements": list(built.values()),
            "openings": list(placed.values()),
            "unresolved": missing,
            "typical_floors": typical_info,
            "stats": {
                "elements_by_class": dict(classes),
                "excluded_by_class": dict(excluded),
                "unclassified_families": dict(unclassified),
            },
            "issues": self.issues,
        }

    def _typical_levels(self):
        """Levels drawn by a shared plan such as 03-04: {level with no plan: level the plan shows}."""
        numbered = {}
        for name in self.levels:
            m = re.match(r"^\s*(-?\d+)\s*$", name)
            if m:
                numbered[int(m.group(1))] = name
        typical = {}
        for view in self.plans.values():
            m = re.search(r"(\d+)-(\d+)", view.source_name.split(":", 1)[-1])
            if not m or view.level is None:
                continue
            for number in range(int(m.group(1)), int(m.group(2)) + 1):
                name = numbered.get(number)
                if name and name != view.level and name not in self.plans:
                    typical[name] = view.level
        return typical

    def _apply_typical(self, built, placed, pending):
        """A typical floor is an exact copy of the floor its plan shows, moved up or down one storey.

        Revit ids from the source are reused where a same-size element exists, for traceability.
        A wall already standing there, rising from another level, is used instead of a copy.
        """
        info = {}
        for target, donor in self.typical.items():
            tid, did = level_id(target), level_id(donor)
            dz = self.levels[target]["z"] - self.levels[donor]["z"]
            pool = collections.defaultdict(list)
            for el, cls in pending:
                if _level_of(el) == target and el.id not in built:
                    pool[(el.family, el.props.get("Type Name"), cls)].append(el)
            standing = [r for r in built.values() if r["class"] == "wall" and r["level"] != did]
            donors = [r for r in built.values() if r["level"] == did and r["class"] in ("wall", "floor", "railing", "generic", "mullion", "curtain_panel")]
            hosts = {}
            copied = collections.Counter()
            used_covering = 0
            for rec in donors:
                top = rec["z1"] + dz if rec["class"] != "wall" else min(rec["z1"] + dz, rec["z0"] if rec["z1"] + dz > rec["z0"] else rec["z1"] + dz)
                if rec["class"] == "wall":
                    cover = _covering_wall(rec, rec["z0"] + dz, top, standing)
                    if cover is not None:
                        hosts[rec["id"]] = cover["id"]
                        used_covering += 1
                        continue
                new = copy.deepcopy(rec)
                twin = _take_twin(pool, rec["source_family"], rec["source_type"], rec["class"], rec, self)
                new.update(
                    id=twin.id if twin is not None else "{}@{}".format(rec["id"], tid),
                    source_element=twin.id if twin is not None else None,
                    ifc_guid=twin.props.get("IfcGUID") if twin is not None else None,
                    level=tid,
                    z0=round(rec["z0"] + dz, 4),
                    z1=round(top, 4),
                    cloned_from=rec["id"],
                    typical_of=did,
                )
                new["top_level"] = self._level_shifted(rec.get("top_level"), dz)
                built[new["id"]] = new
                hosts[rec["id"]] = new["id"]
                copied[rec["class"]] += 1
            for o in [o for o in placed.values() if o["level"] == did and o.get("host") in hosts]:
                new = copy.deepcopy(o)
                twin = _take_twin(pool, o["source_family"], o["source_type"], o["class"], o, self)
                new.update(
                    id=twin.id if twin is not None else "{}@{}".format(o["id"], tid),
                    source_element=twin.id if twin is not None else None,
                    ifc_guid=twin.props.get("IfcGUID") if twin is not None else None,
                    level=tid,
                    z0=round(o["z0"] + dz, 4),
                    host=hosts[o["host"]],
                    cloned_from=o["id"],
                    typical_of=did,
                )
                placed[new["id"]] = new
                copied[o["class"]] += 1
            info[tid] = {"level": tid, "copied_from": did, "copied": dict(copied), "walls_already_standing": used_covering}
            self.issues.append("Level {} is a typical floor: copied from {} because the plan shows both.".format(tid, did))
        return info

    def _level_shifted(self, top_id, dz):
        if not top_id:
            return None
        current = next((v["z"] for k, v in self.levels.items() if level_id(k) == top_id), None)
        if current is None:
            return None
        return next((level_id(k) for k, v in self.levels.items() if abs(v["z"] - (current + dz)) < 0.005), None)

    def _shift_to_origin(self):
        """Move the aligned frame so the model sits near 0,0 instead of at its sheet position."""
        corners = [v.to_m(c) for v in self.plans.values() if v.offset is not None for c in v.contour]
        if not corners:
            return
        ox, oy = round(min(c[0] for c in corners)), round(min(c[1] for c in corners))
        for v in self.aligned.values():
            if v.offset is not None:
                v.offset = (v.offset[0] - ox, v.offset[1] - oy)

    def _record(self, el, cls, geometry, heights, level):
        z0, z1, zsrc = heights
        return {
            "id": el.id,
            "class": cls,
            "ifc_guid": el.props.get("IfcGUID"),
            "family": english(el.family),
            "type": english(el.props.get("Type Name")) or english(el.family),
            "source_family": el.family,
            "source_type": el.props.get("Type Name"),
            "material": english(el.props.get("Structural Material")) or None,
            "level": level_id(level) if level else None,
            "z0": round(z0, 4),
            "z1": round(z1, 4),
            "height_source": zsrc,
            "geometry": geometry,
            "cloned_from": None,
            "top_level": level_id(el.props.get("Top Constraint")) if el.props.get("Top Constraint") in self.levels else None,
            "top_offset": self._length(el.props.get("Top Offset")),
        }

    def _build_one(self, el, cls):
        level = _level_of(el)
        if cls == "wall":
            g, h = self.wall_geometry(el), self.wall_heights(el)
        elif cls == "floor":
            g, h = self.floor_geometry(el), self.floor_heights(el)
        elif cls == "stair":
            g = self.stair_geometry(el)
            lv = self.levels.get(el.props.get("Base Level"))
            risers = _num(el.props.get("Actual Number of Risers"))
            riser = self._length(el.props.get("Actual Riser Height"))
            if lv and risers and riser:
                z0 = lv["z"] + (self._length(el.props.get("Base Offset")) or 0.0)
                h = (z0, z0 + risers * riser, "properties")
            else:
                h = self.box_heights(el, level)
            if g is None and h is not None:
                g = self.box_geometry(el, level)
            if g is not None and h is not None:
                rec = self._record(el, cls, g, h, level)
                rec["top_level"] = level_id(el.props.get("Top Level")) if el.props.get("Top Level") in self.levels else None
                rec["base_offset"] = self._length(el.props.get("Base Offset")) or 0.0
                return rec
        elif cls == "railing":
            g = self.railing_geometry(el)
            zr = self.z_range(el.id)
            height = self._length(el.props.get("Railing Height")) or 1.1
            view, _ = self.plan_prims(el.id, el.props.get("Base Level"))
            plan_level = view.level if view is not None else None
            base = self.levels.get(el.props.get("Base Level"))
            if base is not None:
                z0 = base["z"] + (self._length(el.props.get("Base Offset")) or 0.0)
                h = (z0, z0 + height, "properties")
            elif zr:
                h = (zr[0], zr[0] + height, "section")
            else:
                lv = self.levels.get(plan_level)
                h = (lv["z"], lv["z"] + height, "assumed") if lv else None
            level = level or plan_level
        else:
            g, h = self.box_geometry(el, level), self.box_heights(el, level)
        if g is None or h is None:
            return None
        return self._record(el, cls, g, h, level)

    def _clone_missing(self, built, pending):
        """Typical-floor copy: same family and type, same size within 3 cm, nearest level with geometry."""
        def size(el):
            p = el.props
            return _num(p.get("Length")) if el.family != "Floor" else _num(p.get("Area"))

        sources = collections.defaultdict(list)
        for rec in built.values():
            el = self.elements[rec["id"]]
            if rec["class"] in ("wall", "floor") and size(el) is not None:
                sources[(el.family, el.props.get("Type Name"))].append((size(el), rec))
        used = collections.defaultdict(set)
        for el, cls in pending:
            if el.id in built or cls not in ("wall", "floor") or size(el) is None or _level_of(el) in self.typical:
                continue
            level = _level_of(el)
            lv = self.levels.get(level)
            heights = self.wall_heights(el) if cls == "wall" else self.floor_heights(el)
            if lv is None or heights is None:
                continue
            tol = 0.03 / self.unit if cls == "wall" else max(0.2, size(el) * 0.02)
            lid = level_id(level)
            candidates = [
                r
                for s_, r in sources[(el.family, el.props.get("Type Name"))]
                if abs(s_ - size(el)) <= tol and r["level"] != lid and r["id"] not in used[lid] and not r["cloned_from"]
            ]
            if cls == "wall":
                walls = [r for r in built.values() if r["class"] == "wall"]
                candidates = [r for r in candidates if _covering_wall(r, heights[0], heights[1], walls, ignore=r["id"]) is None]
            if not candidates:
                continue
            src = min(candidates, key=lambda r: abs(self.levels.get(_level_of(self.elements[r["id"]]), {"z": 1e9})["z"] - lv["z"]))
            used[lid].add(src["id"])
            rec = self._record(el, cls, src["geometry"], heights, level)
            rec["cloned_from"] = src["id"]
            built[el.id] = rec

    def _hosted_on(self, walls, pts, z, host_type):
        """Find the wall on which the opening sits; returns (wall, axis position, measured centre)."""
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        best = None
        for w in walls:
            g = w["geometry"]
            if g.get("kind") != "axis" or not (w["z0"] - 0.05 <= z <= w["z1"] + 0.05):
                continue
            (x0, y0), (x1, y1) = g["p0"], g["p1"]
            length = math.dist(g["p0"], g["p1"])
            if length < 0.1:
                continue
            ux, uy = (x1 - x0) / length, (y1 - y0) / length
            s = (cx - x0) * ux + (cy - y0) * uy
            d = abs(-(cx - x0) * uy + (cy - y0) * ux)
            if s < -0.3 or s > length + 0.3 or d > g["width"] / 2 + 1.5:
                continue
            band = [
                (px - x0) * ux + (py - y0) * uy
                for px, py in pts
                if abs(-(px - x0) * uy + (py - y0) * ux) <= g["width"] / 2 + 0.06
            ]
            if not band:
                continue
            type_bonus = 0.0 if w["type"] == host_type else 0.25
            score = d + type_bonus
            if best is None or score < best[0]:
                best = (score, w, (min(band) + max(band)) / 2, (ux, uy, length))
        return best

    def _place_openings(self, built, pending):
        placed, unplaced = {}, []
        walls = [r for r in built.values() if r["class"] == "wall"]
        for el, cls in pending:
            p = el.props
            level = p.get("Level")
            lv = self.levels.get(level)
            view, prims = self.plan_prims(el.id, level)
            width, height = self._length(p.get("Width")), self._length(p.get("Height"))
            if view is None or not prims or lv is None or not width or not height or view.level != level:
                unplaced.append((el, cls))
                continue
            sill = self._length(p.get("Sill Height")) or 0.0
            pts = self._points(view, prims)
            if not pts:
                unplaced.append((el, cls))
                continue
            hit = self._hosted_on(walls, pts, lv["z"] + sill + height / 2, english(p.get("Host Id")))
            placed[el.id] = self._opening(el, cls, level, lv, width, height, sill, hit, pts, view.key)
        return placed, unplaced

    def _opening(self, el, cls, level, lv, width, height, sill, hit, pts, source):
        rec = {
            "id": el.id,
            "class": cls,
            "ifc_guid": el.props.get("IfcGUID"),
            "family": english(el.family),
            "type": english(el.props.get("Type Name")) or english(el.family),
            "source_family": el.family,
            "source_type": el.props.get("Type Name"),
            "level": level_id(level),
            "width": round(width, 4),
            "height": round(height, 4),
            "sill": round(sill, 4),
            "z0": round(lv["z"] + sill, 4),
            "source": source,
            "host": None,
            "cloned_from": None,
        }
        if hit is None:
            cx = sum(q[0] for q in pts) / len(pts)
            cy = sum(q[1] for q in pts) / len(pts)
            rec.update(center=_round2((cx, cy)), direction=[1.0, 0.0], depth=0.2)
            return rec
        _, wall, s, (ux, uy, length) = hit
        s = max(width / 2, min(length - width / 2, s)) if length > width else length / 2
        x0, y0 = wall["geometry"]["p0"]
        rec.update(
            center=_round2((x0 + ux * s, y0 + uy * s)),
            direction=[round(ux, 5), round(uy, 5)],
            depth=wall["geometry"]["width"],
            host=wall["id"],
        )
        return rec

    def _clone_openings(self, built, placed, unplaced):
        """Copy openings on typical floors: follow the host wall's clone relation."""
        clone_of = collections.defaultdict(dict)  # target level -> source wall id -> cloned wall id
        for r in built.values():
            if r["cloned_from"]:
                clone_of[r["level"]][r["cloned_from"]] = r["id"]
        pool = collections.defaultdict(list)
        for rec in placed.values():
            pool[(rec["family"], rec["type"])].append(rec)
        used = collections.defaultdict(set)
        for el, cls in unplaced:
            p = el.props
            level = p.get("Level")
            lv = self.levels.get(level)
            width, height = self._length(p.get("Width")), self._length(p.get("Height"))
            if lv is None or not width or not height or level in self.typical:
                continue
            lid = level_id(level)
            key = (english(el.family), english(p.get("Type Name")) or english(el.family))
            match = None
            for src in pool[key]:
                if src["level"] == lid or src["id"] in used[lid] or src.get("cloned_from"):
                    continue
                host = clone_of[lid].get(src["host"])
                if host:
                    match = (src, host)
                    break
            if match is None:
                continue
            src, host = match
            used[lid].add(src["id"])
            sill = self._length(p.get("Sill Height")) or 0.0
            rec = dict(src)
            rec.update(
                id=el.id, ifc_guid=p.get("IfcGUID"), source_family=el.family, source_type=p.get("Type Name"), level=lid, sill=round(sill, 4), z0=round(lv["z"] + sill, 4),
                width=round(width, 4), height=round(height, 4), host=host, cloned_from=src["id"],
            )
            placed[el.id] = rec


def _level_of(el):
    p = el.props
    return p.get("Level") or p.get("Base Constraint") or p.get("Base Level")


def _close(shape, r):
    """Morphological closing; the mitre limit keeps acute corners from growing spikes."""
    return shape.buffer(r, join_style=2, mitre_limit=1.5).buffer(-r, join_style=2, mitre_limit=1.5)


def _long_side(shape):
    r = shape.minimum_rotated_rectangle
    if r.geom_type != "Polygon":
        return 0.0
    c = list(r.exterior.coords)
    return max(math.dist(c[0], c[1]), math.dist(c[1], c[2]))


def _path_length(path):
    return sum(math.dist(path[i], path[i + 1]) for i in range(len(path) - 1))


def _railing_paths(segs, polylines, prop_len):
    """Reduce plan graphics of a railing to centre lines.

    Rails, posts and balusters draw many parallel lines. Long parallel lines close
    together collapse to one centre segment, and segments chain where their ends meet.
    Graphics with no long lines (curved rails) fall back to the longest drawn polyline,
    and then to the long axis of everything drawn.
    """
    pts = [q for s in segs for q in s]
    rect = MultiPoint(pts).minimum_rotated_rectangle
    axis = None
    if rect.geom_type == "Polygon":
        c = list(rect.exterior.coords)
        a, b = math.dist(c[0], c[1]), math.dist(c[1], c[2])
        if a >= b:
            axis = [((c[0][0] + c[3][0]) / 2, (c[0][1] + c[3][1]) / 2), ((c[1][0] + c[2][0]) / 2, (c[1][1] + c[2][1]) / 2)]
        else:
            axis = [((c[0][0] + c[1][0]) / 2, (c[0][1] + c[1][1]) / 2), ((c[3][0] + c[2][0]) / 2, (c[3][1] + c[2][1]) / 2)]
        if min(a, b) <= 0.3:
            return [axis]
    elif rect.geom_type == "LineString":
        return [list(rect.coords)]

    chains = _chain_segments(_centre_segments(segs))
    drawn = sum(_path_length(c) for c in chains)
    if chains and (not prop_len or drawn >= 0.5 * prop_len):
        return chains
    if polylines:
        best = max(polylines, key=_path_length)
        simple = list(LineString(best).simplify(0.02).coords)
        if _path_length(simple) > drawn:
            return [simple]
    return chains or ([axis] if axis else [list(segs[0])])


def _centre_segments(segs, min_length=0.25, rail_gap=0.15):
    buckets = collections.defaultdict(list)
    for a, b in segs:
        if math.dist(a, b) >= min_length:
            buckets[round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180 / 5) % 36].append((a, b))
    out = []
    for items in buckets.values():
        lead = max(items, key=lambda s: math.dist(*s))
        length = math.dist(*lead)
        ux, uy = (lead[1][0] - lead[0][0]) / length, (lead[1][1] - lead[0][1]) / length
        nx, ny = -uy, ux
        rows = []
        for a, b in items:
            t0, t1 = a[0] * ux + a[1] * uy, b[0] * ux + b[1] * uy
            rows.append((((a[0] + b[0]) * nx + (a[1] + b[1]) * ny) / 2, min(t0, t1), max(t0, t1)))
        rows.sort()
        group = [rows[0]]
        groups = [group]
        for r in rows[1:]:
            if r[0] - group[-1][0] <= rail_gap:
                group.append(r)
            else:
                group = [r]
                groups.append(group)
        for g in groups:
            offset = sum(r[0] for r in g) / len(g)
            spans = sorted((r[1], r[2]) for r in g)
            lo, hi = spans[0]
            merged = []
            for s0, s1 in spans[1:]:
                if s0 - hi <= 0.3:
                    hi = max(hi, s1)
                else:
                    merged.append((lo, hi))
                    lo, hi = s0, s1
            merged.append((lo, hi))
            for t0, t1 in merged:
                if t1 - t0 >= min_length:
                    out.append(((ux * t0 + nx * offset, uy * t0 + ny * offset), (ux * t1 + nx * offset, uy * t1 + ny * offset)))
    return out


def _chain_segments(segments, join=0.6):
    """Join segments end to end, meeting at their line intersection when it is close."""
    remaining = sorted(segments, key=lambda s: -math.dist(*s))
    chains = []
    while remaining:
        chain = list(remaining.pop(0))
        grew = True
        while grew:
            grew = False
            for at_end in (True, False):
                tip = chain[-1] if at_end else chain[0]
                best, best_d = None, join
                for s in remaining:
                    d = min(math.dist(tip, s[0]), math.dist(tip, s[1]))
                    if d <= best_d:
                        best, best_d = s, d
                if best is None:
                    continue
                remaining.remove(best)
                near, far = (best[0], best[1]) if math.dist(tip, best[0]) <= math.dist(tip, best[1]) else (best[1], best[0])
                prev = chain[-2] if at_end else chain[1]
                corner = _meet(prev, tip, near, far)
                if corner is not None and math.dist(corner, tip) <= join * 1.5:
                    joint = [corner]
                    if at_end:
                        chain[-1:] = joint + [far]
                    else:
                        chain[:1] = [far] + joint
                elif at_end:
                    chain += [near, far]
                else:
                    chain[:0] = [far, near]
                grew = True
        chains.append(chain)
    long_enough = [c for c in chains if _path_length(c) >= 0.3]
    return long_enough or chains


def _meet(a, b, c, d):
    """Intersection of infinite lines ab and cd; None when nearly parallel."""
    dx1, dy1, dx2, dy2 = b[0] - a[0], b[1] - a[1], d[0] - c[0], d[1] - c[1]
    den = dx1 * dy2 - dy1 * dx2
    if abs(den) < 0.05 * math.hypot(dx1, dy1) * math.hypot(dx2, dy2):
        return None
    t = ((c[0] - a[0]) * dy2 - (c[1] - a[1]) * dx2) / den
    return (a[0] + t * dx1, a[1] + t * dy1)


def _round2(p):
    return [round(p[0], 4), round(p[1], 4)]


def _polys_out(shape, tol, min_area=1e-3):
    """Outer rings and holes as coordinate lists that stay valid after rounding.

    Pinched corners are opened by a tiny shrink and grow, so Revit never gets a ring that
    touches itself. A hole is kept only if it lies clear inside its outer ring.
    """
    cleaned = shape.buffer(-0.002, join_style=2, mitre_limit=1.5).buffer(0.002, join_style=2, mitre_limit=1.5) if shape.area > 0.01 else shape
    cleaned = make_valid(cleaned.simplify(tol))
    out = []
    for g in getattr(cleaned, "geoms", [cleaned]):
        if g.geom_type != "Polygon" or g.area < min_area:
            continue
        outer = [_round2(c) for c in g.exterior.coords[:-1]]
        ring = Polygon(outer)
        if not ring.is_valid:
            ring = make_valid(ring)
            if ring.geom_type != "Polygon":
                ring = max((q for q in getattr(ring, "geoms", []) if q.geom_type == "Polygon"), key=lambda q: q.area, default=None)
            if ring is None or ring.area < min_area:
                continue
            outer = [_round2(c) for c in ring.exterior.coords[:-1]]
        inner = ring.buffer(-0.02)
        holes = []
        for r in g.interiors:
            hole = Polygon(r)
            if hole.area > 0.25 and inner.contains(hole):
                holes.append([_round2(c) for c in r.coords[:-1]])
        out.append({"outer": outer, "holes": holes})
    return out


STAIR_WELL_M = 0.135
REVIT_MIN_GAP_M = 0.012
REVIT_MIN_ANGLE_DEG = 3.0


def _ring_problem(rings):
    """What Revit refuses in a sketch though it is valid geometry: spikes, slits, tiny edges."""
    edges = []
    for k, ring in enumerate(rings):
        n = len(ring)
        for i in range(n):
            a, b, c = ring[i - 1], ring[i], ring[(i + 1) % n]
            if math.dist(b, c) < REVIT_MIN_GAP_M:
                return "short edge"
            v1, v2 = (a[0] - b[0], a[1] - b[1]), (c[0] - b[0], c[1] - b[1])
            if abs(math.degrees(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]))) < REVIT_MIN_ANGLE_DEG:
                return "spike"
            edges.append((k, i, n, LineString([b, c])))
    for x in range(len(edges)):
        for y in range(x + 1, len(edges)):
            e, f = edges[x], edges[y]
            if e[0] == f[0] and (e[1] - f[1]) % e[2] in (1, e[2] - 1):
                continue
            if e[3].distance(f[3]) < REVIT_MIN_GAP_M:
                return "touching edges"
    return None


def _slab_outline(shape, tol=0.005, min_area=0.05):
    """Slab outlines: spikes and slits narrower than r are removed, growing r until Revit can use it."""
    best = None
    for r in (0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12):
        g = shape
        if r:
            g = shape.buffer(-r, join_style=2, mitre_limit=1.5).buffer(2 * r, join_style=2, mitre_limit=1.5).buffer(-r, join_style=2, mitre_limit=1.5)
        polys = _polys_out(g, max(tol, r / 2), min_area=min_area)
        if not polys:
            continue
        best = polys
        if not any(_ring_problem([q["outer"]] + q["holes"]) for q in polys):
            return polys
    return best or []


def _footprint_of(rec):
    g = rec["geometry"]
    if g["kind"] == "axis":
        return LineString([g["p0"], g["p1"]]).buffer(g["width"] / 2, cap_style=2)
    parts = [Polygon(q["outer"]) for q in g.get("polygons", [])]
    return unary_union([make_valid(q) for q in parts]) if parts else None


def _covering_wall(rec, z0, z1, walls, ignore=None):
    """The wall that already stands on most of this footprint at this height, if any."""
    shape = _footprint_of(rec)
    if shape is None or shape.area == 0:
        return None
    mid = (z0 + z1) / 2
    for w in walls:
        if w["id"] == ignore or not (w["z0"] < mid < w["z1"]):
            continue
        other = _footprint_of(w)
        if other is not None and other.intersection(shape).area > 0.5 * shape.area:
            return w
    return None


def _take_twin(pool, family, type_name, cls, rec, extractor):
    """A source element on the typical floor with the same family, type and size, used once."""
    candidates = pool.get((family, type_name, cls))
    if not candidates:
        return None
    if cls in ("door", "window"):
        return candidates.pop(0)
    donor = extractor.elements.get(rec["id"])
    key = "Area" if cls == "floor" else "Length"
    want = _num(donor.props.get(key)) if donor is not None else None
    if want is not None:
        for i, el in enumerate(candidates):
            have = _num(el.props.get(key))
            if have is not None and abs(have - want) <= max(0.03 / extractor.unit if cls != "floor" else 0.2, want * 0.02 if cls == "floor" else 0):
                return candidates.pop(i)
        return None
    return candidates.pop(0)


def _slice_centerline(drawn, p0, p1, length, step=0.4):
    """Walk along the long axis and take the middle of what is drawn across it at each stop."""
    count = max(3, int(length / step))
    ux, uy = (p1[0] - p0[0]) / length, (p1[1] - p0[1]) / length
    nx, ny = -uy, ux
    reach = max(length, 1.0)
    points = []
    for i in range(count + 1):
        t = length * (0.002 + 0.996 * i / count)
        cx, cy = p0[0] + ux * t, p0[1] + uy * t
        hit = drawn.intersection(LineString([(cx - nx * reach, cy - ny * reach), (cx + nx * reach, cy + ny * reach)]))
        if hit.is_empty:
            continue
        x0, y0, x1, y1 = hit.bounds
        points.append(((x0 + x1) / 2, (y0 + y1) / 2))
    if len(points) < 2:
        return None
    return list(LineString(points).simplify(0.02).coords)


def _tread_runs(segs):
    """Group plan line segments into flights: parallel, similar length, evenly spaced."""
    groups = collections.defaultdict(list)
    for a, b in segs:
        ang = round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180 / 3) * 3 % 180
        groups[ang].append((a, b))
    runs = []
    for ang, items in groups.items():
        if len(items) < 3:
            continue
        rad = math.radians(ang)
        ux, uy = math.cos(rad), math.sin(rad)
        nx, ny = -uy, ux
        rows = []
        for a, b in items:
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            s = mid[0] * nx + mid[1] * ny
            t0 = a[0] * ux + a[1] * uy
            t1 = b[0] * ux + b[1] * uy
            rows.append((s, min(t0, t1), max(t0, t1)))
        rows.sort()
        dedup = []
        for r in rows:
            if dedup and abs(r[0] - dedup[-1][0]) < 0.02:
                dedup[-1] = (dedup[-1][0], min(dedup[-1][1], r[1]), max(dedup[-1][2], r[2]))
            else:
                dedup.append(r)
        current = [dedup[0]]
        for r in dedup[1:]:
            gap = r[0] - current[-1][0]
            same_band = abs((r[1] + r[2]) - (current[-1][1] + current[-1][2])) / 2 < 0.4
            if 0.18 <= gap <= 0.42 and same_band:
                current.append(r)
            else:
                if len(current) >= 3:
                    runs.append(((ux, uy), (nx, ny), current))
                current = [r]
        if len(current) >= 3:
            runs.append(((ux, uy), (nx, ny), current))
    return runs


def _stack_treads(runs, z_base, riser, risers):
    """Stepped massing: flights ordered by proximity of their ends, rising one riser per tread."""
    def corner(run, row, s):
        u, n, _ = run
        return [
            [u[0] * row[1] + n[0] * s[0], u[1] * row[1] + n[1] * s[0]],
            [u[0] * row[2] + n[0] * s[0], u[1] * row[2] + n[1] * s[0]],
            [u[0] * row[2] + n[0] * s[1], u[1] * row[2] + n[1] * s[1]],
            [u[0] * row[1] + n[0] * s[1], u[1] * row[1] + n[1] * s[1]],
        ]

    def end_point(run, row):
        u, n, _ = run
        mid = (row[1] + row[2]) / 2
        return (u[0] * mid + n[0] * row[0], u[1] * mid + n[1] * row[0])

    ordered = []
    remaining = list(runs)
    current = max(remaining, key=lambda r: len(r[2]))
    remaining.remove(current)
    rows = list(current[2])
    ordered.append((current, rows))
    while remaining:
        tip = end_point(current, rows[-1])
        def dist(r):
            return min(math.dist(tip, end_point(r, r[2][0])), math.dist(tip, end_point(r, r[2][-1])))
        nxt = min(remaining, key=dist)
        remaining.remove(nxt)
        rows = list(nxt[2])
        if math.dist(tip, end_point(nxt, rows[-1])) < math.dist(tip, end_point(nxt, rows[0])):
            rows.reverse()
        ordered.append((nxt, rows))
        current = nxt

    treads = []
    step = 0
    for run, rows in ordered:
        for i, row in enumerate(rows):
            step += 1
            if step > risers:
                break
            nxt_s = rows[i + 1][0] if i + 1 < len(rows) else row[0] + (row[0] - rows[i - 1][0] if i else 0.28)
            treads.append({"polygon": [_round2(c) for c in corner(run, row, (row[0], nxt_s))], "z_top": round(z_base + riser * step, 4)})
    return treads
