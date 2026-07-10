"""Small pure helpers used across the package.

Formatting, checksums, a name tokenizer for the analyzer, an identifier
validator (the first line of defense against SQL injection through table/column
names), a dependency-free box-drawing table renderer, and a timing context
manager.
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(n: int) -> str:
    """Render a byte count as a human-friendly string, e.g. 1536 -> '1.5 KB'."""
    size = float(n)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, streamed in 8 KB blocks so large files stay cheap."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8192), b""):
            digest.update(block)
    return digest.hexdigest()


def tokenize(identifier: str) -> list[str]:
    """Break a column name into lowercase word tokens.

    Splits on snake_case separators and camelCase boundaries, and separates
    trailing digits. 'CreatedDate' -> ['created', 'date'];
    'order_ship_date' -> ['order', 'ship', 'date']; 'dob' -> ['dob'].
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", identifier)
    parts = _SPLIT_RE.split(spaced)
    tokens: list[str] = []
    for part in parts:
        if not part:
            continue
        # Separate a digit run stuck to letters, e.g. 'col2' -> ['col', '2'].
        for chunk in re.findall(r"[A-Za-z]+|[0-9]+", part):
            tokens.append(chunk.lower())
    return tokens


def valid_identifier(name: str) -> bool:
    """True if *name* is a plain SQL identifier safe to quote and interpolate."""
    return bool(_IDENT_RE.match(name))


def color_enabled() -> bool:
    """True only when stdout is an interactive terminal."""
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _paint(code: str, text: str, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def render_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    color: bool = False,
    max_col_width: int = 44,
) -> str:
    """Render a box-drawing table as a string. Truncates over-wide cells."""
    if not rows:
        return _paint("2", "  (no data)", color)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], min(len(cell), max_col_width))

    def trunc(text: str, width: int) -> str:
        return (text[: width - 1] + "…") if len(text) > width else text

    def line(left: str, mid: str, right: str, fill: str = "═") -> str:
        return left + mid.join(fill * (w + 2) for w in widths) + right

    out: list[str] = [_paint("2", line("╔", "╦", "╗"), color)]
    header_cells = "║".join(
        f" {_paint('1', h.ljust(widths[i]), color)} " for i, h in enumerate(headers)
    )
    out.append(_paint("2", "║", color) + header_cells + _paint("2", "║", color))
    out.append(_paint("2", line("╠", "╬", "╣"), color))
    for row in rows:
        cells = "║".join(
            f" {trunc(row[i] if i < len(row) else '', widths[i]).ljust(widths[i])} "
            for i in range(len(headers))
        )
        out.append(_paint("2", "║", color) + cells + _paint("2", "║", color))
    out.append(_paint("2", line("╚", "╩", "╝"), color))
    return "\n".join(out)


class Timer:
    """Context manager exposing elapsed wall-clock seconds after it exits."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self._start = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self._start
