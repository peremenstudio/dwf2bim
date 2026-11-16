"""The plot found on a survey map by its shape, whatever colour its boundary is drawn in."""
import math
import unittest

from dwf2bim import survey

PLOT = [(0.0, 0.0), (20.0, 0.0), (22.0, 15.0), (10.0, 18.0), (-1.0, 14.0)]


def on_map(ring, theta, tx, ty):
    c, s = math.cos(theta), math.sin(theta)
    return [(c * x - s * y + tx, s * x + c * y + ty) for x, y in ring]


def edges(ring):
    return [(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]


def clutter():
    # neighbouring parcels and a street, as any map has
    return [((180100.0 + k * 7, 668000.0), (180100.0 + k * 7, 668060.0)) for k in range(12)] + \
           [((180090.0, 668000.0 + k * 9), (180200.0, 668000.0 + k * 9)) for k in range(8)]


class Colours(unittest.TestCase):
    def test_near_colours_are_one_and_far_ones_are_not(self):
        from dwf2bim import site
        self.assertTrue(site.same_colour("#0000FF", "#0101FF", 12))
        self.assertTrue(site.same_colour("#FF0101FF", "#0101FF", 12))  # with an alpha byte in front
        self.assertFalse(site.same_colour("#0000FF", "#FF0000", 12))
        self.assertFalse(site.same_colour(None, "#0101FF", 12))


class PlotByShape(unittest.TestCase):
    def test_the_plot_is_found_among_other_lines_with_its_rotation_and_shift(self):
        theta, tx, ty = math.radians(-37.0), 180140.0, 668025.0
        found = survey.find_plot_by_shape(PLOT, edges(on_map(PLOT, theta, tx, ty)) + clutter())
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found[0], theta, places=3)
        self.assertAlmostEqual(found[1], tx, delta=0.02)
        self.assertAlmostEqual(found[2], ty, delta=0.02)
        self.assertLess(found[3].max(), 0.02)
        self.assertEqual(len(found[4]), len(PLOT))

    def test_a_map_without_the_plot_gives_nothing(self):
        self.assertIsNone(survey.find_plot_by_shape(PLOT, clutter()))

    def test_corners_that_meet_with_no_edges_drawn_between_them_do_not_count(self):
        ring = on_map(PLOT, 0.3, 180140.0, 668025.0)
        stubs = [((x, y), (x + 0.5, y + 0.5)) for x, y in ring]
        self.assertIsNone(survey.find_plot_by_shape(PLOT, stubs + clutter()))

    def test_a_plot_with_one_corner_drawn_otherwise_is_still_found(self):
        ring = on_map(PLOT, 1.1, 180140.0, 668025.0)
        moved = ring[:4] + [(ring[4][0] + 0.6, ring[4][1] + 0.4)]
        found = survey.find_plot_by_shape(PLOT, edges(moved) + edges(ring)[:3] + clutter())
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found[0], 1.1, places=3)

    def test_a_rounded_corner_is_left_out_of_the_match_and_the_plot_is_still_found(self):
        arc = [(20.0 + 2.0 * math.cos(math.radians(t)), 2.0 + 2.0 * math.sin(math.radians(t))) for t in range(-90, 1, 15)]
        rounded = [(0.0, 0.0)] + arc + [(22.0, 15.0), (10.0, 18.0), (-1.0, 14.0)]
        self.assertEqual(len(survey.sharp_corners(rounded)), 4)
        # the survey draws the same plot with a plain corner
        found = survey.find_plot_by_shape(rounded, edges(on_map([(0.0, 0.0), (22.0, 0.0), (22.0, 15.0), (10.0, 18.0), (-1.0, 14.0)], 0.5, 180140.0, 668025.0)) + clutter())
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found[0], 0.5, places=3)
        self.assertEqual(len(found[4]), len(rounded))



if __name__ == "__main__":
    unittest.main()
