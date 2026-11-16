---
name: fix-model
description: Change how models are built - fix a fault found in a review, add a modelling rule, a family, a term or a check - without harming other projects. Use for any change under src/dwf2bim, revit/ or config/ that alters a model.
---

# Fix how models are built

## 1. Say the scope before writing
State it in one line, and repeat it when reporting: **all models**, or **this model**.

| Scope | Goes in | Test |
|---|---|---|
| All models: a reading bug, a rule of how buildings work, a Revit limit | Code or `config/` | A unit test with made-up numbers, no project's |
| This model: one project's odd drawing | A named project exception, never a general rule | The project's rebuild |

## 2. Where things live

| Change | Place |
|---|---|
| A family to a class | `config/classification.json` (first matching rule wins) |
| A Hebrew term in a name | `config/glossary.json` |
| An archive document type | `config/archive.json` |
| A threshold, an area code, a verdict limit | `config/site.json` |
| A DWF type name to a template type | `config/revit_mapping.json` |
| Reading the file | `whip.py` (DWF), `xps.py` and `dwfx.py` (DWFX), `package.py` |
| Views, plan alignment, heights | `views.py` |
| Elements from the plans | `extract.py` |
| Survey, plot, area plan, slabs against areas | `site.py` |
| Floor outlines per storey | `contours.py` |
| Modelling rules and their order | `conventions.py`, `apply()` |
| What stands on nothing, the verdict | `support.py` |
| Report, images, DXF | `qa.py` |
| The Revit build | `revit/build_native.py`, `revit/build_areas.py` |
| A message the person reads | English in the code, Hebrew in `MESSAGES` in `src/dwf2bim/ui.html` |

## 3. Rules that are not negotiable
- An element is modelled, excluded by configuration, or listed as unresolved. Never dropped silently. A rule that removes or changes elements adds a line to the report saying what it changed.
- A Revit property beats a measurement, and a measurement beats an inference. Record the source on the element.
- The decoder raises on an unknown opcode. Never make it skip.
- No project's numbers in code or tests. Code, names and documents in English.
- A new check lists faults. It does not delete elements to make the count go down.

## 4. Prove it
```powershell
$env:PYTHONPATH = "src"
python -m unittest discover tests
```
Then rebuild **every** project in the corpus, not only the one that showed the fault. The corpus is the folder in `.dwf2bim_settings.json` (`save_folder`): each `<block>_<parcel>` sub-folder with documents. Build into a scratch folder, so the person's models stay as they are until they ask:

```powershell
python -m dwf2bim build "<scratch>\proj\<block>_<parcel>" --out "<scratch>\out"
```

Compare each project's `quality.json` and report section counts with those from before the change. Look at the images of the storeys the change touches. A metric that got worse on any project is a finding to report, not to hide.

## 5. Finish
- When stages or rules changed, update `docs/BUILD_PROTOCOL.md`. When a roadmap item is done, mark it in `docs/PRODUCT_SPEC.md`.
- Report in Hebrew: the scope, what changed, the numbers before and after on each corpus project, and what is still open.
- Commit only when asked. The repository is public: check `git status` and what is staged, and keep project files out.
