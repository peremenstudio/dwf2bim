"""Stage 2 and 3 - split sheets into views, align plans, calibrate heights."""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

from .names import level_id, slug

MODEL_PRIMS = ("contour", "polygon", "polyline", "line", "circle", "ellipse")
_SECTION_LETTERS = {"א": "ALEF", "ב": "BET", "ג": "GIMEL", "ד": "DALET", "ה": "HE", "ו": "VAV"}
_TYPES = {
    "Floor Plan": "plan",
    "Section": "section",
    "Elevation": "elevation",
    "Area Plan": "area_plan",
    "Legend": "legend",
    "Drafting View": "drafting",
    "Detail View": "detail",
    "Sheet": "sheet",
}


@dataclass
class View:
    key: str  # English identifier, unique within the run
    source_name: str
    kind: str
    upm: float  # sheet units per model millimetre
    scale: int
    contour: list
    sheet: int
    prims: list = field(default_factory=list)  # model graphics
    annotation: list = field(default_factory=list)
    level: str | None = None  # source name of the associated level
    orientation: str | None = None
    primary: bool = False
    offset: tuple | None = None  # plan: metres added to sheet coordinates / upm
    z_offset: float | None = None  # section/elevation: metres added to sheet y
    z_residual: float | None = None

    def to_m(self, pt):
        k = 1.0 / (self.upm * 1000.0)
        ox, oy = self.offset or (0.0, 0.0)
        return (pt[0] * k + ox, pt[1] * k + oy)

    def z_of(self, y):
        return y / (self.upm * 1000.0) + self.z_offset


def _view_kind(name: str) -> str:
    return _TYPES.get(name.split(":")[0].split(" (")[0].strip(), "other")


def _view_key(name: str, kind: str) -> str:
    body = name.split(":", 1)[-1].strip()
    body = body.replace("_GAR", " ").strip()
    body = _SECTION_LETTERS.get(body, body)
    prefix = {"plan": "FP", "section": "SEC", "elevation": "ELEV", "area_plan": "AREA", "legend": "LEG",
              "drafting": "DRAFT", "detail": "DET", "sheet": "SHEET"}.get(kind, "VIEW")
    return f"{prefix}_{slug(body)}"


def split_views(sheets) -> dict:
    """Group W2D graphics into views; each X_Annotation stream joins its view X."""
    views: dict[str, View] = {}
    used: set[str] = set()
    with_model = set()
    for sheet_index, sheet in enumerate(sheets):
        index_to_view = {}
        # the sheet's own viewport gives the sheet's units; a DWF lists it first, a DWFX anywhere
        sheet_upm = next((vp.units_per_mm for vp in sheet.whip.views if vp.name.startswith("Sheet:")), sheet.whip.views[0].units_per_mm if sheet.whip.views else 0)
        for i, vp in enumerate(sheet.whip.views):
            is_annotation = vp.name.endswith("_Annotation")
            base = vp.name[: -len("_Annotation")] if is_annotation else vp.name
            uid = (sheet_index, base)
            if not is_annotation:
                with_model.add(uid)
            if uid not in views:
                kind = _view_kind(base)
                key = _view_key(base, kind)
                n = 2
                while key in used:
                    key = f"{_view_key(base, kind)}_{n}"
                    n += 1
                used.add(key)
                scale = round(sheet_upm / vp.units_per_mm) if vp.units_per_mm else 0
                views[uid] = View(key, base, kind, vp.units_per_mm, scale, vp.contour, sheet_index)
            index_to_view[i] = (views[uid], is_annotation)
        for p in sheet.whip.primitives:
            target = index_to_view.get(p.view)
            if target is None:
                continue
            (target[0].annotation if target[1] else target[0].prims).append(p)
        # attach viewport properties (associated level, orientation)
        for el in sheet.elements.values():
            if el.family != "Viewport" or not el.props.get("View"):
                continue
            for v in views.values():
                if v.sheet == sheet_index and v.source_name.split(":", 1)[-1].strip() == el.props["View"]:
                    v.level = el.props.get("Associated Level")
                    v.orientation = el.props.get("Orientation")
    # a view exported with only its annotation stream, as DWFX sections and elevations are, draws its model there
    for uid, v in views.items():
        if uid not in with_model:
            v.prims, v.annotation = v.annotation, []
    # some sheet sets draw the floors only as area plans, which show the same cut walls
    planned = {v.level for v in views.values() if v.kind == "plan" and v.level and v.scale <= 100 and v.orientation != "True North"}
    for v in views.values():
        if v.kind == "area_plan" and v.level and v.level not in planned:
            v.kind = "plan"
    return {v.key: v for v in views.values()}


def select_primary_plans(views: dict, sheets) -> dict:
    """Choose one plan per level: Project North, scale 1:100 or larger, most wall cuts."""
    best: dict = {}
    for v in views.values():
        if v.kind != "plan" or not v.level or v.orientation == "True North" or v.scale > 100:
            continue
        sheet = sheets[v.sheet]
        cuts = sum(
            1
            for p in v.prims
            if p.kind == "contour"
            and getattr(sheet.elements.get(sheet.node_to_element.get(p.node)), "family", "") == "Basic Wall"
        )
        if cuts and (v.level not in best or cuts > best[v.level][0]):
            best[v.level] = (cuts, v)
    out = {}
    for level, (_, v) in best.items():
        v.primary = True
        out[level] = v
    return out


def alignable_plans(views: dict) -> dict:
    """Plans that share the project frame: not rotated to true north, scale 1:100 or larger."""
    return {
        v.key: v
        for v in views.values()
        if v.kind == "plan" and v.level and v.orientation != "True North" and v.scale <= 100
    }


def align_plans(plans: dict, sheets) -> dict:
    """Solve a translation per plan from elements shared between plans.

    A vertical wall or a site boundary line drawn in two plans has identical vertices
    up to the sheet offset between them, so the true offset collects a sharp peak of
    votes. Plans are linked by a maximum spanning tree over the peak strength, then
    each link is re-measured: the share of common elements that really coincide.
    Returns diagnostics per link.
    """
    plan_list = list(plans.values())
    points: dict = {}
    for v in plan_list:
        sheet = sheets[v.sheet]
        k = 1.0 / (v.upm * 1000.0)
        by_el = collections.defaultdict(list)
        for p in v.prims:
            eid = sheet.node_to_element.get(p.node)
            if eid is not None and p.kind in MODEL_PRIMS and len(by_el[eid]) < 80:
                by_el[eid] += [(x * k, y * k) for x, y in p.points[:40]]
        points[v.key] = by_el

    edges = []
    for i, a in enumerate(plan_list):
        for b in plan_list[i + 1 :]:
            votes = collections.Counter()
            for el_id in points[a.key].keys() & points[b.key].keys():
                seen = set()
                for x0, y0 in points[a.key][el_id]:
                    for x1, y1 in points[b.key][el_id]:
                        seen.add((round((x0 - x1) * 200), round((y0 - y1) * 200)))  # 5 mm bins
                votes.update(seen)  # one vote per element, so a large imported drawing cannot dominate
            if votes:
                top = votes.most_common(2)
                margin = top[0][1] / (top[1][1] if len(top) > 1 else 1)
                edges.append((top[0][1], margin, a, b, top[0][0]))

    edges.sort(key=lambda e: -e[0])
    root = max(plan_list, key=lambda v: len(points[v.key]))
    root.offset = (0.0, 0.0)
    solved = {root.key}
    used = {}
    pending = True
    while pending:
        pending = False
        for votes, margin, a, b, (dx, dy) in edges:
            if (a.key in solved) == (b.key in solved):
                continue
            src, dst, sign = (a, b, 1) if a.key in solved else (b, a, -1)
            dst.offset = (src.offset[0] + sign * dx / 200.0, src.offset[1] + sign * dy / 200.0)
            solved.add(dst.key)
            common = points[src.key].keys() & points[dst.key].keys()
            hits = 0
            for el_id in common:
                ax, ay = src.offset
                bx, by = dst.offset
                pa = [(x + ax, y + ay) for x, y in points[src.key][el_id]]
                pb = [(x + bx, y + by) for x, y in points[dst.key][el_id]]
                if any(abs(q[0] - r[0]) < 0.02 and abs(q[1] - r[1]) < 0.02 for q in pa for r in pb):
                    hits += 1
            used[f"{src.key} -> {dst.key}"] = {
                "votes": votes,
                "margin": round(margin, 2),
                "common_elements": len(common),
                "coinciding_elements": hits,
            }
            pending = True
            break
    for v in plan_list:
        if v.offset is None:
            used[f"{v.key} (unaligned)"] = {"votes": 0, "margin": 0, "common_elements": 0, "coinciding_elements": 0}
    return used


def calibrate_heights(views: dict, sheets, levels: dict) -> None:
    """Find sheet-y to elevation per section from Level lines in its annotation stream."""
    for v in views.values():
        if v.kind not in ("section", "elevation"):
            continue
        sheet = sheets[v.sheet]
        k = 1.0 / (v.upm * 1000.0)
        pairs = []
        by_level = collections.defaultdict(list)
        for p in v.annotation:
            el = sheet.elements.get(sheet.node_to_element.get(p.node))
            if el is not None and el.family == "Level" and p.kind in ("line", "polyline"):
                (x0, y0), (x1, y1) = p.points[0], p.points[-1]
                if abs(y1 - y0) < 50:
                    by_level[el.props.get("Name")].append((abs(x1 - x0), y0))
        for name, lines in by_level.items():
            if name in levels:
                y = max(lines)[1]
                pairs.append(levels[name]["z"] - y * k)
        if pairs:
            pairs.sort()
            v.z_offset = pairs[len(pairs) // 2]
            v.z_residual = max(abs(p - v.z_offset) for p in pairs)


def in_model_frame(v) -> bool:
    """A plan aligned to the others has the model's coordinates, so its drawing can sit under the model."""
    return v.kind == "plan" and v.offset is not None and v.orientation != "True North"


def plan_drawings(views: dict, levels, typical: dict) -> dict:
    """For each level, the DWF plan to show in its floor plan, as a path beside building.json.

    Only a plan in the model's coordinates qualifies, and of a level's plans the base one, with the
    shortest name, wins over its variants. A typical storey drawn once shows the plan it was copied from.
    """
    by_level = {}
    for v in views.values():
        if in_model_frame(v) and v.level:
            by_level.setdefault(level_id(v.level), []).append(v)
    out = {}
    for lid in levels:
        source = lid if lid in by_level else (typical.get(lid) or {}).get("copied_from")
        if by_level.get(source):
            best = min(by_level[source], key=lambda v: (len(v.source_name), v.key))
            out[lid] = f"views/{best.key}.dxf"
    return out


def views_manifest(views: dict) -> list:
    return [
        {
            "key": v.key,
            "source_name": v.source_name,
            "kind": v.kind,
            "scale": v.scale,
            "level": level_id(v.level) if v.level else None,
            "primary_plan": v.primary,
            "model_primitives": len(v.prims),
            "annotation_primitives": len(v.annotation),
            "plan_offset_m": v.offset,
            "height_calibrated": v.z_offset is not None,
            "height_residual_m": v.z_residual,
            "dxf_in_model_frame": in_model_frame(v),
        }
        for v in views.values()
    ]
