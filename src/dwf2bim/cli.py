"""Command line: python -m dwf2bim build <file-or-folder> [--out OUTPUT] [--skip-dxf], and see main()."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import ifcopenshell
import ifcopenshell.validate

from . import conventions, dwfx, dynamo, fetch, ifc_export, package, qa, site, views as views_mod, xps
from .extract import Extractor
from .names import level_id

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = ROOT / ".dwf2bim_settings.json"
MODEL_FOLDER = "model"
log_listeners = []


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    for listener in log_listeners:
        listener(msg)


def classify_folder(folder: Path) -> dict:
    """In a project folder: which file is the building, the survey and the area plan.

    The download's manifest says so, when there is one. Otherwise a DWF is the building, and a
    DWFX is read: an area table makes it the area plan, a plot boundary the survey, and thousands
    of Revit objects the building.
    """
    found = {"building": None, "survey": None, "area": None, "other": []}
    roles = {}
    manifest = folder / fetch.MANIFEST
    if manifest.exists():
        documents = json.loads(manifest.read_text(encoding="utf8")).get("documents", {})
        roles = {d["file"]: role for role, d in documents.items() if d and (folder / d["file"]).exists()}
    for f in sorted(folder.iterdir()):
        role = roles.get(f.name)
        if role == "architecture" or (not role and f.suffix.lower() == ".dwf"):
            found["building"] = f
        elif role in ("area", "survey"):
            found[role] = xps.load(f)[0]
        elif f.suffix.lower() == ".dwfx":
            sheet = xps.load(f)[0]
            if site.is_area_sheet(sheet):
                found["area"] = sheet
            elif site.survey_corners(sheet):
                found["survey"] = sheet
            elif found["building"] is None and dwfx.is_building(f):
                found["building"] = f
            else:
                found["other"].append(f.name)
    return found


def last_save_folder() -> str:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf8")).get("save_folder", "")
    except (OSError, ValueError):
        return ""


def remember_save_folder(folder) -> None:
    SETTINGS.write_text(json.dumps({"save_folder": str(folder)}, ensure_ascii=False), encoding="utf8")


def build(source: Path, out_root: Path, skip_dxf: bool = False, survey=None, area=None, name=None, graph_folder: Path = None) -> Path:
    out = out_root / (name or source.stem)
    (out / "views").mkdir(parents=True, exist_ok=True)
    (out / "qa").mkdir(exist_ok=True)

    _log(f"Stage 1/6 read package: {source.name}")
    sheets = package.load(source)
    _log(f"  sheets={len(sheets)} elements={sum(len(s.elements) for s in sheets)}")

    _log("Stage 2/6 split views")
    views = views_mod.split_views(sheets)
    _log(f"  views={len(views)}")

    _log("Stage 3-4/6 align plans, calibrate heights, reconstruct elements")
    ex = Extractor(sheets, views)
    building = ex.run()
    building["sections"] = ex.section_marks(building)
    ex.unresolved_outlines(building["unresolved"])
    _log(f"  elements={len(building['elements'])} openings={len(building['openings'])} unresolved={len(building['unresolved'])}")

    _log("Stage 4b/6 survey, plot and building lines, area plan, slab limits")
    site_builder = site.SiteBuilder(ex, building)
    site_builder.place(survey)
    site_builder.areas(area)
    if building.get("areas"):
        site_builder.clip_floors()
    site.assign_apartments(building)
    site.merge_storey_slabs(building)
    site.keep_plate_masses(building)
    _log(f"  modelling rules: { {k: (len(v) if isinstance(v, list) else v) for k, v in conventions.apply(building).items()} }")
    placement = building["site"].get("placement")
    _log(f"  placement={placement} slab_clip={building['site'].get('slab_clip')}")
    if not skip_dxf:
        building["plan_drawings"] = views_mod.plan_drawings(views, building["levels"], building.get("typical_floors") or {})
    (out / "building.json").write_text(json.dumps(building, ensure_ascii=False, indent=1), encoding="utf8")

    manifest = views_mod.views_manifest(views)
    (out / "views" / "views.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf8")
    for v in views.values():
        if v.prims:
            qa.render_view(v, out / "views" / f"{v.key}.png")
            if not skip_dxf and v.kind in ("plan", "section", "elevation"):
                qa.export_dxf(v, out / "views" / f"{v.key}.dxf")

    _log("Stage 5/6 write IFC")
    ifc_path = out / f"{source.stem}.ifc"
    counts = ifc_export.export(building, ifc_path, source.stem)
    _log(f"  {counts}")

    _log("Stage 6/6 verify")
    log = ifcopenshell.validate.json_logger()
    ifc_file = ifcopenshell.open(str(ifc_path))
    ifcopenshell.validate.validate(ifc_file, log)
    errors = [s for s in log.statements if s.get("level") == "error"]
    validation = f"{len(errors)} schema errors."
    if errors:
        validation += " First: " + "; ".join(str(e.get("message"))[:160] for e in errors[:5])
    solids, failed_solids = qa.check_geometry(ifc_file)
    validation += f" Solids built: {solids}, failed: {len(failed_solids)}."
    if failed_solids:
        validation += " Failed: " + ", ".join(failed_solids[:20])
    _log(f"  {validation}")
    checks = qa.check_against_sources(building, ex)
    for level, v in ex.plans.items():
        if v.offset is not None:
            qa.overlay_plan(v, building, level_id(level), out / "qa" / f"overlay_{level_id(level)}.png")
    if building.get("site", {}).get("plot"):
        qa.render_site(building, out / "qa" / "site_plan.png")
    for floor in building.get("areas", []):
        qa.render_area_plan(floor, out / "qa" / f"area_plan_{'_'.join(floor['levels'])}.png")
    qa.render_3d(building, out / "qa" / "preview_3d.png")
    qa.render_3d(building, out / "qa" / "preview_3d_back.png", azimuth=125.0)
    qa.write_report(out / "BUILD_REPORT.md", source.name, building, checks, counts, manifest, validation, qa.level_summary(building, ex))
    project = graph_folder.name if graph_folder else out.name
    _log(f"Dynamo graph: {dynamo.write_graph(ROOT, project, out, graph_folder or out)}")
    _log(f"Done: {out}")
    return out


def compare_areas(folder: Path) -> int:
    """Compare the area plans Revit built with the DWF's."""
    building = json.loads((folder / "building.json").read_text(encoding="utf8"))
    result_path = folder / "revit_areas_result.json"
    if not result_path.exists():
        print(f"No {result_path.name} in {folder}: run revit/build_areas.py first.", file=sys.stderr)
        return 2
    result = json.loads(result_path.read_text(encoding="utf8"))
    rows = qa.compare_areas(building, result)
    qa.write_area_comparison(folder / "AREA_COMPARISON.md", rows)
    for floor in building.get("areas", []):
        for lid in floor["levels"]:
            if lid in result:
                qa.render_area_plan(floor, folder / "qa" / f"area_compare_{lid}.png", revit_lines=result[lid]["lines"])
    for r in rows:
        _log(str({k: (len(v) if k == "off_table" else v) for k, v in r.items()}))
    _log(f"Done: {folder / 'AREA_COMPARISON.md'}")
    return 0


def build_folder(project: Path, out_root: Path = None, skip_dxf: bool = False) -> Path:
    """Build a project folder: the DWF is the building, DWFX sheets are the survey and the area plan.

    The Dynamo graph goes in the project folder. So does the model, in a folder of its own, unless out_root says otherwise.
    """
    found = classify_folder(project)
    if found["building"] is None:
        raise package.UnsupportedInput("the folder has no DWF of the building")
    for other in found["other"]:
        _log(f"Not recognised as a survey or an area plan, skipped: {other}")
    if out_root is None:
        return build(found["building"], project, skip_dxf, found["survey"], found["area"], MODEL_FOLDER, project)
    return build(found["building"], out_root, skip_dxf, found["survey"], found["area"], project.name, project)


def run_project(block, parcel, save_folder, use_local: bool = False, log=print) -> dict:
    """From a block and parcel to a model and a Dynamo graph, all in <save_folder>/<block>_<parcel>."""
    name = fetch.project_name(block, parcel)
    save_folder = Path(save_folder)
    if not save_folder.is_absolute():
        raise fetch.FetchError(f"Choose a full folder path, such as D:\\Projects. Got '{save_folder}'")
    folder = save_folder / name
    remember_save_folder(save_folder)
    manifest = {}
    if use_local:
        if not folder.exists():
            raise fetch.FetchError(f"There is no folder {folder}")
        log(f"Using the files in {folder}")
    else:
        manifest = fetch.fetch(block, parcel, folder, log=log)
    out = build_folder(folder)
    building = json.loads((out / "building.json").read_text(encoding="utf8"))
    counts = {}
    for record in building["elements"] + building["openings"]:
        counts[record["class"]] = counts.get(record["class"], 0) + 1
    counts["areas"] = sum(len(f["cells"]) * len(f["levels"]) for f in building.get("areas", []))
    counts["apartments"] = len(building.get("apartments", {}).get("units", []))
    counts["unresolved"] = len(building.get("unresolved", []))
    warnings = list(manifest.get("warnings", []))
    if building.get("apartments", {}).get("source") == "inferred":
        warnings.append("Apartment numbers are inferred, as the area plan does not list them. Check BUILD_REPORT.md.")
    return {
        "project": name, "documents": manifest.get("documents", {}), "missing": manifest.get("missing", []), "warnings": warnings,
        "counts": counts, "folder": str(folder), "model": str(out), "graph": str(folder / f"Build_{name}.dyn"), "report": str(out / "BUILD_REPORT.md"),
    }


def _ask_save_folder(given) -> Path:
    if given:
        return Path(given)
    last = last_save_folder()
    answer = input(f"Where should the project folder be created?{f' [{last}]' if last else ''} ").strip().strip('"') or last
    if not answer:
        raise fetch.FetchError("No folder was given.")
    return Path(answer)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dwf2bim", description="Build an IFC model from a Revit-exported permit DWF.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="Build one file, or every DWF/DWFX in a folder")
    b.add_argument("source", nargs="?", default=str(ROOT / "DWF"))
    b.add_argument("--out", default=str(ROOT / "OUTPUT"))
    b.add_argument("--skip-dxf", action="store_true")
    c = sub.add_parser("compare-areas", help="Compare the area plans built in Revit with the DWF")
    c.add_argument("folder", help="OUTPUT/<project>")
    f = sub.add_parser("fetch", help="Download a project's newest documents from the Tel Aviv engineering archive")
    f.add_argument("block")
    f.add_argument("parcel")
    f.add_argument("--folder", help="Where to create the <block>_<parcel> folder. Asked if left out")
    r = sub.add_parser("run", help="Download a project's documents and build it")
    r.add_argument("block")
    r.add_argument("parcel")
    r.add_argument("--folder", help="Where the <block>_<parcel> folder is, or is to be created. Asked if left out")
    r.add_argument("--local", action="store_true", help="Use the files already in the <block>_<parcel> folder")
    u = sub.add_parser("ui", help="Open the local page")
    u.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "compare-areas":
        return compare_areas(Path(args.folder))
    if args.command == "ui":
        from . import ui
        return ui.serve(args.port)
    if args.command in ("fetch", "run"):
        try:
            if args.command == "fetch":
                folder = _ask_save_folder(args.folder) / fetch.project_name(args.block, args.parcel)
                print(json.dumps(fetch.fetch(args.block, args.parcel, folder, log=_log), ensure_ascii=False, indent=1))
            else:
                print(json.dumps(run_project(args.block, args.parcel, _ask_save_folder(args.folder), args.local, log=_log), ensure_ascii=False, indent=1))
        except (fetch.FetchError, package.UnsupportedInput) as e:
            print(e, file=sys.stderr)
            return 1
        return 0

    source = Path(args.source)
    if source.is_file():
        projects = [source]
    else:
        loose = sorted(p for p in source.iterdir() if p.suffix.lower() == ".dwf")
        folders = sorted(p for p in source.iterdir() if p.is_dir())
        has_own = any(p.suffix.lower() == ".dwf" for p in source.iterdir())
        projects = [source] if has_own and any(p.suffix.lower() == ".dwfx" for p in source.iterdir()) else loose + folders
    if not projects:
        print(f"No DWF files or project folders in {source}", file=sys.stderr)
        return 2
    failed = 0
    for project in projects:
        try:
            if project.is_dir():
                build_folder(project, Path(args.out), args.skip_dxf)
            else:
                build(project, Path(args.out), args.skip_dxf)
        except package.UnsupportedInput as e:
            print(f"{project.name}: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0
