"""English naming for everything that leaves the pipeline."""
from __future__ import annotations

import json
import re
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_G = json.loads((CONFIG_DIR / "glossary.json").read_text(encoding="utf8"))
_PHRASES = sorted(_G["phrases"].items(), key=lambda kv: -len(kv[0]))
_LETTERS = _G["letters"]
_MARKS = re.compile("[‎‏‪-‮֑-ׇ]")


def english(text: str | None) -> str:
    """Translate known phrases, then transliterate any remaining Hebrew letters."""
    if not text:
        return ""
    s = _MARKS.sub("", text).replace("׳", "'").replace("״", '"')
    s = re.sub(r"\s+", " ", s).strip()
    for src, dst in _PHRASES:
        s = s.replace(src, f" {dst} " if dst else " ")
    s = "".join(_LETTERS.get(c, c) for c in s)
    s = s.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]+", "_", english(text)).strip("_") or "unnamed"


def level_id(name: str | None) -> str:
    """-02 -> L-02, 0.00=19.36 -> L00, 01 -> L01, roof -> ROOF."""
    raw = _MARKS.sub("", name or "").strip()
    if re.match(r"^-?\d+$", raw):
        return f"L{raw}"
    if re.match(r"^0(\.0+)?(=|$)", raw):
        return "L00"
    return slug(raw)


def absolute_datum(level_names) -> float | None:
    """Read the absolute height from a level named like '0.00=19.36'."""
    for name in level_names:
        m = re.match(r"^\s*0(?:\.0+)?\s*=\s*(-?\d+(?:\.\d+)?)", _MARKS.sub("", name or ""))
        if m:
            return float(m.group(1))
    return None
