"""Helpers for rendering terminal reports and exporting them to disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ArchiveRecord
from .utils import human_bytes


class ReportGenerator:
    """Create simple text/json reports for summary and archive history."""

    def __init__(self, output_dir: Path | str) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_text(self, name: str, content: str) -> Path:
        path = self.output_dir / f"{name}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.output_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def render_archive_history(self, records: list[ArchiveRecord]) -> str:
        lines = ["ARCHIVE HISTORY", ""]
        if not records:
            lines.append("(no archive history)")
            return "\n".join(lines)
        for rec in records:
            lines.append(
                f"- {rec.batch_id} | {rec.source_table} | rows={rec.rows} | "
                f"size={human_bytes(rec.gz_bytes)} | status={rec.status}"
            )
        return "\n".join(lines)
