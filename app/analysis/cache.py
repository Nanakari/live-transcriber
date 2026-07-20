from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .schemas import AnalysisChunk, ChunkAnalysisResult


def cache_key(
    *,
    transcript_file: Path,
    chunk: AnalysisChunk,
    model: str,
    profile: str,
    prompt_version: str,
) -> str:
    payload = {
        "transcript_file": str(transcript_file.resolve()),
        "chunk_id": chunk.chunk_id,
        "model": model,
        "profile": profile,
        "prompt_version": prompt_version,
        "segment_ids": [segment.segment_id for segment in chunk.segments],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def chunk_result_path(chunks_dir: Path, chunk: AnalysisChunk, key: str) -> Path:
    return chunks_dir / f"{chunk.chunk_id}_{key}.json"


def load_cached_result(path: Path) -> ChunkAnalysisResult | None:
    if not path.exists():
        return None
    return ChunkAnalysisResult.model_validate_json(path.read_text(encoding="utf-8"))


def save_chunk_result(path: Path, result: ChunkAnalysisResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")


def save_failed_response(failed_dir: Path, chunk_id: str, raw_text: str, error: str) -> Path:
    failed_dir.mkdir(parents=True, exist_ok=True)
    path = failed_dir / f"{chunk_id}_failed.txt"
    path.write_text(f"ERROR:\n{error}\n\nRAW RESPONSE:\n{raw_text}", encoding="utf-8")
    return path
