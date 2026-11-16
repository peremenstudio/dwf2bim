"""A project's own corrections, from overrides.json in its folder.

These are project exceptions: they change this project only, and each one applied is listed in the
report. When the same correction is needed on a second project, a general rule is missing.

    {"elements": {"<id>": {"leave_out": "why"}},
     "storeys": {"<level>": {"main_floor": [[x, y], ...], "balconies": [[[x, y], ...]], "open": [[[x, y], ...]]}}}

`main_floor` replaces the storey's main floor outline, `balconies` its balcony outlines, and `open`
gives the openings in the main floor. Coordinates are the model's, in metres, as in the storey DXF.
"""
from __future__ import annotations

import json
from pathlib import Path

FILE = "overrides.json"


def load(folder) -> dict:
    path = Path(folder) / FILE if folder else None
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf8"))
    unknown = set(data) - {"elements", "storeys", "_comment"}
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    return data


def leave_out_elements(building, overrides) -> list:
    """Take out the elements the project's file names. Returns what was applied; an id not in the model is reported too."""
    wanted = {str(i): spec["leave_out"] for i, spec in (overrides.get("elements") or {}).items() if spec.get("leave_out")}
    applied = []
    for key in ("elements", "openings"):
        kept = []
        for rec in building.get(key, []):
            if str(rec["id"]) in wanted:
                reason = "project exception: " + wanted[str(rec["id"])]
                building.setdefault("left_out", []).append({"id": rec["id"], "class": rec["class"], "level": rec.get("level"),
                                                            "type": rec.get("type"), "reason": reason})
                applied.append({"id": rec["id"], "what": "left out", "why": wanted.pop(str(rec["id"]))})
            else:
                kept.append(rec)
        building[key] = kept
    applied += [{"id": i, "what": "not found in the model", "why": why} for i, why in wanted.items()]
    return applied
