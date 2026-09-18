from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .schemas import AnalysisChunk, AnalysisDocument, ChunkAnalysisResult, VideoSummary


CACHE_KEY_VERSION = 2


def cache_key(
    *,
    transcript_file: Path,
    chunk: AnalysisChunk,
    model: str,
    profile: str,
    prompt_version: str,
    parameters: dict | None = None,
) -> str:
    # The chunk payload already contains the transcript content.  Do not include
    # the absolute transcript path: Skill reruns create a new media directory,
    # but identical content should still reuse the shared analysis cache.
    payload = {
        "cache_key_version": CACHE_KEY_VERSION,
        "chunk_id": chunk.chunk_id,
        "model": model,
        "profile": profile,
        "prompt_version": prompt_version,
        "segments": [segment.model_dump() for segment in chunk.segments],
        "parameters": parameters or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def chunk_result_path(chunks_dir: Path, chunk: AnalysisChunk, key: str) -> Path:
    return chunks_dir / f"{chunk.chunk_id}_{key}.json"


def summary_cache_key(
    document: AnalysisDocument,
    *,
    prompt_version: str,
    media_context: str,
) -> str:
    payload = {
        "cache_key_version": CACHE_KEY_VERSION,
        "prompt_version": prompt_version,
        "provider": document.meta.provider,
        "model": document.meta.model,
        "reasoning_effort": document.meta.reasoning_effort,
        "source_language": document.meta.source_language,
        "target_language": document.meta.target_language,
        "media_context": media_context,
        "chunks": [
            {
                "chunk_id": chunk.chunk_id,
                "start": chunk.start,
                "end": chunk.end,
                "summary": chunk.chunk_summary_zh,
                "key_points": chunk.key_points_zh,
                "importance": chunk.content_importance,
            }
            for chunk in document.chunks
        ],
        "failed_chunks": [item.model_dump() for item in document.failed_chunks],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def summary_result_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"summary_{key}.json"


def load_cached_summary(path: Path) -> VideoSummary | None:
    if not path.exists():
        return None
    try:
        return VideoSummary.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_summary_result(path: Path, result: VideoSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(result.model_dump_json(indent=2))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_cached_result(path: Path) -> ChunkAnalysisResult | None:
    if not path.exists():
        return None
    try:
        return ChunkAnalysisResult.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_chunk_result(path: Path, result: ChunkAnalysisResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(result.model_dump_json(indent=2))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_failed_response(failed_dir: Path, chunk_id: str, raw_text: str, error: str) -> Path:
    failed_dir.mkdir(parents=True, exist_ok=True)
    path = failed_dir / f"{chunk_id}_failed.txt"
    path.write_text(f"ERROR:\n{error}\n\nRAW RESPONSE:\n{raw_text}", encoding="utf-8")
    return path
