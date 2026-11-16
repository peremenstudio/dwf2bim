---
name: build-model
description: Build a Revit-ready model from a block and parcel, or from a project folder that already holds the permit documents. Use when asked to build, rebuild or run a model, in any language (for example "תבנה מודל לגוש 6625 חלקה 538").
---

# Build a model

The steps are fixed. Do not skip the look at the images: a schema-valid model can still be wrong.

## 1. Where to save
Ask, unless the person named a folder or a project folder. The last one used is in `.dwf2bim_settings.json` (`save_folder`); offer it.

## 2. Run
In PowerShell, from the repository root:

```powershell
$env:PYTHONPATH = "src"
python -m dwf2bim run <block> <parcel> --folder "<where>"            # downloads, then builds
python -m dwf2bim run <block> <parcel> --folder "<where>" --local    # the documents are already in <where>\<block>_<parcel>
```

- Without `--local` a browser opens on the city archive. **The person approves the archive's terms there. Never click that approval, and never work around it.** Tell them the window is waiting, and wait.
- A build takes one to two minutes. Run it in the background and keep the log.
- To try a code change without touching the person's project folder, copy the documents and `fetch_manifest.json` to a scratch folder and run `python -m dwf2bim build <scratch project folder> --out <scratch out>`.

If the run stops:

| Message | Meaning | Do |
|---|---|---|
| `No usable document in the archive for: architecture` | No plan in a readable format | Report it. The model cannot be built |
| `has no Revit object data` | The DWF was not exported from Revit with object data | Report it. Not supported yet |
| `unknown opcode`, or an unknown W2X record | The decoder met something new | Do not make it skip. Fix the decoder, see the fix-model skill |

## 3. Read the result
In `<block>_<parcel>/model/`, in this order:

1. `quality.json`: the verdict (`ready`, `review`, `failed`) and its reasons.
2. `BUILD_REPORT.md`: "Verdict", "Standing on nothing", "Inferred geometry", then the unresolved table.
3. `qa/preview_3d.png` and `qa/preview_3d_back.png`: red elements stand on nothing.
4. `qa/contour_<level>.png` for every storey: the main floor in red, balconies in orange. Check that the main floor follows the outer walls, that each balcony is separate, and that every hole is a stair, a lift or a shaft.
5. `qa/overlay_<level>.png`: the model drawn on the source plan.

For anything beyond a first look, use the review-model skill.

## 4. Report to the person, in Hebrew
- The verdict and each reason, in plain words.
- The counts of **unresolved** and **inferred** elements, and of elements standing on nothing. Not only what worked.
- Documents missing or taken from another online request.
- The next step: in Revit, open a new project from the template, open `<block>_<parcel>\Build_<block>_<parcel>.dyn` in Dynamo, and press Run.

Do not say the model is good because the build finished. Say what the verdict and the images show.
