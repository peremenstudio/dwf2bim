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
    return {"levels": {"G": {"z": 0.0}, "U": {"z": 3.0}}, "elements": box + floors, "areas": []}


class Outlines(unittest.TestCase):
    def test_main_floor_runs_to_the_walls_outer_faces_with_its_shaft_and_no_stub(self):
        out = contours.storey_outlines(building(), "U")
        main = out["main"]
        self.assertEqual([round(v, 2) for v in main.bounds], [-0.1, -0.1, 10.1, 10.1])
        self.assertEqual(len(main.interiors), 1)  # the shaft no floor covers stays open
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


if __name__ == "__main__":
    unittest.main()
