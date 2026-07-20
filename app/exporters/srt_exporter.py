from __future__ import annotations

from pathlib import Path

from app.schemas import TranscriptDocument
from app.utils import format_srt_timestamp


def export_srt(document: TranscriptDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for index, segment in enumerate(document.segments, start=1):
        blocks.append(
            f"{index}\n"
            f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}\n"
            f"{segment.text}"
        )
    output_path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
