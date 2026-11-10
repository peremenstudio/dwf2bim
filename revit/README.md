# Revit scripts

Both run inside Revit 2026 from a Dynamo **Python Script** node.

## Build the model
Every build writes `Build_<project>.dyn` in the project folder. In Revit:

1. Open a **new project from your template**, or a **saved copy** of an existing project, and open a 3D view.
2. Manage > Dynamo > Open, and choose that file.
3. Click Run. The result shows in the graph's Result node.

Dynamo does not run an unchanged node twice in one session. To run again, close the graph and open it again.

The graph has one switch, **Delete the model already in this file**. When True it **deletes the file's whole model** (walls, floors, doors, windows, stairs, railings, furniture and so on), including what an earlier run built.

To run a script without the graph, put this in a Python Script node, with `ROOT` set to this repository's folder:
```python
ROOT = r"C:\path\to\dwf2bim"
exec(open(ROOT + r"\revit\build_native.py", encoding="utf8").read(), globals())
```

The script stops if the file already has walls it did not create. In a Python Script node, add one of these above the `exec` line:

| Line | Effect |
|---|---|
| `CLEAR_EXISTING = True` | **Deletes the file's whole model** (walls, floors, doors, windows, stairs, railings, furniture and so on), including what an earlier run built, then builds. Types, levels and views stay. Use only on a saved copy |
| `ALLOW_EXISTING = True` | Builds next to the existing model |
| `BUILDING = r"<folder>"` | The full path of the folder holding `building.json`, or a project name under `OUTPUT/` |

Running it again skips elements that already exist, identified by `DWF:<id>` in Comments.

Writes `OUTPUT/<name>/revit_build_log.json`: counts, failures, which types were substituted or duplicated, Revit's messages (`revit_messages`), and doors or windows whose wall was not cut (`openings_not_cut`).

### The areas
The build runs the areas too, right after the model. Set `SKIP_AREAS = True` above the `exec` line to leave them out. To run them again alone:
```python
ROOT = r"C:\path\to\dwf2bim"
exec(open(ROOT + r"\revit\build_areas.py", encoding="utf8").read(), globals())
```
Each area plan of the scheme is cleared, then gets the boundaries of the DWF's numbered cells, unchanged, and an Area in each. Plans of levels with no floor in the DWF are cleared and left empty, because what they hold came from another project. **Lines and Areas drawn by hand in those plans are deleted too.** Optional lines above the `exec`: `SCHEME_NAME = "..."`, `SNAP_M = 0.10` to snap to wall faces, `KEEP_PREVIOUS = True` to keep what is there. Writes `OUTPUT/<name>/revit_areas_report.txt` and `revit_areas_result.json`.

To compare with the DWF, from the project folder: `python -m dwf2bim compare-areas OUTPUT\<name>`. It writes `AREA_COMPARISON.md` and `qa/area_compare_<level>.png`: green fits, red is only in Revit, orange is only in the DWF.

### Export the catalog (changes nothing in the file)
Run this in a Python Script node on your template, to see what it offers when you write `config/revit_mapping.json`:
```python
ROOT = r"C:\path\to\dwf2bim"
exec(open(ROOT + r"\revit\export_catalog.py", encoding="utf8").read(), globals())
```
Writes `revit/catalog/<document>.json`: levels, wall and floor types with layers, railing types, door and window families with their parameters.

## What gets created

| Source | In Revit | Type comes from |
|---|---|---|
| Straight wall | Real wall with level constraints | Same name and thickness, else a similar type duplicated and resized |
| Floor | Real floor | Same way |
| Door, window | Real family, hosted in its wall | Same family, else `opening_families` in the mapping, else the first available |
| Railing | Real railing on a path | Same name, else mapping, else the first available |
| Curved wall | Short real walls, end to end | Same as a straight wall |
| Stair | Real stair by component: runs and an automatic landing | The file's default stair type |
| Thin horizontal plate of another family | Real floor of a type named `DWF floor <thickness>` | Duplicated and resized |
| Posts, bars and masses already covered by a slab | Not built, listed in the report under "Masses left out" | |

Everything is native. An element that cannot be built natively is listed in the log under `failed`, and is never replaced by fixed geometry. `DWF floor` types are easy to select by type and replace.

## Site
Revit's API has no way to create a Property Line, so the build draws 2D lines on the styles named in `site_line_names` in `config/revit_mapping.json`. To turn them into Property Lines:

1. Open a plan of the ground floor. Massing & Site > Property Line > Create by sketching.
2. Pick Lines, then Tab to select the closed chain on the plot line style. Finish, and check that the area reads 806 m2.
3. Do the same for the building line style: 316 m2 for the closed line.

Last of all the script sets the shared coordinates (ITM, true north, elevation) from the survey fit and moves the Project Base Point to a plot corner. The result is in the log under `placement`.

## Levels
The build creates the DWF's levels under their DWF names, each with a floor plan. A level of the file is never taken over for another height. Levels the DWF does not have are deleted with their views, when the file was cleared or holds no model. Revit cannot delete the active view, so run from a 3D view; a level that could not be deleted is listed in the node's output and in the log under `levels_not_deleted`.

## Mapping
`config/revit_mapping.json` connects DWF names to template names.

| Key | Meaning |
|---|---|
| `wall_name_rules`, `floor_name_rules` | Regex from the DWF type name to the template's naming style |
| `wall_types`, `floor_types` | Exact names, which win over the rules |
| `wall_base_by_word`, `floor_base_by_word` | Which type to duplicate when a new one is needed. The first word found in the DWF name wins, in the listed order |
| `opening_families` | DWF family to template family, with `type_format` for type names such as `{w}w/{h}h`. A family may be of the other category |
| `railing_types`, `default_railing_type` | Railing type names |

## Floors, plaster and joins
- A floor whose DWF name has fill (`מילוי`) or a balcony (`מרפסת`) gets a type made from `finished_floor.base` in `config/revit_mapping.json`: the concrete is the number in its name, the tiles stay 2 cm, and the fill takes the rest of the DWF thickness.
- A wall marked exterior gets a type named `<core type> + טיח חוץ 2`, with 2 cm of the material `טיח חוץ` on the outside. Its location line is the core centreline, so the core stays on the DWF's axis, and it is turned so the plaster faces out. Interior walls have no plaster.
- After the walls and floors, each floor is joined with the walls that cross it. Each wall is also joined with the wall it stands on. The log's `joins` gives the counts.
- Ground level exterior walls get plaster too.
- Last of the model, every wall is joined with each wall it stands on or runs into. Revit undoes a join that would stop a door or window cutting its wall.
- A curtain wall inferred for doors the DWF hosted in an unexported curtain wall takes the type `curtain_wall_type` from the mapping. It is split at the doors' edges, and those panels become the doors' family.

## Views
- Each level's floor plan shows its DWF plan, imported from `model/views/<plan>.dxf` into that view only. Delete the import to hide it; a later run does not add it twice.
- Each section line in the DWF's plans becomes a section view named `חתך <letter>` (`section_name` in the mapping). If one looks the wrong way, flip it in Revit and tell us which.
- Scope box: Revit's API cannot create one. Draw a scope box around the building once, name it `DWF` (`scope_box_name` in the mapping), and run again: every plan and section takes it. Until then, plans are cropped to the plot.

## Checking the result
The log lists walls under `walls_moved_by_revit` when their ends are over 30 cm from the DWF's, and under `walls_not_from_the_dwf` when they lack its stamp in Comments.

## Known limits
- Door swing and window facing are not read from the DWF; flip them in Revit where needed.
- Railings are created at one height along the path, so a railing on a sloped stair needs hosting on the stair.
- A stair's way up is read from the plans. Stairs where this could not be checked are in the log under `stairs_direction_unverified`.
- Elements' Comments are used for the `DWF:<id>` stamp.
