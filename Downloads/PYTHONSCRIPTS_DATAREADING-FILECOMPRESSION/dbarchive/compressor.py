"""Gzip compression with size accounting and a checksum.

The checksum is the trust anchor for the whole archive: it is computed here at
write time and re-verified before any restore, so a silently corrupted archive
file can never be replayed into a database.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

from .models import CompressionResult
from .utils import sha256_file


def compress_csv(csv_path: Path, gz_path: Path) -> CompressionResult:
    """Gzip *csv_path* to *gz_path*, returning sizes, ratio, and gz checksum."""
    csv_path = Path(csv_path)
    gz_path = Path(gz_path)
    gz_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("rb") as src, gzip.open(gz_path, "wb") as dst:
        shutil.copyfileobj(src, dst)

    csv_bytes = csv_path.stat().st_size
    gz_bytes = gz_path.stat().st_size
    ratio = round((1 - (gz_bytes / csv_bytes)) * 100, 2) if csv_bytes else 0.0
    return CompressionResult(
        csv_bytes=csv_bytes,
        gz_bytes=gz_bytes,
        ratio=ratio,
        checksum=sha256_file(gz_path),
    )


def decompress(gz_path: Path, out_path: Path) -> None:
    """Restore *gz_path* to a plain file at *out_path*."""
    gz_path = Path(gz_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(gz_path, "rb") as src, out_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)
