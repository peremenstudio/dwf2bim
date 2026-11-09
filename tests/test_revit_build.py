"""Runs build_native.py against a mocked Revit API.

This catches logic and naming errors in the script. It does not show that Revit
accepts the calls, which only a run inside Revit can.
"""
import importlib.util
import math
import sys
import types
import unittest

from shapely.geometry import LineString
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "revit" / "build_native.py"


def load(fake_revit=False):
    saved = dict(sys.modules)
    if fake_revit:
        for name in ("clr", "Autodesk", "Autodesk.Revit", "Autodesk.Revit.DB", "Autodesk.Revit.DB.Architecture", "Autodesk.Revit.DB.Structure",
                     "RevitServices", "RevitServices.Persistence", "RevitServices.Transactions", "System", "System.Collections", "System.Collections.Generic"):
            sys.modules[name] = mock.MagicMock(name=name)
    spec = importlib.util.spec_from_file_location("build_native_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if fake_revit:
        sys.modules.clear()
        sys.modules.update(saved)
    return module


class PureLogic(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_short_edges_are_removed(self):
        ring = [[0, 0], [0.001, 0], [1, 0], [1, 1], [0.001, 0.001]]
        self.assertEqual(self.m.clean_ring(ring), [[0, 0], [1, 0], [1, 1]])

    def test_path_keeps_order(self):
        self.assertEqual(self.m.clean_path([[0, 0], [0.001, 0], [2, 0]]), [[0, 0], [2, 0]])

    def test_base_type_prefers_mapping_then_first_word(self):
        names = ["Block 10", "Concrete 20"]
        self.assertEqual(self.m.pick_base(names, "Concrete 33"), "Concrete 20")
        self.assertEqual(self.m.pick_base(names, "Concrete 33", "Block 10"), "Block 10")
        self.assertEqual(self.m.pick_base(names, "Other"), "Block 10")
        self.assertIsNone(self.m.pick_base([], "Other"))

    def test_opening_resolution_order(self):
        available = {"F1": {"80/210": 1}, "F2": {"X": 1}}
        r = self.m.resolve_opening
        self.assertEqual(r(available, "F1", "80/210", "door", {}), ("F1", "80/210", "same_family"))
        self.assertEqual(r(available, "F1", "90/210", "door", {}), ("F1", None, "same_family"))
        self.assertEqual(r(available, "Z", "X", "door", {"opening_families": {"Z": "F2"}}), ("F2", "X", "mapped"))
        self.assertEqual(r(available, "Z", "A", "door", {"default_door_family": "F2"}), ("F2", None, "default"))
        self.assertEqual(r(available, "Z", "A", "door", {}), ("F1", None, "first_available"))
        self.assertEqual(r({}, "Z", "A", "door", {}), (None, None, "none"))

    def test_mapping_wins_over_a_family_of_the_same_name(self):
        available = {"F1": {}, "Z": {"A": 1}}
        mapped = {"opening_families": {"Z": {"family": "F1", "type_format": "{w}w/{h}h"}}}
        self.assertEqual(self.m.resolve_opening(available, "Z", "A", "door", mapped), ("F1", None, "mapped"))
        self.assertEqual(self.m.mapped_family(mapped, "Z"), ("F1", "{w}w/{h}h"))

    def test_mapping_may_cross_categories(self):
        windows = {"W": {"1": 1}}
        every = {"W": {"1": 1}, "D": {"280x210": 1}}
        mapped = {"opening_families": {"Storefront": "D"}}
        self.assertEqual(self.m.resolve_opening(windows, "Storefront", "x", "window", mapped, every), ("D", None, "mapped"))

    def test_type_format_matches_template_naming(self):
        self.assertEqual(self.m.sized_type_name("160/110", 1.6, 1.1, "{w}w/{h}h"), "160w/110h")
        self.assertEqual(self.m.sized_type_name("100/220 x", 1.0, 2.2, "{w}/{h}"), "100/220")
        self.assertEqual(self.m.sized_type_name("160/110", 1.6, 1.1), "160/110")

    def test_name_rules_and_explicit_map(self):
        rules = [["^concrete (\\d+)$", "wall concrete \\1"]]
        self.assertEqual(self.m.target_type_name("concrete 20", {}, rules), "wall concrete 20")
        self.assertEqual(self.m.target_type_name("concrete 40", {"concrete 40": "wall concrete40"}, rules), "wall concrete40")
        self.assertEqual(self.m.target_type_name("other 5", {}, rules), "other 5")

    def test_base_follows_the_mapping_order(self):
        words = {"balcony": "B", "fill": "F", "concrete": "C"}
        self.assertEqual(self.m.base_by_word(["B", "C", "F"], "concrete 25 + 13 fill balcony", words), "B")
        self.assertEqual(self.m.base_by_word(["B", "C", "F"], "concrete 25", words), "C")
        self.assertIsNone(self.m.base_by_word(["B"], "concrete 25", words))

    def test_marks_are_stripped_from_names(self):
        self.assertEqual(self.m.clean_name("‏גג "), "גג")


class FakeType:
    def __init__(self, name, thickness=0.2):
        self.Name = name
        self.thickness = thickness
        self.Kind = "basic"
        self.Id = name
        self.structures = []

    def GetCompoundStructure(self):
        cs = mock.MagicMock()
        cs.GetWidth.return_value = self.thickness / 0.3048
        layer = mock.MagicMock()
        layer.Width = self.thickness / 0.3048
        cs.GetLayers.return_value = [layer]
        return cs

    def SetCompoundStructure(self, cs):
        self.structures.append(cs)

    def Duplicate(self, name):
        return FakeType(name, self.thickness)


class MockedBuild(unittest.TestCase):
    def build(self, pool_names=("Concrete 20",)):
        m = load(fake_revit=True)
        log = {"classes": {}, "errors": [], "levels_created": []}
        b = m.Builder.__new__(m.Builder)
        b.doc = mock.MagicMock()
        b.b = {}
        b.map = {}
        b.log = log
        level = mock.MagicMock()
        level.Elevation = 0.0
        b.levels = {"L00": level}
        b.created = {}
        b.pending = {}
        b.wall_types = {n: FakeType(n) for n in pool_names}
        b.floor_types = {n: FakeType(n) for n in pool_names}
        b.railing_types = {"Rail A": FakeType("Rail A")}
        b.symbols = {"door": {}, "window": {}}
        return m, b

    def wall(self):
        return {"id": "1", "class": "wall", "level": "L00", "z0": -0.5, "z1": 3.0, "type": "T", "source_type": "Concrete 33",
                "geometry": {"kind": "axis", "p0": [0.0, 0.0], "p1": [4.0, 0.0], "width": 0.33}}

    def test_wall_with_new_thickness_duplicates_and_resizes_a_type(self):
        m, b = self.build()
        t = b._host_type("wall", b.wall_types, "Concrete 33", 0.33, "wall_types", "wall_base_by_word")
        self.assertEqual(t.Name, "Concrete 33")
        self.assertEqual(len(t.structures), 1)
        self.assertIn("Concrete 33", b.wall_types)

    def test_same_name_wrong_thickness_gets_its_own_type(self):
        m, b = self.build(("Concrete 33",))
        t = b._host_type("wall", b.wall_types, "Concrete 33", 0.2, "wall_types", "wall_base_by_word")
        self.assertEqual(t.Name, "Concrete 33")  # FakeType default thickness is 0.2, so the name is reused
        t = b._host_type("wall", b.wall_types, "Concrete 33", 0.4, "wall_types", "wall_base_by_word")
        self.assertEqual(t.Name, "Concrete 33 (40)")

    def test_wall_name_rule_reuses_the_template_type(self):
        m, b = self.build(("wall concrete 20",))
        b.map = {"wall_name_rules": [["^concrete (\\d+)$", "wall concrete \\1"]]}
        t = b._host_type("wall", b.wall_types, "concrete 20", 0.2, "wall_types", "wall_base_by_word")
        self.assertEqual(t.Name, "wall concrete 20")
        self.assertEqual(t.structures, [])

    def test_new_type_takes_the_template_style_name_and_mapped_base(self):
        m, b = self.build(("wall concrete 20", "another"))
        b.map = {"wall_name_rules": [["^concrete (\\d+)$", "wall concrete \\1"]], "wall_base_by_word": {"concrete": "wall concrete 20"}}
        t = b._host_type("wall", b.wall_types, "concrete 15", 0.15, "wall_types", "wall_base_by_word")
        self.assertEqual(t.Name, "wall concrete 15")
        self.assertEqual(b.log["classes"]["wall"]["substitutions"]["wall concrete 15"], "duplicated from wall concrete 20")

    def test_make_wall_uses_level_and_offset(self):
        m, b = self.build()
        m.Wall.Create.reset_mock()
        b.make_wall(self.wall())
        args = m.Wall.Create.call_args[0]
        self.assertAlmostEqual(args[4], 3.5 / 0.3048)  # height
        self.assertAlmostEqual(args[5], -0.5 / 0.3048)  # base offset

    def test_a_mass_ends_up_as_a_floor_and_never_a_wall(self):
        m, b = self.build()
        m.Wall.Create.reset_mock()
        m.Floor.Create.reset_mock()
        tall_and_thin = {"id": "7", "class": "mullion", "level": "L00", "z0": 0.0, "z1": 2.8, "source_type": "x",
                         "geometry": {"kind": "extrusion", "polygons": [{"outer": [[0, 0], [0.05, 0], [0.05, 0.15], [0, 0.15]], "holes": []}]}}
        b.make_mass_floor(tall_and_thin)
        m.Wall.Create.assert_not_called()
        m.Floor.Create.assert_called_once()
        self.assertIn("DWF floor 280", b.floor_types)

    def test_stair_paths_are_at_absolute_height(self):
        m, b = self.build()
        ground, upper = mock.MagicMock(), mock.MagicMock()
        ground.Elevation = 3.5 / 0.3048
        upper.Elevation = 7.0 / 0.3048
        b.levels = {"L01": ground, "L02": upper}
        m.XYZ.reset_mock()
        rec = {"id": "s", "class": "stair", "level": "L01", "top_level": "L02", "z0": 3.5, "z1": 7.0, "base_offset": 0.0,
               "geometry": {"risers": 20, "flights": [
                   {"start": [0, 0], "end": [0, 2.5], "width": 1.15, "risers": 10},
                   {"start": [1.3, 2.5], "end": [1.3, 0], "width": 1.15, "risers": 10}]}}
        b._make_stair(rec, object())
        heights = [round(c.args[2] * 0.3048, 3) for c in m.XYZ.call_args_list if len(c.args) == 3 and c.args[2]]
        self.assertEqual(heights[:2], [3.5, 3.5])
        self.assertEqual(heights[2:4], [5.25, 5.25])

    def test_failed_element_is_logged_and_others_continue(self):
        m, b = self.build()
        bad = self.wall()
        good = dict(self.wall(), id="2")

        def make(rec):
            if rec["id"] == "1":
                raise ValueError("boom")
            return mock.MagicMock()

        b._transact = lambda label, action: (action(), True)[1]
        b.process("wall", [bad, good], make)
        section = b.log["classes"]["wall"]
        self.assertEqual(section["created"], 1)
        self.assertEqual(section["failed"][0]["id"], "1")
        self.assertIn("2", b.created)

    def test_rejected_batch_is_retried_one_by_one(self):
        m, b = self.build()
        calls = []

        def transact(label, action):
            action()
            calls.append(label)
            return len(calls) > 1  # the first commit, the whole batch, is rejected

        b._transact = transact
        b.process("wall", [self.wall(), dict(self.wall(), id="2")], lambda rec: mock.MagicMock())
        self.assertEqual(b.log["classes"]["wall"]["created"], 2)
        self.assertEqual(len(calls), 3)

    def test_existing_element_is_skipped(self):
        m, b = self.build()
        b.created = {"1": "existing"}
        b._transact = lambda label, action: (action(), True)[1]
        made = []
        b.process("wall", [self.wall()], lambda rec: made.append(rec))
        self.assertEqual(made, [])
        self.assertEqual(b.log["classes"]["wall"]["skipped"], 1)

    def test_opening_needs_a_created_host(self):
        m, b = self.build()
        with self.assertRaises(ValueError):
            b.make_opening({"id": "9", "class": "door", "host": "404", "level": "L00", "z0": 0.0, "center": [0, 0], "width": 0.8, "height": 2.1})


class OpeningOrder(unittest.TestCase):
    """The first Revit test left new types uncut because they were placed as they were made."""

    def rec(self, rid="1", host="w"):
        return {"id": rid, "class": "door", "host": host, "level": "L00", "z0": 0.0, "center": [1.0, 0.0], "width": 0.8, "height": 2.2,
                "source_family": "F", "source_type": "80/220", "type": "80/220"}

    def builder(self):
        m = load(fake_revit=True)
        b = m.Builder.__new__(m.Builder)
        b.doc = mock.MagicMock()
        b.map = {}
        b.log = {"classes": {}, "errors": []}
        b.created = {}
        b.pending = {}
        level = mock.MagicMock()
        level.Elevation = 0.0
        b.levels = {"L00": level}
        base = mock.MagicMock()
        duplicate = mock.MagicMock(IsActive=False)
        duplicate.Activate.side_effect = lambda: setattr(duplicate, "IsActive", True)  # as in Revit
        base.Duplicate.return_value = duplicate
        b.symbols = {"door": {"F": {"80/210": base}}, "window": {}}
        return m, b, base

    def test_placing_refuses_a_type_that_was_not_prepared(self):
        m, b, base = self.builder()
        with self.assertRaises(ValueError):
            b._opening_symbol(self.rec())
        base.Duplicate.assert_not_called()

    def test_prepared_type_is_created_once_and_activated(self):
        m, b, base = self.builder()
        b.prepare_opening_types([self.rec("1"), self.rec("2")])
        self.assertEqual(base.Duplicate.call_count, 1)
        b.symbols["door"]["F"]["80/220"].Activate.assert_called_once()
        self.assertIs(b._opening_symbol(self.rec("3")), b.symbols["door"]["F"]["80/220"])

    def test_handler_dismisses_warnings_and_resolves_errors(self):
        m, b, base = self.builder()
        log = {}
        handler = m.FailureHandler(log)
        warning, error = mock.MagicMock(), mock.MagicMock()
        warning.GetSeverity.return_value = m.FailureSeverity.Warning
        error.GetSeverity.return_value = "error"
        error.HasResolutions.return_value = True
        warning.GetDescriptionText.return_value = "W"
        error.GetDescriptionText.return_value = "E"
        args = mock.MagicMock()
        args.GetFailuresAccessor.return_value.GetFailureMessages.return_value = [warning, error]
        handler(None, args)
        args.GetFailuresAccessor.return_value.DeleteWarning.assert_called_once_with(warning)
        args.GetFailuresAccessor.return_value.ResolveFailure.assert_called_once_with(error)
        args.SetProcessingResult.assert_called_once_with(m.FailureProcessingResult.ProceedWithCommit)
        self.assertEqual(log["revit_messages"], {"warning: W": 1, "error: E": 1})

    def test_handler_rolls_back_an_error_with_no_resolution(self):
        m, b, base = self.builder()
        handler = m.FailureHandler({})
        error = mock.MagicMock()
        error.GetSeverity.return_value = "error"
        error.HasResolutions.return_value = False
        args = mock.MagicMock()
        args.GetFailuresAccessor.return_value.GetFailureMessages.return_value = [error]
        handler(None, args)
        args.SetProcessingResult.assert_called_once_with(m.FailureProcessingResult.ProceedWithRollBack)


class ExtractionGeometry(unittest.TestCase):
    def test_outlines_stay_valid_and_drop_slivers(self):
        from dwf2bim.extract import _polys_out
        from shapely.geometry import Polygon, box
        bow = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)]).buffer(0)
        for poly in _polys_out(bow, 0.005):
            self.assertTrue(Polygon(poly["outer"]).is_valid)
        pinched = box(0, 0, 1, 1).union(box(1, 1, 2, 2))
        for poly in _polys_out(pinched, 0.005):
            self.assertTrue(Polygon(poly["outer"]).is_valid)
        self.assertEqual(_polys_out(box(0, 0, 0.1, 0.1), 0.005, min_area=0.05), [])

    def test_hole_touching_the_edge_is_not_kept(self):
        from dwf2bim.extract import _polys_out
        from shapely.geometry import Polygon
        clear = Polygon([(0, 0), (6, 0), (6, 6), (0, 6)], [[(1, 1), (3, 1), (3, 3), (1, 3)]])
        self.assertEqual(len(_polys_out(clear, 0.005)[0]["holes"]), 1)
        edge = Polygon([(0, 0), (6, 0), (6, 6), (0, 6)], [[(0.005, 1), (3, 1), (3, 3), (0.005, 3)]])
        self.assertEqual(len(_polys_out(edge, 0.005)[0]["holes"]), 0)

    def test_centerline_follows_a_curved_strip(self):
        from dwf2bim.extract import _path_length, _slice_centerline
        from shapely.geometry import LineString
        arc = LineString([(5 * math.cos(a / 40), 5 * math.sin(a / 40)) for a in range(21)]).buffer(0.1, cap_style=2)
        start, end = (5.0, 0.0), (5 * math.cos(0.5), 5 * math.sin(0.5))
        path = _slice_centerline(arc, start, end, math.dist(start, end))
        self.assertGreater(len(path), 3)
        self.assertAlmostEqual(_path_length(path), 2.5, delta=0.1)
        for x, y in path:
            self.assertAlmostEqual(math.hypot(x, y), 5.0, delta=0.05)

    def test_covering_wall_prevents_a_second_wall_in_the_same_place(self):
        from dwf2bim.extract import _covering_wall
        def wall(i, z0, z1, x=0.0):
            return {"id": i, "z0": z0, "z1": z1, "geometry": {"kind": "axis", "p0": [x, 0.0], "p1": [x + 4.0, 0.0], "width": 0.2}}
        tall = wall("tall", 0.0, 7.0)
        self.assertIs(_covering_wall(wall("new", 3.5, 7.0), 3.5, 7.0, [tall]), tall)
        self.assertIsNone(_covering_wall(wall("new", 7.0, 10.5), 7.0, 10.5, [tall]))
        self.assertIsNone(_covering_wall(wall("new", 3.5, 7.0, x=10.0), 3.5, 7.0, [tall]))


def Polygon_of(rec):
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    return unary_union([Polygon(q["outer"], q.get("holes")) for q in rec["geometry"]["polygons"]])


class Levels(unittest.TestCase):
    def builder(self, names):
        m = load(fake_revit=True)
        b = m.Builder.__new__(m.Builder)
        b.doc = mock.MagicMock()
        b.map = {}
        b.log = {"classes": {}, "errors": [], "levels_created": []}
        b.levels = {}
        b.created = {}
        b.pending = {}
        existing = []
        for name, z in names:
            level = mock.MagicMock()
            level.Name = name
            level.Elevation = z / 0.3048
            level.Id.ToString.return_value = name
            existing.append(level)
        b._collect = lambda cls: existing if cls is m.Level else []
        b._transact = lambda label, action: action()
        b.b = {"levels": {"L01": {"z": 3.5, "source_name": "01"}, "L00": {"z": 0.0, "source_name": "0.00=19.36"}}}
        return m, b, existing

    def test_a_level_of_the_template_is_not_taken_over(self):
        m, b, existing = self.builder([("Floor 1", 3.32)])
        created = mock.MagicMock()
        m.Level.Create = mock.MagicMock(return_value=created)
        b.make_levels()
        self.assertEqual(m.Level.Create.call_count, 2)
        self.assertEqual(sorted(b.log["levels_created"]), ["0.00=19.36", "01"])
        self.assertEqual(existing[0].Elevation, 3.32 / 0.3048)

    def test_a_level_of_the_same_name_is_reused_at_the_dwf_height(self):
        m, b, existing = self.builder([("01", 3.0), ("0.00=19.36", 0.0)])
        m.Level.Create = mock.MagicMock()
        b.make_levels()
        m.Level.Create.assert_not_called()
        self.assertAlmostEqual(existing[0].Elevation * 0.3048, 3.5)

    def test_other_levels_are_deleted_and_failures_reported(self):
        m, b, existing = self.builder([("01", 3.5), ("0.00=19.36", 0.0), ("Old", 9.0), ("Active", 12.0)])
        b.levels = {"L01": existing[0], "L00": existing[1]}
        deleted = []

        def delete(element_id):
            if element_id is existing[3].Id:
                raise RuntimeError("active view")
            deleted.append(element_id)

        b.doc.Delete = delete
        b.delete_other_levels()
        self.assertEqual(deleted, [existing[2].Id])
        self.assertEqual(b.log["levels_deleted"], ["Old"])
        self.assertEqual(b.log["levels_not_deleted"][0]["level"], "Active")


class Site(unittest.TestCase):
    def test_rigid_fit_recovers_rotation_and_shift(self):
        from dwf2bim.site import fit_rigid
        model = [(0.0, 0.0), (10.0, 0.0), (10.0, 6.0), (0.0, 8.0)]
        theta = math.radians(-105.9)
        world = [(math.cos(theta) * x - math.sin(theta) * y + 1000.0, math.sin(theta) * x + math.cos(theta) * y + 2000.0) for x, y in model]
        got, tx, ty, res = fit_rigid(model, world[2:] + world[:2])
        self.assertAlmostEqual(got, theta, places=6)
        self.assertAlmostEqual(tx, 1000.0, places=4)
        self.assertAlmostEqual(ty, 2000.0, places=4)
        self.assertLess(res.max(), 1e-6)

    def test_rings_of_different_size_do_not_fit(self):
        from dwf2bim.site import fit_rigid
        self.assertIsNone(fit_rigid([(0, 0), (1, 0), (1, 1)], [(0, 0), (1, 0), (1, 1), (0, 1)]))

    def test_corner_on_a_straight_edge_is_dropped(self):
        from dwf2bim.site import drop_collinear
        ring = [(0, 0), (5, 0.01), (10, 0), (10, 10), (0, 10)]
        self.assertEqual(len(drop_collinear(ring)), 4)

    def test_dashed_line_collapses_to_a_ring(self):
        from dwf2bim.site import _building_line
        from shapely.geometry import Polygon
        square = [((0, 0), (10, 0)), ((10, 0), (10, 10)), ((10, 10), (0, 10)), ((0, 10), (0, 0))]
        dashes = []
        for a, b in square:
            for i in range(4):
                t0, t1 = i / 4, (i + 0.7) / 4
                dashes.append([(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0), (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)])
        line = _building_line(dashes, 100.0, "x")
        self.assertTrue(line["closed"])
        self.assertAlmostEqual(Polygon(line["points"]).area, 100.0, delta=0.01)

    def test_open_line_keeps_its_full_length(self):
        from dwf2bim.site import _building_line
        line = _building_line([[(0, 0), (1.8, 0)], [(3, 0), (4.8, 0)], [(6, 0), (7, 0)]], -0.09, "y")
        self.assertFalse(line["closed"])
        self.assertAlmostEqual(math.dist(line["points"][0], line["points"][-1]), 7.0, places=3)

    def _clip(self, slabs):
        from dwf2bim.site import SiteBuilder
        def box(x0, y0, x1, y1):
            return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        building = {
            "levels": {"L01": {"z": 3.5}}, "issues": [], "site": {},
            "areas": [{"levels": ["L01"], "cells": [
                {"c": "1", "ring": box(0, 0, 10, 10), "holes": []},
                {"c": "2004", "ring": box(0, 10, 10, 12), "holes": []},
            ]}],
            "elements": [{"id": n, "class": "floor", "level": "L01", "type": n, "geometry": {"polygons": [{"outer": box(*b), "holes": []}]}} for n, b in slabs.items()],
        }
        SiteBuilder(None, building).clip_floors()
        return {r["id"]: Polygon_of(r) for r in building["elements"]}, building

    def test_main_slab_stops_at_the_building_and_keeps_out_of_the_balcony(self):
        result, _ = self._clip({"main": (-1, 0, 10, 11)})
        self.assertLessEqual(result["main"].bounds[3], 10.0 + 1e-6)
        self.assertGreaterEqual(result["main"].bounds[0], -1e-6)

    def test_balcony_slab_fills_its_cell(self):
        result, _ = self._clip({"main": (0, 0, 10, 10), "balcony": (1, 10, 9, 12)})
        self.assertAlmostEqual(result["balcony"].area, 20.0, delta=0.1)
        self.assertAlmostEqual(result["main"].area, 100.0, delta=0.1)

    def test_slab_standing_on_its_own_stays_as_drawn(self):
        result, building = self._clip({"main": (0, 0, 10, 10), "ledge": (-1, 2, -0.2, 4)})
        self.assertAlmostEqual(result["ledge"].area, 1.6, places=3)
        self.assertEqual(building["site"]["slab_clip"]["independent_slabs_kept"], 1)

    def test_room_hidden_by_furniture_is_filled(self):
        result, _ = self._clip({"main": (0, 0, 10, 8)})
        self.assertAlmostEqual(result["main"].area, 100.0, delta=0.1)

    def test_plot_ring_keeps_every_corner_in_order(self):
        from dwf2bim.site import _walk_ring
        a, b, c, d = (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)
        ring = _walk_ring([(a, b), (c, b), (c, d), (d, a)])
        self.assertEqual(ring, [a, b, c, d])

    def test_broken_plot_line_gives_no_ring(self):
        from dwf2bim.site import _walk_ring
        self.assertEqual(_walk_ring([((0, 0), (10, 0)), ((10, 0), (10, 10)), ((10, 10), (5, 10))]), [])

    def test_collinear_pieces_across_the_angle_wrap_are_one_edge(self):
        from dwf2bim.site import _merge_collinear
        pieces = [((0, 0), (5, -0.01)), ((6, 0.0), (10, 0.01))]
        self.assertEqual(len(_merge_collinear(pieces, offset_tol=0.05)), 1)

    def test_cell_with_a_curved_side_is_joined_from_open_pieces(self):
        from dwf2bim.site import _chain_pieces
        pieces = [[(0, 0), (10, 0)], [(10, 10), (10, 0)], [(10, 10), (5, 12), (0, 10)], [(0, 10), (0, 0)]]
        rings = _chain_pieces(pieces)
        self.assertEqual(len(rings), 1)
        self.assertEqual(len(rings[0]), 5)

    def test_open_pieces_that_do_not_close_are_not_a_cell(self):
        from dwf2bim.site import _chain_pieces
        self.assertEqual(_chain_pieces([[(0, 0), (10, 0)], [(10, 0), (10, 10)]]), [])

    def test_area_lines_come_only_from_numbered_cells(self):
        from dwf2bim.site import cell_edges, label_cells
        def square(x, y, side):
            return [(x, y), (x + side, y), (x + side, y + side), (x, y + side), (x, y)]
        fence = [(-5, 20), (40, 20)]
        panel = {
            "lines": [square(0, 0, 10), square(10, 0, 10), fence, square(30, 0, 5)],
            "labels": {"1": (5, 5), "2": (15, 5)},
            "rows": {"1": {"t": 100.0}, "2": {"t": 100.0}},
        }
        edges = cell_edges(label_cells(panel).values())
        self.assertEqual(len(edges), 7)  # two squares, the wall between them once
        everything = [p for e in edges for p in e]
        self.assertFalse(any(p[1] == 20 for p in everything))
        self.assertFalse(any(p[0] >= 30 for p in everything))

    def test_comparison_finds_lines_only_on_one_side(self):
        from dwf2bim.qa import compare_lines
        dwf = [[[0, 0], [10, 0]], [[10, 0], [10, 10]]]
        revit = [[[0, 0], [10, 0.01]], [[20, 0], [30, 0]]]
        extra, missing = compare_lines(dwf, revit)
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0][0][0], 20.0)
        self.assertEqual(len(missing), 1)
        self.assertAlmostEqual(LineString(missing[0]).length, 10.0, delta=0.1)

    def _apartments(self, floor_cells, level="L03"):
        from dwf2bim.site import assign_apartments
        def cell(n, c, b, t=1.0):
            x0, y0, x1, y1 = b
            return {"n": n, "c": c, "t": t, "ring": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]}
        floor = {"floors": [3], "levels": [level], "cells": [cell(*c) for c in floor_cells]}
        building = {"levels": {level: {"z": 10.5}}, "areas": [floor]}
        assign_apartments(building)
        return floor["unit_ids"][level], building

    def test_each_apartment_gets_a_shelter_before_any_gets_a_second(self):
        # the shelter S1 touches both apartments, S2 only the second: S1 must go to the first
        ids, building = self._apartments([
            ("A", "1", (0, 0, 10, 10), 60), ("B", "1", (10.2, 0, 20, 10), 60),
            ("S1", "1001", (9, 10.2, 11, 13)), ("S2", "1001", (15, 10.2, 17, 13)),
        ])
        self.assertEqual(ids["S1"], ids["A"])
        self.assertEqual(ids["S2"], ids["B"])
        self.assertEqual([u["shelters"] for u in building["apartments"]["units"]], [1, 1])

    def test_a_shelter_wall_follows_its_shelter_not_the_apartment_it_touches(self):
        # the wall W touches apartment A, and wraps the shelter that belongs to B
        ids, _ = self._apartments([
            ("A", "1", (0, 0, 10, 10), 60), ("B", "1", (0, 13.1, 10, 23), 60),
            ("W", "1017", (2, 10, 5, 10.2)), ("S", "1001", (2, 10.2, 5, 13)),
            ("S2", "1001", (20, 0, 22, 3)), ("S3", "1001", (-3, 0, -0.1, 3)),
        ])
        self.assertEqual(ids["W"], ids["S"])

    def test_every_balcony_joins_an_apartment_however_far(self):
        ids, building = self._apartments([("A", "1", (0, 0, 10, 10), 60), ("B", "2004", (40, 0, 45, 5), 5)])
        self.assertEqual(ids["B"], ids["A"])
        self.assertEqual(building["apartments"]["parts_with_no_apartment"], [])

    def test_a_small_residential_cell_is_not_an_apartment(self):
        ids, building = self._apartments([("A", "1", (0, 0, 10, 10), 60), ("small", "1", (10.2, 0, 13, 3), 8)])
        self.assertEqual(len(building["apartments"]["units"]), 1)
        self.assertEqual(ids["small"], ids["A"])

    def test_common_cells_stay_out_and_a_far_shelter_is_listed(self):
        ids, building = self._apartments([("A", "1", (0, 0, 10, 10), 60), ("L", "1005", (11, 0, 13, 5)), ("far", "1001", (40, 0, 43, 3))])
        self.assertNotIn("L", ids)
        self.assertNotIn("far", ids)
        self.assertEqual(building["apartments"]["parts_with_no_apartment"], [{"floors": [3], "cell": "far"}])

    def test_numbers_run_from_the_bottom_up_across_levels(self):
        from dwf2bim.site import assign_apartments
        def cell(n):
            return {"n": n, "c": "1", "t": 60.0, "ring": [[0, 0], [10, 0], [10, 10], [0, 10]]}
        typical = {"floors": [3, 4], "levels": ["L03", "L04"], "cells": [cell("10")]}
        lower = {"floors": [1], "levels": ["L01"], "cells": [cell("1")]}
        building = {"levels": {"L01": {"z": 3.5}, "L03": {"z": 10.5}, "L04": {"z": 14.0}}, "areas": [typical, lower]}
        assign_apartments(building)
        self.assertEqual((lower["unit_ids"]["L01"]["1"], typical["unit_ids"]["L03"]["10"], typical["unit_ids"]["L04"]["10"]), (1, 2, 3))

    def test_a_difference_keeps_only_area_after_overlays_leave_lines(self):
        from shapely.geometry import GeometryCollection, LineString, Polygon
        from dwf2bim.site import _difference
        square = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])
        mixed = GeometryCollection([Polygon([(0, 0), (2, 0), (2, 4), (0, 4)]), LineString([(0, 0), (9, 9)])])
        self.assertAlmostEqual(_difference(square, mixed).area, 8.0)

    def test_plot_drawn_in_pieces_becomes_one_ring(self):
        from dwf2bim.site import ring_from_pieces
        square = [(0, 0), (10, 0), (10, 10), (0, 10)]
        pieces = [[square[i], square[(i + 1) % 4]] for i in (2, 0, 3, 1)]
        ring = ring_from_pieces(pieces, 100.0)
        self.assertEqual(sorted(ring), sorted(square))
        self.assertIsNone(ring_from_pieces(pieces, 150.0))
        self.assertEqual(sorted(ring_from_pieces([square + [square[0]]], 100.0)), sorted(square))

    def test_an_empty_level_with_a_twin_at_its_height_is_left_out(self):
        from dwf2bim.extract import drop_empty_twin_levels
        levels = {"02": {"z": 6.6}, "2": {"z": 6.6}, "03": {"z": 9.9}, "Top": {"z": 30.0}}
        kept, dropped = drop_empty_twin_levels(levels, {"02": 40, "03": 12})
        self.assertEqual(sorted(kept), ["02", "03", "Top"])
        self.assertEqual(dropped, {"2": "02"})
        kept, dropped = drop_empty_twin_levels({"A": {"z": 1.0}, "B": {"z": 1.0}}, {})
        self.assertEqual((sorted(kept), dropped), (["A", "B"], {}))

    def test_open_building_lines_give_way_to_a_closed_one(self):
        from dwf2bim.site import closed_lines_only
        closed, opened = {"id": "c", "closed": True}, {"id": "o", "closed": False}
        self.assertEqual(closed_lines_only([opened, closed]), ([closed], ["o"]))
        self.assertEqual(closed_lines_only([opened]), ([opened], []))

    def test_a_wall_counts_as_moved_only_past_the_tolerance_in_either_direction(self):
        expected = ([0.0, 0.0], [5.0, 0.0])
        wall_moved = load().wall_moved
        self.assertFalse(wall_moved(expected, ([5.0, 0.1], [0.1, 0.0])))
        self.assertTrue(wall_moved(expected, ([0.0, 0.0], [9.0, 0.0])))

    def test_apartment_column_beats_inference_and_basements_stay_common(self):
        from dwf2bim.site import assign_apartments
        def cell(n, c, u=None):
            return {"n": n, "c": c, "t": 1.0, "u": u, "ring": [[0, 0], [1, 0], [1, 1], [0, 1]]}
        floor = {"floors": [1], "levels": ["L01"], "cells": [cell("1", "1", "2a"), cell("2", "1", "1a"), cell("3", "2004", "1a"), cell("4", "1005")]}
        basement = {"floors": [-1], "levels": ["L-01"], "cells": [cell("5", "1017", "1a"), cell("6", "1005")]}
        building = {"levels": {"L01": {"z": 3.5}, "L-01": {"z": -3.0}}, "areas": [floor, basement]}
        assign_apartments(building)
        self.assertEqual(floor["unit_ids"]["L01"], {"2": 1, "3": 1, "1": 2})
        self.assertEqual(basement["unit_ids"]["L-01"], {})
        self.assertEqual(building["apartments"]["source"], "area_table")

    def test_apartment_label_goes_to_the_nearest_table_row(self):
        from types import SimpleNamespace
        from dwf2bim.site import _unit_labels
        texts = [SimpleNamespace(x=190.0, y=102.0, text="1a"), SimpleNamespace(x=190.0, y=108.0, text="2a"),
                 SimpleNamespace(x=100.0, y=102.0, text="3.1"), SimpleNamespace(x=190.0, y=300.0, text="far")]
        self.assertEqual(_unit_labels(texts, 0.0, {"10": 100.0, "11": 106.4}), {"10": "1a", "11": "2a"})

    def test_plate_belongs_to_the_level_at_its_top(self):
        from dwf2bim.site import keep_plate_masses
        plate = {"id": "p", "class": "generic", "level": None, "z0": 17.1, "z1": 17.5,
                 "geometry": {"polygons": [{"outer": [[0, 0], [4, 0], [4, 4], [0, 4]], "holes": []}]}}
        building = {"levels": {"L04": {"z": 14.0}, "L05": {"z": 17.5}}, "elements": [plate]}
        keep_plate_masses(building)
        self.assertEqual(building["elements"][0]["level"], "L05")

    def _merge(self, level="L01", slabs=None):
        from dwf2bim.site import merge_storey_slabs
        def box(x0, y0, x1, y1):
            return [{"outer": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "holes": []}]
        elements = [{"id": i, "class": "floor", "level": level, "source_type": t, "type": t, "material": None, "z0": z - d, "z1": z,
                     "geometry": {"polygons": box(*b), "slab_role": role, "fitted_to_area_plan": True}} for i, t, z, d, b, role in slabs]
        levels = {"L01": {"z": 3.5}, "L00": {"z": 0.0}, "L-01": {"z": -4.0}}
        building = {"levels": levels, "elements": elements, "site": {}, "areas": []}
        merge_storey_slabs(building)
        return building

    def test_slabs_at_one_height_become_one_floor_across_a_wall(self):
        b = self._merge(slabs=[("a", "T1", 3.5, 0.4, (0, 0, 10, 10), "main"), ("b", "T2", 3.5, 0.5, (10.2, 0, 15, 10), "main")])
        self.assertEqual(len(b["elements"]), 1)
        rec = b["elements"][0]
        self.assertEqual(rec["id"], "a")
        self.assertEqual(rec["geometry"]["merged_ids"], ["b"])
        self.assertEqual(rec["source_type"], "T1")
        self.assertAlmostEqual(rec["z1"] - rec["z0"], 0.4)
        self.assertEqual(len(rec["geometry"]["polygons"]), 1)
        self.assertAlmostEqual(Polygon_of(rec).area, 150.0, delta=0.1)

    def test_balcony_and_other_height_stay_separate(self):
        b = self._merge(slabs=[
            ("a", "T1", 3.5, 0.4, (0, 0, 10, 10), "main"), ("b", "T1", 3.5, 0.4, (10, 0, 20, 10), "main"),
            ("wet", "T2", 3.0, 0.25, (2, 2, 4, 4), "main"), ("balcony", "T3", 3.48, 0.38, (0, 10, 10, 12), "balcony"),
            ("ledge", "T2", 3.0, 0.25, (-1, 0, -0.5, 2), "independent"),
        ])
        self.assertEqual({r["id"] for r in b["elements"]}, {"a", "wet", "balcony", "ledge"})

    def test_ground_floor_and_basements_keep_their_separate_slabs(self):
        for level, z in (("L00", 0.0), ("L-01", -4.0)):
            b = self._merge(level=level, slabs=[("a", "T1", z, 0.4, (0, 0, 10, 10), "main"), ("b", "T1", z, 0.4, (10, 0, 20, 10), "main")])
            self.assertEqual(len(b["elements"]), 2, level)

    def test_height_limit_is_5_cm(self):
        near = self._merge(slabs=[("a", "T1", 3.5, 0.4, (0, 0, 10, 10), "main"), ("b", "T1", 3.54, 0.4, (10, 0, 20, 10), "main")])
        self.assertEqual(len(near["elements"]), 1)
        far = self._merge(slabs=[("a", "T1", 3.5, 0.4, (0, 0, 10, 10), "main"), ("b", "T1", 3.57, 0.4, (10, 0, 20, 10), "main")])
        self.assertEqual(len(far["elements"]), 2)

    def test_slab_not_fitted_inside_the_outer_walls_does_not_join(self):
        from dwf2bim.site import merge_storey_slabs
        def box(x0, y0, x1, y1):
            return [{"outer": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "holes": []}]
        elements = [{"id": i, "class": "floor", "level": "L01", "source_type": "T", "type": "T", "material": None, "z0": 3.1, "z1": 3.5,
                     "geometry": {"polygons": box(*b), "slab_role": "main"}} for i, b in (("a", (0, 0, 10, 10)), ("b", (10, 0, 20, 10)))]
        building = {"levels": {"L01": {"z": 3.5}}, "elements": elements, "site": {}, "areas": []}
        merge_storey_slabs(building)
        self.assertEqual(len(building["elements"]), 2)

    def _masses(self, *recs):
        from dwf2bim.site import keep_plate_masses
        def box(x0, y0, x1, y1):
            return [{"outer": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "holes": []}]
        slab = {"id": "slab", "class": "floor", "z0": 3.1, "z1": 3.5, "geometry": {"polygons": box(0, 0, 10, 10)}}
        elements = [slab] + [{"id": i, "class": "generic", "family": "f", "z0": z0, "z1": z1, "geometry": {"polygons": box(*b)}} for i, z0, z1, b in recs]
        building = {"elements": elements}
        keep_plate_masses(building)
        return {r["id"] for r in building["elements"]}, {m["id"]: m["reason"] for m in building["masses_left_out"]}

    def test_post_is_not_built_as_a_floor(self):
        kept, out = self._masses(("post", 6.9, 17.1, (20, 20, 20.3, 20.3)))
        self.assertNotIn("post", kept)
        self.assertEqual(out["post"], "taller than a slab")

    def test_plate_stays_as_a_floor(self):
        kept, out = self._masses(("plate", 17.1, 17.5, (20, 20, 25, 25)))
        self.assertIn("plate", kept)
        self.assertEqual(out, {})

    def test_small_bar_and_plate_on_a_slab_are_left_out(self):
        kept, out = self._masses(("bar", 4.9, 4.93, (20, 20, 20.2, 20.2)), ("double", 3.1, 3.5, (1, 1, 5, 5)))
        self.assertEqual(out["bar"], "too small to be a slab")
        self.assertEqual(out["double"], "already covered by a slab")

    def test_gap_tolerant_cell_needs_the_table_area(self):
        from dwf2bim.site import label_cells
        def square(x, y, side):
            return [(x, y), (x + side, y), (x + side, y + side), (x, y + side), (x, y)]
        panel = {
            "lines": [square(0, 0, 10), square(20, 0, 10)[:-1] + [(20, 0.1)]],
            "labels": {"1": (5, 5), "2": (25, 5)},
            "rows": {"1": {"t": 100.0}, "2": {"t": 100.0}},
        }
        cells = label_cells(panel)
        self.assertAlmostEqual(cells["1"].area, 100.0, places=3)
        self.assertAlmostEqual(cells["2"].area, 100.0, delta=1.0)
        panel["rows"]["2"]["t"] = 10.0
        self.assertNotIn("2", label_cells(panel))


class RailingPaths(unittest.TestCase):
    def test_parallel_rails_collapse_to_one_line(self):
        from dwf2bim.extract import _railing_paths
        segs = [((0, y), (5, y)) for y in (0.0, 0.05, 0.1)] + [((x, 0.0), (x, 0.1)) for x in (0.0, 1.0, 2.0)]
        paths = _railing_paths(segs, [], 5.0)
        self.assertEqual(len(paths), 1)
        self.assertAlmostEqual(math.dist(paths[0][0], paths[0][-1]), 5.0, places=3)

    def test_l_shape_chains_into_one_path_with_a_corner(self):
        from dwf2bim.extract import _railing_paths, _path_length
        segs = [((0, 0), (5, 0)), ((0, 0.06), (5, 0.06)), ((5.03, 0.1), (5.03, 3)), ((5.09, 0.1), (5.09, 3))]
        paths = _railing_paths(segs, [], 8.0)
        self.assertEqual(len(paths), 1)
        self.assertEqual(len(paths[0]), 3)
        self.assertAlmostEqual(_path_length(paths[0]), 8.0, delta=0.2)

    def test_curved_rail_uses_the_drawn_polyline(self):
        from dwf2bim.extract import _railing_paths, _path_length
        arc = [(3 * math.cos(a / 20), 3 * math.sin(a / 20)) for a in range(30)]
        segs = [(arc[i], arc[i + 1]) for i in range(len(arc) - 1)]
        paths = _railing_paths(segs, [arc], 4.3)
        self.assertAlmostEqual(_path_length(paths[0]), 4.3, delta=0.2)


if __name__ == "__main__":
    unittest.main()
