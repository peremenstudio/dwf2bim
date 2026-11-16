"""The layers of a view's DXF: one per class, with the dimensions left out."""
import types
import unittest

from dwf2bim import qa
from dwf2bim.extract import Rules


def element(family, type_name=None):
    return types.SimpleNamespace(family=family, props={"Type Name": type_name} if type_name else {})


class DrawingLayers(unittest.TestCase):
    def setUp(self):
        elements = {1: element("Basic Wall"), 2: element("Linear Dimension Style"), 3: element("Spot Elevations"),
                    4: element("Text"), 5: element("Detail Lines"), 6: element("A family no rule knows")}
        sheet = types.SimpleNamespace(elements=elements, node_to_element={n: n for n in elements})
        self.layer = qa.drawing_layers(sheet, Rules())

    def of(self, node, annotation=False):
        return self.layer(types.SimpleNamespace(node=node), annotation)

    def test_model_graphics_go_on_their_class(self):
        self.assertEqual(self.of(1), "DWF_WALL")
        self.assertEqual(self.of(5), "DWF_DETAIL")
        self.assertEqual(self.of(6), "DWF_OTHER")
        self.assertEqual(self.of(99), "DWF_OTHER")

    def test_dimensions_are_left_out_of_both_streams(self):
        self.assertIsNone(self.of(2, annotation=True))
        self.assertIsNone(self.of(2))

    def test_annotation_goes_on_named_layers(self):
        self.assertEqual(self.of(3, annotation=True), "DWF_SPOT_HEIGHTS")
        self.assertEqual(self.of(4, annotation=True), "DWF_NOTES")
        self.assertEqual(self.of(99, annotation=True), "DWF_ANNOTATION")


if __name__ == "__main__":
    unittest.main()
