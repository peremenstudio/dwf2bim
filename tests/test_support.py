"""What stands on nothing, and the model's verdict. Numbers here belong to no project."""
import unittest

from dwf2bim import support


def wall(i, p0, p1, z0, z1, level="L01"):
    return {"id": i, "class": "wall", "level": level, "type": "W", "z0": z0, "z1": z1,
            "geometry": {"kind": "axis", "p0": list(p0), "p1": list(p1), "width": 0.2}}


def floor(i, ring, z0, z1, level="L01"):
    return {"id": i, "class": "floor", "level": level, "type": "F", "z0": z0, "z1": z1,
            "geometry": {"kind": "extrusion", "polygons": [{"outer": [list(q) for q in ring], "holes": []}]}}


def opening(i, host, z0, height=1.0):
    return {"id": i, "class": "window", "level": "L01", "type": "O", "host": host, "z0": z0, "height": height,
            "center": [2.0, 0.0], "direction": [1.0, 0.0], "width": 1.0}


SQUARE = [(0, 0), (5, 0), (5, 5), (0, 5)]


def model(elements, openings=()):
    return {"levels": {"L00": {"z": 0.0}, "L01": {"z": 3.0}}, "elements": list(elements), "openings": list(openings)}


def flagged(building):
    return {r["id"]: r["why"] for r in support.check(building)["unsupported"]}


class StandingOnNothing(unittest.TestCase):
    def setUp(self):
        self.ground = wall("g", (0, 0), (5, 0), 0.0, 3.0, "L00")
        self.slab = floor("s", SQUARE, 2.8, 3.0)

    def test_a_wall_on_a_floor_and_a_floor_on_a_wall_stand(self):
        self.assertEqual(flagged(model([self.ground, self.slab, wall("w", (1, 2), (4, 2), 3.0, 6.0)])), {})

    def test_a_wall_with_nothing_under_it_is_listed(self):
        out = flagged(model([self.ground, self.slab, wall("w", (10, 2), (14, 2), 3.0, 6.0)]))
        self.assertEqual(list(out), ["w"])

    def test_a_wall_on_the_wall_below_stands_also_when_they_overlap_in_height(self):
        upper = wall("w", (0, 0), (5, 0), 2.6, 6.0)
        self.assertEqual(flagged(model([self.ground, upper])), {})

    def test_a_short_wall_under_a_slab_hangs_from_it(self):
        beam = wall("b", (1, 2), (4, 2), 2.2, 2.8)
        self.assertEqual(flagged(model([self.ground, self.slab, beam])), {})

    def test_a_tall_wall_that_only_touches_the_slab_above_is_listed(self):
        hanging = wall("h", (1, 2), (4, 2), 0.9, 2.8)
        self.assertIn("h", flagged(model([self.ground, self.slab, hanging])))

    def test_a_floor_far_from_any_wall_or_floor_is_listed(self):
        island = floor("i", [(20, 20), (22, 20), (22, 22), (20, 22)], 2.8, 3.0)
        self.assertEqual(list(flagged(model([self.ground, self.slab, island]))), ["i"])

    def test_a_balcony_beside_its_storey_floor_stands(self):
        balcony = floor("b", [(5, 0), (7, 0), (7, 5), (5, 5)], 2.75, 2.95)
        self.assertEqual(flagged(model([self.ground, self.slab, balcony])), {})

    def test_what_is_in_the_ground_is_not_checked(self):
        self.assertEqual(flagged(model([wall("f", (30, 0), (35, 0), -0.4, 1.0, "L00")])), {})

    def test_a_railing_needs_a_floor_at_its_base(self):
        rail = {"id": "r", "class": "railing", "level": "L01", "type": "R", "z0": 3.0, "z1": 4.1,
                "geometry": {"kind": "extrusion", "polygons": [{"outer": [[5, 0], [5.1, 0], [5.1, 5], [5, 5]], "holes": []}]}}
        self.assertEqual(flagged(model([self.ground, self.slab, rail])), {})
        rail = dict(rail, z0=5.0, z1=6.1)
        self.assertEqual(list(flagged(model([self.ground, self.slab, rail]))), ["r"])

    def test_an_opening_outside_its_wall_or_with_no_wall_is_listed(self):
        out = flagged(model([self.ground], [opening("in", "g", 1.0), opening("low", "g", -12.0), opening("none", None, 1.0)]))
        self.assertEqual(out, {"low": "outside the height of its wall", "none": "no host wall"})


class Verdict(unittest.TestCase):
    def build(self, elements, **more):
        b = model(elements)
        b.update(unresolved=[], issues=[], stats={"unclassified_families": {}}, site={"placement": {"rotation_deg": 0}},
                 areas=[{"levels": ["L01"], "walls_inside_share": 0.95}])
        b.update(more)
        support.check(b)
        return support.quality(b)

    def test_a_model_with_nothing_over_its_limits_is_ready(self):
        out = self.build([wall("g", (0, 0), (5, 0), 0.0, 3.0, "L00"), floor("s", SQUARE, 2.8, 3.0)])
        self.assertEqual((out["verdict"], out["reasons"]), ("ready", []))

    def test_an_element_in_the_air_asks_for_review(self):
        out = self.build([wall("g", (0, 0), (5, 0), 0.0, 3.0, "L00"), floor("s", SQUARE, 2.8, 3.0)] +
                         [wall(f"k{n}", (n, 1), (n, 4), 0.0, 3.0, "L00") for n in range(1, 30)] + [wall("w", (40, 0), (45, 0), 3.0, 6.0)])
        self.assertEqual(out["verdict"], "review")
        self.assertTrue(any("unsupported_share" in r for r in out["reasons"]))

    def test_many_elements_not_rebuilt_fail_the_model(self):
        out = self.build([wall("g", (0, 0), (5, 0), 0.0, 3.0, "L00")], unresolved=[{"id": str(n), "class": "wall"} for n in range(5)])
        self.assertEqual(out["verdict"], "failed")

    def test_no_survey_and_no_area_plan_ask_for_review(self):
        out = self.build([wall("g", (0, 0), (5, 0), 0.0, 3.0, "L00")], site={}, areas=[])
        self.assertEqual(out["verdict"], "review")
        self.assertEqual(len(out["reasons"]), 2)


if __name__ == "__main__":
    unittest.main()
