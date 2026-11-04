import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dwf2bim import dynamo, fetch

AREA = "חישוב שטחים"
PLAN = "תכנית מתוקנת (אדריכלות)"


class ChoosingDocuments(unittest.TestCase):
    def test_newest_of_the_kind_comes_first_by_date_not_by_text(self):
        rows = [{"type": AREA, "date": "6/5/2020", "url": "a"}, {"type": AREA, "date": "28/12/2021", "url": "b"},
                {"type": AREA, "date": "4/3/2024", "url": "c"}, {"type": PLAN, "date": "1/1/2026", "url": "d"}, {"type": AREA, "date": "", "url": "e"}]
        self.assertEqual([r["url"] for r in fetch.candidates(rows, "area")], ["c", "b", "a", "e"])

    def test_documents_of_different_requests_are_flagged(self):
        chosen = {"architecture": {"online_request": "28078"}, "area": {"online_request": "28078"}, "survey": {"online_request": "21683"}}
        warnings = fetch.request_warnings(chosen)
        self.assertEqual(len(warnings), 1)
        self.assertIn("survey", warnings[0])

    def test_block_and_parcel_must_be_numbers(self):
        self.assertEqual(fetch.project_name(" 6625", "538 "), "6625_538")
        for block, parcel in (("66/25", "538"), ("..", "1"), ("", "1")):
            with self.assertRaises(fetch.FetchError):
                fetch.project_name(block, parcel)


class ReadingDownloads(unittest.TestCase):
    def test_format_comes_from_the_content(self):
        self.assertEqual(fetch.file_format(b"(DWF V06.00)"), ".dwf")
        self.assertEqual(fetch.file_format(b"PK\x03\x04"), ".dwfx")
        self.assertEqual(fetch.file_format(b"%PDF-1.7"), ".pdf")
        self.assertEqual(fetch.file_format(b"<html>"), "unknown")

    def test_answer_may_be_encoded_twice(self):
        answer = {"fileName": "a.dwf", "buffer": base64.b64encode(b"(DWF").decode()}
        for body in (json.dumps(answer), json.dumps(json.dumps(answer))):
            self.assertEqual(fetch.decode_download(body.encode()), ("a.dwf", b"(DWF"))

    def test_answer_without_a_file_raises(self):
        for body in (b"<html>", b'{"fileName": "a"}', b"null"):
            with self.assertRaises(fetch.FetchError):
                fetch.decode_download(body)

    def test_a_name_from_the_server_cannot_leave_the_folder(self):
        self.assertEqual(fetch.safe_name("..\\..\\evil.dwf"), "evil.dwf")
        self.assertEqual(fetch.safe_name("../../evil.dwf"), "evil.dwf")
        self.assertEqual(fetch.safe_name("a:b?.dwf"), "a_b_.dwf")
        self.assertEqual(fetch.safe_name(None), "document")
        self.assertEqual(fetch.safe_name(".."), "document")


class KeepingFiles(unittest.TestCase):
    def test_older_files_move_aside_and_are_not_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder)
            for name in ("new.dwf", "old.dwf", "old.dwfx", "fetch_manifest.json"):
                (dest / name).write_text(name)
            fetch.retire_old_files(dest, {"new.dwf"}, lambda m: None)
            self.assertEqual(sorted(f.name for f in dest.iterdir() if f.is_file()), ["fetch_manifest.json", "new.dwf"])
            self.assertEqual(sorted(f.name for f in dest.glob("_previous/*/*")), ["old.dwf", "old.dwfx"])

    def test_same_content_is_not_saved_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder)
            (dest / "mine.dwf").write_bytes(b"content")
            self.assertEqual(fetch._existing_file(dest, b"content"), "mine.dwf")
            self.assertIsNone(fetch._existing_file(dest, b"other!!"))


class DynamoGraph(unittest.TestCase):
    def test_graph_builds_the_named_project_and_only_on_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = dynamo.write_graph(Path(r"C:\some folder\dwf2bim"), "6625_538", Path(folder))
            graph = json.loads(path.read_text(encoding="utf8"))
        self.assertEqual(path.name, "Build_6625_538.dyn")
        self.assertEqual(graph["View"]["Dynamo"]["RunType"], "Manual")
        code = next(n["Code"] for n in graph["Nodes"] if n["NodeType"] == "PythonScriptNode")
        self.assertIn(r'ROOT = r"C:\some folder\dwf2bim"', code)
        self.assertIn('BUILDING = "6625_538"', code)
        compile(code, "node", "exec")

    def test_deleting_the_file_model_is_off_by_default(self):
        graph = dynamo.graph(Path("C:/x"), "1_2")
        switch = next(n for n in graph["Nodes"] if n["NodeType"] == "BooleanInputNode")
        self.assertIs(switch["InputValue"], False)

    def test_every_connector_joins_ports_of_the_graph(self):
        graph = dynamo.graph(Path("C:/x"), "1_2")
        outputs = {p["Id"] for n in graph["Nodes"] for p in n["Outputs"]}
        inputs = {p["Id"] for n in graph["Nodes"] for p in n["Inputs"]}
        self.assertEqual(len(graph["Connectors"]), 2)
        for c in graph["Connectors"]:
            self.assertIn(c["Start"], outputs)
            self.assertIn(c["End"], inputs)
        self.assertEqual({v["Id"] for v in graph["View"]["NodeViews"]}, {n["Id"] for n in graph["Nodes"]})


if __name__ == "__main__":
    unittest.main()
