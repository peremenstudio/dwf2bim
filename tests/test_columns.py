"""Columns: read from their cut in plan, carried into the model, and counted as what carries a floor."""
import unittest

from shapely.geometry import Point, Polygon

from dwf2bim import support
from dwf2bim.extract import Rules, column_section


class Section(unittest.TestCase):
    def test_a_round_cut_is_a_round_column_with_its_diameter(self):
        g = column_section(Point(3, 4).buffer(0.25))
        self.assertEqual(g["section"]["shape"], "round")
        self.assertAlmostEqual(g["section"]["diameter"], 0.5, delta=0.01)
        self.assertEqual(g["center"], [3.0, 4.0])

    def test_a_rectangular_cut_keeps_its_sides_and_its_turn(self):
        g = column_section(Polygon([(0, 0), (0.8, 0), (0.8, 0.3), (0, 0.3)]))
        self.assertEqual(g["section"], {"shape": "rectangle", "b": 0.3, "h": 0.8, "angle_deg": 0.0})
        turned = column_section(Polygon([(0, 0), (0.3, 0), (0.3, 0.8), (0, 0.8)]))
        self.assertEqual(turned["section"]["angle_deg"], 90.0)

    def test_a_square_cut_is_a_rectangle_not_a_circle(self):
        self.assertEqual(column_section(Polygon([(0, 0), (0.4, 0), (0.4, 0.4), (0, 0.4)]))["section"]["shape"], "rectangle")


class Class(unittest.TestCase):
    def test_columns_are_a_class_that_is_built_and_beams_stay_structure(self):
        rules = Rules()
        self.assertEqual(rules.classify("Concrete-Rectangular-Column"), "column")
        self.assertEqual(rules.classify("עמוד קונסטרוקטיבי"), "column")
        self.assertEqual(rules.classify("Concrete-Rectangular Beam1"), "structure")
        self.assertIn("column", rules.enabled)


class Carrying(unittest.TestCase):
    def test_a_floor_on_columns_stands(self):
        column = {"id": "c", "class": "column", "level": "L00", "type": "C", "z0": 0.0, "z1": 2.8,
                  "geometry": column_section(Point(2, 2).buffer(0.25))}
        slab = {"id": "s", "class": "floor", "level": "L01", "type": "F", "z0": 2.8, "z1": 3.0,
                "geometry": {"kind": "extrusion", "polygons": [{"outer": [[0, 0], [5, 0], [5, 5], [0, 5]], "holes": []}]}}
        b = {"levels": {"L00": {"z": 0.0}, "L01": {"z": 3.0}}, "elements": [column, slab], "openings": []}
        self.assertEqual(support.check(b)["unsupported"], [])
        b["elements"] = [slab]
        self.assertEqual([r["id"] for r in support.check(b)["unsupported"]], ["s"])


if __name__ == "__main__":
    unittest.main()
