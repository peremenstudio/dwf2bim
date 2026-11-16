# Product specification

What dwf2bim is for, what a finished model must be, and the order in which the missing parts are built. [BUILD_PROTOCOL.md](BUILD_PROTOCOL.md) describes the stages as they are today; this document says where they are going and why. When a milestone here is finished, its rules move into the protocol and its row is marked done.

Written on 2026-11-16, from a review of the code and of fresh builds of the two test projects.

## 1. Purpose

A person enters a block and a parcel. When the archive holds the three documents (the architecture plan, the area plan, the survey map), the tool writes a Dynamo graph, and running that graph in Revit gives a native model of high quality.

The models are not an end in themselves. They feed a second system, which checks a model against the planning rules that apply to its parcel. Three things follow:

1. **Many models.** The tool runs on a list of parcels with no one watching each run.
2. **No hand repair.** A model that needs a person to fix floors or delete stray walls is not finished. The tool must either build it right or say plainly that it could not.
3. **A model that tells how far to trust it.** Every model carries a verdict the second system can read before it checks anything.

The model must match the permit drawings. Correct IFC classes and quantities matter less than geometry, heights, openings, areas and position.

## 2. What a good model is

A model can pass every schema check and still look machine-made: a wall in mid air, a floor in pieces, a tab of slab past a facade. The standard here is that **the model looks as if a person built it from the drawings.** That standard is made of rules a program can test. Each has a short name, used in the reports and in the roadmap.

| Rule | A person would... | Tested by |
|---|---|---|
| STANDS | put every element on something: a wall on a floor or on the wall below, a floor on walls, a railing on a floor edge, a stair between two floors | `support.py`, done |
| IN-WALL | put every door and window in a wall, inside that wall's height | `support.py`, done |
| ONE-FLOOR | draw one main floor per storey, and one floor per balcony, never pieces | floor outlines per storey |
| HOLES | open a floor only for a stair, a lift, a shaft or a void, and floor everything else, the lobby included | `contours.holes_with_a_reason`, done |
| EDGE | end the main floor at the outer face of the exterior walls' core, and a balcony at its drawn edge, with no tab and no sliver | outline check, M2 |
| STACK | run walls from floor to floor and stand them on the walls below, so facades are flush | modelling rules 2 and 6 |
| NO-DOUBLE | never model the same wall twice, in plan or in height | overlap check, M3 |
| CARRIED | carry what hangs over open space: columns under a storey on pilotis, beams where the plan shows them | structure class, M3 |
| TYPICAL | make storeys that share one plan identical | typical floor copy |
| IN-PLOT | keep the building inside the plot, and mark what is outside it, such as a sidewalk, as site | plot check, M3 |
| SOURCED | take every height and thickness from a Revit property when one exists, and say when it was inferred | `height_source`, `geometry.source` |

A rule that is broken is never hidden. The element is listed in the report under the rule's name, shown in red in the previews, and counted in the verdict.

## 3. The flow

```
block + parcel
   -> fetch          the three documents, the person approves the archive's terms
   -> read           W2D or XPS graphics, Revit object data, views
   -> frame          units, levels, plan alignment, placement on the survey
   -> storey sheets  one clean 2D drawing per storey   <- the gate that decides the model's quality
   -> model          building.json, made from the storey sheets
   -> check          support, outlines, sources -> quality.json, BUILD_REPORT.md, previews
   -> Dynamo graph   -> Revit -> revit_build_log.json -> final verdict
```

### 3.1 Storey sheets: the 2D drawing comes first

Experience on both test projects: when the floor outlines of a storey are right, the storey's 3D is right, and when they are wrong, no later rule repairs it. So the 2D stage is the product's centre, with its own output and its own gate.

For each storey the tool writes one **storey sheet**: a DXF and a PNG, in the model's coordinates, holding only what the model is built from.

| Layer | Holds |
|---|---|
| `DWF2BIM_MAIN_FLOOR` | The closed outline of the storey's main floor |
| `DWF2BIM_BALCONY_FLOOR` | One closed outline per balcony |
| `DWF2BIM_SHAFT` | Each opening in the main floor, with its reason: stair, lift, shaft, void |
| `DWF2BIM_WALL` | Wall centre lines, exterior and interior apart |
| `DWF2BIM_OPENING` | Door and window positions on their walls |
| `DWF_<CLASS>` | The source drawing, one layer per class, for comparison |

The source drawing's dimensions are left out: they carry nothing the model uses and they hide the outlines. Other annotation sits on its own layers (`DWF_NOTES`, `DWF_TAGS`, `DWF_SPOT_HEIGHTS`, `DWF_SECTION_MARKS`) and can be switched off. This part is done.

**How an outline is decided.** Three sources say where a storey's floor is. They are used in this order, and the sheet records which one decided each edge:

1. The area plan's cells, where the plan sits on the walls (80% of the walls or more). Its codes also say what each cell is: a balcony (2001, 2002, 2004), a stair or lobby (1005), a lift (1010), a void (-1).
2. What the storey's full-height walls close.
3. The drawn slabs.

**The gate.** A storey sheet passes when:
- the main floor is one closed outline, with no edge under 12 mm, no spike and no slit (Revit refuses these);
- its area is within 3% of the area plan's cells for that storey, when the plan is trusted;
- every hole has a reason: it holds a stair, a lift, or an area plan cell coded as a void, a stair or a lift. A hole with no reason is closed and listed (rule HOLES);
- every balcony touches the main floor and has a drawn slab or a balcony cell behind it;
- no part of the main floor lies more than 20 cm outside the walls unless a drawn slab continues there (rule EDGE).

A storey that fails the gate is still built, from the best outline available, and the failure goes into the verdict.

**Corrections.** A person, or an agent reviewing the PNG, can correct a storey sheet without touching code. The correction lives in the project folder, in `overrides.json`, and names itself as a project exception:

```json
{"storeys": {"L06": {"main_floor": [[x, y], ...], "close_holes": [[x, y]], "balconies_add": [[[x, y], ...]]}},
 "elements": {"8104800": {"leave_out": "a beam drawn as a wall, already in the slab"}}}
```

A build reads `overrides.json` when it exists, applies it after the outlines are computed, and lists every applied override in the report. An override never changes what another project gets. When the same override is needed on a second project, that is a sign of a missing general rule.

### 3.2 The verdict

Each build writes `model/quality.json`. This part is done; the Revit half is M4.

```json
{"verdict": "ready | review | failed",
 "reasons": ["unsupported_share is 0.021, over 0.0"],
 "metrics": {"unresolved_share": 0.031, "unsupported_share": 0.021, "placed_on_the_survey": true, "...": "..."}}
```

- `ready`: nothing passes its limit. The second system may use the model.
- `review`: a person should look first. The reasons say where.
- `failed`: not to be used as it is.

The limits are in `config/site.json` under `quality`. After the Revit run, `python -m dwf2bim verdict <project folder>` reads `revit_build_log.json` and adds what Revit decided: elements it removed, walls it moved, doors and windows that did not cut. The final verdict is the worse of the two.

### 3.3 Many parcels

`python -m dwf2bim batch <file>` takes a list of block and parcel pairs and a save folder. For each it fetches, builds and records the verdict, and goes on when one fails. It writes `batch_report.md` and `batch_report.json`: one row per parcel with the documents found, the verdict, the reasons and the build time. A second run skips parcels already built from the same documents.

The archive's terms are approved by the person, in the browser, once in 7 days. The batch waits for that approval and never gives it.

The Revit step stays a person's click per model for now. Running Revit unattended (a journal file, or Revit's batch tools) is M6, after the models themselves are right.

### 3.4 What the second system may rely on

| In the model | From | Stated in |
|---|---|---|
| Levels and storey heights | Revit level properties, else sections | `levels[].source` |
| Walls, floors, openings, stairs, railings | The plans, with Revit properties | `geometry.source`, `height_source` |
| Exterior walls and floors | The storey outlines | `exterior`, IFC `IsExternal` |
| Areas with their codes, per storey | The area plan's table and cells | `areas[]` |
| Apartment number per area | The area table, else inferred | `apartments.source` |
| Plot line, building lines | The survey map | `site` |
| Position in ITM, rotation, datum | The survey fit | `site.placement` |
| The verdict | The checks | `quality.json` |

Anything inferred says so. The second system should treat an inferred value as weaker evidence than a read one.

## 4. Where the tool stands

Measured on 2026-11-16, on builds made that day.

| | 6625/538 (DWF) | 6952/149 (DWFX, another office) |
|---|---:|---:|
| Build time | 64 s | 40 s |
| Walls, floors, doors and windows built | 689, 73, 234 | 950, 128, 393 |
| Unresolved, without mullions and panels | 39 | 147 |
| Elements standing on nothing | 6 walls | 7 walls |
| Doors and windows with no wall or outside it | 0 | 31 |
| Unclassified families | 1 | 23 |
| Placed on the survey | yes, 2 cm | no |
| Area plan fit, worst storey | 41% (ground) | 22% (L07) |
| Verdict | review | review |

What makes these models look machine-made, in the order of what it costs the look:

1. **Holes in the main floor where there is floor.** On 6952 the hole around the stair is 22 to 24 m2 on every storey and takes in the lobby, and on L06 a bathroom too. On 6625 L01 the main floor has six holes, two of them 46 and 30 m2. The drawn slabs there were not rebuilt, and the outline keeps any hole no slab covers. Rule HOLES, built the same day, floors the parts with no reason: 2.7 to 3.6 m2 of each such hole on 6952, and the small holes with nothing in them. The landing between the stair and the lift stays open with the stair, which still needs a look in Revit.
2. **Columns and beams are not built.** The `structure` class is switched off in `config/classification.json`, so a storey on pilotis hangs in the air, and beams drawn as short walls hang under slabs.
3. **Walls doubled in height.** Roof walls keep their drawn base, 40 cm under the roof level, while the walls below were run up to that level. Both stand in the same 40 cm.
4. **Stray pieces.** On 6625 a curved wall stands alone outside the building and four roof walls have nothing under them. Pipe sleeves on 6952 (`8" AC`) sit 12 to 28 m under their storey, because their sill is read as a height above sea level.
5. **Site elements taken as building.** On 6952 the sidewalks and the road, 617 m2 outside the plot, are floors of the model.
6. **Outlines that follow drawing noise.** A sliver 5 cm wide and a small tab remain on 6952 L04 at the corner where a slanted facade meets a straight one.
7. **Ground and basement floors built from fill pieces**, 9 to 17 a level, rather than from one outline per height as the upper storeys are.

Built on 2026-11-16 after the decision on holes: a void or lift cell of a trusted area plan opens the floor even where a slab is drawn across it, and a lift cell counts on every storey between the lowest and highest that show it.

Still open: **a lift drawn on a slab, on a storey whose area plan does not fit.** On 6952 L06 a slab is drawn across the lift shaft, so the main floor closes it. Opening the walled room around every drawn lift or shaft symbol was tried and withdrawn: the symbols also stand in bathrooms and bedrooms, and whole rooms lost their floor. The area plans of both projects code the lift as a void (-1) on the upper storeys, and on 6952 L06 and L07 the plan holds under 30% of the walls, so it is not trusted there. The fix is to fit those panels (set-back storeys) better, which is the area check of M2.

Process faults found in the same review:

- `build <folder>` refused a project whose building is a DWFX. Fixed.
- A build could only be judged by reading a 350-line report and opening images. The verdict and the red previews now say where to look.
- Nothing compares a new build with the last one, so a fix for one project can quietly harm another. See M5.
- 7047/44 is refused: its DWF holds no Revit object data. The geometry-only route in the protocol's known limits is still open, and is outside this roadmap.

## 5. Roadmap

Each milestone is general (all models) unless it says otherwise. Each ends with tests that use no project's numbers, a rebuild of every corpus project, and no corpus verdict getting worse.

| | Milestone | Done when |
|---|---|---|
| M1 | **See the faults.** Support check, verdict, red previews, DXF layers without dimensions | Done 2026-11-16 |
| M2 | **Storey sheets.** The outline gate of 3.1. Done 2026-11-16: holes need a reason, `DWF2BIM_SHAFT`. Open: the area check, the edge check, wall and opening layers, `qa/storey_<level>.png`, `overrides.json`, and a lift drawn on a slab (below) | Every storey of both corpus projects passes the gate or is listed with its reason; the lobby on 6952 L04 to L06 is floored |
| M3 | **Built as a person builds.** Columns and beams built natively, loading families from Revit's library when the template lacks them; ground floors continuous over the plot, with only clearly drawn holes open (the two kinds, paved and for seepage, are done 2026-11-16); one wall per place in height (NO-DOUBLE); ground and basements from one outline per height; site elements outside the plot marked as site (IN-PLOT); openings whose height makes no sense left out and listed | `unsupported_share` is 0 on both projects, with no element dropped silently |
| M4 | **Revit closes the loop.** `verdict` command merges `revit_build_log.json`; the Dynamo graph's result shows the verdict; a wall Revit moved or an element it removed counts | A model's final verdict is known without opening the model |
| M5 | **Do not go backwards.** `python -m dwf2bim regress <corpus folder>` rebuilds every project there and compares each `quality.json` with the one kept from the last accepted build | A change that worsens any metric on any corpus project is shown before it is committed |
| M6 | **Many parcels.** The `batch` command of 3.3, then Revit unattended | A list of 20 parcels runs to a report with no one watching |

M2 comes first because it decides the most. M5 can be built at any point, and the sooner the better: it is small, and every later milestone leans on it.

### A Revit MCP server

Connecting Claude to Revit through an MCP server is worth it for one thing: closing the loop of M4 without a person. With it an agent can run the build, read Revit's warnings, query the built elements and take views of the model, and so see what the previews cannot show. It should not be used to repair a model by hand. A repair made inside one Revit file is lost on the next build and teaches the tool nothing; every fault found through the server goes back into the code as a rule or a check. Add it when M2 and M3 are done, not before: until then the faults are visible without Revit.

### Notes for whoever builds a milestone

- Read `CLAUDE.md` first. Every fix is either for all models or a named exception for one.
- The order of the modelling rules matters; `conventions.apply()` is the one place that sets it. Add a rule there, with a line in the report saying what it changed.
- An element is modelled, excluded by configuration, or listed as unresolved. A new check lists; it does not delete.
- A threshold goes in `config/site.json`, not in code.
- The corpus is not in the repository: project files are private. It is a folder on the machine, one sub-folder per `<block>_<parcel>`, each with its documents and `fetch_manifest.json`.
- Look at the previews after every rebuild. The verdict counts faults it knows; a person's eye finds the ones it does not, and each of those becomes a new check.

## 6. Decisions

Answered by the product owner on 2026-11-16.

| Question | Decision |
|---|---|
| Columns and beams (M3) | Built as native Revit structural columns and framing |
| Families the template lacks | The build may load them. First choice is Revit's own library, the one Insert > Load Family opens, so the template stays light. A loaded family is the base for an element the template has no type for, and each load is listed in the build log |
| Holes in floors | As in reality, and as the area plan says: no floor in a lift, and none over a double-height space on its upper storey. The area plan numbers these cells, so its codes decide. A slab drawn across such a cell does not floor it |
| Ground floors | Continuous over the whole plot. The ground level tells paving from ground meant for seepage, such as soil or lawn, as two kinds of floor. A part the drawing leaves empty, usually under dense linework, is filled. Only what is clearly drawn as a hole stays open, a sunken court included |
| Downloaded files | Named `<block>_<parcel>_<kind>_<date filed>`, and the page says each one arrived, with its date in the archive |
| Sidewalks and roads outside the plot | Left out of the model, and listed in the report as outside the plot |
| A storey that fails its gate | Built from the best outline, and the model is marked `review` with the reason. A build never stops to wait for an override |
| What the second system reads | The IFC exported from Revit. So the verdict and each element's sources must be written as Revit parameters that reach the IFC, not only into `quality.json` (M4) |

Still open:

1. **The limits of `ready`.** Today any element standing on nothing makes the model `review`. Is a model with one stray wall usable by the second system, or must `ready` mean zero?
