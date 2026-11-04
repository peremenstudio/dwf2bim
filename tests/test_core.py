import struct
import unittest

from dwf2bim import whip
from dwf2bim.extract import _close, _stack_treads, _tread_runs
from dwf2bim.names import absolute_datum, english, level_id
from shapely.geometry import Polygon, box


def wide(text):
    return b"{" + struct.pack("<I", len(text)) + text.encode("utf-16-le") + b"}"


class WhipDecoder(unittest.TestCase):
    def stream(self, body):
        return b"(W2D V06.01)" + body + b"(EndOfDWF)\r\n"

    def test_relative_coordinates_accumulate_from_origin(self):
        body = b"O" + struct.pack("<ii", 1000, 2000) + b"\x10\x03" + struct.pack("<hhhhhh", 0, 0, 10, 0, 0, -5)
        d = whip.decode(self.stream(body))
        self.assertEqual(d.primitives[0].points, [(1000, 2000), (1010, 2000), (1010, 1995)])

    def test_contour_sets_keep_rings_separate(self):
        body = b"k\x02\x03\x03" + struct.pack("<12i", *([1] * 12))
        d = whip.decode(self.stream(body))
        self.assertEqual([len(p.points) for p in d.primitives], [3, 3])

    def test_node_and_viewport_tag_following_graphics(self):
        vp = b"(Viewport 'Floor Plan: 01_GAR' (Contour 1 4 0,0 10,0 10,10 0,10) (Units Millimeters ((2064.7 0 0 0)(0 2064.7 0 0)(0 0 1 0)(0 0 0 1))))"
        body = vp + b"(Node 7 'Basic Wall [123]')" + b"l" + struct.pack("<4i", 1, 2, 3, 4)
        d = whip.decode(self.stream(body))
        self.assertEqual(d.views[0].name, "Floor Plan: 01_GAR")
        self.assertAlmostEqual(d.views[0].units_per_mm, 2064.7)
        self.assertEqual((d.primitives[0].node, d.primitives[0].view), (7, 0))
        self.assertEqual(d.nodes[7], "Basic Wall [123]")

    def test_hebrew_viewport_name_is_hex_utf16(self):
        hexname = "Section: א".encode("utf-16-be").hex().upper().encode()
        body = b'(Viewport "' + hexname + b'" (Contour 1 4 0,0 1,0 1,1 0,1) (Units Millimeters ((1 0 0 0)(0 1 0 0)(0 0 1 0)(0 0 0 1))))'
        self.assertEqual(whip.decode(self.stream(body)).views[0].name, "Section: א")

    def test_text_supports_wide_and_quoted_strings(self):
        body = b"x" + struct.pack("<ii", 5, 5) + wide("אb") + b"x" + struct.pack("<ii", 1, 1) + b"'12.26'"
        d = whip.decode(self.stream(body))
        self.assertEqual([p.text for p in d.primitives], ["אb", "12.26"])

    def test_unknown_opcode_is_an_error_not_silent(self):
        with self.assertRaises(whip.WhipError):
            whip.decode(self.stream(b"\x93"))

    def test_truncated_stream_is_an_error(self):
        with self.assertRaises((whip.WhipError, struct.error)):
            whip.decode(b"(W2D V06.01)l" + b"\x00\x00")


class Names(unittest.TestCase):
    def test_no_hebrew_survives(self):
        for raw in ("בטון 20", "קיר בלוק 10", "דלת שלא במילון"):
            self.assertTrue(english(raw).isascii())
        self.assertEqual(english("בטון 20"), "Concrete 20")

    def test_level_ids(self):
        self.assertEqual(level_id("-02"), "L-02")
        self.assertEqual(level_id("0.00=19.36"), "L00")
        self.assertEqual(level_id("‏גג"), "ROOF")

    def test_datum(self):
        self.assertEqual(absolute_datum(["01", "0.00=19.36"]), 19.36)
        self.assertIsNone(absolute_datum(["01"]))


class Geometry(unittest.TestCase):
    def test_closing_does_not_grow_spikes_on_acute_corners(self):
        sliver = Polygon([(0, 0), (10, 0), (10, 0.5)])
        closed = _close(sliver, 0.3)
        self.assertLess(closed.bounds[2], 10.5)
        self.assertGreater(closed.bounds[0], -0.5)

    def test_slab_outline_removes_spikes_revit_refuses(self):
        from dwf2bim.extract import _ring_problem, _slab_outline
        # the shape of a slab Revit rejected: a slit folding back on itself
        ring = [(31.9623, 35.8627), (28.9669, 36.1768), (29.0707, 37.1661), (29.0389, 37.1696), (29.2667, 39.4057),
                (32.2911, 39.0913), (32.282, 39.0015), (29.298, 39.3144), (32.2829, 39.0014)]
        shape = Polygon(ring).buffer(0)
        self.assertIsNotNone(_ring_problem([[list(c) for c in ring]]))
        out = _slab_outline(shape)
        self.assertTrue(out)
        for poly in out:
            self.assertIsNone(_ring_problem([poly["outer"]] + poly["holes"]))
        self.assertGreater(sum(Polygon(q["outer"]).area for q in out), shape.area * 0.9)

    def test_stair_running_against_its_core_takes_their_direction(self):
        from dwf2bim.extract import Extractor

        def stair(i, ys):
            flights = [{"start": [22.88, ys[0]], "end": [22.88, ys[1]], "width": 1.15, "risers": 10},
                       {"start": [21.62, ys[1]], "end": [21.62, ys[0]], "width": 1.15, "risers": 10}]
            return {"id": i, "class": "stair", "geometry": {"flights": flights, "direction_checked": True, "risers": 20}}

        built = {"a": stair("a", (38.2, 40.7)), "b": stair("b", (38.2, 40.7)), "c": stair("c", (38.2, 40.7)), "odd": stair("odd", (40.7, 38.2))}
        Extractor._borrow_stair_layout(None, built)
        self.assertEqual(built["odd"]["geometry"]["flights"][0]["start"], [22.88, 38.2])
        self.assertEqual(built["a"]["geometry"]["flights"][0]["start"], [22.88, 38.2])

    def test_stair_in_one_plan_borrows_from_the_same_core(self):
        from dwf2bim.extract import Extractor

        def stair(i, checked, ys):
            flights = [{"start": [22.88, ys[0]], "end": [22.88, ys[1]], "width": 1.15, "risers": 20}]
            return {"id": i, "class": "stair", "geometry": {"flights": flights, "direction_checked": checked, "risers": 20}}

        built = {"sure": stair("sure", True, (38.2, 43.0)), "seen_once": stair("seen_once", False, (43.0, 38.2))}
        Extractor._borrow_stair_layout(None, built)
        self.assertEqual(built["seen_once"]["geometry"]["flights"][0]["start"], [22.88, 38.2])
        self.assertTrue(built["seen_once"]["geometry"]["direction_checked"])

    def test_clean_slab_keeps_its_shape(self):
        from dwf2bim.extract import _slab_outline
        out = _slab_outline(box(0, 0, 10, 5))
        self.assertAlmostEqual(Polygon(out[0]["outer"]).area, 50.0, places=3)
        self.assertEqual(len(out[0]["outer"]), 4)

    def test_closing_fills_gap(self):
        two = box(0, 0, 1, 1).union(box(1.1, 0, 2, 1))
        self.assertEqual(_close(two, 0.1).geom_type, "Polygon")

    def test_tread_lines_become_one_rising_run(self):
        segs = [((0.0, i * 0.28), (1.2, i * 0.28)) for i in range(6)]
        runs = _tread_runs(segs)
        self.assertEqual(len(runs), 1)
        treads = _stack_treads(runs, 0.0, 0.17, 20)
        self.assertEqual(len(treads), 6)
        self.assertAlmostEqual(treads[-1]["z_top"], 6 * 0.17)

    def test_unevenly_spaced_lines_are_not_a_run(self):
        segs = [((0.0, y), (1.2, y)) for y in (0.0, 2.0, 5.0)]
        self.assertEqual(_tread_runs(segs), [])


if __name__ == "__main__":
    unittest.main()
