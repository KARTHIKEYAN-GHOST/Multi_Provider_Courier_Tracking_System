"""The demo site's verification step: a one-time code or a CAPTCHA before the tracking page.

Real courier sites do this; the demo site does it so the whole "a person is needed" workflow
can be practised without touching anyone else's website.

For a demo the answers are predictable on purpose: both are derived from the tracking number,
so a test can compute them. A person gets the one-time code from the site's pretend phone
inbox (``/inbox?number=...``) and reads the CAPTCHA from the picture.
"""

from __future__ import annotations

import hashlib
import secrets
import struct
import threading
import zlib

_DIGITS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}
_SCALE = 6
_MARGIN = 10


def _digits(purpose: str, number: str, count: int) -> str:
    value = int.from_bytes(hashlib.sha256(f"{purpose}:{number.upper()}".encode()).digest()[:8])
    return str(value % 10**count).zfill(count)


def otp_code(number: str) -> str:
    """The six-digit code the site 'texts' for this tracking number."""
    return _digits("otp", number, 6)


def captcha_answer(number: str) -> str:
    """The five digits in this tracking number's CAPTCHA picture."""
    return _digits("captcha", number, 5)


def captcha_png(text: str) -> bytes:
    """A small greyscale PNG showing ``text`` (digits) with a few distracting lines."""
    width = 2 * _MARGIN + len(text) * 6 * _SCALE
    height = 2 * _MARGIN + 7 * _SCALE
    pixels = [bytearray([235]) * width for _ in range(height)]
    for index, char in enumerate(text):
        left = _MARGIN + index * 6 * _SCALE
        lift = (index % 2) * 3  # alternate digits sit a little higher
        for row, bits in enumerate(_DIGITS[char]):
            for column, bit in enumerate(bits):
                if bit == "1":
                    for y in range(_SCALE):
                        for x in range(_SCALE):
                            pixels[_MARGIN - lift + row * _SCALE + y][left + column * _SCALE + x] = 30
    for line in range(3):  # diagonal strokes across the picture
        for x in range(width):
            y = (x // 3 + line * 19 + 5) % height
            pixels[y][x] = 120
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class Verifications:
    """Open verification steps: a random single-use token for each page shown."""

    def __init__(self) -> None:
        self._open: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def start(self, kind: str, number: str) -> str:
        token = secrets.token_hex(16)
        with self._lock:
            if len(self._open) > 10_000:
                self._open.clear()
            self._open[token] = (kind, number)
        return token

    def peek(self, token: str) -> tuple[str, str] | None:
        with self._lock:
            return self._open.get(token)

    def finish(self, token: str) -> tuple[str, str] | None:
        """Uses the token up, whether the answer turns out right or wrong."""
        with self._lock:
            return self._open.pop(token, None)


def expected_answer(kind: str, number: str) -> str:
    return otp_code(number) if kind == "otp" else captcha_answer(number)
