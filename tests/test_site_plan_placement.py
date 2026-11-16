"""A model placed on the survey through the site plan of its drawing sheet, and the survey's plot found with nothing to compare it to."""
import math
import types
import unittest

from dwf2bim import survey

PLOT = [(0.0, 0.0), (30.0, 0.0), (33.0, 22.0), (14.0, 26.0), (-2.0, 20.0)]
HOUSE = [(0.0, 0.0), (12.0, 0.0), (12.0, 5.0), (7.0, 5.0), (7.0, 9.0), (0.0, 9.0)]  # an L: it fits one way only


def moved(ring, theta, tx, ty, scale=1.0):
    c, s = math.cos(theta), math.sin(theta)
    return [((c * x - s * y + tx) * scale, (s * x + c * y + ty) * scale) for x, y in ring]


def edges(ring):
    return [(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))]


class ThroughTheSitePlan(unittest.TestCase):
    def sheet(self, unit):
        """A sheet drawn in this unit: the plot turned and shifted, with the house inside it, and a border."""
        plot = moved(PLOT, 0.4, 500.0, 300.0, 1 / unit)
        house = moved(moved(HOUSE, 0.2, 8.0, 6.0), 0.4, 500.0, 300.0, 1 / unit)
        border = [(0.0, 0.0), (900.0 / unit, 0.0), (900.0 / unit, 700.0 / unit), (0.0, 700.0 / unit)]
        return edges(plot) + edges(house) + edges(border)

    def test_the_model_lands_where_the_site_plan_draws_it_in_the_plot(self):
        on_map = moved(PLOT, -1.1, 180200.0, 668300.0)
        for unit in (0.01, 0.25):
            found = survey.place_by_site_plan(on_map, HOUSE, HOUSE, self.sheet(unit), [1.0, 0.01, 0.25])
            self.assertIsNotNone(found, unit)
            self.assertEqual(found["unit"], unit)
            # the house stands at (8, 6) in the plot, turned 0.2: the same on the map, after the map's own turn
            self.assertAlmostEqual(found["rotation"], 0.2 - 1.1, places=3)
            want = moved(moved(HOUSE, 0.2, 8.0, 6.0), -1.1, 180200.0, 668300.0)[0]
            self.assertAlmostEqual(found["tx"], want[0], delta=0.05)
            self.assertAlmostEqual(found["ty"], want[1], delta=0.05)
            self.assertTrue(found["sure"])

    def test_a_sheet_without_the_plot_gives_no_place(self):
        border = edges([(0.0, 0.0), (90000.0, 0.0), (90000.0, 70000.0), (0.0, 70000.0)])
        self.assertIsNone(survey.place_by_site_plan(moved(PLOT, 0.0, 180200.0, 668300.0), HOUSE, HOUSE, border * 4, [0.01]))

    def test_a_plot_with_no_building_drawn_in_it_gives_no_place(self):
        sheet = edges(moved(PLOT, 0.4, 500.0, 300.0, 100.0)) + edges([(0.0, 0.0), (90000.0, 0.0), (90000.0, 70000.0), (0.0, 70000.0)])
        self.assertIsNone(survey.place_by_site_plan(moved(PLOT, 0.0, 180200.0, 668300.0), HOUSE, HOUSE, sheet, [0.01]))


class PlotCandidates(unittest.TestCase):
    def sheet(self, coloured):
        """A survey sheet in map coordinates: each colour with its segments."""
        paths = [types.SimpleNamespace(stroke=stroke, rings=[([(a[0], -a[1]), (b[0], -b[1])], False)]) for stroke, segs in coloured.items() for a, b in segs]
        return types.SimpleNamespace(paths=paths, drawing_transform=(1.0, 1.0, 0.0, 0.0), clip=(0.0, 0.0, 0.0, 0.0), content_matrix=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0))

    def test_the_colour_that_is_mostly_one_closed_ring_comes_first(self):
        plot = moved(PLOT, 0.3, 180200.0, 668300.0)
        shed = moved([(0, 0), (12, 0), (12, 9), (0, 9)], 0.0, 180260.0, 668300.0)
        loose = [((180000.0 + k, 668000.0), (180000.0 + k, 668200.0)) for k in range(0, 60, 3)]
        rings = survey.plot_candidates(self.sheet({"#112233": edges(plot), "#000000": edges(shed) + loose}))
        self.assertEqual(len(rings), 2)
        self.assertEqual(len(rings[0]), len(PLOT))  # the plot's own colour draws nothing else

    def test_a_ring_too_small_or_too_large_for_a_plot_is_no_candidate(self):
        hut = moved([(0, 0), (4, 0), (4, 3), (0, 3)], 0.0, 180200.0, 668300.0)
        frame = moved([(0, 0), (400, 0), (400, 300), (0, 300)], 0.0, 180000.0, 668000.0)
        self.assertEqual(survey.plot_candidates(self.sheet({"#112233": edges(hut), "#445566": edges(frame)})), [])


if __name__ == "__main__":
    unittest.main()
