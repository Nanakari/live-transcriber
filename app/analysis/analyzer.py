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
from .prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_chunk_prompt,
    build_missing_lines_prompt,
    build_repair_prompt,
)
from .schemas import (
    AnalysisChunk,
    AnalysisDocument,
    AnalysisMeta,
    BilingualLine,
    ChunkAnalysisResult,
    FailedChunk,
)


COVERAGE_RETRY_ATTEMPTS = 2
FALLBACK_TRANSLATION_NOTE = "模型未返回翻译，已使用原文占位"


@dataclass
class AnalyzeOptions:
    input_file: Path
    provider: str
    profile: str
    source_language: str
    target_language: str
    model: str
    fallback_model: str
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
    character_profile: bool = False
    summary: bool = True
    study_notes: bool = True


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
            model=_cache_model_name(options),
            profile=options.profile,
            prompt_version=PROMPT_VERSION,
            parameters={"provider": options.provider, "source_language": source_language,
                        "target_language": options.target_language, "temperature": options.temperature,
                        "character_profile": options.character_profile, "summary": options.summary,
                        "study_notes": options.study_notes},
        )
        result_path = chunk_result_path(chunks_dir, chunk, key)
        shared_result_path = chunk_result_path(shared_cache_dir, chunk, key)
        if options.resume and options.cache_enabled:
            cached = load_cached_result(shared_result_path) or load_cached_result(result_path)
            if cached:
                coverage_issues = _chunk_coverage_issues(cached, chunk)
                if coverage_issues:
                    logger.write(
                        f"ignore incomplete cache {chunk.chunk_id}: "
                        + "; ".join(coverage_issues)
                    )
                else:
                    skipped += 1
                    cached = _canonicalize_result(cached, chunk)
                    save_chunk_result(result_path, cached)
                    results.append(cached)
                    logger.write(f"skip validated cache {chunk.chunk_id} ({index}/{len(chunks)})")
                    continue

        logger.write(f"process {chunk.chunk_id} ({index}/{len(chunks)})")
        try:
            result = _process_chunk(client, chunk, options, source_language, logger)
            save_chunk_result(result_path, result)
            if options.cache_enabled and not _result_has_fallback(result):
                save_chunk_result(shared_result_path, result)
            results.append(result)
        except Exception as exc:
            raw_text = getattr(exc, "raw_text", "")
            failed_path = save_failed_response(failed_dir, chunk.chunk_id, raw_text, str(exc))
            failed.append(FailedChunk(chunk_id=chunk.chunk_id, error=str(exc), raw_response_file=str(failed_path)))
            logger.write(f"failed {chunk.chunk_id}: {exc}")
            fallback_result = _fallback_chunk_result(chunk)
            save_chunk_result(result_path, fallback_result)
            results.append(fallback_result)
            logger.write(f"fallback {chunk.chunk_id}: preserved {len(chunk.segments)} source segments")

    results = _ensure_global_coverage(chunks, results, logger)
    fallback_lines = sum(
        1 for result in results for line in result.bilingual_lines
        if line.brief_note == FALLBACK_TRANSLATION_NOTE
    )
    fallback_chunks = sum(1 for result in results if _result_has_fallback(result))

    meta = AnalysisMeta(
        character_profile=options.character_profile,
        summary=options.summary,
        study_notes=options.study_notes,
        run_id=analysis_run_id,
        input_file=str(options.input_file),
        transcript_run_id=document.meta.run_id,
        provider=options.provider,
        model=options.model,
        fallback_model=options.fallback_model,
        profile=options.profile,
        source_language=source_language,
        target_language=options.target_language,
        chunk_minutes=options.chunk_minutes,
        max_segments_per_chunk=options.max_segments_per_chunk,
        prompt_version=PROMPT_VERSION,
        total_chunks=len(chunks),
        succeeded_chunks=len(chunks) - len(failed),
        failed_chunks=len(failed),
        skipped_chunks=skipped,
        fallback_chunks=fallback_chunks,
        fallback_lines=fallback_lines,
    )
    analysis_document = AnalysisDocument(meta=meta, chunks=results, failed_chunks=failed)
    output_paths = export_all(analysis_document, output_dir)
    logger.write(
        f"analysis finished success={len(chunks) - len(failed)} failed={len(failed)} skipped={skipped} "
        f"fallback_chunks={fallback_chunks} fallback_lines={fallback_lines} output={output_dir}"
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
            fallback_model=options.fallback_model,
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


def _cache_model_name(options: AnalyzeOptions) -> str:
    fallback = options.fallback_model.strip()
    if not fallback or fallback == options.model:
        return options.model
    return f"{options.model}|fallback:{fallback}"


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
        character_profile=options.character_profile,
        summary=options.summary,
        study_notes=options.study_notes,
    )
    raw = client.generate_json_text(prompt, system_prompt=SYSTEM_PROMPT)
    result = _parse_chunk_result(client, raw, chunk, logger, options=options)
    return _complete_chunk_coverage(client, result, chunk, source_language, options.target_language, logger)


def _parse_chunk_result(
    client: LLMClient,
    raw: str,
    chunk: AnalysisChunk,
    logger: RunLogger,
    options: AnalyzeOptions | None = None,
) -> ChunkAnalysisResult:
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

    if options:
        if not options.character_profile:
            parsed.pop("profile_observations", None)
        if not options.summary:
            for key in ("chunk_summary_zh", "key_points_zh", "content_importance"):
                parsed.pop(key, None)
        if not options.study_notes:
            for key in ("vocabulary", "grammar", "fixed_expressions", "tone_notes", "learning_value"):
                parsed.pop(key, None)
    parsed.setdefault("chunk_id", chunk.chunk_id)
    parsed.setdefault("start", chunk.start)
    parsed.setdefault("end", chunk.end)
    try:
        return ChunkAnalysisResult.model_validate(parsed)
    except Exception as exc:
        wrapped = AppError(f"Gemini JSON 字段校验失败：{exc}")
        setattr(wrapped, "raw_text", raw)
        raise wrapped from exc


def _complete_chunk_coverage(
    client: LLMClient,
    result: ChunkAnalysisResult,
    chunk: AnalysisChunk,
    source_language: str,
    target_language: str,
    logger: RunLogger,
) -> ChunkAnalysisResult:
    result = _canonicalize_result(result, chunk)
    for attempt in range(1, COVERAGE_RETRY_ATTEMPTS + 1):
        missing = _missing_segments(result, chunk)
        if not missing:
            break
        logger.write(
            f"coverage retry {chunk.chunk_id} attempt={attempt}/{COVERAGE_RETRY_ATTEMPTS} "
            f"missing={len(missing)} ids={','.join(str(item.segment_id) for item in missing)}"
        )
        prompt = build_missing_lines_prompt(
            chunk,
            missing,
            source_language=source_language,
            target_language=target_language,
        )
        try:
            raw = client.generate_json_text(prompt, system_prompt=SYSTEM_PROMPT)
            supplement = _parse_chunk_result(client, raw, chunk, logger)
            result = _merge_supplement(result, supplement, chunk)
        except Exception as exc:
            logger.write(f"coverage retry failed {chunk.chunk_id}: {exc}")
            break

    missing = _missing_segments(result, chunk)
    if missing:
        logger.write(
            f"coverage fallback {chunk.chunk_id}: model still omitted {len(missing)} segments; "
            "preserving original text in subtitle timeline"
        )
        result = _add_fallback_lines(result, missing, chunk)

    issues = _chunk_coverage_issues(result, chunk)
    if issues:
        raise AppError(f"{chunk.chunk_id} 最终逐段完整性校验失败：{'；'.join(issues)}")
    return _canonicalize_result(result, chunk)


def _canonicalize_result(result: ChunkAnalysisResult, chunk: AnalysisChunk) -> ChunkAnalysisResult:
    expected = {segment.segment_id: segment for segment in chunk.segments}
    by_id: dict[int, BilingualLine] = {}
    for line in result.bilingual_lines:
        segment = expected.get(line.segment_id)
        if segment is None or line.segment_id in by_id or not line.translation_zh.strip():
            continue
        by_id[line.segment_id] = line.model_copy(
            update={
                "start": segment.start,
                "end": segment.end,
                "original": segment.text,
            }
        )
    ordered = [by_id[segment.segment_id] for segment in chunk.segments if segment.segment_id in by_id]
    return result.model_copy(
        update={
            "chunk_id": chunk.chunk_id,
            "start": chunk.start,
            "end": chunk.end,
            "bilingual_lines": ordered,
        }
    )


def _merge_supplement(
    result: ChunkAnalysisResult,
    supplement: ChunkAnalysisResult,
    chunk: AnalysisChunk,
) -> ChunkAnalysisResult:
    merged = result.model_copy(
        update={"bilingual_lines": [*result.bilingual_lines, *supplement.bilingual_lines]}
    )
    return _canonicalize_result(merged, chunk)


def _missing_segments(result: ChunkAnalysisResult, chunk: AnalysisChunk):
    present = {line.segment_id for line in result.bilingual_lines if line.translation_zh.strip()}
    return [segment for segment in chunk.segments if segment.segment_id not in present]


def _add_fallback_lines(result: ChunkAnalysisResult, missing, chunk: AnalysisChunk) -> ChunkAnalysisResult:
    fallback_lines = [
        BilingualLine(
            segment_id=segment.segment_id,
            start=segment.start,
            end=segment.end,
            original=segment.text,
            translation_zh=f"[翻译暂缺] {segment.text}",
            literal_zh=segment.text,
            brief_note=FALLBACK_TRANSLATION_NOTE,
            confidence=0.0,
        )
        for segment in missing
    ]
    merged = result.model_copy(update={"bilingual_lines": [*result.bilingual_lines, *fallback_lines]})
    return _canonicalize_result(merged, chunk)


def _fallback_chunk_result(chunk: AnalysisChunk) -> ChunkAnalysisResult:
    empty = ChunkAnalysisResult(chunk_id=chunk.chunk_id, start=chunk.start, end=chunk.end)
    return _add_fallback_lines(empty, chunk.segments, chunk)


def _result_has_fallback(result: ChunkAnalysisResult) -> bool:
    return any(line.brief_note == FALLBACK_TRANSLATION_NOTE for line in result.bilingual_lines)


def _chunk_coverage_issues(result: ChunkAnalysisResult, chunk: AnalysisChunk) -> list[str]:
    expected = {segment.segment_id: segment for segment in chunk.segments}
    actual_ids = [line.segment_id for line in result.bilingual_lines]
    actual_set = set(actual_ids)
    issues: list[str] = []
    missing = [segment_id for segment_id in expected if segment_id not in actual_set]
    unknown = sorted(actual_set - set(expected))
    duplicates = sorted({segment_id for segment_id in actual_ids if actual_ids.count(segment_id) > 1})
    empty = sorted(line.segment_id for line in result.bilingual_lines if not line.translation_zh.strip())
    misaligned = sorted(
        line.segment_id
        for line in result.bilingual_lines
        if line.segment_id in expected
        and (
            abs(line.start - expected[line.segment_id].start) > 0.01
            or abs(line.end - expected[line.segment_id].end) > 0.01
            or line.original != expected[line.segment_id].text
        )
    )
    if missing:
        issues.append(f"缺少 segment_id={missing}")
    if unknown:
        issues.append(f"未知 segment_id={unknown}")
    if duplicates:
        issues.append(f"重复 segment_id={duplicates}")
    if empty:
        issues.append(f"空翻译 segment_id={empty}")
    if misaligned:
        issues.append(f"时间或原文未对齐 segment_id={misaligned}")
    return issues


def _ensure_global_coverage(
    chunks: list[AnalysisChunk],
    results: list[ChunkAnalysisResult],
    logger: RunLogger,
) -> list[ChunkAnalysisResult]:
    by_chunk = {result.chunk_id: result for result in results}
    complete: list[ChunkAnalysisResult] = []
    for chunk in chunks:
        result = by_chunk.get(chunk.chunk_id) or _fallback_chunk_result(chunk)
        missing = _missing_segments(result, chunk)
        if missing:
            logger.write(f"global coverage fallback {chunk.chunk_id}: missing={len(missing)}")
            result = _add_fallback_lines(result, missing, chunk)
        result = _canonicalize_result(result, chunk)
        issues = _chunk_coverage_issues(result, chunk)
        if issues:
            raise AppError(f"导出前完整性校验失败 {chunk.chunk_id}：{'；'.join(issues)}")
        complete.append(result)
    return complete


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
