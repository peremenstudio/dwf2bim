"""Modelling rules that tidy the model for Revit: walls over walls, envelopes, ground floors, arcs, plan drawings."""
import math
import unittest
from types import SimpleNamespace

from dwf2bim import conventions, views


def wall(i, level, p0, p1, width=0.2, z0=0.0, z1=3.0):
    return {"id": i, "class": "wall", "level": level, "z0": z0, "z1": z1, "source_type": "T",
            "geometry": {"kind": "axis", "p0": list(p0), "p1": list(p1), "width": width}}


def floor(i, level, ring, z1=0.0, role=None):
    return {"id": i, "class": "floor", "level": level, "z0": z1 - 0.3, "z1": z1, "source_type": "F",
            "geometry": {"polygons": [{"outer": [list(q) for q in ring], "holes": []}], "slab_role": role}}


class WallsOverWalls(unittest.TestCase):
    def building(self, offset):
        return {"levels": {"G": {"z": 0.0}, "U": {"z": 3.0}},
                "elements": [wall("g", "G", (0, 0), (10, 0)),
                             wall("u", "U", (0, offset), (10, offset), z0=3.0, z1=6.0),
                             wall("t", "U", (5, offset), (5, 4), z0=3.0, z1=6.0)],
                "openings": [{"id": "o", "host": "u", "center": [2.0, offset]}]}

    def test_a_wall_a_little_off_the_wall_below_moves_onto_it(self):
        b = self.building(0.3)
        report = conventions.align_walls_to_below(b)
        u, t = b["elements"][1]["geometry"], b["elements"][2]["geometry"]
        self.assertAlmostEqual(u["p0"][1], 0.0)
        self.assertAlmostEqual(u["p1"][1], 0.0)
        self.assertAlmostEqual(t["p0"][1], 0.0)  # the wall meeting it follows
        self.assertAlmostEqual(b["openings"][0]["center"][1], 0.0)
        self.assertEqual(len(report["walls_aligned_to_wall_below"]), 1)

    def test_a_real_setback_stays(self):
        b = self.building(1.2)
        self.assertEqual(conventions.align_walls_to_below(b)["walls_aligned_to_wall_below"], [])
        self.assertAlmostEqual(b["elements"][1]["geometry"]["p0"][1], 1.2)

    def test_a_thinner_wall_goes_flush_with_one_face_below(self):
        b = self.building(0.12)
        b["elements"][0]["geometry"]["width"] = 0.4  # faces at -0.2 and 0.2; upper wall 0.2 thick, faces at 0.02 and 0.22
        conventions.align_walls_to_below(b)
        self.assertAlmostEqual(b["elements"][1]["geometry"]["p0"][1], 0.1)


class WallLines(unittest.TestCase):
    def test_pieces_of_one_line_become_one_wall_but_a_shelter_wall_stays(self):
        a = wall("a", "U", (0, 0), (6, 0), width=0.27)
        b = wall("b", "U", (6.2, -0.055), (9, -0.055), width=0.38)  # thicker, flush on the north face, 20 cm on
        c = wall("c", "U", (9, 0), (12, 0), width=0.27)
        c["source_type"] = "MMD 36"
        for w in (a, b, c):
            w["z0"], w["z1"] = 3.0, 6.0
        b["source_type"] = "EXT 38"
        bld = {"levels": {"U": {"z": 3.0}}, "elements": [a, b, c], "openings": [{"id": "o", "host": "b", "center": [7.0, -0.055]}], "areas": []}
        report = conventions.merge_wall_lines(bld)
        ids = sorted(r["id"] for r in bld["elements"])
        self.assertEqual(ids, ["a", "c"])
        self.assertAlmostEqual(a["geometry"]["p1"][0], 9.0)
        self.assertEqual(bld["openings"][0]["host"], "a")
        self.assertAlmostEqual(bld["openings"][0]["center"][1], 0.0)
        self.assertEqual(report["walls_that_took_another_type"][0]["type"], "EXT 38")


class Envelope(unittest.TestCase):
    def test_a_recess_with_a_slab_but_open_to_the_air_is_outside(self):
        # a 10 x 10 storey whose slab covers a 2 m recess in its north side, walled on three sides
        levels = {"G": {"z": 0.0}, "U": {"z": 3.0}}
        elements = [floor("s", "U", [(0, 0), (10, 0), (10, 10), (0, 10)], 3.0, "main"),
                    wall("s1", "U", (0, 0), (10, 0)), wall("s2", "U", (10, 0), (10, 10)), wall("s3", "U", (0, 10), (0, 0)),
                    wall("n1", "U", (10, 10), (6, 10)), wall("n2", "U", (4, 10), (0, 10)),
                    wall("r1", "U", (6, 10), (6, 8)), wall("r2", "U", (6, 8), (4, 8)), wall("r3", "U", (4, 8), (4, 10))]
        for e in elements:
            e["z0"], e["z1"] = 3.0, 6.0
        b = {"levels": levels, "elements": elements}
        conventions.mark_exterior_walls(b)
        side = {e["id"]: e.get("exterior_side") for e in elements if e["class"] == "wall"}
        self.assertEqual(side["r2"], -1)  # p0 to p1 runs west, so the recess to the north is on its right
        self.assertIsNotNone(side["r1"])
        self.assertIsNotNone(side["s1"])

    def test_ground_walls_take_their_outline_from_the_storey_above(self):
        # the ground's floors cover the whole plot; the building is the first storey's main slab
        b = {"levels": {"G": {"z": 0.0}, "U": {"z": 3.0}},
             "elements": [floor("g", "G", [(-5, -5), (15, -5), (15, 15), (-5, 15)]), floor("s", "U", [(0, 0), (10, 0), (10, 10), (0, 10)], 3.0, "main"),
                          wall("w", "G", (0, -0.1), (10, -0.1)), wall("i", "G", (5, 1), (5, 9))]}
        conventions.mark_exterior_walls(b)
        walls = {e["id"]: e for e in b["elements"] if e["class"] == "wall"}
        self.assertEqual(walls["w"]["exterior_side"], -1)
        self.assertNotIn("plaster", walls["w"])  # plaster on, as the settings say
        self.assertIsNone(walls["i"].get("exterior_side"))

    def test_exterior_walls_above_each_other_line_up_their_outside_faces(self):
        b = {"levels": {"G": {"z": 0.0}, "U": {"z": 3.0}},
             "elements": [wall("g", "G", (0, 0), (10, 0), width=0.3), wall("u", "U", (0, 0), (10, 0), width=0.2, z0=3.0, z1=6.0)],
             "openings": []}
        for e in b["elements"]:
            e["exterior_side"] = -1  # outside to the south
        conventions.align_walls_to_below(b)
        # face below at y = -0.15; the upper wall's face moves there, so its axis goes to -0.05
        self.assertAlmostEqual(b["elements"][1]["geometry"]["p0"][1], -0.05)

    def test_a_parapet_leaving_the_slab_drags_no_strip(self):
        b = {"levels": {"U": {"z": 3.0}},
             "elements": [floor("s", "U", [(0, 0), (10, 0), (10, 10), (0, 10)], 3.0, "main"),
                          wall("e", "U", (0, -0.1), (10, -0.1)), wall("p", "U", (10.1, 0), (10.1, -4))]}
        for e in b["elements"][1:]:
            e["exterior_side"] = 1
        conventions.extend_slabs_to_wall_core(b)
        ys = [q[1] for poly in b["elements"][0]["geometry"]["polygons"] for q in poly["outer"]]
        self.assertGreaterEqual(min(ys), -0.21)  # reaches the core of e, not down the parapet


class GroundFloors(unittest.TestCase):
    def test_every_gap_in_the_plot_gets_a_floor_and_a_walled_one_is_listed(self):
        shaft = [wall("a", "G", (2, 6), (4, 6)), wall("b", "G", (4, 6), (4, 8)), wall("c", "G", (4, 8), (2, 8)), wall("d", "G", (2, 8), (2, 6))]
        b = {"levels": {"G": {"z": 0.0}},
             "site": {"plot": {"ring": [[0, 0], [10, 0], [10, 10], [0, 10]]}},
             "elements": [floor("f", "G", [(0, 0), (10, 0), (10, 5), (0, 5)], z1=-0.05),
                          floor("h", "G", [(0, 5), (10, 5), (10, 10), (8, 10), (8, 5.5), (0, 5.5)])] + shaft,
             "unresolved": []}
        report = conventions.fill_ground_floors(b)
        self.assertEqual(len(report["ground_floors_added"]), 2)  # the open gap, and the walled one too
        self.assertEqual(report["ground_floors_added"][0]["continues"], "h")
        self.assertTrue(any(x["reason"] == "closed by walls" for x in report["ground_filled_with_care"]))

    def test_the_place_of_an_unbuilt_ramp_is_floored_and_listed(self):
        b = {"levels": {"G": {"z": 0.0}},
             "site": {"plot": {"ring": [[0, 0], [10, 0], [10, 10], [0, 10]]}},
             "elements": [floor("f", "G", [(0, 0), (10, 0), (10, 5), (0, 5)])],
             "unresolved": [{"id": "r", "class": "floor", "type": "Ramp", "level": "G", "plan_outline": [[0, 5], [10, 5], [10, 10], [0, 10]]}]}
        report = conventions.fill_ground_floors(b)
        self.assertEqual(len(report["ground_floors_added"]), 1)
        self.assertIn("r", report["ground_filled_with_care"][0]["reason"])


class SlabAtTheCore(unittest.TestCase):
    def test_a_slab_drawn_past_the_core_stops_at_it_but_one_that_carries_on_stays(self):
        b = {"levels": {"U": {"z": 3.0}},
             "elements": [floor("s", "U", [(0, -0.3), (10, -0.3), (10, 10), (0, 10)], 3.0, "main"),
                          wall("e", "U", (0, -0.1), (10, -0.1)),
                          wall("n", "U", (0, 9.9), (10, 9.9))]}
        b["elements"][0]["geometry"]["polygons"][0]["outer"] = [[0, -0.3], [10, -0.3], [10, 10], [6, 10], [6, 12], [4, 12], [4, 10], [0, 10]]
        b["elements"][1]["exterior_side"] = -1
        b["elements"][2]["exterior_side"] = 1
        conventions.extend_slabs_to_wall_core(b)
        shape = conventions._shape(b["elements"][0])
        self.assertAlmostEqual(shape.bounds[1], -0.2, places=2)  # the 10 cm finish strip past the core is gone
        self.assertAlmostEqual(shape.bounds[3], 12.0, places=2)  # the slab running on past the north wall stays


class CurtainInfill(unittest.TestCase):
    def test_doors_of_a_curtain_wall_the_dwf_left_out_get_one_in_the_gap(self):
        b = {"levels": {"G": {"z": 0.0}},
             "elements": [wall("a", "G", (0, 0), (4, 0)), wall("c", "G", (6.6, 0), (10, 0))],
             "openings": [],
             "unresolved": [{"id": "d1", "class": "door", "family": "Glass", "type": "P", "level": "G", "host_kind": "Curtain Wall",
                             "plan_outline": [[4.05, 0.0], [5.3, 0.0], [5.3, -1.25], [4.05, -1.25]]},
                            {"id": "d2", "class": "door", "family": "Glass", "type": "P", "level": "G", "host_kind": "Curtain Wall",
                             "plan_outline": [[5.3, 0.0], [6.55, 0.0], [6.55, -1.25], [5.3, -1.25]]}]}
        report = conventions.curtain_wall_infill(b)
        curtain = b["elements"][-1]
        self.assertEqual((curtain["geometry"]["p0"], curtain["geometry"]["p1"]), ([4.0, 0.0], [6.6, 0.0]))
        self.assertEqual([p["id"] for p in curtain["geometry"]["curtain"]["panels"]], ["d1", "d2"])
        self.assertEqual(b["unresolved"], [])
        self.assertEqual(len(report["curtain_walls_inferred"]), 1)


class CurtainEmbedded(unittest.TestCase):
    def test_doors_inside_a_wall_get_a_curtain_wall_embedded_in_it(self):
        b = {"levels": {"G": {"z": 0.0}},
             "elements": [wall("a", "G", (0, 0), (10, 0))], "openings": [],
             "unresolved": [{"id": "d", "class": "door", "family": "Glass", "type": "P", "level": "G", "host_kind": "Curtain Wall",
                             "plan_outline": [[4.0, 0.05], [5.2, 0.05], [5.2, -1.1], [4.0, -1.1]]}]}
        report = conventions.curtain_wall_infill(b)
        curtain = b["elements"][-1]["geometry"]
        self.assertEqual((curtain["p0"], curtain["p1"]), ([4.0, 0.0], [5.2, 0.0]))
        self.assertEqual(curtain["curtain"]["embedded_in"], "a")
        self.assertEqual(report["curtain_walls_inferred"][0]["panels"], ["d"])


class GroundUnified(unittest.TestCase):
    def test_floors_of_one_height_outside_the_building_join_across_a_garden_wall(self):
        b = {"levels": {"G": {"z": 0.0}, "U": {"z": 3.0}},
             "site": {"plot": {"ring": [[0, 0], [20, 0], [20, 10], [0, 10]]}},
             "elements": [floor("s", "U", [(0, 0), (5, 0), (5, 10), (0, 10)], 3.0, "main"),
                          floor("a", "G", [(5, 0), (10, 0), (10, 10), (5, 10)], -0.02),
                          floor("b", "G", [(10.2, 0), (20, 0), (20, 10), (10.2, 10)], 0.0),
                          floor("p", "G", [(15, 4), (17, 4), (17, 6), (15, 6)], 0.35),
                          wall("gw", "G", (10.1, 0), (10.1, 10))],
             "unresolved": []}
        b["elements"][3]["source_type"] = "other"
        report = conventions.unify_ground_floors(b)
        ids = [e["id"] for e in b["elements"] if e["class"] == "floor"]
        self.assertEqual(sorted(ids), ["b", "p", "s"])  # a joins the larger b, the planter stays
        joined = report["ground_floors_joined"][0]
        self.assertAlmostEqual(joined["area_m2"], 150 - 4, delta=0.5)


class Arcs(unittest.TestCase):
    def test_points_on_a_circle_become_one_arc_and_a_straight_run_one_line(self):
        circle = [(5 * math.cos(t / 20 * math.pi), 5 * math.sin(t / 20 * math.pi)) for t in range(11)]
        pieces = conventions.fit_arcs(circle, 0.01)
        self.assertEqual(len(pieces), 1)
        self.assertEqual(len(pieces[0]), 3)
        self.assertEqual(conventions.fit_arcs([(0, 0), (1, 0), (2, 0.001), (3, 0)], 0.01), [[[0, 0], [3, 0]]])

    def test_a_bend_splits_into_pieces(self):
        pieces = conventions.fit_arcs([(0, 0), (2, 0), (4, 0), (4, 2), (4, 4)], 0.01)
        self.assertEqual(pieces, [[[0, 0], [4, 0]], [[4, 0], [4, 4]]])


class PlanDrawings(unittest.TestCase):
    def view(self, key, name, level, offset=(0, 0)):
        return SimpleNamespace(key=key, source_name=name, kind="plan", level=level, offset=offset, orientation=None)

    def test_each_level_shows_its_base_plan_and_a_typical_storey_its_source(self):
        vs = {v.key: v for v in (self.view("FP_00", "Floor Plan: 00", "00"), self.view("FP_00_L", "Floor Plan: 00 landscape", "00"),
                                 self.view("FP_04", "Floor Plan: 03-04", "04"), self.view("FP_X", "Floor Plan: site", "00", None))}
        out = views.plan_drawings(vs, {"L00": {}, "L03": {}, "L04": {}}, {"L03": {"copied_from": "L04"}})
        self.assertEqual(out, {"L00": "views/FP_00.dxf", "L03": "views/FP_04.dxf", "L04": "views/FP_04.dxf"})


if __name__ == "__main__":
    unittest.main()
