# dwf2bim

Builds a native Revit model from a building's permit documents. All code, names and outputs are in English; the user speaks Hebrew.

## When asked to build a model
1. From a block and parcel, with `PYTHONPATH=src`: `python -m dwf2bim run <block> <parcel>`. The person using the tool approves the archive's terms in the browser window. Never click that approval for them. Or put the files in `DWF/<block>_<parcel>/` and add `--local`.
2. Read `OUTPUT/<name>/BUILD_REPORT.md`, then look at `qa/overlay_*.png` and `qa/preview_3d.png`. Schema validity does not show that the model is right.
3. Report the unresolved and inferred counts, not only the successes.
4. In Revit, the person opens `OUTPUT/<name>/Build_<name>.dyn` and presses Run.

Follow [docs/BUILD_PROTOCOL.md](docs/BUILD_PROTOCOL.md). Change the protocol there when the stages change.

## Rules
- A Revit property beats a measurement, and a measurement beats an inference. Each element records its source.
- Never drop an element silently: it is either modelled, excluded by configuration, or listed as unresolved.
- A new family goes in `config/classification.json`, a new term in `config/glossary.json`, an archive document type in `config/archive.json`. Do not hard-code them.
- The decoder must raise on an unknown opcode. A silent desync once made it stop at 5 of 73 views.

## For each fix: this model, or all models?
Decide before writing it, and say which it is when reporting it.
- **All models** is the default: a bug in reading, a rule of how buildings work, a Revit limit. It goes in the code or the config, with a test that does not depend on this project's numbers.
- **This model** is a quirk of one project's drawings, such as one mistaken family name. It must not change what other projects get, and must be named as a project exception, never as a general rule. If unsure, ask the user.

## Git
The repository is public, so project files stay out of it: `DWF/`, `OUTPUT/`, `TEST/`, `reference/` and `revit/catalog/` are ignored. Check what is staged before each commit.

Tests: `python -m unittest discover tests` with `PYTHONPATH=src`.
