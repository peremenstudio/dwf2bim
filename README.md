# dwf2bim

Builds a native Revit model from the permit documents of a building in Tel Aviv. Enter a block and parcel. It downloads the newest documents from the city's engineering archive, reconstructs the building from them, and writes a Dynamo graph that builds the model in Revit.

## What you need
- Windows, with Chrome or Edge
- Python 3.11 or newer
- Revit 2026 with Dynamo, and a template with your wall, floor, door and window types

## Use

Double-click `start.bat`. The first run installs what it needs. A page opens in your browser:

1. Enter the block and parcel, then click **Get files and build**.
2. A browser window opens on the archive. Read its terms of use and approve them there. The tool reads nothing from the archive until you do, and the approval lasts 7 days.
3. Wait a few minutes. The page shows the documents it chose, what it built, and what needs checking.
4. In Revit, open a new project from your template, open a 3D view, and start Dynamo.
5. Open `OUTPUT/<block>_<parcel>/Build_<block>_<parcel>.dyn` and press **Run**. Set the graph's switch to True only to replace a model already in the file.

If the download does not work, put the files in `DWF/<block>_<parcel>/` yourself and tick **Use the files already in the project's folder**.

### Which documents it takes

| Document | Archive type | Format | Needed |
|---|---|---|---|
| Building | תכנית מתוקנת (אדריכלות) | DWF | Yes |
| Area plan | חישוב שטחים | DWFX | No, there are no areas without it |
| Survey | מפת מדידה להיתר | DWFX | No, there is no site or true position without it |

It takes the newest of each type in a format it can read, and warns when they come from different requests. The choices are recorded in `DWF/<project>/fetch_manifest.json`. Files replaced by a newer download move to `DWF/<project>/_previous/`. To change the types, edit `config/archive.json`.

### About the archive
The archive's documents are photographic copies, not certified documents, and may hold errors or be out of date. Some are protected by copyright, and their use is your responsibility. These terms are the city's, so read them in the archive. Downloaded files stay on your machine and are not part of this repository.

## Command line

```powershell
$env:PYTHONPATH = "src"
python -m dwf2bim ui                       # the page
python -m dwf2bim run 6625 538             # download and build
python -m dwf2bim run 6625 538 --local     # build from the files already in DWF/6625_538
python -m dwf2bim fetch 6625 538           # download only
python -m dwf2bim build DWF\6625_538       # build a folder
python -m dwf2bim compare-areas OUTPUT\6625_538   # compare Revit's area plans with the DWF
```

## Output

`OUTPUT/<project>/`

| Path | Content |
|---|---|
| `Build_<project>.dyn` | The Dynamo graph to run in Revit |
| `BUILD_REPORT.md` | What was built, what was inferred, what is missing. Read this first |
| `building.json` | Neutral model, in metres, which Revit builds from |
| `<file name>.ifc` | The same model as IFC, to check it without Revit |
| `views/` | Each view of the sheet as PNG and DXF |
| `qa/` | Plan overlays and 3D previews |

## Folders

| Folder | Content | In Git |
|---|---|---|
| `src/dwf2bim/` | Download, reading, reconstruction, reports, the page | Yes |
| `revit/` | Scripts that run inside Revit. See [revit/README.md](revit/README.md) | Yes |
| `config/` | Everything that changes without code. See below | Yes |
| `docs/` | [The build protocol](docs/BUILD_PROTOCOL.md) | Yes |
| `tests/` | `python -m unittest discover tests`, with `PYTHONPATH=src` | Yes |
| `DWF/`, `OUTPUT/`, `TEST/`, `reference/`, `revit/catalog/` | Project files on this machine | No |

## Configure

| File | Controls |
|---|---|
| `config/archive.json` | The archive's address, and which documents make a project |
| `config/classification.json` | Which families become which class, and which classes are modelled |
| `config/glossary.json` | Name translation |
| `config/site.json` | Area codes, survey markers, slab and apartment rules |
| `config/revit_mapping.json` | How DWF names map to your template's types |

## Known limits
- Only Tel Aviv's archive, and only DWF exported from Revit.
- A parcel with several building files may mix documents. The request warning is the guard, so check it.
- A model always needs checking against its sources. Schema validity does not show that it is right.
