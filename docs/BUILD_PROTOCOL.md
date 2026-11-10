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

A DWFX holds the same three layers in another form: XPS paths in W2D logical units with y pointing down, a W2X list that says in drawing order which element (`Object_Node`) and view (`Viewport`) each named path belongs to, and the object XML. `dwfx.py` turns it into the primitives, views and nodes a W2D stream gives, so every later stage is the same. Its sections and elevations come with only an annotation stream, which then counts as the view's model. A sheet set whose floors are drawn only as area plans gives those plans as the floor plans.

In a project folder, the download's manifest says which file is which. Without one, a DWF is the building, and a DWFX is the area plan if it holds an area table, the survey if it holds a plot boundary, and the building if it describes thousands of Revit objects.

**Gate:** the stream decodes to `(EndOfDWF)` with no unknown opcode, or the W2X has no unknown record, and object data exists. Otherwise stop with a clear message.

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

### 4b2. Floor outlines
Before any floor is built, each storey above the ground gets two kinds of outline, as closed polygons:
- **Main floor:** what the storey's full-height walls close all round, to the outer face of their core, joined with the slabs the area plan fitted as main and with its room cells, so a wide glazing that leaves a wall line open does not cut a room out. Walls standing out of the building leave no tab. A hole stays only where no floor covers it, as at a shaft or a stair. A roof takes the walls of the storey below.
- **Balconies:** what the drawn floors cover outside the main floor, within 1.5 m of the plot. The area plan's balcony cells only mark a balcony inside the walls, such as a recessed one, and only where they cover more than a strip, since the cells sit a little off the walls. The drawn slabs and room cells likewise only add to the main floor a room the walls leave open, never a strip along a facade.

Area plan cells count only on a storey where the area plan holds at least 80% of the walls. The outlines are written in red to the plan DXFs and to `views/CONTOUR_<level>.dxf`, on the layers `DWF2BIM_MAIN_FLOOR` and `DWF2BIM_BALCONY_FLOOR`, and drawn on the plan in `qa/contour_<level>.png`. The storey then gets one main floor from its outline, of the type that covers most of it, and one floor per balcony, of the balcony slab it covers most. A drawn floor at another height inside the main floor, such as a lowered bathroom, stays and is cut out of it.

### 4c. Modelling rules
These make the model clean to build in Revit, and are set under `modelling` in `config/site.json`. The report lists what each changed.

1. **Walls start at their level.** A wall drawn down through the slab under it is raised to sit on it; its top stays. Ground floor and roof walls, and those on the top level, keep their drawn heights.
2a. **Walls carry on along their line.** Pieces on one line, touching or up to 30 cm apart, at the same height and with one face flush, become the longer piece even when drawn with another type; their doors and windows move to it. A shelter's walls (in the area plan's shelter-wall cells where the plan fits, or named MMD/ממ"ד) are a real change and stay apart. Pieces that took another type are listed.
2. **Walls meet floor to floor.** A wall that stops up to a metre under the level above runs up to that level, to meet the wall standing there. Walls of one kind that continue each other on one line become one wall, and their doors and windows move to it.
3. **Main slabs end at the wall core.** A storey's main slab reaches the outer face of the exterior walls' core, as Revit's "extend into wall (to core)" does, and no further: where the drawn slab runs past the core by up to 20 cm and stops, that strip is trimmed. A slab that carries on past the wall, as into a recess, stays. Only the stretch of a wall that stands on the slab counts, so a balcony parapet leaving the slab, or a wall running past its corner, adds no tab.
4. **Floors never overlap.** Where two floors fill the same space, a balcony wins, so it keeps its full size, then a storey's main slab, then the larger. A floor left with almost nothing is removed and listed.
5. **Exterior walls are marked**, from the ground floor up, with the side that faces outside: the side whose face looks off the outer outline of the storey's main slabs, or, above the ground, outside what the storey's walls close all round. A recess for laundry has a slab but is open to the air, so its walls are exterior. The walls' outline counts only where it closes at least 80% of the storey. Holes for shafts and stairs count as inside. Only these walls get exterior plaster in Revit; interior walls get none.
6. **Walls stand on the wall below.** From the ground up, a wall parallel to a wall on the storey below, overlapping most of its length and off by up to 50 cm, moves sideways to stand on it. Two exterior walls line up their outside faces, so the facade is flush. Other walls take the smaller move: axis on axis, or one face flush with the face below. A larger offset is a real setback and stays. Walls meeting the moved wall follow it, and so do its doors and windows. Moves of 30 cm or more are listed in the report.
7. **Curved walls are arcs.** A curved wall's centre path is split into the fewest circular arcs and straight lines that fit it within 2 cm, and each is built as one wall.
10. **Curtain walls the DWF left out.** A door or window hosted in a curtain wall that the DWF did not export stands in a gap of a wall line. From that wall's end, across the doors that follow each other, to the next wall on the line when it is within a metre, the gap gets a curtain wall of that wall's height, and each door becomes a door panel of its own family in it. Doors that fall inside an existing wall's span were in a curtain wall embedded in that wall, so they get a curtain wall of a type that embeds itself. The curtain wall is inferred and listed.
9. **The ground outside the building is one floor per height.** Ground floors whose tops are within 5 cm join, across garden walls and slivers, up to the plot edge, and take the type covering most of them. Raised planters and sunken pits keep their own floor. The building's outline is that of the first storey's main slabs; ground level exterior walls take it too, and get plaster.
8. **The ground has floors everywhere in the plot.** Each gap between the ground level's floors continues the floor it shares the longest edge with, at its height and type, or else the nearest floor. Only stairs stay open. A gap closed by walls, or where an element was not built, such as a ramp, is floored too and listed for checking.

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
3. In a new project from the template, run `revit/build_native.py`. A floor with fill gets `finished_floor` from `config/revit_mapping.json`: concrete as its DWF name says, the template's 2 cm tiles, and fill for the rest. An exterior wall gets its core type with `exterior_plaster` on the outside face, with the core kept on the DWF's axis. Floors are then joined with the walls that cross them. It reads `building.json` and creates real walls, floors, hosted doors and windows, and railings. A type missing from the template is duplicated from a similar one and resized.
4. Read `revit_build_log.json` for failures and substitutions, and review the report's inferred and unresolved lists against the overlays.
5. Revit Areas are created from the area plan right after the model, on area plans cleared first and with no snapping, so they show the DWF's lines. Then compare with `python -m dwf2bim compare-areas OUTPUT/<project>`: `AREA_COMPARISON.md` and `qa/area_compare_<level>.png` show cells, areas against the table, and lines found on one side only. Turn the 2D plot and building lines into Property Lines by hand, as described in `revit/README.md`.
6. Replace `DWF floor` elements, which stand for thin plates of other families, with your own where needed. Posts, bars and masses already covered by a slab are not built, and are listed in the report.

Everything built is native, and fixed geometry (DirectShape) is not used. A pergola, or any mass drawn as separate slats and beams, becomes one `DWF floor` per piece. An element that cannot be built natively is reported instead.

Each level's floor plan shows its own DWF plan, imported from `views/<plan>.dxf` into that view only, origin to origin; plan DXFs are written in the model's coordinates for this. A typical storey shows the plan it was copied from. Each section line in the DWF's plans becomes a section view; the side it looks to is measured from the elements the DWF's section drawing shares with the plan, and the section head decides when too few are shared. Plans and sections take the scope box named `scope_box_name` in `config/revit_mapping.json`. Revit's API cannot create a scope box, so without one the plans are cropped to the plot and the log says to draw it. Walls are joined with the wall they stand on.

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
