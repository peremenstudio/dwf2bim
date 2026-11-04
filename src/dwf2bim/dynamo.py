"""Write a Dynamo graph that builds one project, so nobody has to paste code into a Python node."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

PY_TEMPLATE = """# Written by dwf2bim for project {project}. Build it again to refresh this file.
import os
ROOT = r"{root}"
BUILDING = r"{model}"
CLEAR_EXISTING = bool(IN[0])
exec(open(os.path.join(ROOT, "revit", "build_native.py"), encoding="utf8").read(), globals())
"""


def _id() -> str:
    return uuid.uuid4().hex


def _port(name: str, description: str) -> dict:
    return {"Id": _id(), "Name": name, "Description": description, "UsingDefaultValue": False, "Level": 2, "UseLevels": False, "KeepListStructure": False}


def _view(node_id: str, name: str, x: int, y: int) -> dict:
    return {"Id": node_id, "Name": name, "IsSetAsInput": False, "IsSetAsOutput": False, "Excluded": False, "ShowGeometry": True, "X": x, "Y": y}


def graph(root: Path, project: str, model: Path) -> dict:
    """A switch for deleting the file's model, the build, and a view of its result. Runs only when Run is pressed."""
    switch, script, watch = _id(), _id(), _id()
    switch_out = _port("", "Boolean")
    script_in, script_out = _port("IN[0]", "Input #0"), _port("OUT", "Result of the python script")
    watch_in, watch_out = _port("", "Node to show output from"), _port("", "Node output")
    nodes = [
        {"ConcreteType": "CoreNodeModels.Input.BoolSelector, CoreNodeModels", "NodeType": "BooleanInputNode", "InputValue": False, "Id": switch,
         "Inputs": [], "Outputs": [switch_out], "Replication": "Disabled", "Description": "Enables selection between True and False"},
        {"ConcreteType": "PythonNodeModels.PythonNode, PythonNodeModels", "Code": PY_TEMPLATE.format(root=str(root), project=project, model=str(model)),
         "Engine": "CPython3", "EngineName": "CPython3", "VariableInputPorts": True, "Id": script, "NodeType": "PythonScriptNode",
         "Inputs": [script_in], "Outputs": [script_out], "Replication": "Disabled", "Description": "Runs an embedded Python script."},
        {"ConcreteType": "CoreNodeModels.Watch, CoreNodeModels", "WatchWidth": 600.0, "WatchHeight": 500.0, "Id": watch, "NodeType": "ExtensionNode",
         "Inputs": [watch_in], "Outputs": [watch_out], "Replication": "Disabled", "Description": "Visualizes a node's output"},
    ]
    connectors = [
        {"Start": switch_out["Id"], "End": script_in["Id"], "Id": _id(), "IsHidden": "False"},
        {"Start": script_out["Id"], "End": watch_in["Id"], "Id": _id(), "IsHidden": "False"},
    ]
    return {
        "Uuid": str(uuid.uuid4()), "IsCustomNode": False, "Description": f"Builds project {project} from its building.json. Press Run.",
        "Name": f"Build {project}", "ElementResolver": {"ResolutionMap": {}}, "Inputs": [], "Outputs": [], "Nodes": nodes, "Connectors": connectors,
        "Dependencies": [], "NodeLibraryDependencies": [], "EnableLegacyPolyCurveBehavior": None, "Thumbnail": "", "GraphDocumentationURL": None,
        "ExtensionWorkspaceData": [], "Author": "", "Linting": {"activeLinter": "None", "activeLinterId": "7b75fb44-43fd-4631-a878-29f4d5d8399a", "warningCount": 0, "errorCount": 0},
        "Bindings": [],
        "View": {
            "Dynamo": {"ScaleFactor": 1.0, "HasRunWithoutCrash": True, "IsVisibleInDynamoLibrary": True, "Version": "3.0.0.0", "RunType": "Manual", "RunPeriod": "1000"},
            "Camera": {"Name": "_Background Preview", "EyeX": -17.0, "EyeY": 24.0, "EyeZ": 50.0, "LookX": 12.0, "LookY": -13.0, "LookZ": -58.0, "UpX": 0.0, "UpY": 1.0, "UpZ": 0.0},
            "ConnectorPins": [],
            "NodeViews": [
                _view(switch, "Delete the model already in this file", 0, 100),
                _view(script, f"Build {project}", 380, 100),
                _view(watch, "Result", 720, 100),
            ],
            "Annotations": [], "X": 60.0, "Y": 60.0, "Zoom": 0.9,
        },
    }


def write_graph(root: Path, project: str, model: Path, folder: Path) -> Path:
    """The graph goes in folder, and builds from the building.json in model."""
    path = Path(folder) / f"Build_{project}.dyn"
    path.write_text(json.dumps(graph(Path(root), project, Path(model).resolve()), ensure_ascii=False, indent=1), encoding="utf8")
    return path
