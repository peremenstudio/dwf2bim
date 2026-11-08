# Build Protocol

How a permit DWF becomes a 3D model. Every run follows these stages in this order, and each stage has a gate. A failed gate stops the run or is written to `BUILD_REPORT.md`; nothing is dropped silently.

## Principle

A Revit-exported DWF is not just a drawing. It holds three layers, and the protocol uses all of them:

| Layer | Holds | Used for |
|---|---|---|
| W2D vector stream | Every line and filled cut outline, tagged with its view and element | Plan position and shape |
| Object definition XML | Revit element id, family, type, level, offsets, sizes | Heights, thickness, classification |
| Viewports | View name, clip outline, scale | Splitting the sheet into views |

**Source priority:** a Revit property beats a measurement, and a measurement beats an inference. Each element records its source.

## Stages

### 0. Get the documents
The person says where to save, and the project folder `<block>_<parcel>` is created there. The documents, the model (in `model/`) and the Dynamo graph all go in it. From a block and parcel, `fetch` opens the archive in a visible browser, because the person using the tool must approve its terms there. It reads nothing until the terms' cookie exists. It lists every page of results, and for each document in `config/archive.json` takes the newest in a format the build can read, trying older ones when the newest is another format. The format is read from the file's content.

**Gate:** the architecture document exists, or the run stops. A missing area plan or survey is reported, and the model is built without it. Documents from different online requests are reported, since they may describe different designs. The choices go to `fetch_manifest.json` in the project folder.

### 1. Read the package
Unzip the DWF and decode the W2D stream and the object XML.

**Gate:** the stream decodes to `(EndOfDWF)` with no unknown opcode, and object data exists. Otherwise stop with a clear message.

### 2. Split into views
Each `(Viewport)` block starts a view. `X_Annotation` is merged into view `X`, as its annotation layer.

| Source view type | Key | Role |
|---|---|---|
| Floor Plan | `FP_<level>` | Plan geometry |
| Section | `SEC_<name>` | Height measurement and checks |
| Elevation | `ELEV_<direction>` | Height measurement and checks |
| Area Plan, Drafting, Detail, Legend, Sheet | `AREA_`, `DRAFT_`, `DET_`, `LEG_`, `SHEET_` | Exported only, not used for geometry |

Output: `views/<key>.png`, `views/<key>.dxf` (plans, sections and elevations), `views/views.json`.

### 3. Set up the frame
1. **Display unit:** compare the wall `Length` property with measured length, and snap to mm, cm, m, inch or foot.
2. **Levels:** read `Level` elements. A level referenced by elements but missing from the sheet is measured from sections and flagged.
3. **Heights per section:** `Level` lines in the annotation stream map sheet Y to elevation.
4. **Primary plan per level:** the plan linked to that level, Project North, scale 1:100 or larger, with the most wall cuts.
5. **Plan alignment:** each view sits somewhere else on the sheet. The same element drawn in two plans has identical vertices up to one shift, so that shift gets a sharp peak of votes. Plans are linked through their strongest peaks.
6. **Origin:** shift the frame close to 0,0. Project zero is kept; the absolute datum comes from a level named like `0.00=19.36`.

**Gate:** each alignment link must have at least half of its common elements coinciding within 2 cm. Otherwise it is reported as a weak link.

### 4. Reconstruct elements, in this order
Order matters: openings need walls, and floors use walls to fill hidden areas.

| Order | Class | Plan shape from | Height from |
|---|---|---|---|
| 1 | Wall | Minimum rectangle of the cut outline gives axis and length; `Width` property gives thickness. Openings leave gaps in the outline, so the rectangle closes them. A wall that is not straight keeps its drawn outline. A wall cropped by a view is merged from the other plans. | Base level + `Base Offset` + `Unconnected Height` |
| 2 | Floor | Cut outline, plus furniture and walls that hide it, closed until the `Area` property is met | `Elevation at Top` or level + offset; thickness = `Volume` / `Area` |
| 3 | Railing | Centre path: parallel rails collapse to one line, lines chain at corners. Kept in `building.json`; IFC shows it as a mass 10 cm wide | `Base Level` + `Base Offset` + `Railing Height`, else lowest point in sections |
| 4 | Stair | Evenly spaced parallel plan lines become treads | Base level + riser height per tread |
| 5 | Mullion, panel, generic | Convex outline in plan | Range measured in sections |
| 6 | Typical floors | A plan named like `03-04` covers both floors, and the level with no plan of its own becomes an **exact copy** of the level the plan shows: walls, floors, railings, generics and openings. A wall already standing there, rising from another level, is used instead of a copy. Source elements with no twin are left out and listed in the report. | Copied, moved one storey |
| 6b | Other missing elements | Same family and type, same size within 3 cm, nearest level with geometry, and only if no wall already stands there. **Marked as inferred.** | Own properties |
| 7 | Door, window | Hosted on the nearest wall by plan position; centred on the symbol's extent along the wall axis | Level + `Sill Height`, `Height`; `Width` |

Classes are set in `config/classification.json`. Default: walls, floors, doors, windows, stairs, railings, mullions, panels and generic models are in; furniture, plumbing, equipment, MEP, site and soil volumes are out. A family that matches no rule is listed in the report, never guessed.

All text and annotation is left out. Names are translated with `config/glossary.json`, and anything left over is transliterated, so the model contains only English.

Output: `building.json`, the neutral model that all later stages read.

### 4b. Survey map, plot and building lines, area plan
A project folder under `DWF/` holds the building DWF and two DWFX sheets, told apart by their content.

1. **Plot line:** the survey map's blue boundary, followed end to end into a ring in ITM.
2. **Building line:** the survey map's red dashes inside the plot, collapsed to edges. Dash length keeps out dimension lines and parcel extensions of the same colour. Open building lines appear only on the Revit sheet, as `Property` elements.
3. **Placement:** the plot on the Revit sheet is fitted to the survey's plot. A corner on a straight edge is dropped, and the fit is rigid (rotation and shift, no scale). Limit: 10 cm at the worst corner, otherwise reported. The survey's lines are brought into the model by this fit. With no survey, the lines on the Revit sheet are used.
4. **Area plan:** each panel gives floor number, height and a table of polygon number, code and area. The face around each number label is its cell. A line end that stops just short of another line may be joined only if the face still matches the table area. Only the boundaries of numbered cells become area lines, so a fence or any other line in no cell stays out. A floor listed as `3, 4` serves both levels.
5. **Fitting panels to the model:** the shift is chosen by the walls it holds. A panel holding under 90% of its plan's walls is anchored to the sure floors through the repeating core.
6. **Slabs:** a slab mostly inside the building is a main slab, and is cut at the interior cells and their walls, so it passes neither the outer walls nor into a balcony. A slab mostly in a balcony cell may fill that cell. Any other slab stands on its own and stays as drawn. Room cells are filled where furniture hid the slab; stairs, lifts and voids stay open. A level with over 10% of its walls outside the plan, such as a ground floor with site walls, is skipped and reported.
7. **Storey slab:** from level 01 up, a storey's slab is one floor. Slabs inside the outer walls and within 5 cm of each other in height join across the walls between them, and take the type that covers most of the floor. Balconies, slabs standing on their own, lower slabs, the ground floor and the basements stay separate.

### 5. Write IFC
IFC4, metres, one storey per level. Revit's `IfcGUID` is reused where valid, so a rebuild keeps identity. Straight walls carry an axis and a rectangular profile. Openings are real voids in the host wall. Pset `Dwf2Bim` records `RevitElementId`, `GeometrySource`, `HeightSource` and `ClonedFrom`.

### 6. Verify
| Check | Meaning |
|---|---|
| IFC schema validation | File is well formed |
| Solids built | Every element, voids included, produces geometry |
| Wall length vs `Length` | Plan reconstruction against Revit |
| Wall volume vs `Volume` | Sanity only: Revit deducts openings and joins, so expect a ratio slightly above 1 |
| Floor area vs `Area` | Outline reconstruction against Revit |
| Wall drawn outside modelled height | Heights against independent section views |
| Levels, heights and slab areas | Per level: elevation, storey height, slab area in the model against Revit `Area`. Slab area is not a regulatory floor area |
| `qa/overlay_<level>.png` | Model drawn on the source plan: green walls, blue floors, orange copied, magenta openings, red openings with no host |
| `qa/preview_3d*.png` | Overall look |

Output: `BUILD_REPORT.md`. Start every review from its "Inferred geometry" section.

### 7. Build in Revit
IFC import gives fixed geometry whose type cannot be swapped, so editable models are built by script instead. See [revit/README.md](../revit/README.md).

1. Run `revit/export_catalog.py` on the template. It writes the available types and families to `revit/catalog/`.
2. Fill `config/revit_mapping.json` where DWF names differ from template names.
3. In a new project from the template, run `revit/build_native.py`. It reads `building.json` and creates real walls, floors, hosted doors and windows, and railings. A type missing from the template is duplicated from a similar one and resized.
4. Read `revit_build_log.json` for failures and substitutions, and review the report's inferred and unresolved lists against the overlays.
5. Revit Areas are created from the area plan right after the model, on area plans cleared first and with no snapping, so they show the DWF's lines. Then compare with `python -m dwf2bim compare-areas OUTPUT/<project>`: `AREA_COMPARISON.md` and `qa/area_compare_<level>.png` show cells, areas against the table, and lines found on one side only. Turn the 2D plot and building lines into Property Lines by hand, as described in `revit/README.md`.
6. Replace `DWF floor` elements, which stand for thin plates of other families, with your own where needed. Posts, bars and masses already covered by a slab are not built, and are listed in the report.

Everything built is native, and fixed geometry (DirectShape) is not used. An element that cannot be built natively is reported instead.

`build_native.py` draws the plot and building lines as 2D lines, because Revit's API cannot create a Property Line. Last of all, it sets the shared coordinates from the survey fit and moves the Project Base Point to a plot corner.

**Rules the script must keep:**
- Door and window types are created and committed in their own transaction before any is placed. An instance placed in the transaction that created its type does not cut the wall.
- After placing, each door and window is checked against its wall's volume, placed again if the wall shows no opening, and reported if it still does not cut.
- Revit's failure messages are handled in code and counted in the log, so no dialog interrupts the run. Elements Revit removes while resolving an error are listed under `elements_removed_by_revit`.
- Slab outlines must have no spike, slit or edge under 12 mm, even when the geometry is valid. Revit refuses them.

The IFC remains useful as a quick geometry check.

## Known limits

| Limit | Effect | Planned fix |
|---|---|---|
| Needs Revit object data | A DWF from AutoCAD, or exported without object data, is refused | Geometry-only route |
| DWFX as a building sheet | Read only as a survey or area plan | Building DWFX needs a sample file |
| Elements seen only in sections or elevations | Most mullions, some panels and generics are unresolved | Register sections to plans |
| Levels with no plan of their own | Typical floors are copied from the shared plan, and differences between the two floors are not seen | Check copies with registered sections |
| Stairs | Layout rebuilt from the treads visible in plan, the riser count and the tread depth; only straight flights and a U with a landing | Winders and curved flights |
| Revit build not yet run in Revit | API names are checked against `RevitAPI.dll` and logic against a mock, but Revit's acceptance is untested | First run on the template |
| Door swing, window facing | Not read from the DWF; flip in Revit | Read from plan symbols |
| Railings on stairs | Plan shows only part of the rail, so the path is short | Host on native stairs |

## Other limits of the survey and area route
- The plot and building lines are found by colour (`survey_plot_stroke`, `survey_building_line` in `config/site.json`). A survey drawn in other colours needs those values, and until then the model is not placed and the report says so.
- `IL_UnitID` runs through the building from the bottom up. It comes from the area table's apartment column when that is filled in. When it is empty it is inferred: a large residential cell is an apartment. Shelters are shared out so that as many apartments as possible have one, a spare one going to the apartment beside it. A shelter's walls follow it, every balcony joins the nearest apartment, and a niche the one it borders. The report lists the result, its source, and apartments without exactly one shelter. Other cells, and every cell on a floor with no residential cell, get 0.
- Each area is set to Export to IFC = Yes.
