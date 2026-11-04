"""Every Revit API name the scripts import must exist in that exact namespace.

A wrong namespace once stopped the whole build script from running. It needs the Revit
2026 assemblies, and is skipped where they are not installed.
"""
import ast
import importlib.util
import os
import unittest
from pathlib import Path

REVIT = r"C:\Program Files\Autodesk\Revit 2026"
SCRIPTS = Path(__file__).resolve().parents[1] / "revit"


@unittest.skipUnless(os.path.exists(os.path.join(REVIT, "RevitAPI.dll")) and importlib.util.find_spec("dnfile"), "Revit assemblies or dnfile not available")
class RevitImports(unittest.TestCase):
    def test_names_exist_in_their_namespace(self):
        import dnfile

        known = {}
        for dll in ("RevitAPI.dll", "RevitAPIUI.dll"):
            for t in dnfile.dnPE(os.path.join(REVIT, dll)).net.mdtables.TypeDef.rows:
                known.setdefault(str(t.TypeName), set()).add(str(t.TypeNamespace))
        wrong = []
        for script in SCRIPTS.glob("*.py"):
            for node in ast.walk(ast.parse(script.read_text(encoding="utf8"))):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("Autodesk"):
                    wrong += [(script.name, node.module, a.name) for a in node.names if node.module not in known.get(a.name, set())]
        self.assertEqual(wrong, [])


class Methods(unittest.TestCase):
    """A method lost in an edit fails only at run time, inside Revit."""

    def test_every_method_called_on_self_exists(self):
        for script in SCRIPTS.glob("*.py"):
            tree = ast.parse(script.read_text(encoding="utf8"))
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                defined = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
                called = {
                    n.func.attr for n in ast.walk(cls)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"
                }
                self.assertEqual(called - defined, set(), "{} {}".format(script.name, cls.name))

    def test_builder_methods_called_from_main_exist(self):
        script = SCRIPTS / "build_native.py"
        tree = ast.parse(script.read_text(encoding="utf8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Builder")
        defined = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        called = {
            n.func.attr for n in ast.walk(main)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "builder"
        }
        self.assertTrue(called)
        self.assertEqual(called - defined, set())


class NotInRevit(unittest.TestCase):
    def test_a_wrong_import_inside_revit_is_not_silently_ignored(self):
        source = (SCRIPTS / "build_native.py").read_text(encoding="utf8")
        start = source.index("import clr")
        end = source.index("IN_REVIT = False")
        self.assertLess(end - start, 80, "only the clr import may be guarded")


if __name__ == "__main__":
    unittest.main()
