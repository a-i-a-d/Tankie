"""Synthetic test scenes for the VLM benchmark -- pure stdlib PNG encoder.

No third-party dependencies (no Pillow): we write a valid 8-bit RGB PNG
directly with ``struct`` + ``zlib`` and draw shapes with simple math.
Scenes are generated fresh on every run (no binary assets in the repo).

Scenes
------
``detect``    one frame: red circle (upper-left), green square (center),
              blue square (lower-right).
``sequence``  4 frames, ~0.5 s apart: the green square shifts +40 px to the
              right between frames (the only moving object).
``drive``     a red obstacle on the road: ``center`` (blocks the middle),
              ``left`` (offset, clear path on the right), or ``clear`` (none).
"""

from __future__ import annotations

import math
import os
import struct
import zlib

W, H = 640, 480
BG = (40, 40, 40)

# ---------------------------------------------------------------------------
# minimal 5x7 bitmap font (lowercase + digits + a few symbols)
# ---------------------------------------------------------------------------
_FONT = {
    "a": ["01110", "10001", "01110", "10001", "10001", "10001", "11111"],
    "b": ["10000", "10010", "10100", "11000", "10100", "10010", "10000"],
    "c": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "d": ["00001", "00011", "00101", "10001", "10101", "00011", "00001"],
    "e": ["11111", "10000", "10000", "11111", "10000", "10000", "11111"],
    "f": ["01110", "10001", "10000", "11100", "10000", "10000", "10000"],
    "g": ["11111", "10000", "10000", "11111", "10001", "01110", "00001"],
    "h": ["10000", "10000", "10100", "11000", "10100", "10000", "10000"],
    "i": ["01000", "00000", "01000", "01000", "01000", "01000", "01110"],
    "j": ["00010", "00000", "00010", "00010", "00010", "10010", "01100"],
    "k": ["10000", "10000", "10100", "11000", "10100", "10000", "10000"],
    "l": ["01000", "01000", "01000", "01000", "01000", "01000", "01110"],
    "m": ["11111", "11111", "11111", "11111", "11111", "11111", "11111"],
    "n": ["11111", "10100", "10100", "10100", "10100", "10100", "10100"],
    "o": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "p": ["10000", "10000", "10000", "11100", "10100", "10000", "10000"],
    "q": ["11111", "10101", "11011", "01110", "11011", "10101", "11111"],
    "r": ["10110", "11001", "10000", "10000", "10000", "10000", "10000"],
    "s": ["01110", "10001", "10000", "01110", "00001", "10001", "01110"],
    "t": ["10000", "10000", "11111", "10000", "10000", "10001", "01110"],
    "u": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "v": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "w": ["10001", "10001", "10001", "10101", "10101", "01010", "01010"],
    "x": ["10001", "01010", "00100", "00100", "01010", "10001", "10001"],
    "y": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00110", "01001", "10001", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["01110", "10000", "11110", "10001", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "=": ["00000", "11111", "00000", "11111", "00000", "00000", "00000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "00000", "00100"],
    ",": ["00000", "00000", "00000", "00000", "00100", "00000", "00100"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    ":": ["00000", "00100", "00000", "00000", "00100", "00000", "00000"],
}


def _chunk(tag: bytes, data: bytes) -> bytes:
    out = struct.pack(">I", len(data)) + tag + data
    out += struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return out


def _put_rect(px, x0, y0, x1, y1, color):
    for y in range(max(0, y0), min(H, y1 + 1)):
        row = px[y]
        for x in range(max(0, x0), min(W, x1 + 1)):
            row[x] = color


def _put_circle(px, cx, cy, r, color):
    for y in range(max(0, int(cy - r)), min(H, int(cy + r) + 1)):
        row = px[y]
        for x in range(max(0, int(cx - r)), min(W, int(cx + r) + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                row[x] = color


def _put_text(px, x, y, text, color):
    for i, ch in enumerate(text):
        glyph = _FONT.get(ch)
        if glyph is None:
            continue
        for gy, bits in enumerate(glyph):
            for gx, bit in enumerate(bits):
                if bit == "1":
                    _put_rect(px, x + i * 6 + gx, y + gy, x + i * 6 + gx, y + gy, color)


def _encode_png(px) -> bytes:
    raw = b"".join(b"\x00" + b"".join(bytes(px[y]) for x in range(W) for _ in (0,)) for y in range(H)) if False else b"".join(
        b"\x00" + b"".join(bytes(px[y][x]) for x in range(W)) for y in range(H)
    )
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


class _Canvas:
    def __init__(self) -> None:
        self.px = [[BG] * W for _ in range(H)]

    def rect(self, x0, y0, x1, y1, color):
        _put_rect(self.px, x0, y0, x1, y1, color)

    def circle(self, cx, cy, r, color):
        _put_circle(self.px, cx, cy, r, color)

    def text(self, x, y, s, color=(255, 255, 255)):
        _put_text(self.px, x, y, s, color)

    def png(self) -> bytes:
        return _encode_png(self.px)


def _base() -> _Canvas:
    c = _Canvas()
    c.rect(0, 0, W - 1, H - 1, BG)
    return c


def detect_scene() -> bytes:
    c = _base()
    c.circle(120, 120, 60, (220, 40, 40))     # red circle
    c.rect(300, 100, 420, 220, (40, 160, 40))  # green square
    c.rect(480, 300, 580, 400, (40, 80, 220))  # blue square
    c.text(20, 440, "tankie vlm benchmark scene")
    return c.png()


def sequence_frames(n: int = 4, step: int = 40) -> list[bytes]:
    """n frames; the green square moves +step px to the right each frame."""
    frames = []
    for i in range(n):
        shift = i * step
        c = _base()
        c.circle(120, 120, 60, (220, 40, 40))
        c.rect(300 + shift, 100, 420 + shift, 220, (40, 160, 40))
        c.rect(480, 300, 580, 400, (40, 80, 220))
        c.text(20, 440, f"frame {i} of {n - 1}")
        frames.append(c.png())
    return frames


def drive_scene(variant: str) -> bytes:
    c = _base()
    # road stripes
    for i in range(0, W, 80):
        c.rect(i, 240, i + 40, 244, (120, 120, 120))
    if variant == "center":
        c.rect(260, 150, 380, 270, (200, 30, 30))
    elif variant == "left":
        c.rect(90, 150, 210, 270, (200, 30, 30))
    c.text(20, 440, f"drive scenario: {variant}")
    return c.png()


def probe_image() -> bytes:
    """Tiny 64x64 two-color PNG for the ``--probe`` capability check.

    A red square on a blue background -- the probe prompt asks for the
    number of distinct colors, so a vision-capable model answers 2.
    """
    w = h = 64
    px = [[(200, 30, 30) if (8 <= x < 56 and 8 <= y < 56) else (30, 60, 200)
          for x in range(w)] for y in range(h)]
    raw = b"".join(b"\x00" + b"".join(bytes(px[y][x]) for x in range(w)) for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 6))
        + _chunk(b"IEND", b"")
    )


def save_scenes(outdir: str) -> list[str]:
    """Write every benchmark scene as a PNG to *outdir*. Returns the file paths.

    Creates the directory if it doesn't exist. Overwrites existing files.
    """
    os.makedirs(outdir, exist_ok=True)
    written: list[str] = []

    def _save(name: str, png: bytes) -> None:
        p = os.path.join(outdir, name)
        with open(p, "wb") as f:
            f.write(png)
        written.append(p)

    _save("detect.png", detect_scene())
    for i, frame in enumerate(sequence_frames(4)):
        _save(f"sequence_frame{i}.png", frame)
    for v in ("center", "left", "clear"):
        _save(f"drive_{v}.png", drive_scene(v))
    return written
