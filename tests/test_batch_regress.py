"""The regress and batch commands. No real build runs here: the build step is patched. Numbers belong to no project."""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from dwf2bim import cli, fetch, package


def quality(verdict="ready", **metrics):
    base = {"unresolved_share": 0.05, "unsupported_share": 0.0, "openings_not_in_a_wall_share": 0.0, "inferred_share": 0.2,
            "floors_off_their_area_share": 0.0, "weak_plan_links": 0, "unclassified_families": 0, "placed_on_the_survey": True,
            "area_plan_levels_not_placed_surely": [], "storeys_off_their_area_plan": []}
    base.update(metrics)
    return {"verdict": verdict, "kind": "full", "reasons": [], "metrics": base, "unresolved_by_class": {}, "unsupported_by_class": {}}


def quiet(fn, *args, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


class CompareQuality(unittest.TestCase):
    def status(self, before, after):
        return cli.compare_quality(before, after)["status"]

    def test_no_baseline_is_new(self):
        self.assertEqual(self.status(None, quality()), "new")

    def test_identical_is_same(self):
        self.assertEqual(cli.compare_quality(quality(), quality()), {"status": "same", "changes": []})

    def test_verdict_order(self):
        self.assertEqual(self.status(quality("review"), quality("ready")), "better")
        self.assertEqual(self.status(quality("ready"), quality("review")), "worse")
        self.assertEqual(self.status(quality("review"), quality("failed")), "worse")
        self.assertEqual(self.status(quality("failed"), quality("review")), "better")

    def test_lower_share_is_better_and_tolerance_holds(self):
        self.assertEqual(self.status(quality(unresolved_share=0.05), quality(unresolved_share=0.0505)), "same")
        self.assertEqual(self.status(quality(unresolved_share=0.05), quality(unresolved_share=0.08)), "worse")
        result = cli.compare_quality(quality(unresolved_share=0.08), quality(unresolved_share=0.05))
        self.assertEqual(result["status"], "better")
        self.assertEqual(result["changes"], [{"name": "unresolved_share", "before": 0.08, "after": 0.05, "change": "better"}])

    def test_counts(self):
        self.assertEqual(self.status(quality(weak_plan_links=0), quality(weak_plan_links=1)), "worse")
        self.assertEqual(self.status(quality(unclassified_families=3), quality(unclassified_families=1)), "better")

    def test_list_metrics_compare_by_length(self):
        self.assertEqual(self.status(quality(storeys_off_their_area_plan=["A"]), quality(storeys_off_their_area_plan=["B"])), "same")
        result = cli.compare_quality(quality(area_plan_levels_not_placed_surely=["A"]),
                                     quality(area_plan_levels_not_placed_surely=["A", "B"]))
        self.assertEqual(result["status"], "worse")
        self.assertEqual(result["changes"][0]["before"], 1)
        self.assertEqual(result["changes"][0]["after"], 2)
        self.assertEqual(self.status(quality(storeys_off_their_area_plan=["A", "B"]), quality(storeys_off_their_area_plan=[])), "better")

    def test_survey_flag(self):
        self.assertEqual(self.status(quality(placed_on_the_survey=False), quality(placed_on_the_survey=True)), "better")
        self.assertEqual(self.status(quality(placed_on_the_survey=True), quality(placed_on_the_survey=False)), "worse")

    def test_missing_or_none_values_and_other_metrics_are_skipped(self):
        self.assertEqual(self.status(quality(unresolved_share=None), quality(unresolved_share=0.9)), "same")
        before = quality()
        del before["metrics"]["weak_plan_links"]
        self.assertEqual(self.status(before, quality(weak_plan_links=5)), "same")
        self.assertEqual(self.status(quality(inferred_share=0.0), quality(inferred_share=0.9)), "same")

    def test_one_worse_outweighs_better(self):
        result = cli.compare_quality(quality("review", unresolved_share=0.2), quality("ready", unresolved_share=0.0, weak_plan_links=2))
        self.assertEqual(result["status"], "worse")
        self.assertEqual({c["name"]: c["change"] for c in result["changes"]},
                         {"verdict": "better", "unresolved_share": "better", "weak_plan_links": "worse"})


class Regress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.corpus, self.out = root / "corpus", root / "out"
        for name, suffix in (("1_1", ".dwf"), ("1_2", ".dwfx"), ("1_3", ".dwf")):
            (self.corpus / name).mkdir(parents=True)
            (self.corpus / name / f"plan{suffix}").write_bytes(b"x")
        (self.corpus / "notes").mkdir()  # no documents: not a project
        (self.corpus / "notes" / "readme.txt").write_text("x")
        self.baseline = self.corpus / "_baseline"
        self.baseline.mkdir()
        (self.baseline / "1_1.json").write_text(json.dumps(quality("ready")))
        (self.baseline / "1_3.json").write_text(json.dumps(quality("ready")))
        self.new = {"1_1": quality("review"), "1_2": quality("ready")}
        self.built = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_build(self, project, out_root):
        self.built.append(project.name)
        if project.name not in self.new:
            raise package.UnsupportedInput("no elements")
        target = out_root / project.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "quality.json").write_text(json.dumps(self.new[project.name]))
        return target

    def run_regress(self, accept=False):
        with mock.patch.object(cli, "build_folder", self.fake_build):
            return quiet(cli.regress, self.corpus, self.out, accept)

    def test_statuses_report_and_exit_code(self):
        self.assertEqual(self.run_regress(), 1)
        self.assertEqual(self.built, ["1_1", "1_2", "1_3"])
        report = {r["project"]: r for r in json.loads((self.out / cli.REGRESS_REPORT).read_text())}
        self.assertEqual({k: r["status"] for k, r in report.items()}, {"1_1": "worse", "1_2": "new", "1_3": "failed"})
        self.assertEqual(report["1_1"]["verdict_before"], "ready")
        self.assertEqual(report["1_1"]["verdict_after"], "review")
        self.assertIn("no elements", report["1_3"]["error"])
        self.assertFalse((self.corpus / "1_1" / "model").exists())
        # without --accept the baseline stays as it was
        self.assertEqual(json.loads((self.baseline / "1_1.json").read_text())["verdict"], "ready")
        self.assertFalse((self.baseline / "1_2.json").exists())

    def test_accept_writes_the_baseline_of_what_built(self):
        self.run_regress(accept=True)
        self.assertEqual(json.loads((self.baseline / "1_1.json").read_text())["verdict"], "review")
        self.assertTrue((self.baseline / "1_2.json").exists())
        self.assertEqual(json.loads((self.baseline / "1_3.json").read_text())["verdict"], "ready")

    def test_all_same_or_better_exits_zero(self):
        self.new["1_3"] = quality("ready")
        self.new["1_1"] = quality("ready", unresolved_share=0.0)
        self.assertEqual(self.run_regress(), 0)

    def test_baseline_folder_is_created_on_accept(self):
        for f in self.baseline.iterdir():
            f.unlink()
        self.baseline.rmdir()
        self.new["1_3"] = quality()
        self.assertEqual(self.run_regress(accept=True), 0)
        self.assertEqual(sorted(p.name for p in self.baseline.iterdir()), ["1_1.json", "1_2.json", "1_3.json"])

    def test_default_out_is_outside_the_corpus(self):
        with mock.patch.object(cli, "build_folder", self.fake_build), \
                mock.patch.object(cli.tempfile, "mkdtemp", return_value=str(self.out / "tmp")):
            quiet(cli.regress, self.corpus)
        self.assertTrue((self.out / "tmp" / cli.REGRESS_REPORT).exists())


class ParseParcels(unittest.TestCase):
    def test_separators_comments_and_bad_lines(self):
        text = "# list\n\n1 2\n3,4\n5/6\n7_8\n  9 \t 10  \n11 , 12\nnot a parcel\n13\n14 15 16\n"
        entries = cli.parse_parcels(text)
        good = [(e["block"], e["parcel"]) for e in entries if "error" not in e]
        self.assertEqual(good, [("1", "2"), ("3", "4"), ("5", "6"), ("7", "8"), ("9", "10"), ("11", "12")])
        bad = [e for e in entries if "error" in e]
        self.assertEqual([e["text"] for e in bad], ["not a parcel", "13", "14 15 16"])
        self.assertEqual(bad[0]["line"], 9)


class Batch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.save = Path(self.tmp.name) / "save"
        self.list_file = Path(self.tmp.name) / "list.txt"
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, block, parcel, save_folder, use_local=False, log=print):
        self.calls.append((block, parcel, use_local))
        if parcel == "2":
            raise fetch.FetchError("archive closed")
        if parcel == "3":
            raise RuntimeError("fault")
        model = Path(save_folder) / f"{block}_{parcel}" / "model"
        model.mkdir(parents=True, exist_ok=True)
        q = quality("review")
        q["reasons"] = ["the model is not placed on the survey"]
        (model / "quality.json").write_text(json.dumps(q))
        return {"project": f"{block}_{parcel}", "verdict": "review", "missing": ["survey"], "model": str(model)}

    def run_batch(self, text, local=False):
        self.list_file.write_text(text)
        with mock.patch.object(cli, "run_project", self.fake_run):
            return quiet(cli.batch, self.list_file, self.save, local)

    def test_one_failure_does_not_stop_the_run(self):
        self.assertEqual(self.run_batch("1 1\n1 2\n1 3\nbad\n1/4\n"), 1)
        self.assertEqual([c[:2] for c in self.calls], [("1", "1"), ("1", "2"), ("1", "3"), ("1", "4")])
        rows = json.loads((self.save / "batch_report.json").read_text())
        self.assertEqual([(r["project"], r["status"]) for r in rows],
                         [("1_1", "built"), ("1_2", "error"), ("1_3", "error"), ("bad", "error"), ("1_4", "built")])
        self.assertEqual(rows[0]["verdict"], "review")
        self.assertEqual(rows[0]["kind"], "full")
        self.assertEqual(rows[0]["missing"], ["survey"])
        self.assertEqual(rows[0]["reasons"], ["the model is not placed on the survey"])
        self.assertEqual(rows[1]["error"], "archive closed")
        self.assertIn("fault", rows[2]["error"])
        md = (self.save / "batch_report.md").read_text().splitlines()
        self.assertEqual(md[0], "ready: 0, review: 2, failed: 0, errors: 3")
        self.assertEqual(sum(1 for line in md if line.startswith("| 1_")), 4)

    def test_report_is_written_after_every_parcel(self):
        seen = []

        def run(block, parcel, *args, **kwargs):
            report = self.save / "batch_report.json"
            seen.append(len(json.loads(report.read_text())) if report.exists() else 0)
            return self.fake_run(block, parcel, *args, **kwargs)

        self.list_file.write_text("1 1\n1 4\n1 5\n")
        with mock.patch.object(cli, "run_project", run):
            self.assertEqual(quiet(cli.batch, self.list_file, self.save), 0)
        self.assertEqual(seen, [0, 1, 2])

    def test_local_skips_a_parcel_built_from_the_same_documents(self):
        folder = self.save / "1_1"
        (folder / "model").mkdir(parents=True)
        document = folder / "plan.dwf"
        document.write_bytes(b"x")
        old = time.time() - 100
        os.utime(document, (old, old))
        (folder / "model" / "quality.json").write_text(json.dumps(quality("ready")))
        self.assertEqual(self.run_batch("1 1\n", local=True), 0)
        self.assertEqual(self.calls, [])
        row = json.loads((self.save / "batch_report.json").read_text())[0]
        self.assertEqual((row["status"], row["verdict"]), ("skipped", "ready"))
        # a newer document means a rebuild
        os.utime(document, (time.time() + 100, time.time() + 100))
        self.assertEqual(self.run_batch("1 1\n", local=True), 0)
        self.assertEqual(self.calls, [("1", "1", True)])

    def test_without_local_always_runs(self):
        folder = self.save / "1_1"
        (folder / "model").mkdir(parents=True)
        document = folder / "plan.dwf"
        document.write_bytes(b"x")
        os.utime(document, (time.time() - 100, time.time() - 100))
        (folder / "model" / "quality.json").write_text(json.dumps(quality("ready")))
        self.run_batch("1 1\n")
        self.assertEqual(self.calls, [("1", "1", False)])


if __name__ == "__main__":
    unittest.main()
