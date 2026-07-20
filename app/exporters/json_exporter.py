from __future__ import annotations

import json
from pathlib import Path

from app.schemas import TranscriptDocument


def export_json(document: TranscriptDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = document.model_dump(mode="json")
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
