"""Stage 5 - write building.json to an IFC4 file."""
from __future__ import annotations

import math

import ifcopenshell
import ifcopenshell.api
import ifcopenshell.guid

_ENTITY = {
    "wall": "IfcWall",
    "floor": "IfcSlab",
    "stair": "IfcStair",
    "railing": "IfcRailing",
    "mullion": "IfcMember",
    "curtain_panel": "IfcPlate",
    "generic": "IfcBuildingElementProxy",
    "door": "IfcDoor",
    "window": "IfcWindow",
}
_TYPE_ENTITY = {"wall": "IfcWallType", "floor": "IfcSlabType", "door": "IfcDoorType", "window": "IfcWindowType"}


def _valid_guid(g):
    return bool(g) and len(g) == 22


class IfcWriter:
    def __init__(self, building, project_name):
        self.b = building
        self.f = ifcopenshell.api.run("project.create_file", version="IFC4")
        f = self.f
        self.project = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcProject", name=project_name)
        ifcopenshell.api.run("unit.assign_unit", f, length={"is_metric": True, "raw": "METERS"})
        ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model")
        self.body = ifcopenshell.api.run("context.add_context", f, context_type="Model", context_identifier="Body", target_view="MODEL_VIEW", parent=ctx)
        self.axis_ctx = ifcopenshell.api.run("context.add_context", f, context_type="Model", context_identifier="Axis", target_view="GRAPH_VIEW", parent=ctx)
        self.site = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcSite", name="Site")
        self.building = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBuilding", name="Building")
        ifcopenshell.api.run("aggregate.assign_object", f, products=[self.site], relating_object=self.project)
        ifcopenshell.api.run("aggregate.assign_object", f, products=[self.building], relating_object=self.site)
        if building.get("datum_elevation") is not None:
            self.site.RefElevation = building["datum_elevation"]
        self.world = f.createIfcLocalPlacement(None, self._ax3((0.0, 0.0, 0.0)))
        self.site.ObjectPlacement = self.world
        self.building.ObjectPlacement = self.world
        self.storeys = {}
        self.storey_contents = {}
        for lid, lv in building["levels"].items():
            s = ifcopenshell.api.run("root.create_entity", f, ifc_class="IfcBuildingStorey", name=lid)
            s.Elevation = lv["z"]
            s.ObjectPlacement = f.createIfcLocalPlacement(self.world, self._ax3((0.0, 0.0, float(lv["z"]))))
            ifcopenshell.api.run("aggregate.assign_object", f, products=[s], relating_object=self.building)
            self.storeys[lid] = s
            self.storey_contents[lid] = []
        self.lowest = min(building["levels"].items(), key=lambda kv: kv[1]["z"])[0]
        self.materials = {}
        self.types = {}

    # ---- geometry primitives --------------------------------------------------
    def _pt(self, p):
        return self.f.createIfcCartesianPoint([float(c) for c in p])

    def _dir(self, d):
        return self.f.createIfcDirection([float(c) for c in d])

    def _ax3(self, o, z=(0.0, 0.0, 1.0), x=(1.0, 0.0, 0.0)):
        return self.f.createIfcAxis2Placement3D(self._pt(o), self._dir(z), self._dir(x))

    def _ring(self, coords):
        pts = [self._pt(c) for c in coords]
        pts.append(pts[0])
        return self.f.createIfcPolyline(pts)

    def _extrude_poly(self, poly, z0, h):
        outer = self._ring(poly["outer"])
        holes = [self._ring(r) for r in poly.get("holes", [])]
        if holes:
            profile = self.f.createIfcArbitraryProfileDefWithVoids("AREA", None, outer, holes)
        else:
            profile = self.f.createIfcArbitraryClosedProfileDef("AREA", None, outer)
        return self.f.createIfcExtrudedAreaSolid(profile, self._ax3((0.0, 0.0, float(z0))), self._dir((0.0, 0.0, 1.0)), float(h))

    def _rep(self, ctx, ident, kind, items):
        return self.f.createIfcShapeRepresentation(ctx, ident, kind, items)

    def _shape(self, *reps):
        return self.f.createIfcProductDefinitionShape(None, None, list(reps))

    # ---- element construction -------------------------------------------------
    def _new(self, rec):
        entity = _ENTITY[rec["class"]]
        guid = rec["ifc_guid"] if _valid_guid(rec.get("ifc_guid")) else ifcopenshell.guid.new()
        name = f'{rec["type"] or rec["family"]}:{rec["id"]}'
        kwargs = {"GlobalId": guid, "Name": name, "ObjectPlacement": self.world}
        return self.f.create_entity(entity, **kwargs)

    def _assign(self, element, rec):
        lid = rec.get("level") if rec.get("level") in self.storeys else self._storey_for(rec.get("z0", 0.0))
        self.storey_contents[lid].append(element)
        self._pset(element, rec)
        self._type(element, rec)
        self._material(element, rec)

    def _storey_for(self, z):
        below = [(lv["z"], lid) for lid, lv in self.b["levels"].items() if lv["z"] <= z + 0.05]
        return max(below)[1] if below else self.lowest

    def _pset(self, element, rec):
        f = self.f
        def prop(name, value, cls):
            return f.createIfcPropertySingleValue(name, None, f.create_entity(cls, value), None)
        g = rec.get("geometry") or {}
        props = [
            prop("RevitElementId", str(rec["id"]), "IfcIdentifier"),
            prop("SourceFamily", rec.get("family") or "", "IfcLabel"),
            prop("SourceType", rec.get("type") or "", "IfcLabel"),
            prop("GeometrySource", g.get("source") or str(rec.get("source") or ""), "IfcLabel"),
            prop("HeightSource", rec.get("height_source") or "", "IfcLabel"),
            prop("ClonedFrom", str(rec.get("cloned_from") or ""), "IfcLabel"),
        ]
        pset = f.createIfcPropertySet(ifcopenshell.guid.new(), None, "Dwf2Bim", None, props)
        f.createIfcRelDefinesByProperties(ifcopenshell.guid.new(), None, None, None, [element], pset)

    def _type(self, element, rec):
        cls = _TYPE_ENTITY.get(rec["class"])
        name = rec.get("type")
        if not cls or not name:
            return
        key = (cls, name)
        if key not in self.types:
            extra = {"IfcDoorType": {"OperationType": "NOTDEFINED"}, "IfcWindowType": {"PartitioningType": "NOTDEFINED"}}.get(cls, {})
            type_obj = self.f.create_entity(cls, GlobalId=ifcopenshell.guid.new(), Name=name, PredefinedType="NOTDEFINED", **extra)
            self.types[key] = self.f.createIfcRelDefinesByType(ifcopenshell.guid.new(), None, None, None, [element], type_obj)
        else:
            rel = self.types[key]
            rel.RelatedObjects = list(rel.RelatedObjects) + [element]

    def _material(self, element, rec):
        name = rec.get("material")
        if not name:
            return
        if name not in self.materials:
            material = self.f.createIfcMaterial(name)
            self.materials[name] = self.f.createIfcRelAssociatesMaterial(ifcopenshell.guid.new(), None, None, None, [element], material)
        else:
            rel = self.materials[name]
            rel.RelatedObjects = list(rel.RelatedObjects) + [element]

    def wall(self, rec):
        g = rec["geometry"]
        element = self._new(rec)
        z0, h = rec["z0"], rec["z1"] - rec["z0"]
        if h <= 0:
            return None
        if g["kind"] == "axis":
            (x0, y0), (x1, y1) = g["p0"], g["p1"]
            length = math.dist(g["p0"], g["p1"])
            if length < 1e-3:
                return None
            ux, uy = (x1 - x0) / length, (y1 - y0) / length
            element.ObjectPlacement = self.f.createIfcLocalPlacement(self.world, self._ax3((x0, y0, z0), x=(ux, uy, 0.0)))
            axis = self.f.createIfcPolyline([self.f.createIfcCartesianPoint([0.0, 0.0]), self.f.createIfcCartesianPoint([length, 0.0])])
            profile = self.f.createIfcRectangleProfileDef(
                "AREA", None, self.f.createIfcAxis2Placement2D(self.f.createIfcCartesianPoint([length / 2, 0.0]), None), length, float(g["width"])
            )
            solid = self.f.createIfcExtrudedAreaSolid(profile, self._ax3((0.0, 0.0, 0.0)), self._dir((0.0, 0.0, 1.0)), float(h))
            element.Representation = self._shape(self._rep(self.axis_ctx, "Axis", "Curve2D", [axis]), self._rep(self.body, "Body", "SweptSolid", [solid]))
        else:
            items = [self._extrude_poly(p, z0, h) for p in g["polygons"]]
            if not items:
                return None
            element.Representation = self._shape(self._rep(self.body, "Body", "SweptSolid", items))
        self._assign(element, rec)
        return element

    def extruded(self, rec):
        g = rec["geometry"]
        h = rec["z1"] - rec["z0"]
        if h <= 0:
            return None
        if g["kind"] == "treads":
            items = []
            for t in g["treads"]:
                items.append(self._extrude_poly({"outer": t["polygon"]}, t["z_top"] - 0.3, 0.3))
        else:
            items = [self._extrude_poly(p, rec["z0"], h) for p in g["polygons"]]
        if not items:
            return None
        element = self._new(rec)
        element.Representation = self._shape(self._rep(self.body, "Body", "SweptSolid", items))
        if rec["class"] == "floor":
            element.PredefinedType = "FLOOR"
        self._assign(element, rec)
        return element

    def opening(self, rec, walls):
        f = self.f
        cx, cy = rec["center"]
        ux, uy = rec["direction"]
        width, height, depth = float(rec["width"]), float(rec["height"]), float(rec["depth"])
        placement = f.createIfcLocalPlacement(self.world, self._ax3((cx, cy, float(rec["z0"])), x=(ux, uy, 0.0)))
        host = walls.get(rec.get("host"))
        if host is not None:
            profile = f.createIfcRectangleProfileDef("AREA", None, f.createIfcAxis2Placement2D(f.createIfcCartesianPoint([0.0, 0.0]), None), width, depth + 0.1)
            solid = f.createIfcExtrudedAreaSolid(profile, self._ax3((0.0, 0.0, 0.0)), self._dir((0.0, 0.0, 1.0)), height)
            void = f.create_entity(
                "IfcOpeningElement", GlobalId=ifcopenshell.guid.new(), Name=f'Opening:{rec["id"]}', ObjectPlacement=placement,
                Representation=self._shape(self._rep(self.body, "Body", "SweptSolid", [solid])),
            )
            f.createIfcRelVoidsElement(ifcopenshell.guid.new(), None, None, None, host, void)
        else:
            void = None
        element = self._new(rec)
        element.ObjectPlacement = placement
        element.OverallHeight, element.OverallWidth = height, width
        panel_depth = min(depth, 0.1)
        profile = f.createIfcRectangleProfileDef("AREA", None, f.createIfcAxis2Placement2D(f.createIfcCartesianPoint([0.0, 0.0]), None), width, panel_depth)
        solid = f.createIfcExtrudedAreaSolid(profile, self._ax3((0.0, 0.0, 0.0)), self._dir((0.0, 0.0, 1.0)), height)
        element.Representation = self._shape(self._rep(self.body, "Body", "SweptSolid", [solid]))
        if void is not None:
            f.createIfcRelFillsElement(ifcopenshell.guid.new(), None, None, None, void, element)
        self._assign(element, rec)
        return element

    def write(self, path):
        walls = {}
        counts = {}
        for rec in self.b["elements"]:
            element = self.wall(rec) if rec["class"] == "wall" else self.extruded(rec)
            if element is not None:
                counts[rec["class"]] = counts.get(rec["class"], 0) + 1
                if rec["class"] == "wall":
                    walls[rec["id"]] = element
        for rec in self.b["openings"]:
            if self.opening(rec, walls) is not None:
                counts[rec["class"]] = counts.get(rec["class"], 0) + 1
        for lid, items in self.storey_contents.items():
            if items:
                self.f.createIfcRelContainedInSpatialStructure(ifcopenshell.guid.new(), None, None, None, items, self.storeys[lid])
        self.f.write(str(path))
        return counts


def export(building, path, project_name):
    return IfcWriter(building, project_name).write(path)
