from __future__ import annotations

from pathlib import Path
from typing import Any

from app.schemas import TranscriptDocument
from app.utils import AppError, format_timestamp

from .schemas import AnalysisChunk, AnalysisSegment


def load_transcript(path: Path) -> TranscriptDocument:
    """Load a module-one transcript.json document."""
    try:
        data = path.read_text(encoding="utf-8")
        return TranscriptDocument.model_validate_json(data)
    except FileNotFoundError as exc:
        raise AppError(f"找不到 transcript.json：{path}") from exc
    except Exception as exc:
        raise AppError(f"无法读取 transcript.json：{path}\n请确认它是模块一生成的 JSON。") from exc


def transcript_segments(document: TranscriptDocument) -> list[AnalysisSegment]:
    segments: list[AnalysisSegment] = []
    for segment in sorted(document.segments, key=lambda item: (item.start, item.id)):
        segments.append(
            AnalysisSegment(
                segment_id=segment.id,
                start=segment.start,
                end=segment.end,
                start_text=segment.start_text,
                end_text=segment.end_text,
                text=segment.text,
            )
        )
    return segments


def make_chunks(
    segments: list[AnalysisSegment],
    *,
    chunk_minutes: float,
    max_segments_per_chunk: int,
) -> list[AnalysisChunk]:
    """Create stable sequential chunks by time window and segment count."""
    if chunk_minutes <= 0:
        raise AppError("--chunk-minutes 必须大于 0。")
    if max_segments_per_chunk <= 0:
        raise AppError("--max-segments-per-chunk 必须大于 0。")

    chunks: list[AnalysisChunk] = []
    current: list[AnalysisSegment] = []
    chunk_start: float | None = None
    max_duration = chunk_minutes * 60.0

    for segment in segments:
        if chunk_start is None:
            chunk_start = segment.start
        duration = segment.end - chunk_start
        over_time = current and duration > max_duration
        over_count = current and len(current) >= max_segments_per_chunk
        if over_time or over_count:
            chunks.append(_build_chunk(len(chunks), current))
            current = []
            chunk_start = segment.start
        current.append(segment)

    if current:
        chunks.append(_build_chunk(len(chunks), current))
    return chunks


def _build_chunk(index: int, segments: list[AnalysisSegment]) -> AnalysisChunk:
    start = segments[0].start
    end = segments[-1].end
    return AnalysisChunk(
        chunk_id=f"chunk_{index:04d}",
        index=index,
        start=start,
        end=end,
        start_text=format_timestamp(start),
        end_text=format_timestamp(end),
        segments=segments,
    )


def chunk_to_prompt_payload(chunk: AnalysisChunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "start": chunk.start,
        "end": chunk.end,
        "start_text": chunk.start_text,
        "end_text": chunk.end_text,
        "segments": [segment.model_dump() for segment in chunk.segments],
    }
