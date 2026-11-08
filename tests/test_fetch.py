import base64
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dwf2bim import dynamo, fetch

AREA = fetch._CFG["documents"]["area"]["types"][0]
PLAN = fetch._CFG["documents"]["architecture"]["types"][0]


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

    def test_a_hebrew_file_name_is_kept(self):
        name = "\u05ea\u05db\u05e0\u05d9\u05ea \u05de\u05ea\u05d5\u05e7\u05e0\u05ea.dwf"
        self.assertEqual(fetch.safe_name(name), name)
        self.assertEqual(fetch.safe_name(".."), "document")


class DownloadProgress(unittest.TestCase):
    def test_a_slow_download_reports_and_stops_reporting_when_done(self):
        lines = []

        class Request:
            def get(self, url, timeout):
                time.sleep(0.25)
                return "answer"

        class Context:
            request = Request()

        with mock.patch.object(fetch, "PROGRESS_S", 0.1):
            self.assertEqual(fetch._get_with_progress(Context(), "u", lines.append), "answer")
            count = len(lines)
            time.sleep(0.25)
        self.assertGreaterEqual(count, 1)
        self.assertEqual(len(lines), count)
        self.assertIn("Still downloading", lines[0])

    def test_the_page_has_hebrew_for_the_waiting_messages(self):
        from dwf2bim import ui
        self.assertIn("A large file can take several minutes", ui.PAGE)
        self.assertIn("Still downloading", ui.PAGE)


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
            model = Path(folder) / "model"
            path = dynamo.write_graph(Path(r"C:\some folder\dwf2bim"), "6625_538", model, Path(folder))
            graph = json.loads(path.read_text(encoding="utf8"))
        self.assertEqual(path, Path(folder) / "Build_6625_538.dyn")
        self.assertEqual(graph["View"]["Dynamo"]["RunType"], "Manual")
        code = next(n["Code"] for n in graph["Nodes"] if n["NodeType"] == "PythonScriptNode")
        self.assertIn(r'ROOT = r"C:\some folder\dwf2bim"', code)
        self.assertIn(f'BUILDING = r"{model.resolve()}"', code)
        compile(code, "node", "exec")

    def test_deleting_the_file_model_is_off_by_default(self):
        graph = dynamo.graph(Path("C:/x"), "1_2", Path("C:/y/model"))
        switch = next(n for n in graph["Nodes"] if n["NodeType"] == "BooleanInputNode")
        self.assertIs(switch["InputValue"], False)

    def test_every_connector_joins_ports_of_the_graph(self):
        graph = dynamo.graph(Path("C:/x"), "1_2", Path("C:/y/model"))
        outputs = {p["Id"] for n in graph["Nodes"] for p in n["Outputs"]}
        inputs = {p["Id"] for n in graph["Nodes"] for p in n["Inputs"]}
        self.assertEqual(len(graph["Connectors"]), 2)
        for c in graph["Connectors"]:
            self.assertIn(c["Start"], outputs)
            self.assertIn(c["End"], inputs)
        self.assertEqual({v["Id"] for v in graph["View"]["NodeViews"]}, {n["Id"] for n in graph["Nodes"]})


class RevitScriptsFindTheModel(unittest.TestCase):
    def _find_building(self):
        spec = importlib.util.spec_from_file_location("build_native", Path(__file__).resolve().parents[1] / "revit" / "build_native.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.find_building

    def test_building_may_be_a_full_path_even_with_brackets_in_it(self):
        find_building = self._find_building()
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "projects [2026]" / "6625_538" / "model"
            model.mkdir(parents=True)
            (model / "building.json").write_text("{}")
            self.assertEqual(Path(find_building(r"C:\elsewhere", str(model))), model / "building.json")

    def test_building_may_still_be_a_project_under_output(self):
        find_building = self._find_building()
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "OUTPUT" / "1_2"
            model.mkdir(parents=True)
            (model / "building.json").write_text("{}")
            self.assertEqual(Path(find_building(folder, "1_2")), model / "building.json")


class ProjectFolder(unittest.TestCase):
    def test_relative_save_folder_is_refused_before_anything_runs(self):
        from dwf2bim import cli
        with self.assertRaises(fetch.FetchError):
            cli.run_project("1", "2", "relative\\folder", use_local=True)

    def test_local_run_needs_the_folder_to_exist(self):
        from dwf2bim import cli
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(fetch.FetchError):
                cli.run_project("1", "2", folder, use_local=True)


class PageInHebrew(unittest.TestCase):
    def test_page_reads_right_to_left_in_hebrew(self):
        from dwf2bim import ui
        self.assertIn('<html lang="he" dir="rtl">', ui.PAGE)

    def test_every_document_kind_has_a_hebrew_name(self):
        from dwf2bim import ui
        for role in fetch._CFG["documents"]:
            self.assertIn(f"{role}: '", ui.PAGE)


class LocalPageIsLocal(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from dwf2bim import ui
        cls.server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _post(self, headers, body=b'{"block":"x","parcel":"y"}'):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/run", data=body, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(request).status
        except urllib.error.HTTPError as ex:
            return ex.code

    def test_another_site_cannot_post(self):
        self.assertEqual(self._post({"Content-Type": "application/json", "Origin": "https://evil.example"}), 403)

    def test_a_form_post_is_refused(self):
        self.assertEqual(self._post({"Content-Type": "text/plain"}), 403)

    def test_another_host_name_is_refused(self):
        self.assertEqual(self._post({"Content-Type": "application/json", "Host": "evil.example"}), 403)

    def test_the_page_itself_gets_through_to_validation(self):
        self.assertEqual(self._post({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}), 400)


if __name__ == "__main__":
    unittest.main()
