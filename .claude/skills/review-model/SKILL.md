---
name: review-model
description: Review a built model for quality, find what makes it look machine-made, and say for each fault whether it is a general gap or a quirk of this project. Use after a build, after a Revit run, or when the person says a model looks wrong (for example "הרצפה לא נכונה", "יש קירות באוויר").
---

# Review a model

The standard is in `docs/PRODUCT_SPEC.md`, section 2: the model looks as if a person built it from the drawings. Each rule there has a short name. Name the rule for every fault you report.

## 1. Collect
From `<project folder>/model/`:

- `quality.json`, `BUILD_REPORT.md`
- `qa/preview_3d*.png`, `qa/contour_<level>.png`, `qa/overlay_<level>.png`, `qa/site_plan.png`, `qa/area_plan_*.png`
- `revit_build_log.json`, when the model was run in Revit. Its date must be later than `building.json`'s, or it describes an older build: say so.

If the build is older than the last code change, rebuild first (build-model skill).

## 2. Look, storey by storey
Read every contour and overlay image. Do not sample. For each storey ask:

| Question | Rule |
|---|---|
| Is the main floor one outline that follows the outer face of the exterior walls? | ONE-FLOOR, EDGE |
| Is each balcony its own outline, and is anything drawn as a balcony missing? | ONE-FLOOR |
| Does every hole hold a stair, a lift or a shaft? Is a lobby, corridor or room left without floor? | HOLES |
| Does any tab or sliver of floor stick out past a wall? | EDGE |
| Do the walls in the overlay sit on the drawn walls? Is a drawn wall missing? | SOURCED |
| Are doors and windows on walls (magenta), or loose (red)? | IN-WALL |

In the 3D previews: red elements (STANDS), a storey with nothing under it (CARRIED), walls doubled where storeys meet (NO-DOUBLE), anything outside the plot (IN-PLOT), storeys that should match and do not (TYPICAL).

To look closer than the images allow, load `building.json` and measure. Elements are in metres, with `class`, `level`, `z0`, `z1` and `geometry`; `dwf2bim.support.footprint(rec)` gives a plan outline. Write throwaway scripts in the scratch folder, not in the repository.

## 3. From the Revit log
- `elements_removed_by_revit`, `openings_not_cut`, `walls_moved_by_revit`: each is a fault in what the tool sent, not Revit's whim. Find why.
- `revit_messages`: counts of each warning and error.
- `classes.*.failed` and `substitutions`: a type missing from the template is a template or mapping matter (`config/revit_mapping.json`), not geometry.

## 4. Decide the scope of each fault
Follow `CLAUDE.md`, "For each fix: this model, or all models?".

- **All models**, the default: the tool read or reasoned wrongly, and another project would get the same. It becomes a code or config change with a test.
- **This model**: the drawings themselves are odd in one place. It becomes a named project exception.
- Not sure: ask the person.

## 5. Report, in Hebrew
A table, worst first: where (level and element id), what is seen, the rule, the scope, the proposed fix. Then the counts: unresolved, inferred, standing on nothing. Give the image file for each row so the person can open it.

A fault the checks did not catch is the most useful finding: propose the check that would catch it next time.
