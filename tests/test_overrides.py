"""A project's own corrections change that project only, and say so."""
import json
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import Polygon

from dwf2bim import contours, overrides


class Overrides(unittest.TestCase):
    def test_no_file_means_no_corrections(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(overrides.load(tmp), {})
        self.assertEqual(overrides.load(None), {})

    def test_an_unknown_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / overrides.FILE).write_text(json.dumps({"storey": {}}), encoding="utf8")
            with self.assertRaises(ValueError):
                overrides.load(tmp)

    def test_a_named_element_is_left_out_and_listed_and_a_missing_one_is_reported(self):
        b = {"elements": [{"id": "1", "class": "wall", "level": "A", "type": "W"}, {"id": "2", "class": "wall", "level": "A", "type": "W"}],
             "openings": [{"id": "3", "class": "door", "level": "A", "type": "D"}]}
        applied = overrides.leave_out_elements(b, {"elements": {"1": {"leave_out": "drawn twice"}, "3": {"leave_out": "a niche"}, "9": {"leave_out": "x"}}})
        self.assertEqual(([r["id"] for r in b["elements"]], b["openings"]), (["2"], []))
        self.assertEqual(sorted(r["reason"] for r in b["left_out"]), ["project exception: a niche", "project exception: drawn twice"])
        self.assertEqual([a["what"] for a in applied], ["left out", "left out", "not found in the model"])

    def test_a_given_outline_replaces_the_storeys_own(self):
        square = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        out = {"main": square, "balconies": [Polygon([(10, 0), (12, 0), (12, 4), (10, 4)])], "holes": 0, "shaft_reasons": [], "holes_closed": []}
        fixed = {"main_floor": [[0, 0], [8, 0], [8, 8], [0, 8]], "open": [[[2, 2], [4, 2], [4, 4], [2, 4]]], "balconies": []}
        new = contours.with_project_outline(out, fixed)
        self.assertAlmostEqual(new["main"].area, 64 - 4)
        self.assertEqual((new["balconies"], new["shaft_reasons"]), ([], ["project exception"]))
        self.assertAlmostEqual(out["main"].area, 100)  # the computed outline is not touched


if __name__ == "__main__":
    unittest.main()
