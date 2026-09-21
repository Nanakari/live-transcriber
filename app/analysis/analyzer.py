from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import project_root
from app.utils import AppError, RunLogger, generate_run_id
from app.output_layout import ensure_media_subdirs, group_dir_from_artifact_path, group_name_from_stem, media_group_dir, write_media_index

from .cache import (
    cache_key,
    chunk_result_path,
    load_cached_result,
    load_cached_summary,
    save_chunk_result,
    save_failed_response,
    save_summary_result,
    summary_cache_key,
    summary_result_path,
)
from .chunker import load_transcript, make_chunks, transcript_segments
from .exporters import export_all
from .summary import generate_video_summary
from .gemini_client import GeminiClient, LLMClient, LocalLLMClient, parse_json_text
from .prompts import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    build_chunk_prompt,
    build_media_context,
    build_missing_lines_prompt,
    build_repair_prompt,
    chunk_output_schema,
)
from .repairs import (
    apply_high_confidence_repairs,
    collect_effective_review_items,
    select_repair_candidate,
)
from .schemas import (
    AnalysisChunk,
    AnalysisDocument,
    AnalysisMeta,
    BilingualLine,
    ChunkAnalysisResult,
    FailedChunk,
    TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
    TRANSLATION_STATUS_MISSING,
    TRANSLATION_STATUS_TRANSLATED,
    ReviewItem,
    is_missing_translation_payload,
    normalize_repair_confidence,
    repair_reason_with_confidence_note,
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
    invalid_json_retries: int
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
    local_command: str = "codex"
    local_timeout_seconds: float = 360.0
    local_reasoning_effort: str = ""
    local_ignore_user_config: bool = True
    auto_repair_enabled: bool = True
    auto_repair_threshold: float = 0.80
    # Four workers is the default balance for local Codex requests: enough to
    # keep the pipeline busy without turning transient rate limits into a
    # wave of simultaneous failures.  The CLI/config can override it.
    local_concurrency: int = 4


def run_analysis(options: AnalyzeOptions) -> dict[str, Any]:
    document = load_transcript(options.input_file)
    source_language = options.source_language or document.meta.detected_language or document.meta.language or "auto"
    media_context = build_media_context(
        title=document.meta.title,
        source_url=document.meta.source_url,
        initial_prompt=document.meta.initial_prompt_used,
    )
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
    shared_cache_dir = project_root() / "outputs" / "cache" / "analysis"
    chunks_dir = output_dir / "chunks"
    failed_dir = output_dir / "failed"
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    shared_cache_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(output_dir / "run.log", debug=options.debug)
    logger.write(f"analysis started input={options.input_file}")
    logger.write(
        f"chunks={len(chunks)} dry_run={options.dry_run} provider={options.provider} "
        f"model={options.model} reasoning={options.local_reasoning_effort or 'n/a'} "
        f"concurrency={max(1, options.local_concurrency) if options.provider == 'local' else 1}"
    )

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
    results: list[ChunkAnalysisResult | None] = [None] * len(chunks)
    failed_by_index: dict[int, FailedChunk] = {}
    skipped = 0
    pending: list[tuple[int, AnalysisChunk, Path, Path, ChunkAnalysisResult | None]] = []

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
                        "study_notes": options.study_notes, "media_context": media_context,
                        "reasoning_effort": options.local_reasoning_effort,
                        "ignore_user_config": options.local_ignore_user_config},
        )
        result_path = chunk_result_path(chunks_dir, chunk, key)
        shared_result_path = chunk_result_path(shared_cache_dir, chunk, key)
        cached_for_recovery: ChunkAnalysisResult | None = None
        if options.resume and options.cache_enabled:
            cached = load_cached_result(shared_result_path) or load_cached_result(result_path)
            if cached:
                raw_cache_issues = _chunk_coverage_issues(cached, chunk)
                misaligned_ids = _misaligned_cache_segment_ids(cached, chunk)
                if raw_cache_issues:
                    logger.write(
                        f"validate cache before canonicalize {chunk.chunk_id}: "
                        + "; ".join(raw_cache_issues)
                    )
                if misaligned_ids:
                    # A cached translation must not be associated with a
                    # different source/timeline row merely because canonical
                    # normalization can rewrite those fields.  Remove only
                    # the affected rows and let coverage recovery request
                    # those segment IDs.
                    cached = _drop_misaligned_cache_rows(cached, misaligned_ids)
                cached = _canonicalize_result(cached, chunk)
                coverage_issues = _chunk_coverage_issues(cached, chunk)
                retryable_missing = _missing_segments(cached, chunk)
                blocked = _blocked_segments(cached, chunk)
                if retryable_missing and not _cache_result_is_reusable(cached):
                    # An all-fallback cache can come from a failed full
                    # request and has no successful rows to preserve.  Run a
                    # normal full request so summary/study fields are not
                    # permanently stranded in an empty recovery shell.
                    logger.write(
                        f"reprocess cache with no reusable rows {chunk.chunk_id}: "
                        f"retryable_missing={len(retryable_missing)}"
                    )
                elif retryable_missing:
                    cached_for_recovery = cached
                    details = [
                        f"retryable_missing={len(retryable_missing)}",
                        f"blocked_asr_review={len(blocked)}",
                    ]
                    if coverage_issues:
                        details.append("structural=" + "; ".join(coverage_issues))
                    logger.write(
                        f"recover partial cache {chunk.chunk_id}: " + " ".join(details)
                    )
                elif blocked:
                    skipped += 1
                    save_chunk_result(result_path, cached)
                    results[index - 1] = cached
                    logger.write(
                        f"skip cache with blocked ASR review {chunk.chunk_id} "
                        f"blocked={len(blocked)} ({index}/{len(chunks)})"
                    )
                    continue
                elif not coverage_issues:
                    skipped += 1
                    save_chunk_result(result_path, cached)
                    results[index - 1] = cached
                    logger.write(f"skip validated cache {chunk.chunk_id} ({index}/{len(chunks)})")
                    continue
                else:
                    # Canonicalization normally repairs alignment and removes
                    # unknown/duplicate rows.  If a cache is still malformed,
                    # keep whatever valid rows survived and let coverage
                    # recovery request only the missing segment IDs.
                    cached_for_recovery = cached
                    logger.write(
                        f"recover malformed cache {chunk.chunk_id}: "
                        + "; ".join(coverage_issues)
                    )

        logger.write(f"process {chunk.chunk_id} ({index}/{len(chunks)})")

        pending.append((index, chunk, result_path, shared_result_path, cached_for_recovery))

    def process_pending(
        item: tuple[int, AnalysisChunk, Path, Path, ChunkAnalysisResult | None],
    ) -> tuple[int, ChunkAnalysisResult, FailedChunk | None]:
        index, chunk, result_path, shared_result_path, cached_for_recovery = item
        try:
            result = _process_chunk(
                client,
                chunk,
                options,
                source_language,
                logger,
                media_context=media_context,
                initial_result=cached_for_recovery,
            )
            save_chunk_result(result_path, result)
            if options.cache_enabled and _cache_result_is_reusable(result):
                # Partial caches are resumable: translated rows are retained,
                # retryable omissions are requested on the next run, and
                # blocked ASR rows keep their explicit state instead of being
                # sent to the model again.
                save_chunk_result(shared_result_path, result)
            return index, result, None
        except Exception as exc:
            raw_text = getattr(exc, "raw_text", "")
            failed_path = save_failed_response(failed_dir, chunk.chunk_id, raw_text, str(exc))
            failed_chunk = FailedChunk(
                chunk_id=chunk.chunk_id,
                error=str(exc),
                raw_response_file=str(failed_path),
            )
            logger.write(f"failed {chunk.chunk_id}: {exc}")
            if cached_for_recovery is not None:
                # A coverage-only recovery failure must retain every good row
                # already present in the cache.  Fill only the retryable IDs;
                # never replace a partially recovered chunk with a full
                # fallback result.
                retained = _canonicalize_result(cached_for_recovery, chunk)
                missing = _missing_segments(retained, chunk)
                fallback_result = _add_fallback_lines(retained, missing, chunk)
                logger.write(
                    f"coverage recovery fallback {chunk.chunk_id}: "
                    f"retained={len(retained.bilingual_lines) - len(missing)} "
                    f"fallback={len(missing)}"
                )
            else:
                fallback_result = _fallback_chunk_result(chunk)
            save_chunk_result(result_path, fallback_result)
            if options.cache_enabled and _cache_result_is_reusable(fallback_result):
                save_chunk_result(shared_result_path, fallback_result)
            if cached_for_recovery is None:
                logger.write(f"fallback {chunk.chunk_id}: preserved {len(chunk.segments)} source segments")
            return index, fallback_result, failed_chunk

    if pending:
        worker_count = min(
            len(pending),
            max(1, options.local_concurrency) if options.provider == "local" else 1,
        )
        if worker_count == 1:
            completed = (process_pending(item) for item in pending)
            for index, result, failed_chunk in completed:
                results[index - 1] = result
                if failed_chunk is not None:
                    failed_by_index[index] = failed_chunk
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="live-transcriber-analysis",
            ) as executor:
                futures = [executor.submit(process_pending, item) for item in pending]
                for future in as_completed(futures):
                    index, result, failed_chunk = future.result()
                    results[index - 1] = result
                    if failed_chunk is not None:
                        failed_by_index[index] = failed_chunk
                    logger.write(
                        f"finished {chunks[index - 1].chunk_id} ({index}/{len(chunks)})"
                    )

    if any(result is None for result in results):
        raise AppError("分析任务未能为所有 chunk 生成结果。")
    ordered_results = [result for result in results if result is not None]
    failed = [failed_by_index[index] for index in sorted(failed_by_index)]

    ordered_results = _ensure_global_coverage(chunks, ordered_results, logger)
    fallback_lines = sum(
        1 for result in ordered_results for line in result.bilingual_lines
        if _line_has_fallback(line)
    )
    fallback_chunks = sum(1 for result in ordered_results if _result_has_fallback(result))

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
        reasoning_effort=options.local_reasoning_effort if options.provider == "local" else "",
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
        auto_repair_enabled=options.auto_repair_enabled,
        auto_repair_threshold=options.auto_repair_threshold,
    )
    analysis_document = AnalysisDocument(meta=meta, chunks=ordered_results, failed_chunks=failed)
    if options.summary:
        summary_key = summary_cache_key(
            analysis_document,
            prompt_version=PROMPT_VERSION,
            media_context=media_context,
        )
        summary_path = summary_result_path(shared_cache_dir, summary_key)
        cached_summary = (
            load_cached_summary(summary_path)
            if options.resume and options.cache_enabled
            else None
        )
        if cached_summary is not None:
            analysis_document.video_summary = cached_summary
            analysis_document.video_summary_error = ""
            logger.write(f"skip validated summary cache key={summary_key}")
        else:
            generate_video_summary(analysis_document, client, logger, media_context=media_context)
            if analysis_document.video_summary is not None and options.cache_enabled:
                save_summary_result(summary_path, analysis_document.video_summary)
                logger.write(f"saved summary cache key={summary_key}")
    if options.auto_repair_enabled:
        repair_records = apply_high_confidence_repairs(
            analysis_document,
            threshold=options.auto_repair_threshold,
            logger=logger,
        )
    else:
        repair_records = []
        analysis_document.meta = analysis_document.meta.model_copy(update={
            "auto_repair_enabled": False,
            "auto_repair_threshold": options.auto_repair_threshold,
            "auto_repaired_lines": 0,
            "unresolved_review_items": len(
                [item for chunk in analysis_document.chunks for item in chunk.review_items]
            ),
        })
    _update_quality_metadata(analysis_document)
    output_paths = export_all(analysis_document, output_dir)
    write_media_index(group_dir)
    logger.write(
        f"analysis finished success={len(chunks) - len(failed)} failed={len(failed)} skipped={skipped} "
        f"fallback_chunks={fallback_chunks} fallback_lines={fallback_lines} output={output_dir}"
    )
    return {
        "run_id": analysis_run_id,
        "output_dir": output_dir,
        "document": analysis_document,
        "output_paths": output_paths,
        "repair_records": repair_records,
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
        return LocalLLMClient(
            command=options.local_command,
            model=options.model,
            timeout_seconds=options.local_timeout_seconds,
            cwd=project_root(),
            logger=logger,
            reasoning_effort=options.local_reasoning_effort,
            ignore_user_config=options.local_ignore_user_config,
        )
    raise AppError(f"未知 provider：{options.provider}。当前支持 gemini，local 使用本机 Codex CLI。")


def _cache_model_name(options: AnalyzeOptions) -> str:
    local_suffix = ""
    if options.provider == "local":
        local_suffix = f"|reasoning:{options.local_reasoning_effort or 'cli-default'}"
    fallback = options.fallback_model.strip()
    if not fallback or fallback == options.model:
        return options.model + local_suffix
    return f"{options.model}|fallback:{fallback}{local_suffix}"


def _process_chunk(
    client: LLMClient,
    chunk,
    options: AnalyzeOptions,
    source_language: str,
    logger: RunLogger,
    *,
    media_context: str = "",
    initial_result: ChunkAnalysisResult | None = None,
) -> ChunkAnalysisResult:
    if initial_result is None:
        prompt = build_chunk_prompt(
            chunk,
            profile=options.profile,
            source_language=source_language,
            target_language=options.target_language,
            character_profile=options.character_profile,
            summary=options.summary,
            study_notes=options.study_notes,
            media_context=media_context,
        )
        result = _request_valid_chunk_result(client, prompt, chunk, logger, options)
    else:
        result = _canonicalize_result(initial_result, chunk)
        logger.write(
            f"coverage recovery only {chunk.chunk_id}: "
            f"retained={len(result.bilingual_lines) - len(_missing_segments(result, chunk))}"
        )
    return _complete_chunk_coverage(
        client,
        result,
        chunk,
        source_language,
        options.target_language,
        logger,
        media_context=media_context,
        use_output_schema=options.provider == "local",
    )


def _generate_json_text(
    client: LLMClient,
    prompt: str,
    *,
    system_prompt: str,
    output_schema: dict | None = None,
) -> str:
    if output_schema is None:
        return client.generate_json_text(prompt, system_prompt=system_prompt)
    return client.generate_json_text(
        prompt,
        system_prompt=system_prompt,
        output_schema=output_schema,
    )


def _request_valid_chunk_result(
    client: LLMClient,
    original_prompt: str,
    chunk: AnalysisChunk,
    logger: RunLogger,
    options: AnalyzeOptions,
) -> ChunkAnalysisResult:
    total_attempts = max(1, options.invalid_json_retries + 1)
    invalid_raw = ""
    last_error: Exception | None = None
    for attempt in range(1, total_attempts + 1):
        if attempt == total_attempts and total_attempts > 1:
            switch = getattr(client, "activate_fallback", None)
            if callable(switch):
                switch("invalid JSON after repeated repair attempts")
        request_prompt = (
            build_repair_prompt(invalid_raw)
            if attempt == 2 and invalid_raw
            else original_prompt
        )
        mode = "initial" if attempt == 1 else ("repair" if request_prompt != original_prompt else "regenerate")
        logger.write(
            f"json response attempt {chunk.chunk_id} attempt={attempt}/{total_attempts} mode={mode}"
        )
        raw = _generate_json_text(
            client,
            request_prompt,
            system_prompt=SYSTEM_PROMPT,
            output_schema=chunk_output_schema() if options.provider == "local" else None,
        )
        try:
            return _parse_chunk_result(raw, chunk, logger, options=options)
        except Exception as exc:
            last_error = exc
            invalid_raw = getattr(exc, "raw_text", "") or raw
            logger.write(
                f"invalid json response {chunk.chunk_id} attempt={attempt}/{total_attempts}: {exc}"
            )
    wrapped = AppError(
        f"模型连续 {total_attempts} 次返回无效 JSON：{last_error or '未知格式错误'}"
    )
    setattr(wrapped, "raw_text", invalid_raw)
    raise wrapped from last_error


def _parse_chunk_result(
    raw: str,
    chunk: AnalysisChunk,
    logger: RunLogger,
    options: AnalyzeOptions | None = None,
) -> ChunkAnalysisResult:
    try:
        parsed = parse_json_text(raw)
    except Exception as exc:
        wrapped = AppError(f"JSON 解析失败：{exc}")
        setattr(wrapped, "raw_text", raw)
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
    # translation_status and auto_repaired are program-owned fields.  A model
    # response may contain them because it was copied from a prior cache, but
    # it must not decide whether a line is retryable or already accepted.
    for line in parsed.get("bilingual_lines", []) or []:
        if isinstance(line, dict):
            line.pop("translation_status", None)
            line["translation_status"] = (
                TRANSLATION_STATUS_MISSING
                if is_missing_translation_payload(
                    line.get("translation_zh"),
                    line.get("brief_note"),
                )
                else TRANSLATION_STATUS_TRANSLATED
            )
            line["auto_repaired"] = False
    for item in parsed.get("review_items", []) or []:
        if isinstance(item, dict):
            item["auto_repaired"] = False
    parsed.setdefault("chunk_id", chunk.chunk_id)
    parsed.setdefault("start", chunk.start)
    parsed.setdefault("end", chunk.end)
    try:
        return ChunkAnalysisResult.model_validate(parsed)
    except Exception as exc:
        wrapped = AppError(f"模型 JSON 字段校验失败：{exc}")
        setattr(wrapped, "raw_text", raw)
        raise wrapped from exc


def _complete_chunk_coverage(
    client: LLMClient,
    result: ChunkAnalysisResult,
    chunk: AnalysisChunk,
    source_language: str,
    target_language: str,
    logger: RunLogger,
    *,
    media_context: str = "",
    use_output_schema: bool = False,
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
            media_context=media_context,
        )
        try:
            raw = _generate_json_text(
                client,
                prompt,
                system_prompt=SYSTEM_PROMPT,
                output_schema=chunk_output_schema() if use_output_schema else None,
            )
            supplement = _parse_chunk_result(raw, chunk, logger)
            result = _merge_supplement(result, supplement, chunk)
        except Exception as exc:
            logger.write(f"coverage retry failed {chunk.chunk_id}: {exc}")
            continue

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
    review_items: list[ReviewItem] = []
    reviews_by_segment: dict[int, list[ReviewItem]] = {}
    seen_review_keys: set[tuple] = set()
    for item in result.review_items:
        if item.segment_id is None:
            normalized = item.model_copy(update={"auto_repaired": False})
            key = _review_dedup_key(normalized)
            if key not in seen_review_keys:
                seen_review_keys.add(key)
                review_items.append(normalized)
            continue
        segment = expected.get(item.segment_id)
        if segment is None:
            continue
        normalized = item.model_copy(
            update={
                "start": segment.start,
                "end": segment.end,
                "original": segment.text,
                "auto_repaired": False,
            }
        )
        confidence, valid = normalize_repair_confidence(normalized.repair_confidence)
        reason = normalized.repair_reason
        if not valid:
            reason = repair_reason_with_confidence_note(reason, normalized.repair_confidence)
        normalized = normalized.model_copy(update={
            "repair_confidence": confidence,
            "repair_reason": reason,
        })
        key = _review_dedup_key(normalized)
        if key in seen_review_keys:
            continue
        seen_review_keys.add(key)
        review_items.append(normalized)
        reviews_by_segment.setdefault(item.segment_id, []).append(normalized)

    lines_by_segment: dict[int, list[BilingualLine]] = {}
    for line in result.bilingual_lines:
        segment = expected.get(line.segment_id)
        if segment is None:
            continue
        confidence, valid = normalize_repair_confidence(line.repair_confidence)
        reason = line.repair_reason
        if not valid:
            reason = repair_reason_with_confidence_note(reason, line.repair_confidence)
        line = line.model_copy(update={
            "repair_confidence": confidence,
            "repair_reason": reason,
        })
        # Keep a retryable or blocked row even when its translation is empty;
        # dropping it would lose the program-owned state from a recovered
        # cache.  Duplicates are resolved below in favor of a translated row.
        if not line.translation_zh.strip() and line.translation_status == TRANSLATION_STATUS_TRANSLATED:
            continue
        lines_by_segment.setdefault(line.segment_id, []).append(line)

    by_id: dict[int, BilingualLine] = {}
    for segment_id, candidates in lines_by_segment.items():
        line = max(enumerate(candidates), key=lambda item: (_line_priority(item[1]), -item[0]))[1]
        segment = expected[segment_id]
        related_reviews = reviews_by_segment.get(line.segment_id, [])
        review_reason = "；".join(
            dict.fromkeys(item.reason_zh.strip() for item in related_reviews if item.reason_zh.strip())
        )
        asr_reviews = [item for item in related_reviews if _review_is_asr_related(item)]
        selection = select_repair_candidate(line, related_reviews)
        selected = selection.candidate
        line_candidate = (
            select_repair_candidate(line, ()).candidate
            if line.corrected_original.strip()
            else None
        )
        if (
            line_candidate is not None
            and selected is not None
            and line_candidate.text != selected.text
        ):
            # Selecting a review candidate must not erase a distinct
            # candidate that was supplied directly on the bilingual line.
            # Keep it as a separate review tuple so a later repair pass still
            # sees the conflict after canonicalization has run again.
            displaced = ReviewItem(
                segment_id=line.segment_id,
                start=segment.start,
                end=segment.end,
                original=segment.text,
                corrected_original=line_candidate.text,
                repair_confidence=line_candidate.confidence,
                repair_reason=line_candidate.reason,
                reason_zh=(
                    line.review_reason.strip()
                    or line_candidate.reason
                    or "源语言修复候选需要人工核对。"
                ),
                risk_type="ASR" if line.asr_suspect else "复核",
                auto_repaired=False,
            )
            displaced_key = _review_dedup_key(displaced)
            if displaced_key not in seen_review_keys:
                seen_review_keys.add(displaced_key)
                review_items.append(displaced)
                related_reviews = [*related_reviews, displaced]
        status = line.translation_status
        by_id[line.segment_id] = line.model_copy(
            update={
                "start": segment.start,
                "end": segment.end,
                "original": segment.text,
                # Candidate text, confidence and reason are selected as one
                # atomic proposal.  Never combine one candidate's text with
                # another candidate's score or explanation.
                "corrected_original": selected.text if selected else "",
                "repair_confidence": selected.confidence if selected else 0.0,
                "repair_reason": selected.reason if selected else "",
                "auto_repaired": False,
                "review_required": (
                    line.review_required
                    or bool(related_reviews)
                    or status in {
                        TRANSLATION_STATUS_MISSING,
                        TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
                    }
                ),
                "review_reason": line.review_reason.strip() or review_reason,
                "asr_suspect": (
                    line.asr_suspect
                    or status == TRANSLATION_STATUS_BLOCKED_ASR_REVIEW
                    or bool(asr_reviews)
                ),
                "asr_issue": line.asr_issue.strip()
                or "；".join(
                    dict.fromkeys(item.reason_zh.strip() for item in asr_reviews if item.reason_zh.strip())
                ),
            }
        )
    ordered = [by_id[segment.segment_id] for segment in chunk.segments if segment.segment_id in by_id]
    return result.model_copy(
        update={
            "chunk_id": chunk.chunk_id,
            "start": chunk.start,
            "end": chunk.end,
            "bilingual_lines": ordered,
            "review_items": review_items,
        }
    )


def _review_dedup_key(item: ReviewItem) -> tuple:
    confidence, _ = normalize_repair_confidence(item.repair_confidence)
    return (
        item.segment_id,
        " ".join(item.original.split()),
        " ".join(item.reason_zh.split()),
        " ".join(item.risk_type.split()),
        " ".join(item.corrected_original.split()),
        round(confidence, 6),
        " ".join(item.repair_reason.split()),
    )


def _line_priority(line: BilingualLine) -> int:
    if line.translation_status == TRANSLATION_STATUS_TRANSLATED and line.translation_zh.strip():
        return 3
    if line.translation_status == TRANSLATION_STATUS_BLOCKED_ASR_REVIEW:
        return 2
    if line.translation_status == TRANSLATION_STATUS_MISSING and line.translation_zh.strip():
        return 1
    return 0


def _review_is_asr_related(item) -> bool:
    text = f"{item.risk_type} {item.reason_zh}".lower()
    return any(
        marker in text
        for marker in ("asr", "语音识别", "听不清", "听写", "专有名词")
    )


def _merge_supplement(
    result: ChunkAnalysisResult,
    supplement: ChunkAnalysisResult,
    chunk: AnalysisChunk,
) -> ChunkAnalysisResult:
    # A coverage response must only replace rows that were retryable missing.
    # Successful translations are retained even if a supplement repeats their
    # segment_id, and blocked ASR rows are never unblocked by a model response.
    lines_by_id = {line.segment_id: line for line in result.bilingual_lines}
    for line in supplement.bilingual_lines:
        current = lines_by_id.get(line.segment_id)
        if current is None:
            lines_by_id[line.segment_id] = line
        elif current.translation_status == TRANSLATION_STATUS_MISSING:
            lines_by_id[line.segment_id] = line
    merged = result.model_copy(
        update={
            "bilingual_lines": list(lines_by_id.values()),
            "review_items": [*result.review_items, *supplement.review_items],
        }
    )
    return _canonicalize_result(merged, chunk)


def _missing_segments(result: ChunkAnalysisResult, chunk: AnalysisChunk):
    present = {
        line.segment_id
        for line in result.bilingual_lines
        if line.translation_status in {
            TRANSLATION_STATUS_TRANSLATED,
            TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
        }
        and (line.translation_zh.strip() or line.translation_status == TRANSLATION_STATUS_BLOCKED_ASR_REVIEW)
    }
    return [segment for segment in chunk.segments if segment.segment_id not in present]


def _blocked_segments(result: ChunkAnalysisResult, chunk: AnalysisChunk):
    expected_ids = {segment.segment_id for segment in chunk.segments}
    return [
        line
        for line in result.bilingual_lines
        if line.segment_id in expected_ids
        and line.translation_status == TRANSLATION_STATUS_BLOCKED_ASR_REVIEW
    ]


def _add_fallback_lines(result: ChunkAnalysisResult, missing, chunk: AnalysisChunk) -> ChunkAnalysisResult:
    by_id = {line.segment_id: line for line in result.bilingual_lines}
    for segment in missing:
        fallback = BilingualLine(
            segment_id=segment.segment_id,
            start=segment.start,
            end=segment.end,
            original=segment.text,
            translation_zh=f"[翻译暂缺] {segment.text}",
            literal_zh=segment.text,
            brief_note=FALLBACK_TRANSLATION_NOTE,
            review_required=True,
            review_reason=FALLBACK_TRANSLATION_NOTE,
            confidence=0.0,
            translation_status=TRANSLATION_STATUS_MISSING,
        )
        existing = by_id.get(segment.segment_id)
        if existing is not None and existing.translation_status == TRANSLATION_STATUS_MISSING:
            fallback = existing.model_copy(update={
                "start": segment.start,
                "end": segment.end,
                "original": segment.text,
                "translation_zh": fallback.translation_zh,
                "literal_zh": fallback.literal_zh,
                "brief_note": FALLBACK_TRANSLATION_NOTE,
                "review_required": True,
                "review_reason": FALLBACK_TRANSLATION_NOTE,
                "confidence": 0.0,
                "translation_status": TRANSLATION_STATUS_MISSING,
                "auto_repaired": False,
            })
        by_id[segment.segment_id] = fallback
    merged = result.model_copy(update={"bilingual_lines": list(by_id.values())})
    return _canonicalize_result(merged, chunk)


def _fallback_chunk_result(chunk: AnalysisChunk) -> ChunkAnalysisResult:
    empty = ChunkAnalysisResult(chunk_id=chunk.chunk_id, start=chunk.start, end=chunk.end)
    return _add_fallback_lines(empty, chunk.segments, chunk)


def _result_has_fallback(result: ChunkAnalysisResult) -> bool:
    return any(_line_has_fallback(line) for line in result.bilingual_lines)


def _cache_result_is_reusable(result: ChunkAnalysisResult) -> bool:
    """Whether a cache has a row worth retaining across a failed retry."""
    return any(
        line.translation_status in {
            TRANSLATION_STATUS_TRANSLATED,
            TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
        }
        for line in result.bilingual_lines
    )


def _line_has_fallback(line: BilingualLine) -> bool:
    return (
        line.translation_status in {
            TRANSLATION_STATUS_MISSING,
            TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
        }
        or line.brief_note.strip() == FALLBACK_TRANSLATION_NOTE
        or line.translation_zh.lstrip().startswith("[翻译暂缺]")
    )


def _misaligned_cache_segment_ids(result: ChunkAnalysisResult, chunk: AnalysisChunk) -> set[int]:
    expected = {segment.segment_id: segment for segment in chunk.segments}
    misaligned: set[int] = set()
    for line in result.bilingual_lines:
        segment = expected.get(line.segment_id)
        if segment is None:
            continue
        if (
            abs(line.start - segment.start) > 0.01
            or abs(line.end - segment.end) > 0.01
            or line.original != segment.text
        ):
            misaligned.add(line.segment_id)
    return misaligned


def _drop_misaligned_cache_rows(
    result: ChunkAnalysisResult,
    segment_ids: set[int],
) -> ChunkAnalysisResult:
    return result.model_copy(update={
        "bilingual_lines": [
            line for line in result.bilingual_lines
            if line.segment_id not in segment_ids
        ],
        "review_items": [
            item for item in result.review_items
            if item.segment_id is None or item.segment_id not in segment_ids
        ],
    })


def _update_quality_metadata(document: AnalysisDocument) -> None:
    effective_review_items = collect_effective_review_items(document)
    review_segments = {
        (chunk_id, item.segment_id)
        for chunk_id, item in effective_review_items
        if item.segment_id is not None
    }
    document.meta.review_items = len(effective_review_items)
    document.meta.review_segments = len(review_segments)
    document.meta.unresolved_review_items = sum(
        1 for _, item in effective_review_items if not item.auto_repaired
    )
    if not document.meta.total_chunks or document.meta.failed_chunks >= document.meta.total_chunks:
        document.meta.quality_status = "failed"
    elif document.meta.failed_chunks or document.meta.fallback_lines:
        document.meta.quality_status = "partial"
    elif document.meta.unresolved_review_items or document.video_summary_error:
        document.meta.quality_status = "complete_with_warnings"
    else:
        document.meta.quality_status = "complete"


def _chunk_coverage_issues(result: ChunkAnalysisResult, chunk: AnalysisChunk) -> list[str]:
    expected = {segment.segment_id: segment for segment in chunk.segments}
    actual_ids = [line.segment_id for line in result.bilingual_lines]
    actual_set = set(actual_ids)
    issues: list[str] = []
    missing = [segment_id for segment_id in expected if segment_id not in actual_set]
    unknown = sorted(actual_set - set(expected))
    duplicates = sorted({segment_id for segment_id in actual_ids if actual_ids.count(segment_id) > 1})
    empty = sorted(
        line.segment_id
        for line in result.bilingual_lines
        if not line.translation_zh.strip()
        and line.translation_status != TRANSLATION_STATUS_BLOCKED_ASR_REVIEW
    )
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
