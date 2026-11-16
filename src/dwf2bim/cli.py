"""Command line: python -m dwf2bim build <file-or-folder> [--out OUTPUT] [--skip-dxf], and see main()."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import ifcopenshell
import ifcopenshell.validate
from shapely.geometry import Polygon

from . import conventions, dwfx, dynamo, fetch, ifc_export, massing, overrides, package, qa, site, support, survey as survey_mod, views as views_mod, xps
from .extract import Extractor
from .names import CONFIG_DIR, level_id

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
        elif role == "survey" and package.is_dwf(f):
            found[role] = survey_mod.load_dwf(f)  # a survey map in DWF form
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
    building["shaft_hints"] = ex.shaft_hints()
    building["overrides"] = overrides.load(graph_folder)
    building["overrides_applied"] = overrides.leave_out_elements(building, building["overrides"])
    building["ground_cover"] = ex.ground_cover()
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
    standing = support.check(building)
    _log(f"  standing on nothing: {standing['counts'] or 'none'}")
    placement = building["site"].get("placement")
    _log(f"  placement={placement} slab_clip={building['site'].get('slab_clip')}")
    if not skip_dxf:
        building["plan_drawings"] = views_mod.plan_drawings(views, building["levels"], building.get("typical_floors") or {})
    verdict = support.quality(building)
    (out / "building.json").write_text(json.dumps(building, ensure_ascii=False, indent=1), encoding="utf8")
    (out / "quality.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf8")

    manifest = views_mod.views_manifest(views)
    (out / "views" / "views.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf8")
    layers = {i: qa.drawing_layers(sheet, ex.rules) for i, sheet in enumerate(sheets)}
    for v in views.values():
        if v.prims:
            qa.render_view(v, out / "views" / f"{v.key}.png")
            if not skip_dxf and v.kind in ("plan", "section", "elevation"):
                qa.export_dxf(v, out / "views" / f"{v.key}.dxf", building.get("contours", {}).get(level_id(v.level)) if v.level else None,
                              layers[v.sheet])
    if not skip_dxf:
        for lid, outline in building.get("contours", {}).items():
            qa.export_contours_dxf(outline, out / "views" / f"CONTOUR_{lid}.dxf")

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
    for lid, outline in building.get("contours", {}).items():
        plan = next((v for name, v in ex.plans.items() if level_id(name) == lid and v.offset is not None), None)
        qa.render_contours(plan, outline, out / "qa" / f"contour_{lid}.png")
    if building.get("site", {}).get("plot"):
        qa.render_site(building, out / "qa" / "site_plan.png")
    for floor in building.get("areas", []):
        qa.render_area_plan(floor, out / "qa" / f"area_plan_{'_'.join(floor['levels'])}.png")
    qa.render_3d(building, out / "qa" / "preview_3d.png")
    qa.render_3d(building, out / "qa" / "preview_3d_back.png", azimuth=125.0)
    qa.write_report(out / "BUILD_REPORT.md", source.name, building, checks, counts, manifest, validation, qa.level_summary(building, ex))
    project = graph_folder.name if graph_folder else out.name
    _log(f"Dynamo graph: {dynamo.write_graph(ROOT, project, out, graph_folder or out)}")
    _log(f"Verdict: {verdict['verdict']}")
    for reason in verdict["reasons"]:
        _log(f"  - {reason}")
    _log(f"Done: {out}")
    return out


def build_from_area_plan(source_name: str, out: Path, area, survey=None, graph_folder: Path = None, drawing: Path = None) -> Path:
    """Build from the area plan alone, when the architecture file holds no Revit object data."""
    (out / "views").mkdir(parents=True, exist_ok=True)
    (out / "qa").mkdir(exist_ok=True)
    _log("Building from the area plan alone: storeys, floors, balconies, voids and exterior walls")
    building = massing.build(area)
    massing.place(building, survey, drawing)
    _log(f"  placement={building['site'].get('placement')}")
    building["overrides"] = overrides.load(graph_folder)
    building["overrides_applied"] = overrides.leave_out_elements(building, building["overrides"])
    standing = support.check(building)
    _log(f"  storeys={len(building['areas'])} elements={len(building['elements'])} standing on nothing: {standing['counts'] or 'none'}")
    verdict = support.quality(building)
    (out / "building.json").write_text(json.dumps(building, ensure_ascii=False, indent=1), encoding="utf8")
    (out / "quality.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf8")
    for lid, outline in building["contours"].items():
        qa.export_contours_dxf(outline, out / "views" / f"CONTOUR_{lid}.dxf")
        qa.render_contours(None, outline, out / "qa" / f"contour_{lid}.png")
    for floor in building["areas"]:
        qa.render_area_plan(floor, out / "qa" / f"area_plan_{'_'.join(floor['levels'])}.png")
    stem = Path(source_name).stem
    ifc_path = out / f"{stem}.ifc"
    counts = ifc_export.export(building, ifc_path, stem)
    log = ifcopenshell.validate.json_logger()
    ifc_file = ifcopenshell.open(str(ifc_path))
    ifcopenshell.validate.validate(ifc_file, log)
    errors = [s for s in log.statements if s.get("level") == "error"]
    solids, failed_solids = qa.check_geometry(ifc_file)
    validation = f"{len(errors)} schema errors. Solids built: {solids}, failed: {len(failed_solids)}."
    if building["site"].get("plot"):
        qa.render_site(building, out / "qa" / "site_plan.png")
    qa.render_3d(building, out / "qa" / "preview_3d.png")
    qa.render_3d(building, out / "qa" / "preview_3d_back.png", azimuth=125.0)
    order = list(building["levels"].items())
    table = []
    for i, (lid, lv) in enumerate(order):
        area = sum(Polygon(q["outer"]).area for e in building["elements"] if e["class"] == "floor" and e["level"] == lid for q in e["geometry"]["polygons"])
        table.append({"level": lid, "z": lv["z"], "storey_height": round(order[i + 1][1]["z"] - lv["z"], 3) if i + 1 < len(order) else None,
                      "source": lv["source"], "slabs": 0, "slabs_built": 0, "slab_area_revit": 0.0, "slab_area_model": round(area, 1)})
    qa.write_report(out / "BUILD_REPORT.md", source_name, building, {}, counts, [], validation, table)
    project = graph_folder.name if graph_folder else out.name
    _log(f"Dynamo graph: {dynamo.write_graph(ROOT, project, out, graph_folder or out)}")
    _log(f"Verdict: {verdict['verdict']}")
    for reason in verdict["reasons"]:
        _log(f"  - {reason}")
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
        if found["area"] is None:
            raise package.UnsupportedInput("the folder has no DWF of the building, and no area plan to build from instead")
        _log("No architecture file in the folder.")
        target = (project / MODEL_FOLDER) if out_root is None else (out_root / project.name)
        return build_from_area_plan(f"{project.name}_area_plan", target, found["area"], found["survey"], project)
    for other in found["other"]:
        _log(f"Not recognised as a survey or an area plan, skipped: {other}")
    out_root, name = (project, MODEL_FOLDER) if out_root is None else (out_root, project.name)
    try:
        return build(found["building"], out_root, skip_dxf, found["survey"], found["area"], name, project)
    except package.UnsupportedInput as ex:
        if found["area"] is None:
            raise
        # a drawing made in AutoCAD: no elements to read, but the area plan still gives the storeys and their floors
        _log(f"{ex}")
        return build_from_area_plan(found["building"].name, out_root / name, found["area"], found["survey"], project, found["building"])


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
    counts["standing_on_nothing"] = len((building.get("support") or {}).get("unsupported", []))
    warnings = list(manifest.get("warnings", []))
    verdict = building.get("quality") or {}
    if verdict.get("verdict") in ("review", "failed"):
        warnings.append(f"Model verdict: {verdict['verdict']}. {len(verdict['reasons'])} points to check in BUILD_REPORT.md.")
    if building.get("apartments", {}).get("source") == "inferred":
        warnings.append("Apartment numbers are inferred, as the area plan does not list them. Check BUILD_REPORT.md.")
    return {
        "project": name, "documents": manifest.get("documents", {}), "missing": manifest.get("missing", []), "warnings": warnings,
        "counts": counts, "verdict": verdict.get("verdict"), "folder": str(folder), "model": str(out), "graph": str(folder / f"Build_{name}.dyn"), "report": str(out / "BUILD_REPORT.md"),
    }


def _ask_save_folder(given) -> Path:
    if given:
        return Path(given)
    last = last_save_folder()
    answer = input(f"Where should the project folder be created?{f' [{last}]' if last else ''} ").strip().strip('"') or last
    if not answer:
        raise fetch.FetchError("No folder was given.")
    return Path(answer)


DOCUMENT_SUFFIXES = (".dwf", ".dwfx")
BASELINE_FOLDER = "_baseline"
REGRESS_REPORT = "regress_report.json"
BATCH_REPORT = "batch_report"


def _regress_rules() -> dict:
    return json.loads((CONFIG_DIR / "regress.json").read_text(encoding="utf8"))


def _documents(folder: Path) -> list:
    return [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in DOCUMENT_SUFFIXES]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compare_quality(before, after: dict, rules: dict = None) -> dict:
    """Compare a project's new quality.json with its accepted one.

    Returns {"status": "new"|"same"|"better"|"worse", "changes": [{"name", "before", "after", "change"}]}.
    Only the verdict and the metrics named in config/regress.json are compared; for list metrics, their lengths.
    """
    if before is None:
        return {"status": "new", "changes": []}
    rules = rules or _regress_rules()
    tolerance = rules["tolerance"]
    changes = []

    def note(name, old, new, better):
        changes.append({"name": name, "before": old, "after": new, "change": "better" if better else "worse"})

    order = rules["verdict_order"]
    old_v, new_v = before.get("verdict"), after.get("verdict")
    if old_v in order and new_v in order and old_v != new_v:
        note("verdict", old_v, new_v, order.index(new_v) > order.index(old_v))
    old_m, new_m = before.get("metrics") or {}, after.get("metrics") or {}
    for name in rules["lower_is_better"]:
        old, new = old_m.get(name), new_m.get(name)
        if _is_number(old) and _is_number(new) and abs(new - old) > tolerance:
            note(name, old, new, new < old)
    for name in rules["shorter_is_better"]:
        old, new = old_m.get(name), new_m.get(name)
        if isinstance(old, list) and isinstance(new, list) and len(old) != len(new):
            note(name, len(old), len(new), len(new) < len(old))
    for name in rules["true_is_better"]:
        old, new = old_m.get(name), new_m.get(name)
        if isinstance(old, bool) and isinstance(new, bool) and old != new:
            note(name, old, new, new)
    if any(c["change"] == "worse" for c in changes):
        status = "worse"
    elif changes:
        status = "better"
    else:
        status = "same"
    return {"status": status, "changes": changes}


def regress(corpus: Path, out=None, accept: bool = False, build=None) -> int:
    """Build every project in the corpus and compare each model's quality with the one last accepted.

    The models go to `out`, or to a new temporary folder, never into the project folders. Returns 1 when a
    project got worse or failed to build, else 0.
    """
    build = build or build_folder
    corpus = Path(corpus)
    projects = sorted(p for p in corpus.iterdir() if p.is_dir() and p.name != BASELINE_FOLDER and _documents(p))
    if not projects:
        print(f"No project folders holding DWF or DWFX files in {corpus}", file=sys.stderr)
        return 2
    out = Path(out) if out else Path(tempfile.mkdtemp(prefix="dwf2bim_regress_"))
    out.mkdir(parents=True, exist_ok=True)
    baselines = corpus / BASELINE_FOLDER
    rules = _regress_rules()
    records, built = [], {}
    for project in projects:
        baseline_file = baselines / f"{project.name}.json"
        before = json.loads(baseline_file.read_text(encoding="utf8")) if baseline_file.exists() else None
        record = {"project": project.name, "status": None, "verdict_before": (before or {}).get("verdict"),
                  "verdict_after": None, "changes": [], "error": None}
        _log(f"Regress: building {project.name}")
        try:
            model = Path(build(project, out))
            quality_file = model / "quality.json"
            after = json.loads(quality_file.read_text(encoding="utf8"))
        except Exception as e:  # one project's failure is reported and the others still run
            record.update(status="failed", error=f"{type(e).__name__}: {e}")
        else:
            built[project.name] = quality_file
            record["verdict_after"] = after.get("verdict")
            record.update(compare_quality(before, after, rules))
        records.append(record)
    (out / REGRESS_REPORT).write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf8")
    print()
    for r in records:
        line = f"{r['project']}: {r['status']} ({r['verdict_before'] or '-'} -> {r['verdict_after'] or '-'})"
        if r["error"]:
            line += f" {r['error']}"
        print(line)
        for c in r["changes"]:
            print(f"    {c['name']}: {c['before']} -> {c['after']} ({c['change']})")
    if accept:
        baselines.mkdir(exist_ok=True)
        for name, quality_file in built.items():
            shutil.copyfile(quality_file, baselines / f"{name}.json")
        print(f"Accepted {len(built)} baselines into {baselines}")
    print(f"Report: {out / REGRESS_REPORT}")
    return 1 if any(r["status"] in ("worse", "failed") for r in records) else 0


def parse_parcels(text: str) -> list:
    """Read a parcel list: one `block parcel` per line, split by spaces, a comma, a slash or an underscore.

    Blank lines and lines starting with # are skipped. Returns, in order, {"line", "block", "parcel"} for each
    parcel and {"line", "text", "error"} for each line that does not give two numbers.
    """
    entries = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p for p in re.split(r"[\s,/_]+", line) if p]
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            entries.append({"line": number, "block": parts[0], "parcel": parts[1]})
        else:
            entries.append({"line": number, "text": line, "error": f"line {number}: expected a block and a parcel number, got '{line}'"})
    return entries


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return {}


def _built_from_same_documents(folder: Path) -> bool:
    """True when the folder's model is newer than every DWF and DWFX in it."""
    quality_file = folder / MODEL_FOLDER / "quality.json"
    if not folder.is_dir() or not quality_file.exists():
        return False
    documents = _documents(folder)
    return bool(documents) and all(quality_file.stat().st_mtime > d.stat().st_mtime for d in documents)


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_batch_report(save_folder: Path, rows: list) -> None:
    (save_folder / f"{BATCH_REPORT}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf8")
    verdicts = [r.get("verdict") for r in rows if r["status"] != "error"]
    counts = (f"ready: {verdicts.count('ready')}, review: {verdicts.count('review')}, failed: {verdicts.count('failed')}, "
              f"errors: {sum(1 for r in rows if r['status'] == 'error')}")
    columns = ["project", "status", "verdict", "kind", "reasons", "missing", "seconds", "error"]
    lines = [counts, "", "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(_cell(r.get(c)) for c in columns) + " |" for r in rows]
    (save_folder / f"{BATCH_REPORT}.md").write_text("\n".join(lines) + "\n", encoding="utf8")


def batch(list_file: Path, save_folder, use_local: bool = False, run=None) -> int:
    """Run every parcel in a list file, one after another, with nobody watching.

    A parcel that fails is recorded and the next one runs. The reports are rewritten after every parcel.
    Returns 0 when every parcel was built or skipped, else 1.
    """
    run = run or run_project
    save_folder = Path(save_folder)
    if not save_folder.is_absolute():
        print(f"Choose a full folder path, such as D:\\Projects. Got '{save_folder}'", file=sys.stderr)
        return 2
    entries = parse_parcels(Path(list_file).read_text(encoding="utf-8-sig"))
    if not entries:
        print(f"No parcels in {list_file}", file=sys.stderr)
        return 2
    save_folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, entry in enumerate(entries, 1):
        row = {"project": None, "status": None, "verdict": None, "kind": None, "reasons": [], "missing": [], "seconds": None, "error": None}
        if "error" in entry:
            row.update(project=entry["text"], status="error", error=entry["error"])
            _log(f"Batch {i}/{len(entries)}: {entry['error']}")
        else:
            name = f"{entry['block']}_{entry['parcel']}"
            folder = save_folder / name
            row["project"] = name
            start = time.monotonic()
            if use_local and _built_from_same_documents(folder):
                quality = _read_json(folder / MODEL_FOLDER / "quality.json")
                row.update(status="skipped", verdict=quality.get("verdict"), kind=quality.get("kind"), reasons=quality.get("reasons", []),
                           missing=_read_json(folder / fetch.MANIFEST).get("missing", []))
                _log(f"Batch {i}/{len(entries)}: {name} is already built from these documents, skipped")
            else:
                _log(f"Batch {i}/{len(entries)}: {name}")
                try:
                    result = run(entry["block"], entry["parcel"], save_folder, use_local, log=_log)
                except Exception as e:  # FetchError, UnsupportedInput or a fault: record it and go on
                    row.update(status="error", error=f"{type(e).__name__}: {e}" if not isinstance(e, (fetch.FetchError, package.UnsupportedInput)) else str(e))
                    _log(f"  {name} failed: {row['error']}")
                else:
                    quality = _read_json(Path(result["model"]) / "quality.json") if result.get("model") else {}
                    row.update(status="built", verdict=result.get("verdict") or quality.get("verdict"), kind=quality.get("kind"),
                               reasons=quality.get("reasons", []), missing=result.get("missing", []))
            row["seconds"] = round(time.monotonic() - start, 1)
        rows.append(row)
        write_batch_report(save_folder, rows)
    _log(f"Batch done: {save_folder / (BATCH_REPORT + '.md')}")
    return 0 if all(r["status"] in ("built", "skipped") for r in rows) else 1


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
    g = sub.add_parser("regress", help="Build every project in a corpus and compare each model's quality with the accepted one")
    g.add_argument("corpus", help="A folder holding one folder per project, and _baseline/ with the accepted quality files")
    g.add_argument("--out", help="Where the models go. A new temporary folder if left out")
    g.add_argument("--accept", action="store_true", help="Make the new quality files the baseline")
    t = sub.add_parser("batch", help="Download and build every parcel in a list file")
    t.add_argument("list", help="A text file, one 'block parcel' per line")
    t.add_argument("--folder", required=True, help="Where the <block>_<parcel> folders and the batch report go")
    t.add_argument("--local", action="store_true", help="Use the files already in each <block>_<parcel> folder")
    args = parser.parse_args(argv)
    if args.command == "regress":
        return regress(Path(args.corpus), args.out, args.accept)
    if args.command == "batch":
        return batch(Path(args.list), args.folder, args.local)
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
        # a folder holding a DWFX is one project: its sheets are told apart by their content
        projects = [source] if any(p.suffix.lower() == ".dwfx" for p in source.iterdir()) else loose + folders
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
