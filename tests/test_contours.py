"""Floor outlines per storey: the main floor from the full-height walls, balconies outside it."""
import unittest

from dwf2bim import contours


def wall(i, p0, p1, z0=3.0, z1=6.0, width=0.2):
    return {"id": i, "class": "wall", "level": "U", "z0": z0, "z1": z1, "source_type": "W",
            "geometry": {"kind": "axis", "p0": list(p0), "p1": list(p1), "width": width}}


def floor(i, ring, role=None, z1=3.0, holes=()):
    return {"id": i, "class": "floor", "level": "U", "z0": z1 - 0.3, "z1": z1, "source_type": "F",
            "geometry": {"polygons": [{"outer": [list(q) for q in ring], "holes": [list(map(list, h)) for h in holes]}], "slab_role": role}}


def building():
    box = [wall("n", (0, 10), (10, 10)), wall("e", (10, 10), (10, 0)), wall("s", (10, 0), (0, 0)), wall("w", (0, 0), (0, 10)),
           wall("stub", (10, 5), (12, 5)),  # a full-height wall standing out between two balconies
           wall("p", (10, 0), (12, 0), z1=4.1)]  # a parapet: low, so it closes nothing
    shaft = [(3, 4), (5, 4), (5, 6), (3, 6)]
    floors = [floor("m1", [(0.1, 0.1), (6, 0.1), (6, 9.9), (0.1, 9.9)], "main", holes=[shaft]),
              floor("m2", [(6, 0.1), (9.9, 0.1), (9.9, 9.9), (6, 9.9)], "main"),
              floor("b", [(10, 0), (12, 0), (12, 4.9), (10, 4.9)], "balcony", z1=2.98)]
    lift = {"id": "lift", "family": "Lift", "outline": [[3.4, 4.4], [4.6, 4.4], [4.6, 5.6], [3.4, 5.6]]}
    return {"levels": {"G": {"z": 0.0}, "U": {"z": 3.0}}, "elements": box + floors, "areas": [], "shaft_hints": [lift]}


class Outlines(unittest.TestCase):
    def test_main_floor_runs_to_the_walls_outer_faces_with_its_shaft_and_no_stub(self):
        out = contours.storey_outlines(building(), "U")
        main = out["main"]
        self.assertEqual([round(v, 2) for v in main.bounds], [-0.1, -0.1, 10.1, 10.1])
        self.assertEqual(len(main.interiors), 1)  # the lift's shaft stays open
        self.assertEqual(len(out["balconies"]), 1)
        self.assertAlmostEqual(out["balconies"][0].area, 1.9 * 4.9, delta=0.3)

    def test_the_storey_gets_one_main_floor_and_one_floor_per_balcony(self):
        b = building()
        report = contours.apply(b)
        floors = {r["id"]: r for r in b["elements"] if r["class"] == "floor"}
        self.assertEqual(sorted(floors), ["b", "m1"])  # m2 joined the main floor
        self.assertEqual(floors["m1"]["geometry"]["slab_role"], "main")
        self.assertEqual(floors["m1"]["geometry"]["polygons"][0]["holes"], [])  # one plain outline
        self.assertEqual(len(floors["m1"]["geometry"]["shafts"]), 1)  # the shaft is cut in Revit as a shaft
        self.assertIn("U", b["contours"])
        self.assertEqual(report["storeys"][0]["balconies"], 1)

    def test_a_storey_copied_from_another_keeps_its_own_floor(self):
        b = building()
        upper = [dict(r, id=r["id"] + "@V", level="V", z0=r["z0"] + 3, z1=r["z1"] + 3) for r in b["elements"]]  # same geometry dicts
        b["levels"]["V"] = {"z": 6.0}
        b["elements"] += upper
        b["elements"] = [r for r in b["elements"] if r["id"] != "m1@V"]  # the upper storey has no shaft slab
        contours.apply(b)
        lower = next(r for r in b["elements"] if r["id"] == "m1")
        self.assertEqual(len(lower["geometry"]["shafts"]), 1)


class HolesNeedAReason(unittest.TestCase):
    """A hole in a main floor stays only for a stair, a lift, a shaft or a void."""

    def hole(self):
        from shapely.geometry import Polygon
        return Polygon([(0, 0), (6, 0), (6, 3), (0, 3)])

    def walls(self, *lines):
        from shapely.geometry import LineString
        from shapely.ops import unary_union
        return unary_union([LineString(l).buffer(0.1, cap_style=3) for l in lines]) if lines else self.hole().buffer(0).difference(self.hole())

    def test_a_hole_with_nothing_in_it_is_floored(self):
        kept, closed = contours.holes_with_a_reason([self.hole()], self.walls(), [], [], [])
        self.assertEqual(kept, [])
        self.assertEqual(closed[0]["floored_m2"], 18.0)

    def test_a_storey_with_such_a_hole_gets_a_whole_floor_and_says_so(self):
        b = building()
        b["shaft_hints"] = []
        out = contours.storey_outlines(b, "U")
        self.assertEqual(len(out["main"].interiors), 0)
        self.assertEqual(len(out["holes_closed"]), 1)

    def test_the_lobby_beside_a_lift_is_floored_and_the_lift_stays_open(self):
        from shapely.geometry import Polygon
        lift = Polygon([(0.5, 0.5), (1.5, 0.5), (1.5, 2.5), (0.5, 2.5)])
        kept, closed = contours.holes_with_a_reason([self.hole()], self.walls([(2, 0), (2, 3)]), [], [lift], [])
        self.assertEqual([reason for _, reason in kept], ["lift or shaft"])
        self.assertAlmostEqual(kept[0][0].area, 2 * 3, delta=0.5)
        self.assertAlmostEqual(closed[0]["floored_m2"], 4 * 3, delta=0.5)

    def test_a_void_cell_of_the_area_plan_opens_a_floor_drawn_across_it(self):
        from shapely.geometry import Polygon
        main = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        cell = Polygon([(2, 2), (4, 2), (4, 4), (2, 4)])
        out = contours.cells_to_open(main, [(cell, "area plan: void")], [])
        self.assertEqual([(round(shape.area, 2), reason) for shape, reason in out], [(4.0, "area plan: void")])
        self.assertEqual(contours.cells_to_open(main, [(cell, "area plan: void")], [cell.buffer(0.1)]), [])  # open already

    def test_a_lift_cell_counts_between_the_storeys_that_show_it(self):
        ring = [[2, 2], [4, 2], [4, 4], [2, 4]]
        b = {"levels": {"A": {"z": 3.0}, "B": {"z": 6.0}, "C": {"z": 9.0}, "D": {"z": 12.0}},
             "areas": [{"levels": ["A"], "walls_inside_share": 0.95, "cells": [{"c": "1010", "ring": ring}, {"c": "1", "ring": ring}]},
                       {"levels": ["B"], "walls_inside_share": 0.2, "cells": [{"c": "1010", "ring": ring}]},
                       {"levels": ["C"], "walls_inside_share": 0.95, "cells": [{"c": "1010", "ring": ring}]}]}
        self.assertEqual(len(contours.lift_cells(b, 6.0)), 2)  # the storey between, whose own plan is poor
        self.assertEqual(contours.lift_cells(b, 12.0), [])  # above the last storey that shows the lift

    def test_a_storey_opens_for_the_lift_its_area_plan_shows(self):
        b = building()
        b["shaft_hints"] = []
        b["areas"] = [{"levels": ["U"], "walls_inside_share": 0.95, "cells": [{"c": "1010", "ring": [[7, 7], [9, 7], [9, 9], [7, 9]]}]}]
        out = contours.storey_outlines(b, "U")
        self.assertEqual([r.split(":")[0] for r in out["shaft_reasons"]], ["area plan"])
        self.assertEqual(len(out["main"].interiors), 1)  # the drawn hole with nothing in it is floored, the lift is open

    def test_a_hole_a_stair_fills_stays_whole(self):
        from shapely.geometry import Polygon
        stair = Polygon([(0.2, 0.2), (5.8, 0.2), (5.8, 2.8), (0.2, 2.8)])
        kept, closed = contours.holes_with_a_reason([self.hole()], self.walls(), [stair], [], [])
        self.assertEqual((len(kept), closed), (1, []))
        self.assertAlmostEqual(kept[0][0].area, 18.0)

    def test_a_small_hole_closed_by_walls_is_a_shaft(self):
        from shapely.geometry import Polygon
        duct = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        ring = self.walls([(0, 0), (1, 0)], [(1, 0), (1, 1)], [(1, 1), (0, 1)], [(0, 1), (0, 0)])
        kept, _ = contours.holes_with_a_reason([duct.buffer(0.05, join_style=2)], ring, [], [], [])
        self.assertEqual([reason for _, reason in kept], ["shaft closed by walls"])

    def test_a_stair_opening_covers_its_flights_and_the_landing_where_they_turn(self):
        treads = [{"polygon": [[x, 0], [x + 0.3, 0], [x + 0.3, 1], [x, 1]], "z_top": 0.2} for x in (0, 0.3, 0.6)]
        treads += [{"polygon": [[x, 1.2], [x + 0.3, 1.2], [x + 0.3, 2.2], [x, 2.2]], "z_top": 1.0} for x in (0.6, 0.3, 0)]
        flights = [{"start": [0, 0.5], "end": [0.9, 0.5], "width": 1.0, "risers": 3}, {"start": [0.9, 1.7], "end": [0, 1.7], "width": 1.0, "risers": 3}]
        shape = contours.stair_opening({"geometry": {"kind": "treads", "treads": treads, "flights": flights}})
        self.assertEqual([round(v, 2) for v in shape.bounds], [0.0, 0.0, 1.9, 2.2])  # a landing one flight width past the turn


if __name__ == "__main__":
    unittest.main()
