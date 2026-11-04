"""Download a project's newest documents from the Tel Aviv engineering archive.

The archive runs in a visible browser, because the person using this tool has to accept its
terms of use there. Nothing is read from the archive until they have.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_CFG = json.loads((ROOT / "config" / "archive.json").read_text(encoding="utf8"))
PROFILE = ROOT / ".browser_profile"
MANIFEST = "fetch_manifest.json"
TERMS_WAIT_S = 300
PAGE_WAIT_MS = 60000
DOWNLOAD_WAIT_MS = 600000
PAUSE_S = 1.0  # between requests to the archive, which is a public service


class FetchError(Exception):
    pass


def project_name(block, parcel) -> str:
    block, parcel = str(block).strip(), str(parcel).strip()
    if not (block.isdigit() and parcel.isdigit()):
        raise FetchError(f"Block and parcel must be numbers, got '{block}' and '{parcel}'")
    return f"{block}_{parcel}"


def parse_date(text):
    try:
        day, month, year = (int(v) for v in str(text).strip().split("/"))
        return datetime.date(year, month, day)
    except ValueError:
        return None


def candidates(rows, role):
    """Rows of this document kind, newest first. A row with no date comes last."""
    wanted = set(_CFG["documents"][role]["types"])
    found = [r for r in rows if r.get("type") in wanted]
    return sorted(found, key=lambda r: parse_date(r.get("date")) or datetime.date.min, reverse=True)


def file_format(data: bytes) -> str:
    if data[:4] == b"(DWF":
        return ".dwf"
    if data[:2] == b"PK":
        return ".dwfx"
    if data[:4] == b"%PDF":
        return ".pdf"
    return "unknown"


def decode_download(body: bytes):
    """The archive answers with JSON holding a file name and base64 content, sometimes encoded twice."""
    try:
        result = json.loads(body)
        if isinstance(result, str):
            result = json.loads(result)
    except ValueError as ex:
        raise FetchError("The archive did not return a document") from ex
    if not isinstance(result, dict) or not result.get("buffer"):
        raise FetchError("The archive returned no file content")
    return safe_name(result.get("fileName")), base64.b64decode(result["buffer"])


def safe_name(name) -> str:
    """Only the last part of a name from the server, and no characters Windows refuses."""
    cleaned = re.sub(r'[<>:"|?*\x00-\x1f]', "_", re.split(r"[\\/]", str(name or ""))[-1]).strip(" .")
    return cleaned or "document"


def request_warnings(chosen) -> list:
    """Documents of different requests may describe different designs."""
    main = chosen.get("architecture")
    if not main:
        return []
    out = []
    for role, doc in chosen.items():
        if role != "architecture" and doc.get("online_request") != main.get("online_request"):
            out.append(f"{role} is from online request {doc.get('online_request')}, architecture from {main.get('online_request')}")
    return out


def retire_old_files(dest: Path, keep: set, log) -> None:
    """Older DWF and DWFX files move aside, so the build does not pick them up. Nothing is deleted."""
    old = [f for f in dest.iterdir() if f.is_file() and f.suffix.lower() in (".dwf", ".dwfx") and f.name not in keep]
    if not old:
        return
    target = dest / "_previous" / time.strftime("%Y%m%d_%H%M%S")
    target.mkdir(parents=True)
    for f in old:
        shutil.move(str(f), str(target / f.name))
        log(f"Moved aside: {f.name}")


READ_ROWS = """columns => {
    const link = document.querySelector('.ms-listlink');
    if (!link) return [];
    const table = link.closest('table');
    const heads = [...table.querySelectorAll('th')].map(x => x.innerText.trim());
    return [...table.querySelectorAll('.ms-listlink')].map(a => {
        const cells = [...a.closest('tr').querySelectorAll('td')].map(x => x.innerText.trim());
        const row = {url: a.getAttribute('downloadUrl')};
        for (const [key, title] of Object.entries(columns)) row[key] = cells[heads.indexOf(title)] || '';
        return row;
    });
}"""


def _open_browser(playwright):
    for channel in ("chrome", "msedge"):
        try:
            return playwright.chromium.launch_persistent_context(
                str(PROFILE), channel=channel, headless=False, viewport={"width": 1300, "height": 850}, locale="he-IL"
            )
        except Exception as ex:
            last = ex
    raise FetchError(f"Could not open Chrome or Edge: {last}")


def _go_to(page, url) -> None:
    """The site may navigate by itself, as it does after its terms are approved, which aborts a load in progress."""
    for attempt in range(3):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_WAIT_MS)
            return
        except Exception:
            if attempt == 2:
                raise
            page.wait_for_timeout(2000)


def _wait_for_terms(context, page, url, log) -> None:
    base = _CFG["base_url"]

    def accepted():
        return any(c["name"] == _CFG["terms_cookie"] for c in context.cookies(base))

    if accepted():
        log("The archive's terms were accepted in this browser profile within the last 7 days.")
        return
    log("Read the terms of use in the browser window and click the approve button there.")
    end = time.time() + TERMS_WAIT_S
    while time.time() < end:
        if accepted():
            log("Terms accepted.")
            page.wait_for_timeout(2000)
            _go_to(page, url)
            return
        page.wait_for_timeout(1000)
    raise FetchError("The archive's terms were not accepted in the browser, so nothing was read or downloaded.")


def _read_all_rows(page, log) -> list:
    try:
        page.wait_for_selector(".ms-listlink", state="attached", timeout=PAGE_WAIT_MS)
    except Exception as ex:
        raise FetchError("The archive shows no documents for this block and parcel.") from ex
    rows = page.evaluate(READ_ROWS, _CFG["columns"])
    for number in range(2, _CFG["max_result_pages"] + 1):
        link = page.locator(f"#PageLink_{number}")
        if link.count() == 0:
            break
        first = page.locator(".ms-listlink").first.get_attribute("downloadUrl")
        time.sleep(PAUSE_S)
        link.click()
        page.wait_for_function("old => { const a = document.querySelector('.ms-listlink'); return a && a.getAttribute('downloadUrl') !== old; }", arg=first, timeout=PAGE_WAIT_MS)
        rows += page.evaluate(READ_ROWS, _CFG["columns"])
    log(f"The archive lists {len(rows)} documents.")
    return rows


def _load_manifest(dest: Path) -> dict:
    path = dest / MANIFEST
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else {}


def _existing_file(dest: Path, data: bytes):
    """A file already here with this content is used under its own name, whatever the archive calls it."""
    digest = hashlib.sha256(data).hexdigest()
    for f in dest.iterdir():
        if f.is_file() and f.stat().st_size == len(data) and hashlib.sha256(f.read_bytes()).hexdigest() == digest:
            return f.name
    return None


def _download_role(context, role, rows, dest, known, log):
    """The newest document of this kind in a format the build can read. Returns the record and what was skipped."""
    spec = _CFG["documents"][role]
    skipped = []
    for row in candidates(rows, role)[: _CFG["max_tries_per_document"]]:
        doc_id = row["url"].split("id=")[-1]
        previous = known.get(doc_id)
        if previous and (dest / previous["file"]).exists():
            log(f"{role}: {row['type']} of {row['date']} is already here: {previous['file']}")
            return previous, skipped
        log(f"{role}: downloading {row['type']} of {row['date']}")
        time.sleep(PAUSE_S)
        response = context.request.get(_CFG["base_url"] + row["url"], timeout=DOWNLOAD_WAIT_MS)
        if not response.ok:
            skipped.append({**row, "reason": f"HTTP {response.status}"})
            log(f"{role}: the archive answered {response.status}")
            continue
        name, data = decode_download(response.body())
        kind = file_format(data)
        if kind not in spec["formats"]:
            skipped.append({**row, "file": name, "reason": f"format {kind}, need {' or '.join(spec['formats'])}"})
            log(f"{role}: {name} is {kind}, trying an older one")
            continue
        file_name = _existing_file(dest, data)
        if file_name:
            log(f"{role}: same content as {file_name}, already here")
        else:
            file_name = name
            (dest / file_name).write_bytes(data)
            log(f"{role}: saved {file_name} ({len(data) / 1e6:.1f} MB)")
        return {**row, "id": doc_id, "file": file_name, "sha256": hashlib.sha256(data).hexdigest(), "format": kind}, skipped
    return None, skipped


def fetch(block, parcel, dest: Path = None, log=print) -> dict:
    """Download the project's documents to DWF/<block>_<parcel>, and return what was chosen and what was not."""
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import sync_playwright

    name = project_name(block, parcel)
    dest = Path(dest) if dest else ROOT / "DWF" / name
    dest.mkdir(parents=True, exist_ok=True)
    known = {d["id"]: d for d in _load_manifest(dest).get("documents", {}).values() if d}
    url = _CFG["base_url"] + _CFG["search_path"].format(block=str(block).strip(), parcel=str(parcel).strip())
    chosen, skipped = {}, {}
    with sync_playwright() as playwright:
        context = _open_browser(playwright)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _go_to(page, url)
            _wait_for_terms(context, page, url, log)
            rows = _read_all_rows(page, log)
            for role in _CFG["documents"]:
                doc, passed = _download_role(context, role, rows, dest, known, log)
                if passed:
                    skipped[role] = passed
                if doc:
                    chosen[role] = doc
        except BrowserError as ex:
            raise FetchError(f"The browser stopped before the download finished: {str(ex).splitlines()[0]}") from ex
        finally:
            context.close()
    missing = [role for role in _CFG["documents"] if role not in chosen]
    required = [role for role in missing if _CFG["documents"][role]["required"]]
    manifest = {
        "project": name, "url": url, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "documents": chosen,
        "missing": missing, "skipped": skipped, "warnings": request_warnings(chosen),
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf8")
    if required:
        raise FetchError(f"No usable document in the archive for: {', '.join(required)}. See {dest / MANIFEST}")
    retire_old_files(dest, {d["file"] for d in chosen.values()}, log)
    for role in missing:
        log(f"Not found in a usable format: {role}. The model is built without it.")
    for warning in manifest["warnings"]:
        log(f"Check: {warning}")
    return manifest
