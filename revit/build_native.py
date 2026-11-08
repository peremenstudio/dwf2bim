"""Build native Revit elements from building.json, using the types of the open file.

Everything built is a native, editable Revit element: walls, floors, doors, windows,
railings and stairs. A pergola beam or any other mass becomes a floor of a type named
"DWF floor <thickness>". A type is reused when the file has it; otherwise a similar
type is duplicated and resized. An element that cannot be built natively is reported, never
replaced by fixed geometry. Each element's Comments holds DWF:<source element id>, so a
second run skips what already exists.

Run from a Dynamo Python Script node, see revit/README.md.
"""
import glob
import json
import math
import os
import re
import traceback
import uuid

FT = 0.3048
MIN_EDGE_M = 0.005
BATCH = 50
STAMP = "DWF:"
_MARKS = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮"))


# ---- logic with no Revit dependency -----------------------------------------
def ft(m):
    return float(m) / FT


def clean_name(text):
    return (text or "").translate(_MARKS).strip()


def clean_ring(ring, min_edge=MIN_EDGE_M):
    """Drop vertices closer than Revit's shortest usable edge, keeping the ring closed."""
    out = []
    for p in ring:
        if not out or math.dist(p, out[-1]) >= min_edge:
            out.append(p)
    while len(out) > 2 and math.dist(out[0], out[-1]) < min_edge:
        out.pop()
    return out


def clean_path(path, min_edge=MIN_EDGE_M):
    out = []
    for p in path:
        if not out or math.dist(p, out[-1]) >= min_edge:
            out.append(p)
    return out


def find_building(root, name=None):
    """name is a project under OUTPUT, or the full path of the folder holding building.json."""
    pattern = os.path.join(root, "OUTPUT", glob.escape(name) if name else "*", "building.json")
    found = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not found:
        raise IOError("No building.json at " + pattern + ". Run: python -m dwf2bim build")
    return found[-1]


def wall_moved(expected, actual, tolerance=0.3):
    """Whether a wall's two ends, in metres, are not where they were asked to be, in either direction."""
    a0, a1 = actual
    e0, e1 = expected
    gap = min(max(_dist(a0, e0), _dist(a1, e1)), max(_dist(a0, e1), _dist(a1, e0)))
    return gap > tolerance


def _dist(p, q):
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


def target_type_name(source, explicit, rules):
    """The template's name for a DWF type: explicit map first, then regex rules, else unchanged."""
    name = clean_name(source)
    if name in explicit:
        return explicit[name]
    for pattern, replacement in rules:
        if re.match(pattern, name):
            return re.sub(pattern, replacement, name)
    return name


def base_by_word(names, source, words):
    """First mapped word found in the source name, in the order the mapping lists them."""
    text = clean_name(source)
    for word, base in words.items():
        if word in text and base in names:
            return base
    return None


def pick_base(names, source_name, preferred=None):
    """Which existing type to duplicate: mapped, else same first word, else the first one."""
    if preferred and preferred in names:
        return preferred
    words = clean_name(source_name).split()
    if words:
        for n in names:
            if words[0] in n:
                return n
    return names[0] if names else None


def mapped_family(mapping, family):
    """opening_families values are a family name, or {"family": name, "type_format": "{w}/{h}"}."""
    entry = mapping.get("opening_families", {}).get(family)
    if isinstance(entry, dict):
        return entry.get("family"), entry.get("type_format")
    return entry, None


def resolve_opening(available, family, type_name, category, mapping, every_family=None):
    """Choose a family for an opening. Returns (family, exact type or None, how it was chosen).

    A mapping may point to a family of the other category, such as a storefront that the
    template models as a door, so mapped names are looked up in every_family.
    """
    every = every_family if every_family is not None else available
    target, _ = mapped_family(mapping, family)
    if target and target in every:
        return target, (type_name if type_name in every[target] else None), "mapped"
    types = every.get(family)
    if types:
        return family, (type_name if type_name in types else None), "same_family"
    default = mapping.get("default_" + category + "_family")
    if default and default in every:
        return default, None, "default"
    if available:
        return sorted(available)[0], None, "first_available"
    return None, None, "none"


def sized_type_name(type_name, width, height, type_format=None):
    if type_format:
        return type_format.format(w=int(round(width * 100)), h=int(round(height * 100)))
    return clean_name(type_name) or "{:.0f}/{:.0f}".format(width * 100, height * 100)


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def summarize(log):
    lines = ["{}: created {}, skipped {}, failed {}".format(k, v["created"], v["skipped"], len(v["failed"])) for k, v in sorted(log["classes"].items())]
    if "model_elements_deleted" in log:
        lines.insert(0, "Existing model elements deleted: {}".format(log["model_elements_deleted"]))
    if "openings_checked" in log:
        lines.append("Doors and windows: checked {}, placed again {}, wall still not cut {}".format(log["openings_checked"], log.get("openings_replaced", 0), len(log.get("openings_not_cut", []))))
    if log.get("levels_created") or log.get("levels_deleted"):
        lines.append("Levels created: {}, deleted: {}".format(len(log.get("levels_created", [])), len(log.get("levels_deleted", []))))
    if log.get("levels_not_deleted"):
        lines.append("Levels not deleted, open another view and delete by hand: {} (active view: {})".format(", ".join(x["level"] for x in log["levels_not_deleted"]), log.get("active_view")))
    if log.get("elements_removed_by_revit"):
        lines.append("Removed by Revit when resolving its errors: {}".format(len(log["elements_removed_by_revit"])))
    if log.get("walls_moved_by_revit"):
        lines.append("Walls not where the DWF put them: {}".format(len(log["walls_moved_by_revit"])))
    if log.get("walls_not_from_the_dwf"):
        lines.append("Walls that did not come from the DWF: {}".format(len(log["walls_not_from_the_dwf"])))
    if isinstance(log.get("placement"), dict):
        lines.append("Placement: worst plot corner {} m".format(log["placement"].get("worst_corner_m")))
    if log.get("revit_messages"):
        lines.append("Revit messages handled: {}".format(sum(log["revit_messages"].values())))
    return lines + ["Log: " + log.get("path", "")]


# ---- Revit ------------------------------------------------------------------
try:
    import clr

    IN_REVIT = True
except ImportError:
    IN_REVIT = False

if IN_REVIT:
    clr.AddReference("RevitAPI")
    clr.AddReference("RevitServices")
    from Autodesk.Revit.DB import (
        BasePoint,
        BuiltInCategory,
        BuiltInParameter,
        Color,
        CompoundStructure,
        CurveElement,
        CurveLoop,
        ElementId,
        ElementTransformUtils,
        FailureProcessingResult,
        FailureSeverity,
        FamilySymbol,
        FilteredElementCollector,
        Floor,
        FloorType,
        GraphicsStyleType,
        IFailuresPreprocessor,
        Level,
        Line,
        MaterialFunctionAssignment,
        Plane,
        ProjectPosition,
        SketchPlane,
        StairsEditScope,
        Transaction,
        TransactionStatus,
        ViewFamily,
        ViewFamilyType,
        ViewPlan,
        Wall,
        WallKind,
        WallType,
        XYZ,
    )
    from Autodesk.Revit.DB.Architecture import Railing, RailingType, StairsLanding, StairsRun, StairsRunJustification
    from Autodesk.Revit.DB.Structure import StructuralType
    from RevitServices.Persistence import DocumentManager
    from RevitServices.Transactions import TransactionManager
    from System.Collections.Generic import List


def _make_stairs_preprocessor():
    """Revit asks for this object when a stair edit ends. Dynamo reruns need a fresh class name."""
    try:
        class StairsFailures(IFailuresPreprocessor):
            __namespace__ = "Dwf2Bim_" + uuid.uuid4().hex

            def PreprocessFailures(self, accessor):
                return FailureProcessingResult.Continue

        return StairsFailures()
    except Exception:
        return None


class FailureHandler:
    """Keeps Revit's failure dialogs away, and records every message for the log.

    Warnings are dismissed. An error with a resolution is resolved. An error with none
    rolls the transaction back, and process() then retries that batch one element at a time.
    """

    def __init__(self, log):
        self.log = log
        self.counts = log.setdefault("revit_messages", {})

    def __call__(self, sender, args):
        try:
            accessor = args.GetFailuresAccessor()
            resolved = False
            rollback = False
            for message in accessor.GetFailureMessages():
                severity = message.GetSeverity()
                text = "{}: {}".format("warning" if severity == FailureSeverity.Warning else "error", message.GetDescriptionText())
                self.counts[text] = self.counts.get(text, 0) + 1
                if severity == FailureSeverity.Warning:
                    accessor.DeleteWarning(message)
                elif message.HasResolutions():
                    accessor.ResolveFailure(message)
                    resolved = True
                else:
                    rollback = True
            if rollback:
                args.SetProcessingResult(FailureProcessingResult.ProceedWithRollBack)
            elif resolved:
                args.SetProcessingResult(FailureProcessingResult.ProceedWithCommit)
        except Exception as ex:
            self.log.setdefault("errors", []).append("failure handler: {}".format(ex))


def name_of(e):
    try:
        return e.Name
    except Exception:
        p = e.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)
        return p.AsString() if p else ""


class Builder:
    def __init__(self, doc, building, mapping, log):
        self.doc = doc
        self.b = building
        self.map = mapping
        self.log = log
        self.levels = {}
        self.created = {}  # source id -> element id
        self.pending = {}
        self.wall_types = self._basic_wall_types()
        self.floor_types = {name_of(t): t for t in self._collect(FloorType) if not getattr(t, "IsFoundationSlab", False)}
        self.railing_types = {name_of(t): t for t in self._collect(RailingType)}
        self.symbols = {"door": self._symbols(BuiltInCategory.OST_Doors), "window": self._symbols(BuiltInCategory.OST_Windows)}

    # ---- lookups
    def _collect(self, cls):
        return list(FilteredElementCollector(self.doc).OfClass(cls))

    def _basic_wall_types(self):
        return {name_of(t): t for t in self._collect(WallType) if t.Kind == WallKind.Basic}

    def _symbols(self, category):
        out = {}
        for s in FilteredElementCollector(self.doc).OfClass(FamilySymbol).OfCategory(category):
            out.setdefault(s.Family.Name, {})[name_of(s)] = s
        return out

    def _section(self, cls):
        return self.log["classes"].setdefault(cls, {"created": 0, "skipped": 0, "failed": [], "substitutions": {}})

    def existing_stamps(self):
        found = {}
        for cat in (BuiltInCategory.OST_Walls, BuiltInCategory.OST_Floors, BuiltInCategory.OST_Doors, BuiltInCategory.OST_Windows,
                    BuiltInCategory.OST_StairsRailing, BuiltInCategory.OST_Stairs):
            for e in FilteredElementCollector(self.doc).OfCategory(cat).WhereElementIsNotElementType():
                p = e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                text = p.AsString() if p else None
                if text and text.startswith(STAMP):
                    found[text[len(STAMP):]] = e.Id
        return found

    # ---- transactions
    def _transact(self, label, action):
        t = Transaction(self.doc, label)
        options = t.GetFailureHandlingOptions()
        options.SetClearAfterRollback(True)
        options.SetForcedModalHandling(False)
        t.SetFailureHandlingOptions(options)
        t.Start()
        try:
            action()
            return t.Commit() == TransactionStatus.Committed
        except Exception:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            raise

    def process(self, cls, records, make):
        """Batch for speed. If Revit rejects a batch at commit, retry its records one by one."""
        section = self._section(cls)
        for batch in chunks(records, BATCH):
            self.pending = {}
            try:
                committed = self._transact("DWF " + cls, lambda: [self._one(cls, r, make) for r in batch])
            except Exception as ex:
                committed = False
                self.log["errors"].append("{} batch: {}".format(cls, ex))
            if committed:
                self._accept(section)
                continue
            for r in batch:
                self.pending = {}
                try:
                    done = self._transact("DWF " + cls, lambda: self._one(cls, r, make))
                except Exception as ex:
                    done = False
                    section["failed"].append({"id": r["id"], "error": str(ex)})
                if done:
                    self._accept(section)
                elif r["id"] in self.pending:
                    section["failed"].append({"id": r["id"], "error": "rejected by Revit at commit"})

    def _accept(self, section):
        self.created.update(self.pending)
        section["created"] += len(self.pending)

    def _one(self, cls, rec, make):
        section = self._section(cls)
        if rec["id"] in self.created:
            section["skipped"] += 1
            return
        try:
            element = make(rec)
            self._stamp(element, rec)
            self.pending[rec["id"]] = element.Id
        except Exception as ex:
            section["failed"].append({"id": rec["id"], "type": rec.get("type"), "error": str(ex)})

    def _stamp(self, element, rec):
        p = element.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p is not None and not p.IsReadOnly:
            p.Set(STAMP + rec["id"])

    # ---- levels
    def make_levels(self):
        """The DWF's levels, under their DWF names. A level of the file is never taken over for another height."""
        existing = {clean_name(name_of(l)): l for l in self._collect(Level)}
        plan_type = next((t for t in self._collect(ViewFamilyType) if t.ViewFamily == ViewFamily.FloorPlan), None)
        for lid, info in self.b["levels"].items():
            name = clean_name(info.get("source_name")) or lid
            level = existing.get(name)
            if level is None:
                level = Level.Create(self.doc, ft(info["z"]))
                try:
                    level.Name = name
                except Exception:
                    level.Name = lid
                if plan_type is not None:
                    ViewPlan.Create(self.doc, plan_type.Id, level.Id)
                self.log["levels_created"].append(name)
            elif abs(level.Elevation * FT - info["z"]) > 0.0005:
                level.Elevation = ft(info["z"])
            self.levels[lid] = level

    def delete_other_levels(self):
        """Levels the DWF does not have, with their views. The active view cannot be deleted, so that level is reported."""
        keep = {l.Id.ToString() for l in self.levels.values()}
        deleted, stayed = [], []
        for level in self._collect(Level):
            if level.Id.ToString() in keep:
                continue
            name, level_id = clean_name(name_of(level)), level.Id
            try:
                self._transact("DWF delete level", lambda: self.doc.Delete(level_id))
                deleted.append(name)
            except Exception as ex:
                stayed.append({"level": name, "error": str(ex)[:160]})
        self.log["active_view"] = name_of(self.doc.ActiveView) if stayed else None
        self.log["levels_deleted"] = deleted
        self.log["levels_not_deleted"] = stayed

    def check_walls(self):
        """Walls that are not where the DWF put them, or did not come from it, go in the log with their ends."""
        def ends(wall):
            curve = wall.Location.Curve
            return [[round(p.X * FT, 2), round(p.Y * FT, 2)] for p in (curve.GetEndPoint(0), curve.GetEndPoint(1))]

        moved, foreign = [], []
        records = {r["id"]: r for r in self.b["elements"] if r["class"] == "wall" and r["geometry"].get("p0")}
        for wall in FilteredElementCollector(self.doc).OfCategory(BuiltInCategory.OST_Walls).WhereElementIsNotElementType():
            comment = wall.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS).AsString() or ""
            if not comment.startswith(STAMP):
                foreign.append({"element_id": wall.Id.ToString(), "name": name_of(wall), "ends_m": ends(wall)})
                continue
            record = records.get(comment[len(STAMP):].split()[0] if comment[len(STAMP):] else None)
            if record is not None and wall_moved((record["geometry"]["p0"], record["geometry"]["p1"]), ends(wall)):
                moved.append({"id": record["id"], "element_id": wall.Id.ToString(), "ends_m": ends(wall), "expected_m": [record["geometry"]["p0"], record["geometry"]["p1"]]})
        self.log["walls_moved_by_revit"] = moved
        self.log["walls_not_from_the_dwf"] = foreign

    def _level_for(self, rec):
        level = self.levels.get(rec.get("level"))
        if level is not None:
            return level
        z = rec.get("z0", 0.0)
        below = [l for l in self.levels.values() if l.Elevation * FT <= z + 0.05]
        chosen = max(below, key=lambda l: l.Elevation) if below else min(self.levels.values(), key=lambda l: l.Elevation)
        return chosen

    # ---- types
    def _set_thickness(self, host_type, thickness):
        """A type is mostly a size: change the thickest layer so the total is what is needed."""
        cs = host_type.GetCompoundStructure()
        if cs is None:
            raise ValueError("type has no layers")
        layers = list(cs.GetLayers())
        widest = max(range(len(layers)), key=lambda i: layers[i].Width)
        others = sum(l.Width for i, l in enumerate(layers) if i != widest)
        target = ft(thickness)
        if target - others > ft(0.001):
            cs.SetLayerWidth(widest, target - others)
        else:
            cs = CompoundStructure.CreateSingleLayerCompoundStructure(MaterialFunctionAssignment.Structure, target, layers[widest].MaterialId)
        host_type.SetCompoundStructure(cs)

    def _thickness_of(self, host_type):
        cs = host_type.GetCompoundStructure()
        return cs.GetWidth() * FT if cs is not None else None

    def _host_type(self, cls, pool, source_type, thickness, mapping_key, base_key):
        """Reuse a type of that name and thickness, else duplicate a similar one and resize it."""
        source = clean_name(source_type) or "DWF {:.0f}".format(thickness * 100)
        name = target_type_name(source, self.map.get(mapping_key, {}), self.map.get(cls + "_name_rules", []))
        section = self._section(cls)
        wanted = name
        current = pool.get(wanted)
        if current is not None:
            have = self._thickness_of(current)
            if have is not None and abs(have - thickness) <= 0.002:
                return current
            wanted = "{} ({:.0f})".format(name, thickness * 100)
            if wanted in pool:
                return pool[wanted]
        names = sorted(pool)
        base_name = base_by_word(names, source, self.map.get(base_key, {})) or pick_base(names, name)
        if base_name is None:
            raise ValueError("the file has no type to duplicate")
        new_type = pool[base_name].Duplicate(wanted)
        self._set_thickness(new_type, thickness)
        pool[wanted] = new_type
        section["substitutions"][wanted] = "duplicated from " + base_name
        return new_type

    # ---- elements
    def make_wall(self, rec):
        g = rec["geometry"]
        if g["kind"] != "axis":
            if g.get("path"):
                return self._make_wall_chain(rec)
            raise ValueError("wall has no axis or centre path")
        wall_type = self._host_type("wall", self.wall_types, rec.get("source_type"), g["width"], "wall_types", "wall_base_by_word")
        level = self._level_for(rec)
        z = level.Elevation
        line = Line.CreateBound(XYZ(ft(g["p0"][0]), ft(g["p0"][1]), z), XYZ(ft(g["p1"][0]), ft(g["p1"][1]), z))
        wall = Wall.Create(self.doc, line, wall_type.Id, level.Id, ft(rec["z1"] - rec["z0"]), ft(rec["z0"]) - z, False, False)
        top = self.levels.get(rec.get("top_level"))
        if top is not None and rec.get("top_offset") is not None and abs(top.Elevation + ft(rec["top_offset"]) - ft(rec["z1"])) < ft(0.01):
            wall.get_Parameter(BuiltInParameter.WALL_HEIGHT_TYPE).Set(top.Id)
            wall.get_Parameter(BuiltInParameter.WALL_TOP_OFFSET).Set(ft(rec["top_offset"]))
        return wall

    def _make_wall_chain(self, rec):
        """A curved wall becomes short straight walls of the right type, joined end to end."""
        g = rec["geometry"]
        wall_type = self._host_type("wall", self.wall_types, rec.get("source_type"), g["width"], "wall_types", "wall_base_by_word")
        level = self._level_for(rec)
        z = level.Elevation
        pts = clean_path(g["path"], 0.02)
        first = None
        for a, b in zip(pts, pts[1:]):
            line = Line.CreateBound(XYZ(ft(a[0]), ft(a[1]), z), XYZ(ft(b[0]), ft(b[1]), z))
            wall = Wall.Create(self.doc, line, wall_type.Id, level.Id, ft(rec["z1"] - rec["z0"]), ft(rec["z0"]) - z, False, False)
            if first is None:
                first = wall
            else:
                self._stamp(wall, rec)
        if first is None:
            raise ValueError("no usable path")
        return first

    def _loops(self, poly, z):
        loops = List[CurveLoop]()
        for ring in [poly["outer"]] + list(poly.get("holes", [])):
            pts = clean_ring(ring)
            if len(pts) < 3:
                continue
            loop = CurveLoop()
            for i, a in enumerate(pts):
                b = pts[(i + 1) % len(pts)]
                loop.Append(Line.CreateBound(XYZ(ft(a[0]), ft(a[1]), z), XYZ(ft(b[0]), ft(b[1]), z)))
            loops.Add(loop)
        return loops

    def make_floor(self, rec):
        polys = rec["geometry"]["polygons"]
        if not polys:
            raise ValueError("no outline")
        floor_type = self._host_type("floor", self.floor_types, rec.get("source_type"), rec["z1"] - rec["z0"], "floor_types", "floor_base_by_word")
        level = self._level_for(rec)
        first = None
        problem = None
        for poly in polys:
            floor = None
            for attempt in (poly, {"outer": poly["outer"]}):
                try:
                    floor = Floor.Create(self.doc, self._loops(attempt, level.Elevation), floor_type.Id, level.Id)
                    break
                except Exception as ex:
                    problem = ex
            if floor is None:
                continue
            floor.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM).Set(ft(rec["z1"]) - level.Elevation)
            if first is None:
                first = floor
            else:
                self._stamp(floor, rec)
        if first is None:
            raise problem or ValueError("no outline")
        return first

    def _set_first(self, element, values):
        """Set the first writable parameter found, by built-in id or by name."""
        for key in values["keys"]:
            p = element.LookupParameter(key) if isinstance(key, str) else element.get_Parameter(key)
            if p is not None and not p.IsReadOnly:
                p.Set(ft(values["value"]))
                return True
        return False

    def prepare_opening_types(self, records):
        """Create and size every door and window type, and commit, before placing any of them.

        An instance placed in the transaction that created its type does not cut the wall,
        which left 86 of 138 such doors and windows uncut in the first test.
        """
        for rec in records:
            try:
                self._opening_symbol(rec, create=True)
            except Exception as ex:
                self._section(rec["class"])["failed"].append({"id": rec["id"], "type": rec.get("type"), "error": "type: {}".format(ex)})

    def _opening_symbol(self, rec, create=False):
        cls = rec["class"]
        every = {}
        for pool in self.symbols.values():
            every.update(pool)
        family, exact, how = resolve_opening(self.symbols[cls], rec.get("source_family"), rec.get("source_type"), cls, self.map, every)
        if family is None:
            raise ValueError("the file has no {} family".format(cls))
        section = self._section(cls)
        if how != "same_family":
            section["substitutions"][rec.get("source_family") or "?"] = "{} ({})".format(family, how)
        _, type_format = mapped_family(self.map, rec.get("source_family"))
        types = every[family]
        name = sized_type_name(rec.get("source_type"), rec["width"], rec["height"], type_format) if type_format else (exact or sized_type_name(rec.get("source_type"), rec["width"], rec["height"]))
        symbol = types.get(name)
        if symbol is None:
            if not create:
                raise ValueError("type {} of {} was not prepared".format(name, family))
            symbol = next(iter(types.values())).Duplicate(name)
            types[name] = symbol
            sized = self._set_first(symbol, self._size_keys("WIDTH", "Width", "רוחב", rec["width"]))
            sized = self._set_first(symbol, self._size_keys("HEIGHT", "Height", "גובה", rec["height"])) and sized
            if not sized:
                section["substitutions"][name + " in " + family] = "size could not be set"
        if create and not symbol.IsActive:
            symbol.Activate()
        return symbol

    @staticmethod
    def _size_keys(suffix, english_name, local_name, value):
        builtins = [getattr(BuiltInParameter, prefix + suffix) for prefix in ("DOOR_", "WINDOW_")]
        return {"keys": builtins + [getattr(BuiltInParameter, "FAMILY_" + suffix + "_PARAM"), english_name, local_name], "value": value}

    def make_opening(self, rec):
        host_id = self.created.get(rec.get("host"))
        host = self.doc.GetElement(host_id) if host_id is not None else None
        if host is None:
            raise ValueError("host wall {} was not created".format(rec.get("host")))
        if not isinstance(host, Wall):
            raise ValueError("host {} is not a straight wall".format(rec.get("host")))
        symbol = self._opening_symbol(rec)
        level = self._level_for(rec)
        point = XYZ(ft(rec["center"][0]), ft(rec["center"][1]), ft(rec["z0"]))
        instance = self.doc.Create.NewFamilyInstance(point, symbol, host, level, StructuralType.NonStructural)
        sill = instance.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        if sill is not None and not sill.IsReadOnly:
            sill.Set(ft(rec["z0"]) - level.Elevation)
        return instance

    @staticmethod
    def _wall_is_cut(wall, rec):
        """Does the host wall's volume show the opening? None when Revit reports no volume."""
        p = wall.get_Parameter(BuiltInParameter.HOST_VOLUME_COMPUTED)
        if p is None or not p.AsDouble():
            return None
        thickness = wall.WallType.Width
        length = wall.get_Parameter(BuiltInParameter.CURVE_ELEM_LENGTH).AsDouble()
        height = wall.get_Parameter(BuiltInParameter.WALL_USER_HEIGHT_PARAM).AsDouble()
        full = thickness * length * height
        opening = ft(rec["width"]) * ft(rec["height"]) * thickness
        return full - p.AsDouble() >= opening * 0.5

    def repair_cuts(self, records):
        """Place again any door or window whose wall still shows no opening. Report the rest."""
        by_host = {}
        for rec in records:
            if rec["id"] in self.created:
                by_host.setdefault(rec.get("host"), []).append(rec)
        suspects = []
        for host_id, group in by_host.items():
            wall_id = self.created.get(host_id)
            wall = self.doc.GetElement(wall_id) if wall_id is not None else None
            if wall is None or not hasattr(wall, "WallType"):
                continue
            if len(group) == 1 and self._wall_is_cut(wall, group[0]) is False:
                suspects.append(group[0])

        def place_again():
            for rec in suspects:
                self.doc.Delete(self.created[rec["id"]])
                instance = self.make_opening(rec)
                self._stamp(instance, rec)
                self.created[rec["id"]] = instance.Id

        if suspects:
            self._transact("DWF repair openings", place_again)
        still = []
        for rec in suspects:
            wall = self.doc.GetElement(self.created[rec["host"]])
            if self._wall_is_cut(wall, rec) is False:
                still.append({"id": rec["id"], "type": rec.get("type"), "level": rec.get("level")})
        self.log["openings_checked"] = sum(len(g) for g in by_host.values())
        self.log["openings_replaced"] = len(suspects)
        self.log["openings_not_cut"] = still

    def _railing_type(self, rec):
        name = clean_name(rec.get("source_type"))
        target = self.map.get("railing_types", {}).get(name)
        if target and target in self.railing_types:
            return self.railing_types[target]
        if name in self.railing_types:
            return self.railing_types[name]
        default = self.map.get("default_railing_type")
        if default in self.railing_types:
            return self.railing_types[default]
        if not self.railing_types:
            raise ValueError("the file has no railing type")
        first = sorted(self.railing_types)[0]
        self._section("railing")["substitutions"][name or "?"] = first
        return self.railing_types[first]

    def make_railing(self, rec):
        railing_type = self._railing_type(rec)
        level = self._level_for(rec)
        first = None
        for path in rec["geometry"]["path"]:
            pts = clean_path(path, 0.03)
            if len(pts) < 2:
                continue
            try:
                pieces = [self._railing_on(pts, railing_type, level)]
            except Exception:
                # Revit refuses a path that folds back on itself: one railing per straight piece
                pieces = [self._railing_on([a, b], railing_type, level) for a, b in zip(pts, pts[1:])]
            for railing in pieces:
                self._set_first(railing, {"keys": [BuiltInParameter.STAIRS_RAILING_HEIGHT_OFFSET, "Base Offset"], "value": rec["z0"] - level.Elevation * FT})
                if first is None:
                    first = railing
                else:
                    self._stamp(railing, rec)
        if first is None:
            raise ValueError("no usable path")
        return first

    def _railing_on(self, pts, railing_type, level):
        loop = CurveLoop()
        for a, b in zip(pts, pts[1:]):
            loop.Append(Line.CreateBound(XYZ(ft(a[0]), ft(a[1]), level.Elevation), XYZ(ft(b[0]), ft(b[1]), level.Elevation)))
        return Railing.Create(self.doc, loop, railing_type.Id, level.Id)

    def make_mass_floor(self, rec):
        """Any other mass ends up as a floor: its outline, its height as the thickness."""
        polys = rec["geometry"].get("polygons") or []
        height = rec["z1"] - rec["z0"]
        if not polys:
            raise ValueError("no outline")
        if height <= 0.0:
            raise ValueError("no height")
        level = self._level_for(rec)
        name = "DWF floor {:.0f}".format(max(1, height * 100))
        floor_type = self._host_type("floor", self.floor_types, name, height, "floor_types", "floor_base_by_word")
        first = None
        for poly in polys:
            floor = Floor.Create(self.doc, self._loops({"outer": poly["outer"]}, level.Elevation), floor_type.Id, level.Id)
            floor.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM).Set(ft(rec["z1"]) - level.Elevation)
            if first is None:
                first = floor
            else:
                self._stamp(floor, rec)
        return first

    # ---- stairs: Revit builds these in their own edit scope, outside a normal transaction
    def build_stairs(self, records):
        section = self._section("stair")
        checker = _make_stairs_preprocessor()
        for rec in records:
            if rec["id"] in self.created:
                section["skipped"] += 1
                continue
            try:
                element_id = self._make_stair(rec, checker)
                self.created[rec["id"]] = element_id
                section["created"] += 1
                if not rec["geometry"].get("direction_checked"):
                    self.log.setdefault("stairs_direction_unverified", []).append(rec["id"])
            except Exception as ex:
                section["failed"].append({"id": rec["id"], "type": rec.get("type"), "error": str(ex)})

    def _top_level_for(self, rec, base):
        top = self.levels.get(rec.get("top_level"))
        if top is not None and top.Elevation > base.Elevation:
            return top, 0.0
        # a few steps within one level: the top is measured down from the next level
        above = sorted((l for l in self.levels.values() if l.Elevation > base.Elevation), key=lambda l: l.Elevation)
        if not above:
            raise ValueError("no level above the stair")
        return above[0], ft(rec["z1"]) - above[0].Elevation

    def _make_stair(self, rec, checker):
        g = rec["geometry"]
        flights = g.get("flights") or []
        if not flights:
            raise ValueError("no flights could be read from the plans")
        if checker is None:
            raise ValueError("Revit's stair scope needs a failure handler, and it could not be created here")
        base = self._level_for(rec)
        top, top_offset = self._top_level_for(rec, base)
        scope = StairsEditScope(self.doc, "DWF stair")
        stairs_id = scope.Start(base.Id, top.Id)
        t = Transaction(self.doc, "DWF stair runs")
        t.Start()
        try:
            stairs = self.doc.GetElement(stairs_id)
            self._set_first(stairs, {"keys": [BuiltInParameter.STAIRS_BASE_OFFSET], "value": rec.get("base_offset") or 0.0})
            if top_offset:
                self._set_first(stairs, {"keys": [BuiltInParameter.STAIRS_TOP_OFFSET], "value": top_offset * FT})
            stairs.DesiredRisersNumber = int(g["risers"])
            runs = []
            bottom = base.Elevation + ft(rec.get("base_offset") or 0.0)
            riser = ((top.Elevation + top_offset) - bottom) / int(g["risers"])
            climbed = 0
            for flight in flights:
                a, b = flight["start"], flight["end"]
                # run paths are at absolute height: the first test refused a path at 0 above level 01
                z = bottom + climbed * riser
                run = StairsRun.CreateStraightRun(self.doc, stairs_id, Line.CreateBound(XYZ(ft(a[0]), ft(a[1]), z), XYZ(ft(b[0]), ft(b[1]), z)), StairsRunJustification.Center)
                run.ActualRunWidth = ft(flight["width"])
                climbed += int(flight["risers"])
                runs.append(run)
            for lower, upper in zip(runs, runs[1:]):
                if StairsLanding.CanCreateAutomaticLanding(self.doc, lower.Id, upper.Id):
                    StairsLanding.CreateAutomaticLanding(self.doc, lower.Id, upper.Id)
            comment = stairs.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if comment is not None and not comment.IsReadOnly:
                comment.Set(STAMP + rec["id"])
            t.Commit()
        except Exception:
            if t.HasStarted() and not t.HasEnded():
                t.RollBack()
            scope.Cancel()
            raise
        scope.Commit(checker)
        return stairs_id

    # ---- clearing a copy of an existing project, on request only
    def clear_model(self):
        """Delete all model elements, earlier runs of this script included. Types, levels and views stay."""
        categories = [getattr(BuiltInCategory, name) for name in (
            "OST_Doors", "OST_Windows", "OST_StairsRailing", "OST_Stairs", "OST_Floors", "OST_Roofs", "OST_Ceilings", "OST_Columns",
            "OST_StructuralColumns", "OST_StructuralFraming", "OST_Furniture", "OST_FurnitureSystems", "OST_Casework", "OST_PlumbingFixtures",
            "OST_GenericModel", "OST_CurtainWallMullions", "OST_CurtainWallPanels", "OST_Rooms", "OST_Walls")]

        def work():
            count = 0
            for cat in categories:
                ids = [e.Id for e in FilteredElementCollector(self.doc).OfCategory(cat).WhereElementIsNotElementType()]
                for element_id in ids:
                    if self.doc.GetElement(element_id) is None:
                        continue
                    try:
                        self.doc.Delete(element_id)
                        count += 1
                    except Exception:
                        pass
            self.log["model_elements_deleted"] = count

        self._transact("DWF clear model", work)

    # ---- plot and building lines
    def _line_style(self, name, rgb):
        lines = self.doc.Settings.Categories.get_Item(BuiltInCategory.OST_Lines)
        sub = next((c for c in lines.SubCategories if c.Name == name), None)
        if sub is None:
            sub = self.doc.Settings.Categories.NewSubcategory(lines, name)
        sub.LineColor = Color(rgb[0], rgb[1], rgb[2])
        return sub

    def make_site_lines(self):
        """2D lines of a named style, to be picked with Revit's Property Line tool.

        Revit's API has no way to create a Property Line.
        """
        site = self.b.get("site") or {}
        plot = site.get("plot")
        if not plot:
            return
        names = self.map.get("site_line_names", {"plot": "Plot Line", "building": "Building Line"})
        z = self._ground_elevation()
        plane = SketchPlane.Create(self.doc, Plane.CreateByNormalAndOrigin(XYZ.BasisZ, XYZ(0.0, 0.0, z)))
        styles = {"plot": self._line_style(names["plot"], (0, 60, 255)), "building": self._line_style(names["building"], (255, 0, 0))}
        ids = {styles[k].Id.ToString() for k in styles}
        old = [c.Id for c in FilteredElementCollector(self.doc).OfClass(CurveElement)
               if c.LineStyle is not None and c.LineStyle.GraphicsStyleCategory.Id.ToString() in ids]
        for curve_id in old:
            self.doc.Delete(curve_id)
        count = {"plot": 0, "building": 0}

        def draw(points, closed, key):
            style = styles[key].GetGraphicsStyle(GraphicsStyleType.Projection)
            pts = clean_path([tuple(q) for q in points] + ([tuple(points[0])] if closed else []), 0.01)
            for a, b in zip(pts, pts[1:]):
                curve = self.doc.Create.NewModelCurve(Line.CreateBound(XYZ(ft(a[0]), ft(a[1]), z), XYZ(ft(b[0]), ft(b[1]), z)), plane)
                curve.LineStyle = style
                count[key] += 1

        draw(plot["ring"], True, "plot")
        for line in site.get("building_lines", []):
            draw(line["points"], line["closed"], "building")
        self.log["site_lines"] = count

    def _ground_elevation(self):
        below = [l for lid, l in self.levels.items() if abs(self.b["levels"][lid]["z"]) < 0.01]
        return below[0].Elevation if below else 0.0

    # ---- placing the project in the world, last of all
    def place_project(self):
        """Survey coordinates and true north from the fit to the survey; the base point goes to a plot corner."""
        place = (self.b.get("site") or {}).get("placement")
        if not place:
            self.log["placement"] = "not placed: no survey fit"
            return
        theta = math.radians(place["rotation_deg"])
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        elevation = (self.b.get("site") or {}).get("datum_elevation") or 0.0

        def world(q):
            return (cos_t * q[0] - sin_t * q[1] + place["easting"], sin_t * q[0] + cos_t * q[1] + place["northing"])

        test = [world(q) for q in self.b["site"]["plot"]["ring"]]
        model = self.b["site"]["plot"]["ring"]
        location = self.doc.ActiveProjectLocation
        result = {"rotation_deg": place["rotation_deg"], "easting": place["easting"], "northing": place["northing"], "elevation": elevation}

        def error():
            t = self.doc.ActiveProjectLocation.GetTotalTransform()
            best = None
            for candidate in (t, t.Inverse):
                worst = 0.0
                for m, w in zip(model, test):
                    p = candidate.OfPoint(XYZ(ft(m[0]), ft(m[1]), 0.0))
                    worst = max(worst, math.hypot(p.X * FT - w[0], p.Y * FT - w[1]))
                best = worst if best is None else min(best, worst)
            return best

        # Revit's sign for the angle is checked against the result, not assumed
        for angle in (-theta, theta):
            self._transact("DWF placement", lambda: location.SetProjectPosition(XYZ.Zero, ProjectPosition(ft(place["easting"]), ft(place["northing"]), ft(elevation), angle)))
            result["worst_corner_m"] = round(error(), 3)
            if result["worst_corner_m"] < 0.05:
                result["angle_used_deg"] = round(math.degrees(angle), 5)
                break

        # the first test showed that setting SharedPosition leaves the point where it is, so it is moved
        base = BasePoint.GetProjectBasePoint(self.doc)
        corner = world(place["base_point_model"])
        target = XYZ(ft(place["base_point_model"][0]), ft(place["base_point_model"][1]), base.Position.Z)
        try:
            if base.Position.DistanceTo(target) > 0.003:
                self._transact("DWF base point", lambda: ElementTransformUtils.MoveElement(self.doc, base.Id, target.Subtract(base.Position)))
            if error() > 0.05:
                # moving the point must not move the project in the world: restore if it did
                result["base_point_moved_the_project"] = True
                self._transact("DWF placement", lambda: location.SetProjectPosition(XYZ.Zero, ProjectPosition(ft(place["easting"]), ft(place["northing"]), ft(elevation), math.radians(result.get("angle_used_deg", 0.0)))))
            result["worst_corner_after_base_point_m"] = round(error(), 3)
            shared = base.SharedPosition
            result["base_point_shared"] = [round(shared.X * FT, 3), round(shared.Y * FT, 3), round(shared.Z * FT, 3)]
            result["base_point_expected"] = [round(corner[0], 3), round(corner[1], 3), elevation]
            position = base.Position
            result["base_point_internal"] = [round(position.X * FT, 3), round(position.Y * FT, 3), round(position.Z * FT, 3)]
        except Exception as ex:
            result["base_point_error"] = str(ex)
        self.log["placement"] = result

    # ---- order of work
    def run(self, may_delete_levels=False):
        self.created = self.existing_stamps()
        self._transact("DWF levels", self.make_levels)
        if may_delete_levels:
            self.delete_other_levels()
        elements = self.b["elements"]
        by_class = {}
        for rec in elements:
            by_class.setdefault(rec["class"], []).append(rec)
        self.process("wall", by_class.pop("wall", []), self.make_wall)
        self.process("floor", by_class.pop("floor", []), self.make_floor)
        doors = [o for o in self.b["openings"] if o["class"] == "door"]
        windows = [o for o in self.b["openings"] if o["class"] == "window"]
        self._transact("DWF opening types", lambda: self.prepare_opening_types(doors + windows))
        self.process("door", doors, self.make_opening)
        self.process("window", windows, self.make_opening)
        self.repair_cuts(doors + windows)
        self.process("railing", by_class.pop("railing", []), self.make_railing)
        self.build_stairs(by_class.pop("stair", []))
        for cls, records in sorted(by_class.items()):
            self.process(cls, records, self.make_mass_floor)
        lost = [i for i, element_id in self.created.items() if self.doc.GetElement(element_id) is None]
        self.log["elements_removed_by_revit"] = lost
        try:
            self.check_walls()
        except Exception as ex:
            self.log["errors"].append("wall check: {}".format(ex))
        try:
            self._transact("DWF site lines", self.make_site_lines)
        except Exception as ex:
            self.log["errors"].append("site lines: {}".format(ex))
        try:
            self.place_project()
        except Exception as ex:
            self.log["errors"].append("placement: {}".format(ex))


def main(root, building_name=None, allow_existing=False, clear_existing=False):
    doc = DocumentManager.Instance.CurrentDBDocument
    path = find_building(root, building_name)
    with open(path, encoding="utf8") as fh:
        building = json.load(fh)
    mapping_path = os.path.join(root, "config", "revit_mapping.json")
    mapping = {}
    if os.path.exists(mapping_path):
        with open(mapping_path, encoding="utf8") as fh:
            mapping = json.load(fh)
    log = {"document": doc.Title, "building": path, "levels_created": [], "classes": {}, "errors": []}
    TransactionManager.Instance.ForceCloseTransaction()
    builder = Builder(doc, building, mapping, log)
    foreign = sum(
        1 for e in FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Walls).WhereElementIsNotElementType()
        if not (e.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS).AsString() or "").startswith(STAMP)
    )
    if foreign and not (allow_existing or clear_existing):
        return ["STOPPED: this file already has {} walls that this script did not create.".format(foreign),
                "Save a copy of the project first, then set CLEAR_EXISTING = True in the Dynamo node to delete its model and build.",
                "To build next to the existing model instead, set ALLOW_EXISTING = True."]
    handler = FailureHandler(log)
    app = doc.Application
    app.FailuresProcessing += handler
    try:
        if clear_existing:
            builder.clear_model()
        present = len(builder.existing_stamps())
        log["already_present_at_start"] = present
        # a level goes only when nothing that should stay can stand on it
        builder.run(may_delete_levels=clear_existing or (not foreign and not present))
    except Exception:
        log["errors"].append(traceback.format_exc())
    finally:
        app.FailuresProcessing -= handler
    log["path"] = os.path.join(os.path.dirname(path), "revit_build_log.json")
    with open(log["path"], "w", encoding="utf8") as fh:
        json.dump(log, fh, ensure_ascii=False, indent=1)
    if log["errors"] and not builder.created:
        # nothing was built: this must show on the node, not only in a file
        raise RuntimeError("The build failed. " + log["errors"][0])
    return summarize(log) + log["errors"][:3]


if IN_REVIT and "ROOT" in globals():
    OUT = main(ROOT, globals().get("BUILDING"), globals().get("ALLOW_EXISTING", False), globals().get("CLEAR_EXISTING", False))  # noqa: F821
    # the area plans follow unless the build stopped or SKIP_AREAS is set
    if not globals().get("SKIP_AREAS") and not str(OUT[0]).startswith("STOPPED"):
        BUILD_RESULT = OUT
        with open(os.path.join(ROOT, "revit", "build_areas.py"), encoding="utf8") as _fh:  # noqa: F821
            exec(_fh.read(), globals())
        OUT = {"build": BUILD_RESULT, "areas": OUT}
