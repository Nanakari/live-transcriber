from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import project_root
from app.utils import AppError, RunLogger, generate_run_id
from app.output_layout import ensure_media_subdirs, group_dir_from_artifact_path, group_name_from_stem, media_group_dir

from .cache import cache_key, chunk_result_path, load_cached_result, save_chunk_result, save_failed_response
from .chunker import load_transcript, make_chunks, transcript_segments
from .exporters import export_all
from .gemini_client import GeminiClient, LLMClient, LocalLLMClient, parse_json_text
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_chunk_prompt, build_repair_prompt
from .schemas import AnalysisDocument, AnalysisMeta, ChunkAnalysisResult, FailedChunk


@dataclass
class AnalyzeOptions:
    input_file: Path
    provider: str
    profile: str
    source_language: str
    target_language: str
    model: str
    api_key_env: str
    chunk_minutes: float
    max_segments_per_chunk: int
    temperature: float
    max_retries: int
    retry_backoff_seconds: float
    request_timeout_seconds: float
    request_interval_seconds: float
    cache_enabled: bool
    limit_chunks: int | None
    dry_run: bool
    resume: bool
    debug: bool


def run_analysis(options: AnalyzeOptions) -> dict[str, Any]:
    document = load_transcript(options.input_file)
    source_language = options.source_language or document.meta.detected_language or document.meta.language or "auto"
    segments = transcript_segments(document)
    chunks = make_chunks(
        segments,
        chunk_minutes=options.chunk_minutes,
        max_segments_per_chunk=options.max_segments_per_chunk,
    )
    if options.limit_chunks is not None:
        chunks = chunks[: max(0, options.limit_chunks)]

    analysis_run_id = generate_run_id()
    group_dir = group_dir_from_artifact_path(options.input_file)
    if group_dir is None:
        group_dir = media_group_dir(group_name_from_stem(document.meta.run_id or options.input_file.stem))
    media_dirs = ensure_media_subdirs(group_dir)
    output_dir = media_dirs["analysis"] / analysis_run_id
    shared_cache_dir = project_root() / "outputs" / "analysis" / "_cache"
    chunks_dir = output_dir / "chunks"
    failed_dir = output_dir / "failed"
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    shared_cache_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(output_dir / "run.log", debug=options.debug)
    logger.write(f"analysis started input={options.input_file}")
    logger.write(f"chunks={len(chunks)} dry_run={options.dry_run} provider={options.provider} model={options.model}")

    if options.dry_run:
        dry_run_file = output_dir / "chunks_preview.md"
        _export_dry_run(chunks, dry_run_file)
        logger.write(f"dry-run preview={dry_run_file}")
        return {
            "run_id": analysis_run_id,
            "output_dir": output_dir,
            "chunks": chunks,
            "dry_run_file": dry_run_file,
            "dry_run": True,
        }

    client = _make_client(options, logger)
    results: list[ChunkAnalysisResult] = []
    failed: list[FailedChunk] = []
    skipped = 0

    for index, chunk in enumerate(chunks, start=1):
        key = cache_key(
            transcript_file=options.input_file,
            chunk=chunk,
            model=options.model,
            profile=options.profile,
            prompt_version=PROMPT_VERSION,
        )
        result_path = chunk_result_path(chunks_dir, chunk, key)
        shared_result_path = chunk_result_path(shared_cache_dir, chunk, key)
        if options.resume and options.cache_enabled:
            cached = load_cached_result(shared_result_path) or load_cached_result(result_path)
            if cached:
                skipped += 1
                save_chunk_result(result_path, cached)
                results.append(cached)
                logger.write(f"skip cached {chunk.chunk_id} ({index}/{len(chunks)})")
                continue

        logger.write(f"process {chunk.chunk_id} ({index}/{len(chunks)})")
        try:
            result = _process_chunk(client, chunk, options, source_language, logger)
            save_chunk_result(result_path, result)
            if options.cache_enabled:
                save_chunk_result(shared_result_path, result)
            results.append(result)
        except Exception as exc:
            raw_text = getattr(exc, "raw_text", "")
            failed_path = save_failed_response(failed_dir, chunk.chunk_id, raw_text, str(exc))
            failed.append(FailedChunk(chunk_id=chunk.chunk_id, error=str(exc), raw_response_file=str(failed_path)))
            logger.write(f"failed {chunk.chunk_id}: {exc}")

    meta = AnalysisMeta(
        run_id=analysis_run_id,
        input_file=str(options.input_file),
        transcript_run_id=document.meta.run_id,
        provider=options.provider,
        model=options.model,
        profile=options.profile,
        source_language=source_language,
        target_language=options.target_language,
        chunk_minutes=options.chunk_minutes,
        max_segments_per_chunk=options.max_segments_per_chunk,
        prompt_version=PROMPT_VERSION,
        total_chunks=len(chunks),
        succeeded_chunks=len(results),
        failed_chunks=len(failed),
        skipped_chunks=skipped,
    )
    analysis_document = AnalysisDocument(meta=meta, chunks=results, failed_chunks=failed)
    output_paths = export_all(analysis_document, output_dir)
    logger.write(
        f"analysis finished success={len(results)} failed={len(failed)} skipped={skipped} output={output_dir}"
    )
    return {
        "run_id": analysis_run_id,
        "output_dir": output_dir,
        "document": analysis_document,
        "output_paths": output_paths,
        "dry_run": False,
    }


def _make_client(options: AnalyzeOptions, logger: RunLogger) -> LLMClient:
    if options.provider == "gemini":
        return GeminiClient(
            model=options.model,
            api_key_env=options.api_key_env,
            temperature=options.temperature,
            max_retries=options.max_retries,
            retry_backoff_seconds=options.retry_backoff_seconds,
            request_timeout_seconds=options.request_timeout_seconds,
            request_interval_seconds=options.request_interval_seconds,
            logger=logger,
        )
    if options.provider == "local":
        return LocalLLMClient()
    raise AppError(f"未知 provider：{options.provider}。当前支持 gemini，local 仅预留接口。")


def _process_chunk(
    client: LLMClient,
    chunk,
    options: AnalyzeOptions,
    source_language: str,
    logger: RunLogger,
) -> ChunkAnalysisResult:
    prompt = build_chunk_prompt(
        chunk,
        profile=options.profile,
        source_language=source_language,
        target_language=options.target_language,
    )
    raw = client.generate_json_text(prompt, system_prompt=SYSTEM_PROMPT)
    try:
        parsed = parse_json_text(raw)
    except Exception:
        logger.write(f"json parse failed for {chunk.chunk_id}, trying repair")
        repair_raw = client.generate_json_text(build_repair_prompt(raw), system_prompt=SYSTEM_PROMPT)
        try:
            parsed = parse_json_text(repair_raw)
        except Exception as exc:
            wrapped = AppError(f"JSON 解析失败，repair 后仍不是合法 JSON：{exc}")
            setattr(wrapped, "raw_text", repair_raw or raw)
            raise wrapped from exc

    parsed.setdefault("chunk_id", chunk.chunk_id)
    parsed.setdefault("start", chunk.start)
    parsed.setdefault("end", chunk.end)
    try:
        return ChunkAnalysisResult.model_validate(parsed)
    except Exception as exc:
        wrapped = AppError(f"Gemini JSON 字段校验失败：{exc}")
        setattr(wrapped, "raw_text", raw)
        raise wrapped from exc


def _export_dry_run(chunks, output_path: Path) -> None:
    lines = ["# Chunk Preview", ""]
    for chunk in chunks:
        lines.extend(
            [
                f"## {chunk.chunk_id}",
                "",
                f"- 时间：{chunk.start_text} - {chunk.end_text}",
                f"- segments：{len(chunk.segments)}",
                f"- segment_id：{chunk.segments[0].segment_id} - {chunk.segments[-1].segment_id}",
                "",
            ]
        )
        for segment in chunk.segments[:5]:
            lines.append(f"- [{segment.start_text} - {segment.end_text}] #{segment.segment_id} {segment.text[:120]}")
        if len(chunk.segments) > 5:
            lines.append("- ...")
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
