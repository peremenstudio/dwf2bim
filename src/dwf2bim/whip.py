"""Decoder for the WHIP! (W2D) binary vector stream stored inside DWF packages.

Only the opcodes needed for geometry extraction are interpreted; everything else
is skipped using its documented length. Coordinates are returned as absolute
32-bit logical integers.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

_I2 = struct.Struct("<hh")
_I4 = struct.Struct("<ii")
_U4 = struct.Struct("<I")


class WhipError(Exception):
    pass


@dataclass
class Primitive:
    kind: str  # line | polyline | polygon | circle | ellipse | tri | contour | text
    points: list  # [(x, y), ...] absolute logical coordinates
    node: int
    view: int  # index into WhipData.views, -1 before the first viewport
    color: int
    layer: int
    weight: int
    radius: tuple | None = None  # (major, minor, start, end, tilt) for arcs
    text: str | None = None
    fill: bool = False


@dataclass
class Viewport:
    """A named view placed on the sheet."""
    name: str
    contour: list  # sheet-space clip polygon (logical units)
    units_per_mm: float  # logical units per millimetre of model space


@dataclass
class WhipData:
    version: str = ""
    urls: dict = field(default_factory=dict)  # url index -> (href, label)
    layers: dict = field(default_factory=dict)  # layer index -> name
    nodes: dict = field(default_factory=dict)  # node index -> "Family [ElementId]"
    views: list = field(default_factory=list)
    primitives: list = field(default_factory=list)
    extended: dict = field(default_factory=dict)


_FIXED_SKIP = {
    0xCC: 1,  # line pattern
    0x8D: 4,  # marker size
    0xAD: 1,  # marker symbol
    0x91: 2,  # projection (legacy)
}
_NO_DATA = frozenset(b" \t\r\n")


def decode(buf: bytes, keep_text: bool = True) -> WhipData:
    """Decode a whole W2D stream."""
    m = re.match(rb"\(W2D V(\d+\.\d+)\)", buf)
    if not m:
        raise WhipError("Not a W2D stream")
    out = WhipData(version=m.group(1).decode())
    pos = m.end()
    end = len(buf)

    cx = cy = 0  # current point
    node = -1
    view = -1
    color = 0xFFFFFFFF
    layer = 0
    weight = 0
    fill = False
    color_map: list[int] = []

    prims = out.primitives
    Prim = Primitive
    i2 = _I2.unpack_from
    i4 = _I4.unpack_from

    def read_count(p: int) -> tuple[int, int]:
        c = buf[p]
        p += 1
        if c == 0:
            c = struct.unpack_from("<H", buf, p)[0] + 256
            p += 2
        return c, p

    while pos < end:
        op = buf[pos]
        pos += 1

        if op == 0x10:  # polyline/polygon, 16-bit relative
            n, pos = read_count(pos)
            pts = []
            for _ in range(n):
                dx, dy = i2(buf, pos)
                pos += 4
                cx += dx
                cy += dy
                pts.append((cx, cy))
            prims.append(Prim("polygon" if fill else "polyline", pts, node, view, color, layer, weight, fill=fill))
        elif op == 0x70:  # polyline/polygon, 32-bit relative
            n, pos = read_count(pos)
            pts = []
            for _ in range(n):
                dx, dy = i4(buf, pos)
                pos += 8
                cx += dx
                cy += dy
                pts.append((cx, cy))
            prims.append(Prim("polygon" if fill else "polyline", pts, node, view, color, layer, weight, fill=fill))
        elif op == 0x0C:  # line 16R
            dx, dy = i2(buf, pos)
            ax, ay = cx + dx, cy + dy
            dx, dy = i2(buf, pos + 4)
            pos += 8
            cx, cy = ax + dx, ay + dy
            prims.append(Prim("line", [(ax, ay), (cx, cy)], node, view, color, layer, weight))
        elif op == 0x6C or op == 0x92:  # line 32R (0x92 is the same layout)
            dx, dy = i4(buf, pos)
            ax, ay = cx + dx, cy + dy
            dx, dy = i4(buf, pos + 8)
            pos += 16
            cx, cy = ax + dx, ay + dy
            prims.append(Prim("line", [(ax, ay), (cx, cy)], node, view, color, layer, weight))
        elif op == 0x4F:  # set origin (absolute)
            cx, cy = i4(buf, pos)
            pos += 8
        elif op == 0x03:  # color RGBA
            color = _U4.unpack_from(buf, pos)[0]
            pos += 4
        elif op == 0x63:  # color index
            idx = buf[pos]
            pos += 1
            color = color_map[idx] if idx < len(color_map) else idx
        elif op == 0x46:
            fill = True
        elif op == 0x66:
            fill = False
        elif op == 0x0E:  # object node, auto increment
            node += 1
        elif op == 0x4E:  # object node, 32-bit absolute
            node = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
        elif op == 0x6E:  # object node, 16-bit delta
            node += struct.unpack_from("<h", buf, pos)[0]
            pos += 2
        elif op == 0x17:  # line weight
            weight = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
        elif op == 0xAC:  # layer
            layer = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
        elif op == 0x12:  # circle 16R
            dx, dy = i2(buf, pos)
            r = struct.unpack_from("<H", buf, pos + 4)[0]
            pos += 6
            cx += dx
            cy += dy
            prims.append(Prim("circle", [(cx, cy)], node, view, color, layer, weight, radius=(r, r, 0, 65536, 0), fill=fill))
        elif op == 0x72:  # circle 32R
            dx, dy = i4(buf, pos)
            r = _U4.unpack_from(buf, pos + 8)[0]
            pos += 12
            cx += dx
            cy += dy
            prims.append(Prim("circle", [(cx, cy)], node, view, color, layer, weight, radius=(r, r, 0, 65536, 0), fill=fill))
        elif op == 0x65:  # ellipse 32R
            dx, dy = i4(buf, pos)
            maj, mnr = struct.unpack_from("<II", buf, pos + 8)
            st, en, tilt = struct.unpack_from("<HHH", buf, pos + 16)
            pos += 22
            cx += dx
            cy += dy
            prims.append(Prim("ellipse", [(cx, cy)], node, view, color, layer, weight, radius=(maj, mnr, st, en, tilt), fill=fill))
        elif op == 0x14 or op == 0x74:  # polytriangle 16R / 32R
            n, pos = read_count(pos)
            pts = []
            if op == 0x14:
                for _ in range(n):
                    dx, dy = i2(buf, pos)
                    pos += 4
                    cx += dx
                    cy += dy
                    pts.append((cx, cy))
            else:
                for _ in range(n):
                    dx, dy = i4(buf, pos)
                    pos += 8
                    cx += dx
                    cy += dy
                    pts.append((cx, cy))
            prims.append(Prim("tri", pts, node, view, color, layer, weight, fill=True))
        elif op == 0x0B or op == 0x6B:  # contour set 16R / 32R
            nc, pos = read_count(pos)
            counts = []
            for _ in range(nc):
                c, pos = read_count(pos)
                counts.append(c)
            for c in counts:
                pts = []
                if op == 0x0B:
                    for _ in range(c):
                        dx, dy = i2(buf, pos)
                        pos += 4
                        cx += dx
                        cy += dy
                        pts.append((cx, cy))
                else:
                    for _ in range(c):
                        dx, dy = i4(buf, pos)
                        pos += 8
                        cx += dx
                        cy += dy
                        pts.append((cx, cy))
                prims.append(Prim("contour", pts, node, view, color, layer, weight, fill=True))
        elif op == 0x78:  # simple text
            dx, dy = i4(buf, pos)
            pos += 8
            cx += dx
            cy += dy
            s, pos = _read_string(buf, pos)
            if keep_text:
                prims.append(Prim("text", [(cx, cy)], node, view, color, layer, weight, text=s))
        elif op == 0x18:  # complex text
            dx, dy = i4(buf, pos)
            pos += 8
            cx += dx
            cy += dy
            s, pos = _read_string(buf, pos)
            # overscore / underscore bound boxes follow as optional count + points
            pos = _skip_text_options(buf, pos)
            if keep_text:
                prims.append(Prim("text", [(cx, cy)], node, view, color, layer, weight, text=s))
        elif op == 0x28:  # extended ASCII
            start = pos - 1
            pos = _skip_balanced(buf, start)
            head = buf[start + 1 : start + 40]
            name = re.match(rb"[A-Za-z_]+", head)
            key = name.group(0).decode() if name else "?"
            if key == "URL":
                _parse_url(buf[start:pos], out.urls)
            elif key == "Layer":
                _parse_layer(buf[start:pos], out.layers)
            elif key == "Node":
                node = _parse_node(buf[start:pos], out.nodes, node)
            elif key == "Viewport":
                vp = _parse_viewport(buf[start:pos])
                if vp is not None:
                    out.views.append(vp)
                    view = len(out.views) - 1
            out.extended[key] = out.extended.get(key, 0) + 1
        elif op == 0x7B:  # extended binary
            size = _U4.unpack_from(buf, pos)[0]
            if pos + 4 + size > end or buf[pos + 3 + size] != 0x7D:
                raise WhipError(f"Bad extended binary block at offset {pos - 1}: {buf[pos - 41 : pos + 24]!r}")
            pos += 4 + size
            key = "EB"
            out.extended[key] = out.extended.get(key, 0) + 1
        elif op in _NO_DATA:
            continue
        elif op in _FIXED_SKIP:
            pos += _FIXED_SKIP[op]
        elif op == 0x56 or op == 0x76:
            pass  # visibility on / off
        elif op == 0x06:  # font (flagged)
            pos = _skip_font(buf, pos)
        else:
            raise WhipError(f"Unknown opcode 0x{op:02X} at offset {pos - 1}: {buf[pos - 8 : pos + 32]!r}")
    if pos != end:
        raise WhipError(f"Stream ended at {pos}, expected {end}")
    return out


def _read_string(buf: bytes, pos: int) -> tuple[str, int]:
    """WHIP strings are either {count, UTF-16LE data} or quoted Latin-1."""
    c = buf[pos]
    if c == 0x7B:
        n = _U4.unpack_from(buf, pos + 1)[0]
        if pos + 6 + 2 * n > len(buf) or buf[pos + 5 + 2 * n] != 0x7D:
            raise WhipError(f"Bad wide string at offset {pos}: {buf[pos - 12 : pos + 24]!r}")
        return buf[pos + 5 : pos + 5 + 2 * n].decode("utf-16-le", "replace"), pos + 6 + 2 * n
    if c == 0x27 or c == 0x22:
        q = pos + 1
        while buf[q] != c:
            q += 2 if buf[q] == 0x5C else 1
        return buf[pos + 1 : q].decode("latin1", "replace"), q + 1
    raise WhipError(f"Bad string at offset {pos}: {buf[pos - 12 : pos + 24]!r}")


def _skip_text_options(buf: bytes, pos: int) -> int:
    return pos


def _skip_font(buf: bytes, pos: int) -> int:
    fields = struct.unpack_from("<H", buf, pos)[0]
    pos += 2
    if fields & 0x0001:  # name
        pos = _read_string(buf, pos)[1]
    if fields & 0x0002:
        pos += 1  # charset
    if fields & 0x0004:
        pos += 1  # pitch
    if fields & 0x0008:
        pos += 1  # family
    if fields & 0x0010:
        pos += 1  # style
    if fields & 0x0020:
        pos += 4  # height
    if fields & 0x0040:
        pos += 2  # rotation
    if fields & 0x0080:
        pos += 2  # width scale
    if fields & 0x0100:
        pos += 2  # spacing
    if fields & 0x0200:
        pos += 2  # oblique
    if fields & 0x0400:
        pos += 2  # flags
    return pos


def _skip_balanced(buf: bytes, start: int) -> int:
    """Return the offset just past the ')' balancing the '(' at start.

    Binary payloads inside {...} braces carry a 4-byte count of UTF-16 units, and
    quoted strings may contain parentheses, so both are stepped over explicitly.
    """
    depth = 0
    p = start
    end = len(buf)
    while p < end:
        c = buf[p]
        if c == 0x28:
            depth += 1
            p += 1
        elif c == 0x29:
            depth -= 1
            p += 1
            if depth == 0:
                return p
        elif c == 0x22 or c == 0x27:
            p += 1
            while buf[p] != c:
                if buf[p] == 0x5C:
                    p += 1
                p += 1
            p += 1
        elif c == 0x7B:
            n = _U4.unpack_from(buf, p + 1)[0]
            p += 5 + 2 * n
            if buf[p] == 0x7D:
                p += 1
        else:
            p += 1
    raise WhipError("Unbalanced extended ASCII opcode")


def _parse_ws_string(chunk: bytes, p: int) -> tuple[str, int]:
    n = _U4.unpack_from(chunk, p + 1)[0]
    data = chunk[p + 5 : p + 5 + 2 * n]
    return data.decode("utf-16-le", "replace"), p + 5 + 2 * n + 1


def _parse_url(chunk: bytes, urls: dict) -> None:
    # (URL (12 {href} {label})) defines a link target; other forms only toggle it.
    m = re.match(rb"\(URL\s+\(\s*(\d+)\s*", chunk)
    if not m:
        return
    p = chunk.find(b"{", m.end())
    if p < 0:
        return
    href, p = _parse_ws_string(chunk, p)
    q = chunk.find(b"{", p)
    label = _parse_ws_string(chunk, q)[0] if q >= 0 else ""
    urls[int(m.group(1))] = (href, label)


def _parse_node(chunk: bytes, nodes: dict, current: int) -> int:
    m = re.match(rb"\(Node\s+(\d+)\s*", chunk)
    if not m:
        return current
    idx = int(m.group(1))
    rest = chunk[m.end() : -1]
    if rest[:1] == b"{":
        nodes[idx] = _parse_ws_string(chunk, m.end())[0]
    elif rest[:1] in (b"'", b'"'):
        nodes[idx] = rest[1:-1].decode("latin1", "replace")
    return idx


def _parse_viewport(chunk: bytes) -> Viewport | None:
    m = re.match(rb"""\(Viewport\s+(?:"([0-9A-Fa-f]*)"|'([^']*)')""", chunk)
    if not m:
        return None
    if m.group(1) is not None:
        name = bytes.fromhex(m.group(1).decode()).decode("utf-16-be", "replace")
    else:
        name = m.group(2).decode("latin1")
    c = re.search(rb"\(Contour\s+\d+\s+\d+\s+([\d,\s-]+)\)", chunk)
    pts = [tuple(int(v) for v in t.split(b",")) for t in c.group(1).split()] if c else []
    u = re.search(rb"\(\(\s*([\d.eE+-]+)", chunk)
    return Viewport(name, pts, float(u.group(1)) if u else 1.0)


def _parse_layer(chunk: bytes, layers: dict) -> None:
    m = re.match(rb"\(Layer\s+(\d+)\s+", chunk)
    if not m:
        return
    p = m.end()
    if chunk[p : p + 1] == b"{":
        name, _ = _parse_ws_string(chunk, p)
    else:
        q = re.match(rb'"?([^")]*)', chunk[p:])
        name = q.group(1).decode("latin1") if q else ""
    layers[int(m.group(1))] = name
