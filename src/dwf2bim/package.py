"""Stage 1 - open a DWF package and return sheets, views and Revit element records."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import whip


class UnsupportedInput(Exception):
    """The input cannot be processed by the current route; the message says why."""


@dataclass
class Element:
    id: str  # Revit element id
    family: str
    props: dict
    nodes: list = field(default_factory=list)


@dataclass
class Sheet:
    title: str
    whip: whip.WhipData
    elements: dict  # element id -> Element
    node_to_element: dict  # W2D node index -> element id


_OBJECT_ID = re.compile(r"^(.*?)\s*\[(\d+)\]$")


def _parse_objects(data: bytes) -> tuple[dict, dict]:
    root = ET.fromstring(data)
    elements: dict[str, Element] = {}
    node_to_element: dict[int, str] = {}
    instances = root.find("Instances")
    if instances is None:
        return elements, node_to_element
    for inst in instances:
        m = _OBJECT_ID.match(inst.get("object") or "")
        if not m:
            continue
        family, element_id = m.group(1), m.group(2)
        holder = inst.find("Properties")
        props = {p.get("name"): p.get("value") for p in holder} if holder is not None else {}
        nodes = [int(n) for n in (inst.get("nodes") or "").split()]
        el = elements.setdefault(element_id, Element(element_id, family, props))
        el.nodes += nodes
        el.props.update(props)
        for n in nodes:
            node_to_element[n] = element_id
    return elements, node_to_element


def load(path: str | Path) -> list[Sheet]:
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(16)
    sheets: list[Sheet] = []
    if path.suffix.lower() == ".dwfx" or head[:2] == b"PK":
        from . import dwfx

        try:
            pages = dwfx.read(path)
        except zipfile.BadZipFile as ex:
            raise UnsupportedInput(f"{path.name} is not a DWFX package") from ex
        for title, data, objects in pages:
            elements, node_to_element = _parse_objects(objects) if objects else ({}, {})
            sheets.append(Sheet(title, data, elements, node_to_element))
        return _checked(path, sheets)
    if not head.startswith(b"(DWF V"):
        raise UnsupportedInput(f"{path.name} is not a DWF package")

    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        manifest = z.read("manifest.xml").decode("utf8", "replace")
        titles = dict(re.findall(r'name="(com\.autodesk\.dwf\.ePlot_[^"]+)" title="([^"]*)"', manifest))
        for w2d in (n for n in names if n.lower().endswith(".w2d")):
            folder = w2d.replace("\\", "/").rsplit("/", 1)[0]
            data = whip.decode(z.read(w2d))
            elements: dict = {}
            node_to_element: dict = {}
            for name in names:
                norm = name.replace("\\", "/")
                if norm.startswith(folder + "/") and norm.lower().endswith(".xml"):
                    blob = z.read(name)
                    if b"<PageObjectDefinition" in blob[:200]:
                        elements, node_to_element = _parse_objects(blob)
            sheets.append(Sheet(titles.get(folder, folder), data, elements, node_to_element))
    return _checked(path, sheets)


def _checked(path: Path, sheets: list) -> list:
    if not sheets:
        raise UnsupportedInput(f"{path.name} contains no 2D sheets")
    if not any(s.elements for s in sheets):
        raise UnsupportedInput(
            "This DWF has no Revit object data (not exported from Revit, or object data was "
            "disabled on export). The current route depends on that data; a geometry-only "
            "route for AutoCAD-style DWF files is not implemented."
        )
    return sheets
