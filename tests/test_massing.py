"""A model from the area plan alone: storeys stacked by their shared corners, levels from the heights, walls round each floor."""
import unittest

from shapely.geometry import Polygon, box

from dwf2bim import massing


def item(floors, height, cells, at=(0.0, 0.0)):
    """A panel as the stacking sees it: cells in metres, drawn anywhere on the sheet."""
    moved = {n: Polygon([(x + at[0], y + at[1]) for x, y in c.exterior.coords]) for n, c in cells.items()}
    return {"panel": {"floors": floors, "height": height}, "k": 1.0, "cells": moved}


CORE = {"1": box(0, 0, 10, 8), "2": box(4, 3, 6, 5)}


class Stacking(unittest.TestCase):
    def test_storeys_drawn_apart_on_the_sheet_land_on_each_other(self):
        items = [item([0], 3.0, CORE), item([1], 3.0, CORE, at=(40.0, -7.0)), item([-1], 3.0, CORE, at=(-25.0, 13.0))]
        notes = massing.stack_panels(items)
        self.assertEqual(notes, [])
        self.assertEqual(items[0]["shift"], (0.0, 0.0))
        self.assertAlmostEqual(items[1]["shift"][0], -40.0, delta=0.05)
        self.assertAlmostEqual(items[1]["shift"][1], 7.0, delta=0.05)
        self.assertAlmostEqual(items[2]["shift"][0], 25.0, delta=0.05)

    def test_a_storey_sharing_few_corners_with_its_neighbour_is_noted(self):
        odd = {"1": Polygon([(0.3, 0.2), (7.1, 0.4), (6.6, 5.2)])}
        items = [item([0], 3.0, CORE), item([1], 3.0, odd, at=(40.0, 0.0))]
        self.assertEqual(len(massing.stack_panels(items)), 1)


class Heights(unittest.TestCase):
    def test_levels_rise_by_each_storeys_height_from_the_ground_at_zero(self):
        items = [item([n], h, CORE) for n, h in ((-1, 2.8), (0, 3.4), (1, 3.1), (2, 3.1))]
        z = massing._heights(items)
        self.assertEqual({n: v[0] for n, v in z.items()}, {-1: -2.8, 0: 0.0, 1: 3.4, 2: 6.5})

    def test_a_storey_number_the_plan_skips_takes_the_height_below_it(self):
        z = massing._heights([item([0], 3.0, CORE), item([1], 3.0, CORE), item([3], 3.0, CORE)])
        self.assertEqual(z[3][0], 9.0)

    def test_one_panel_for_two_storeys_gives_both(self):
        z = massing._heights([item([0], 3.0, CORE), item([1, 2], 3.0, CORE)])
        self.assertEqual((z[1][0], z[2][0]), (3.0, 6.0))


class Parts(unittest.TestCase):
    def test_codes_from_two_thousand_are_outside_the_building(self):
        self.assertTrue(massing._is_outside("2004"))
        self.assertFalse(massing._is_outside("1017"))
        self.assertFalse(massing._is_outside("-1"))

    def test_walls_stand_inside_the_outline_with_their_outer_face_on_it(self):
        walls = massing._walls(box(0, 0, 10, 8), "L00", 0.0, 3.0, 0.25, "t")
        self.assertEqual(len(walls), 4)
        xs = [v for w in walls for v in (w["geometry"]["p0"][0], w["geometry"]["p1"][0])]
        self.assertEqual((min(xs), max(xs)), (0.125, 9.875))
        self.assertTrue(all(w["exterior"] and w["inferred"] and (w["z0"], w["z1"]) == (0.0, 3.0) for w in walls))
        self.assertEqual(len({w["id"] for w in walls}), 4)

    def test_a_floor_keeps_its_openings_apart_from_its_outline(self):
        rec = massing._floor("f", "L01", 3.0, box(0, 0, 10, 8), "main", 0.3, shafts=[[(4, 3), (6, 3), (6, 5), (4, 5)]])
        self.assertEqual((rec["z0"], rec["z1"], rec["exterior"]), (2.7, 3.0, False))
        self.assertEqual(rec["geometry"]["polygons"][0]["holes"], [])
        self.assertEqual(len(rec["geometry"]["shafts"]), 1)


if __name__ == "__main__":
    unittest.main()
