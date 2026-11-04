"""Create Revit Areas from the area plan, after build_native.py.

Based on DYNAMO/AREA/DWFX_Areas_to_Revit.dyn: the boundaries of the numbered cells of the
area plan and nothing else, an Area in every cell, and a report of Revit area against the
official table. The data comes from OUTPUT/<project>/building.json instead of being
embedded, and is in model coordinates, so it does not depend on the shared coordinates or
on the project. Each area plan of the scheme is cleared first, those of levels with no floor
in the DWF included, so the model holds only the DWF's areas. What was built is written to
revit_areas_result.json for the comparison with the DWF.

Run from a Dynamo Python Script node, see revit/README.md. Optional lines above the exec:
  SCHEME_NAME = "..."     area scheme. Default: the first that is not Gross Building
  SNAP_M = 0.10           snap lines to wall faces within this distance. Default 0: as in the DWF
  KEEP_PREVIOUS = True    keep the lines and areas already in the area plans
"""
import glob
import json
import math
import os

import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitServices")
from Autodesk.Revit.DB import (
    AreaScheme,
    BuiltInCategory,
    BuiltInParameter,
    ElementId,
    FailureProcessingResult,
    FailureSeverity,
    FilteredElementCollector,
    GeometryInstance,
    Level,
    Line,
    Options,
    PlanarFace,
    SketchPlane,
    Solid,
    StorageType,
    Transaction,
    UV,
    ViewDetailLevel,
    ViewPlan,
    ViewType,
    Wall,
    XYZ,
)
from RevitServices.Persistence import DocumentManager
from RevitServices.Transactions import TransactionManager

FT = 1.0 / 0.3048
TAG = "DWF-AREAS"


def set_export_yes(element, missing):
    """Export to IFC goes from By Type to Yes. The value is found by its display text, since the integer is not documented."""
    p = element.get_Parameter(BuiltInParameter.IFC_EXPORT_ELEMENT) or element.LookupParameter("Export to IFC")
    if p is None or p.IsReadOnly:
        missing.add("Export to IFC")
        return
    original = p.AsInteger()
    for value in (1, 0, 2, 3):
        try:
            p.Set(value)
            if (p.AsValueString() or "").strip().lower() in ("yes", "export", "true"):
                return
        except Exception:
            pass
    p.Set(original)
    missing.add("Export to IFC (no Yes value found)")




def clean(name):
    return (name or "").replace("‏", "").replace("‎", "").strip()


def snap_vertex(v, lines, tol):
    """Onto the nearest face line; a corner goes to the intersection of its two faces."""
    near = []
    for p, u, length in lines:
        dx, dy = v[0] - p[0], v[1] - p[1]
        t = dx * u[0] + dy * u[1]
        if t < -tol or t > length + tol:
            continue
        d = abs(dx * u[1] - dy * u[0])
        if d < tol:
            near.append((d, p, u))
    if not near:
        return v
    near.sort(key=lambda x: x[0])
    _, p1, u1 = near[0]
    for _, p2, u2 in near[1:]:
        cross = u1[0] * u2[1] - u1[1] * u2[0]
        if abs(cross) > 0.17:
            t = ((p2[0] - p1[0]) * u2[1] - (p2[1] - p1[1]) * u2[0]) / cross
            q = (p1[0] + u1[0] * t, p1[1] + u1[1] * t)
            if math.hypot(q[0] - v[0], q[1] - v[1]) < tol * 1.5:
                return q
    t = (v[0] - p1[0]) * u1[0] + (v[1] - p1[1]) * u1[1]
    return (p1[0] + u1[0] * t, p1[1] + u1[1] * t)


def snap_network(segments, lines, tol, min_length):
    verts = {}
    for a, b in segments:
        verts.setdefault(a, None)
        verts.setdefault(b, None)
    for k in verts:
        verts[k] = snap_vertex(k, lines, tol) if tol > 0 and lines else k
    for _ in range(10):  # undo snaps that collapse or flip a segment
        bad = set()
        for a, b in segments:
            pa, pb = verts[a], verts[b]
            old = math.hypot(b[0] - a[0], b[1] - a[1])
            new = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
            dot = (b[0] - a[0]) * (pb[0] - pa[0]) + (b[1] - a[1]) * (pb[1] - pa[1])
            if new < min_length or dot <= 0 or (abs(new - old) > 0.5 * old and abs(new - old) > tol):
                for k in (a, b):
                    if verts[k] != k:
                        bad.add(k)
        if not bad:
            break
        for k in bad:
            verts[k] = k
    seen = {}
    for k, p in verts.items():
        key = (round(p[0], 4), round(p[1], 4))
        if key in seen and seen[key] != k:
            verts[k] = k
        else:
            seen[key] = k
    return [(verts[a], verts[b]) for a, b in segments]


def covered(p, q, lines, tol):
    for o, u, length in lines:
        hit = True
        for v in (p, q):
            dx, dy = v[0] - o[0], v[1] - o[1]
            t = dx * u[0] + dy * u[1]
            if t < -tol or t > length + tol or abs(dx * u[1] - dy * u[0]) > tol:
                hit = False
                break
        if hit:
            return True
    return False


def element_id_value(element):
    i = element.Id
    return i.Value if hasattr(i, "Value") else i.IntegerValue


class QuietFailures:
    """Warnings are dismissed and counted, and an error with a resolution is resolved."""

    def __init__(self):
        self.counts = {}

    def __call__(self, sender, args):
        try:
            accessor = args.GetFailuresAccessor()
            resolved = False
            for message in accessor.GetFailureMessages():
                warning = message.GetSeverity() == FailureSeverity.Warning
                text = message.GetDescriptionText()
                self.counts[text] = self.counts.get(text, 0) + 1
                if warning:
                    accessor.DeleteWarning(message)
                elif message.HasResolutions():
                    accessor.ResolveFailure(message)
                    resolved = True
            if resolved:
                args.SetProcessingResult(FailureProcessingResult.ProceedWithCommit)
        except Exception:
            pass


def main(root, scheme_name="", snap_m=0.0, keep_previous=False, building_name=None):
    doc = DocumentManager.Instance.CurrentDBDocument
    quiet = QuietFailures()
    # the same object must be used to subscribe and unsubscribe: each doc.Application is a new one
    app = doc.Application
    app.FailuresProcessing += quiet
    try:
        result = build(doc, root, scheme_name, snap_m, keep_previous, building_name)
    finally:
        app.FailuresProcessing -= quiet
    if quiet.counts:
        result = list(result) + [{"revit_messages": quiet.counts}]
    return result


def build(doc, root, scheme_name, snap_m, keep_previous, building_name):
    found = sorted(glob.glob(os.path.join(root, "OUTPUT", glob.escape(building_name) if building_name else "*", "building.json")), key=os.path.getmtime)
    if not found:
        return ["No building.json in OUTPUT"]
    with open(found[-1], encoding="utf8") as fh:
        building = json.load(fh)
    floors = building.get("areas") or []
    if not floors:
        return ["building.json has no area plan. Put the area DWFX in the project folder and build again."]

    log = []
    schemes = list(FilteredElementCollector(doc).OfClass(AreaScheme))
    scheme = next((s for s in schemes if clean(s.Name) == clean(scheme_name)), None) if scheme_name else None
    if scheme is None:
        scheme = next((s for s in schemes if not s.IsGrossBuildingArea), schemes[0] if schemes else None)
    if scheme is None:
        return ["The file has no Area Scheme"]
    log.append("Area scheme: " + scheme.Name + (" (the file has no other)" if scheme.IsGrossBuildingArea else ""))

    levels = list(FilteredElementCollector(doc).OfClass(Level))
    walls = list(FilteredElementCollector(doc).OfClass(Wall).WhereElementIsNotElementType())
    geometry = Options()
    geometry.DetailLevel = ViewDetailLevel.Fine

    def level_of(lid):
        """The level built for this DWF level. Another level at that height may be left over from the template."""
        info = building["levels"].get(lid)
        if info is None:
            return None
        at_height = [l for l in levels if abs(l.ProjectElevation / FT - info["z"]) < 0.02]
        name = clean(info.get("source_name")) or lid
        return next((l for l in at_height if clean(l.Name) == name), at_height[0] if at_height else None)

    def wall_lines(level):
        z = level.ProjectElevation + 1.0 * FT
        out = []
        for w in walls:
            box = w.get_BoundingBox(None)
            if box is None or box.Min.Z > z or box.Max.Z < z:
                continue
            for g in w.get_Geometry(geometry):
                solids = [g] if isinstance(g, Solid) else ([x for x in g.GetInstanceGeometry() if isinstance(x, Solid)] if isinstance(g, GeometryInstance) else [])
                for solid in solids:
                    if solid.Volume <= 0:
                        continue
                    for face in solid.Faces:
                        if not isinstance(face, PlanarFace) or abs(face.FaceNormal.Z) > 0.01:
                            continue
                        for loop in face.EdgeLoops:
                            for edge in loop:
                                curve = edge.AsCurve()
                                if not isinstance(curve, Line):
                                    continue
                                p, q = curve.GetEndPoint(0), curve.GetEndPoint(1)
                                if abs(p.Z - q.Z) > 1e-4:
                                    continue
                                length = math.hypot(q.X - p.X, q.Y - p.Y)
                                if length > 0.1:
                                    out.append(((p.X, p.Y), ((q.X - p.X) / length, (q.Y - p.Y) / length), length))
        return out

    def existing_lines(view):
        result = []
        for element in FilteredElementCollector(doc, view.Id).OfCategory(BuiltInCategory.OST_AreaSchemeLines).WhereElementIsNotElementType():
            curve = getattr(element, "GeometryCurve", None)
            if not isinstance(curve, Line):
                continue
            p, q = curve.GetEndPoint(0), curve.GetEndPoint(1)
            length = math.hypot(q.X - p.X, q.Y - p.Y)
            if length > 0.01:
                result.append(((p.X, p.Y), ((q.X - p.X) / length, (q.Y - p.Y) / length), length))
        return result

    log_file = os.path.join(os.path.dirname(found[-1]), "revit_areas_created.json")
    TransactionManager.Instance.ForceCloseTransaction()
    created, missing = [], set()
    report = ["Level | Number | Code | Table m2 | Revit m2 | Diff | Status"]
    diagnosis = []
    result_levels = {}

    def in_transaction(name, action):
        t = Transaction(doc, name)
        t.Start()
        try:
            action()
            t.Commit()
        except Exception:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            raise

    if not keep_previous:
        # area plans of levels the DWF has no floor for: what they hold came from another project
        wanted = {level_of(lid).Id.ToString() for floor in floors for lid in floor["levels"] if level_of(lid) is not None}

        def clear_other_levels():
            count = 0
            for view in FilteredElementCollector(doc).OfClass(ViewPlan):
                if view.IsTemplate or view.ViewType != ViewType.AreaPlan or view.AreaScheme.Id != scheme.Id or view.GenLevel is None:
                    continue
                if view.GenLevel.Id.ToString() in wanted:
                    continue
                for category in (BuiltInCategory.OST_Areas, BuiltInCategory.OST_AreaSchemeLines):
                    for element_id in [e.Id for e in FilteredElementCollector(doc, view.Id).OfCategory(category).WhereElementIsNotElementType()]:
                        if doc.GetElement(element_id) is not None:
                            doc.Delete(element_id)
                            count += 1
            log.append("Deleted {} lines and areas on levels with no floor in the DWF".format(count))

        in_transaction("DWF areas: other levels", clear_other_levels)

    tol = snap_m * FT
    min_length = max(doc.Application.ShortCurveTolerance * 2, 0.01 * FT)
    for floor in floors:
        base = []
        for line in floor["segments"]:
            pts = [(q[0] * FT, q[1] * FT) for q in line]
            base += list(zip(pts, pts[1:]))
        for index, lid in enumerate(floor["levels"]):
            info = building["levels"].get(lid)
            level = level_of(lid)
            if level is None:
                log.append("Level not found: " + lid)
                continue
            holder = {}

            def draw_lines():
                view = next((v for v in FilteredElementCollector(doc).OfClass(ViewPlan)
                             if not v.IsTemplate and v.ViewType == ViewType.AreaPlan and v.AreaScheme.Id == scheme.Id
                             and v.GenLevel is not None and v.GenLevel.Id == level.Id), None)
                if view is None:
                    view = ViewPlan.CreateAreaPlan(doc, scheme.Id, level.Id)
                    try:
                        view.Name = u"{} - {}".format(clean(scheme.Name), clean(level.Name))
                    except Exception:
                        pass
                holder["view"] = view
                if not keep_previous:
                    # an earlier run, or an automatic contour, must not stay next to the DWF's lines
                    for category in (BuiltInCategory.OST_Areas, BuiltInCategory.OST_AreaSchemeLines):
                        for element_id in [e.Id for e in FilteredElementCollector(doc, view.Id).OfCategory(category).WhereElementIsNotElementType()]:
                            if doc.GetElement(element_id) is not None:
                                doc.Delete(element_id)
                old = existing_lines(view)
                faces = wall_lines(level) if tol > 0 else []
                segments = snap_network(base, faces + old, tol, min_length)
                z = level.Elevation
                plane = SketchPlane.Create(doc, level.Id)
                drawn = skipped = 0
                for a, b in segments:
                    if math.hypot(b[0] - a[0], b[1] - a[1]) < min_length:
                        continue
                    if old and covered(a, b, old, 0.01 * FT):
                        skipped += 1
                        continue
                    line = doc.Create.NewAreaBoundaryLine(plane, Line.CreateBound(XYZ(a[0], a[1], z), XYZ(b[0], b[1], z)), view)
                    created.append(element_id_value(line))
                    drawn += 1
                holder["drawn"], holder["skipped"] = drawn, skipped

            in_transaction("DWF areas: lines", draw_lines)
            view = holder["view"]
            placed = []

            def place_areas():
                for cell in floor["cells"]:
                    area = doc.Create.NewArea(view, UV(cell["point"][0] * FT, cell["point"][1] * FT))
                    created.append(element_id_value(area))
                    number = cell["n"] if len(floor["levels"]) == 1 else u"{}.{}".format(cell["n"], index + 1)
                    try:
                        area.Number = number
                    except Exception:
                        pass
                    area.Name = cell["name"]
                    comment = area.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    if comment is not None and not comment.IsReadOnly:
                        comment.Set(u"{} | DWF {} | {} m2 | DWF unit {}".format(TAG, cell["n"], cell["t"], cell.get("u") or "-"))
                    set_export_yes(area, missing)
                    values = {"IL_Usage": cell["c"], "IL_UnitID": floor.get("unit_ids", {}).get(lid, {}).get(cell["n"], 0)}
                    for name, value in values.items():
                        p = area.LookupParameter(name)
                        if p is None or p.IsReadOnly:
                            missing.add(name)
                            continue
                        try:
                            if p.StorageType == StorageType.Integer:
                                p.Set(int(value))
                            elif p.StorageType == StorageType.Double:
                                p.Set(float(value))
                            else:
                                p.Set(u"{}".format(value))
                        except Exception as ex:
                            missing.add(u"{} ({})".format(name, ex))
                    placed.append((area, cell, number))

            in_transaction("DWF areas: areas", place_areas)
            # read only after the commit, when Revit has computed the areas
            enclosed = 0
            for area, cell, number in placed:
                revit_area = area.Area / (FT * FT)
                diff = revit_area - cell["t"]
                enclosed += revit_area > 1e-6
                status = "OK" if abs(diff) <= max(0.1, 0.01 * cell["t"]) else ("NOT ENCLOSED" if revit_area < 1e-6 else "CHECK")
                report.append(u"{} | {} | {} | {:.2f} | {:.2f} | {:+.2f} | {}".format(lid, number, cell["c"], cell["t"], revit_area, diff, status))
            if placed:
                area, cell, _ = placed[0]
                location = area.Location
                point = location.Point if location is not None and hasattr(location, "Point") else None
                diagnosis.append({
                    "level": lid, "view": view.Name, "lines_in_view": len(existing_lines(view)), "lines_drawn": holder["drawn"],
                    "areas": len(placed), "enclosed": enclosed, "first_cell": cell["n"], "first_point_m": cell["point"],
                    "first_area_at_m": [round(point.X / FT, 3), round(point.Y / FT, 3), round(point.Z / FT, 3)] if point else None,
                    "first_perimeter_m": round(area.Perimeter / FT, 2), "level_elevation_m": round(level.Elevation / FT, 3),
                })
            log.append(u"{}: {} lines, {} skipped as existing, {} areas, {} enclosed".format(lid, holder["drawn"], holder["skipped"], len(placed), enclosed))
            result_levels[lid] = {
                "view": view.Name,
                "lines": [[[round(p[0] / FT, 4), round(p[1] / FT, 4)], [round((p[0] + u[0] * length) / FT, 4), round((p[1] + u[1] * length) / FT, 4)]] for p, u, length in existing_lines(view)],
                "areas": [{"n": cell["n"], "number": number, "area": round(area.Area / (FT * FT), 3), "table": cell["t"]} for area, cell, number in placed],
            }
    if missing:
        log.append("Parameter problems: " + ", ".join(sorted(missing)))
    with open(log_file, "w") as fh:
        json.dump(created, fh)
    bad = [r for r in report[1:] if not r.endswith("OK")]
    log.append("Done: {} areas, {} to check".format(len(report) - 1, len(bad)))
    folder = os.path.dirname(found[-1])
    with open(os.path.join(folder, "revit_areas_report.txt"), "w", encoding="utf8") as fh:
        fh.write("\n".join(report))
    with open(os.path.join(folder, "revit_areas_diagnosis.json"), "w", encoding="utf8") as fh:
        json.dump(diagnosis, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(folder, "revit_areas_result.json"), "w", encoding="utf8") as fh:
        json.dump(result_levels, fh, ensure_ascii=False)
    return [log, bad]


OUT = main(  # noqa: F821 - ROOT is set by the Dynamo node
    ROOT, globals().get("SCHEME_NAME", ""), globals().get("SNAP_M", 0.0), globals().get("KEEP_PREVIOUS", False), globals().get("BUILDING")
)
