"""A small local page: enter a block and parcel, get the files, the model and a Dynamo graph."""
from __future__ import annotations

import json
import os
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import cli, fetch

PAGE = (Path(__file__).parent / "ui.html").read_text(encoding="utf8")
PREVIEW = "preview_3d.png"


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.running = False
        self.lines = []
        self.result = None
        self.error = None
        self.project = None

    def log(self, message):
        with self.lock:
            self.lines.append(str(message))

    def run(self, block, parcel, use_local):
        cli.log_listeners.append(self.log)
        try:
            self.result = cli.run_project(block, parcel, use_local=use_local, log=self.log)
            self.log("Done.")
        except (fetch.FetchError, cli.package.UnsupportedInput) as ex:
            self.error = str(ex)
        except Exception:
            self.error = "The run failed. Details are in the log."
            self.log(traceback.format_exc())
        finally:
            cli.log_listeners.remove(self.log)
            self.running = False


JOB = Job()


def _folder(what, project):
    return {"input": cli.ROOT / "DWF" / project, "output": cli.ROOT / "OUTPUT" / project}.get(what)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _local_only(self):
        # a web page elsewhere must not be able to start runs on this machine
        host = self.headers.get("Host", "").split(":")[0]
        if host in ("127.0.0.1", "localhost"):
            return True
        self.send_error(403)
        return False

    def _send(self, body, content_type="application/json; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, code=200):
        self._send(json.dumps(value, ensure_ascii=False).encode("utf8"), code=code)

    def do_GET(self):
        if not self._local_only():
            return
        url = urlparse(self.path)
        if url.path == "/":
            self._send(PAGE.encode("utf8"), "text/html; charset=utf-8")
        elif url.path == "/api/status":
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            with JOB.lock:
                self._json({"running": JOB.running, "lines": JOB.lines[since:], "total": len(JOB.lines), "result": JOB.result, "error": JOB.error, "project": JOB.project})
        elif url.path == "/api/preview" and JOB.project:
            image = cli.ROOT / "OUTPUT" / JOB.project / "qa" / PREVIEW
            if image.exists():
                self._send(image.read_bytes(), "image/png")
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._local_only():
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "Bad request"}, 400)
        if self.path == "/api/run":
            if JOB.running:
                return self._json({"error": "A run is already in progress"}, 409)
            try:
                project = fetch.project_name(body.get("block"), body.get("parcel"))
            except fetch.FetchError as ex:
                return self._json({"error": str(ex)}, 400)
            JOB.reset()
            JOB.running = True
            JOB.project = project
            threading.Thread(target=JOB.run, args=(body["block"], body["parcel"], bool(body.get("use_local"))), daemon=True).start()
            self._json({"started": True})
        elif self.path == "/api/open":
            folder = _folder(body.get("what"), JOB.project) if JOB.project else None
            if folder is None or not folder.exists():
                return self._json({"error": "No such folder yet"}, 404)
            os.startfile(folder)
            self._json({"opened": str(folder)})
        else:
            self.send_error(404)


def serve(port=8765, open_page=True):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"dwf2bim is open at {url} . Close this window to stop it.", flush=True)
    if open_page:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
