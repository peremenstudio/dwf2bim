"""Export what the open Revit file offers: levels, types and family parameters.

Read-only: changes nothing in the Revit file. Writes revit/catalog/<document>.json.
Run from a Dynamo Python Script node, see revit/README.md.
"""
import json
import os
import re

import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitServices")
from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    FamilyInstance,
    FamilySymbol,
    FilteredElementCollector,
    FloorType,
    Level,
    StorageType,
    WallKind,
    WallType,
)
from Autodesk.Revit.DB.Architecture import RailingType
from RevitServices.Persistence import DocumentManager

FT = 0.3048


def name_of(e):
    try:
        return e.Name
    except Exception:
        p = e.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)
        return p.AsString() if p else ""


def param_rows(element):
    rows = []
    for p in element.Parameters:
        try:
            value = p.AsValueString() if p.StorageType != StorageType.String else p.AsString()
            rows.append({"name": p.Definition.Name, "value": value, "read_only": p.IsReadOnly, "storage": str(p.StorageType)})
        except Exception:
            pass
    return sorted(rows, key=lambda r: r["name"])


def layers_of(doc, host_type):
    cs = host_type.GetCompoundStructure()
    if cs is None:
        return []
    out = []
    for layer in cs.GetLayers():
        material = doc.GetElement(layer.MaterialId)
        out.append({"function": str(layer.Function), "width_m": round(layer.Width * FT, 4), "material": name_of(material) if material else None})
    return out


def families_in(doc, category):
    families = {}
    for s in FilteredElementCollector(doc).OfClass(FamilySymbol).OfCategory(category):
        fam = families.setdefault(s.Family.Name, {"family": s.Family.Name, "types": [], "instance_parameters": None, "placed": 0})
        fam["types"].append({"type": name_of(s), "parameters": param_rows(s)})
    for inst in FilteredElementCollector(doc).OfClass(FamilyInstance).OfCategory(category):
        fam = families.get(inst.Symbol.Family.Name)
        if fam is not None:
            fam["placed"] += 1
            if fam["instance_parameters"] is None:
                fam["instance_parameters"] = [{"name": r["name"], "read_only": r["read_only"], "storage": r["storage"]} for r in param_rows(inst)]
    return sorted(families.values(), key=lambda f: f["family"])


def count(doc, category):
    return FilteredElementCollector(doc).OfCategory(category).WhereElementIsNotElementType().GetElementCount()


def export(doc, root):
    data = {
        "document": doc.Title,
        "elements_in_file": {
            "walls": count(doc, BuiltInCategory.OST_Walls),
            "floors": count(doc, BuiltInCategory.OST_Floors),
            "doors": count(doc, BuiltInCategory.OST_Doors),
            "windows": count(doc, BuiltInCategory.OST_Windows),
            "railings": count(doc, BuiltInCategory.OST_StairsRailing),
        },
        "levels": sorted(
            ({"name": l.Name, "elevation_m": round(l.Elevation * FT, 4)} for l in FilteredElementCollector(doc).OfClass(Level)),
            key=lambda r: r["elevation_m"],
        ),
        "wall_types": [
            {"name": name_of(t), "kind": str(t.Kind), "width_m": round(t.Width * FT, 4), "layers": layers_of(doc, t) if t.Kind == WallKind.Basic else []}
            for t in FilteredElementCollector(doc).OfClass(WallType)
        ],
        "floor_types": [{"name": name_of(t), "layers": layers_of(doc, t)} for t in FilteredElementCollector(doc).OfClass(FloorType)],
        "railing_types": [{"name": name_of(t)} for t in FilteredElementCollector(doc).OfClass(RailingType)],
        "door_families": families_in(doc, BuiltInCategory.OST_Doors),
        "window_families": families_in(doc, BuiltInCategory.OST_Windows),
    }
    folder = os.path.join(root, "revit", "catalog")
    if not os.path.isdir(folder):
        os.makedirs(folder)
    path = os.path.join(folder, re.sub(r'[\\/:*?"<>|]', "_", doc.Title) + ".json")
    with open(path, "w", encoding="utf8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    return "Catalog written: {} | wall types {} | floor types {} | door families {} | window families {} | railing types {}".format(
        path, len(data["wall_types"]), len(data["floor_types"]), len(data["door_families"]), len(data["window_families"]), len(data["railing_types"])
    )


OUT = export(DocumentManager.Instance.CurrentDBDocument, ROOT)  # noqa: F821 - ROOT is set by the Dynamo node
