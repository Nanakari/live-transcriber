from __future__ import annotations

import json
from pathlib import Path

import app.analysis.analyzer as analyzer_module

from app.analysis.analyzer import (
    AnalyzeOptions,
    FALLBACK_TRANSLATION_NOTE,
    _chunk_coverage_issues,
    _fallback_chunk_result,
    _merge_supplement,
    _missing_segments,
    _process_chunk,
    _result_has_fallback,
    _cache_model_name,
    run_analysis,
)
from app.analysis.cache import cache_key, chunk_result_path, save_chunk_result
from app.analysis.exporters import export_combined_study_markdown, export_translation_srt
from app.analysis.prompts import PROMPT_VERSION
from app.analysis.schemas import (
    AnalysisChunk,
    AnalysisDocument,
    AnalysisMeta,
    AnalysisSegment,
    BilingualLine,
    ChunkAnalysisResult,
    TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
    TRANSLATION_STATUS_MISSING,
)
from app.schemas import TranscriptDocument, TranscriptMeta, TranscriptSegment
from app.utils import RunLogger


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.calls = 0
        self.prompts: list[str] = []

    def generate_json_text(self, prompt: str, *, system_prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return json.dumps(response, ensure_ascii=False)


class RawFakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls = 0
        self.fallback_reasons: list[str] = []

    def generate_json_text(self, prompt: str, *, system_prompt: str) -> str:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response

    def activate_fallback(self, reason: str) -> bool:
        self.fallback_reasons.append(reason)
        return True


def _chunk() -> AnalysisChunk:
    segments = [
        AnalysisSegment(
            segment_id=index,
            start=float(index - 1),
            end=float(index),
            start_text=f"00:00:0{index - 1}.000",
            end_text=f"00:00:0{index}.000",
            text=f"原文{index}",
        )
        for index in range(1, 4)
    ]
    return AnalysisChunk(
        chunk_id="chunk_0000",
        index=0,
        start=0.0,
        end=3.0,
        start_text="00:00:00.000",
        end_text="00:00:03.000",
        segments=segments,
    )


def _options(tmp_path: Path) -> AnalyzeOptions:
    return AnalyzeOptions(
        input_file=tmp_path / "transcript.json",
        provider="gemini",
        profile="multilingual_study",
        source_language="ja",
        target_language="zh",
        model="test",
        fallback_model="",
        api_key_env="TEST_KEY",
        chunk_minutes=2,
        max_segments_per_chunk=20,
        temperature=0.0,
        max_retries=0,
        invalid_json_retries=3,
        retry_backoff_seconds=0,
        request_timeout_seconds=1,
        request_interval_seconds=0,
        cache_enabled=True,
        limit_chunks=None,
        dry_run=False,
        resume=False,
        debug=False,
    )


def _line(segment_id: int, translation: str) -> dict:
    return {
        "segment_id": segment_id,
        "start": 99,
        "end": 100,
        "original": "模型改写的原文",
        "translation_zh": translation,
    }


def test_missing_segments_are_requested_again_and_merged(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {"bilingual_lines": [_line(1, "译文1")]},
            {"bilingual_lines": [_line(2, "译文2"), _line(3, "译文3")]},
        ]
    )
    chunk = _chunk()

    result = _process_chunk(
        client,
        chunk,
        _options(tmp_path),
        "ja",
        RunLogger(tmp_path / "run.log", mirror_stdout=False),
    )

    assert client.calls == 2
    assert [line.segment_id for line in result.bilingual_lines] == [1, 2, 3]
    assert [line.original for line in result.bilingual_lines] == ["原文1", "原文2", "原文3"]
    assert [(line.start, line.end) for line in result.bilingual_lines] == [(0, 1), (1, 2), (2, 3)]
    assert _chunk_coverage_issues(result, chunk) == []


def test_review_items_are_linked_back_to_bilingual_lines(tmp_path: Path) -> None:
    client = FakeClient([
        {
            "bilingual_lines": [_line(1, "译文1"), _line(2, "译文2"), _line(3, "译文3")],
            "review_items": [{
                "segment_id": 2,
                "reason_zh": "专有名词可能听错",
                "risk_type": "ASR/专有名词",
            }],
        }
    ])

    result = _process_chunk(
        client,
        _chunk(),
        _options(tmp_path),
        "ja",
        RunLogger(tmp_path / "run.log", mirror_stdout=False),
    )

    line = result.bilingual_lines[1]
    assert line.review_required is True
    assert line.review_reason == "专有名词可能听错"
    assert line.asr_suspect is True
    assert line.asr_issue == "专有名词可能听错"


def test_invalid_json_is_repaired_before_chunk_is_skipped(tmp_path: Path) -> None:
    complete = {"bilingual_lines": [_line(index, f"译文{index}") for index in range(1, 4)]}
    client = RawFakeClient(["not-json", json.dumps(complete, ensure_ascii=False)])

    result = _process_chunk(
        client,
        _chunk(),
        _options(tmp_path),
        "ja",
        RunLogger(tmp_path / "run.log", mirror_stdout=False),
    )

    assert client.calls == 2
    assert client.fallback_reasons == []
    assert [line.translation_zh for line in result.bilingual_lines] == ["译文1", "译文2", "译文3"]


def test_repeated_invalid_json_uses_fallback_only_on_last_attempt(tmp_path: Path) -> None:
    complete = {"bilingual_lines": [_line(index, f"译文{index}") for index in range(1, 4)]}
    client = RawFakeClient(["bad-1", "bad-2", "bad-3", json.dumps(complete, ensure_ascii=False)])

    result = _process_chunk(
        client,
        _chunk(),
        _options(tmp_path),
        "ja",
        RunLogger(tmp_path / "run.log", mirror_stdout=False),
    )

    assert client.calls == 4
    assert len(client.fallback_reasons) == 1
    assert [line.segment_id for line in result.bilingual_lines] == [1, 2, 3]


def test_repeated_omissions_fall_back_to_original_without_timeline_gap(tmp_path: Path) -> None:
    client = FakeClient(
        [
            {"bilingual_lines": [_line(1, "译文1")]},
            {"bilingual_lines": []},
            {"bilingual_lines": []},
        ]
    )
    chunk = _chunk()

    result = _process_chunk(
        client,
        chunk,
        _options(tmp_path),
        "ja",
        RunLogger(tmp_path / "run.log", mirror_stdout=False),
    )

    assert client.calls == 3
    assert [line.segment_id for line in result.bilingual_lines] == [1, 2, 3]
    assert result.bilingual_lines[1].translation_zh == "[翻译暂缺] 原文2"
    assert result.bilingual_lines[1].brief_note == FALLBACK_TRANSLATION_NOTE
    assert _chunk_coverage_issues(result, chunk) == []


def test_incomplete_cached_result_is_detected() -> None:
    client = FakeClient([{"bilingual_lines": [_line(1, "译文1")] }])
    chunk = _chunk()
    parsed = json.loads(client.generate_json_text("", system_prompt=""))
    from app.analysis.schemas import ChunkAnalysisResult

    result = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=chunk.start,
        end=chunk.end,
        bilingual_lines=parsed["bilingual_lines"],
    )

    issues = _chunk_coverage_issues(result, chunk)

    assert any("缺少 segment_id=[2, 3]" in issue for issue in issues)
    assert any("时间或原文未对齐" in issue for issue in issues)


def test_legacy_and_malformed_translation_statuses_are_safe() -> None:
    legacy_missing = BilingualLine(
        segment_id=1,
        translation_zh="[翻译暂缺] 原文",
        brief_note=FALLBACK_TRANSLATION_NOTE,
    )
    legacy_blocked = BilingualLine(
        segment_id=2,
        translation_zh="[翻译暂缺] 乱码",
        brief_note=FALLBACK_TRANSLATION_NOTE,
        asr_suspect=True,
    )
    malformed = BilingualLine(
        segment_id=3,
        translation_zh="[翻译暂缺] 原文",
        translation_status={},
    )

    assert legacy_missing.translation_status == TRANSLATION_STATUS_MISSING
    assert legacy_blocked.translation_status == TRANSLATION_STATUS_BLOCKED_ASR_REVIEW
    assert malformed.translation_status == TRANSLATION_STATUS_MISSING
    assert _result_has_fallback(ChunkAnalysisResult(
        chunk_id="chunk_0000",
        start=0,
        end=1,
        bilingual_lines=[legacy_missing, legacy_blocked, malformed],
    )) is True


def test_explicit_missing_stays_retryable_when_asr_is_suspect() -> None:
    explicit_missing = BilingualLine(
        segment_id=4,
        translation_zh="",
        asr_suspect=True,
        translation_status=TRANSLATION_STATUS_MISSING,
    )

    assert explicit_missing.translation_status == TRANSLATION_STATUS_MISSING


def test_fresh_empty_asr_response_is_retried_even_if_model_supplies_blocked_status(tmp_path: Path) -> None:
    chunk = _chunk()
    first = {
        "bilingual_lines": [
            {
                **_line(index, ""),
                "asr_suspect": True,
                "asr_issue": "专名可能听错",
                "translation_status": TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
            }
            for index in range(1, 4)
        ],
    }
    second = {
        "bilingual_lines": [_line(index, f"补译{index}") for index in range(1, 4)],
    }
    client = FakeClient([first, second])

    result = _process_chunk(
        client,
        chunk,
        _options(tmp_path),
        "ja",
        RunLogger(tmp_path / "run.log", mirror_stdout=False),
    )

    assert client.calls == 2
    assert [line.translation_zh for line in result.bilingual_lines] == ["补译1", "补译2", "补译3"]
    assert all(line.translation_status == "translated" for line in result.bilingual_lines)


def test_analyze_options_local_timeout_defaults_to_six_minutes(tmp_path: Path) -> None:
    assert _options(tmp_path).local_timeout_seconds == 360.0


def test_failed_whole_chunk_still_exports_every_timeline_segment(tmp_path: Path) -> None:
    chunk = _chunk()
    result = _fallback_chunk_result(chunk)
    meta = AnalysisMeta(
        run_id="test",
        input_file="transcript.json",
        provider="gemini",
        model="test",
        profile="multilingual_study",
        source_language="ja",
        target_language="zh",
        chunk_minutes=2,
        max_segments_per_chunk=20,
        prompt_version="test",
        total_chunks=1,
        failed_chunks=1,
        fallback_chunks=1,
        fallback_lines=3,
    )
    output = tmp_path / "translation.srt"

    export_translation_srt(AnalysisDocument(meta=meta, chunks=[result]), output)

    text = output.read_text(encoding="utf-8")
    assert text.count("-->" ) == 3
    assert "[翻译暂缺] 原文1" in text
    assert "[翻译暂缺] 原文3" in text


def test_cached_partial_recovery_requests_only_missing_and_keeps_blocked_rows(tmp_path: Path) -> None:
    client = FakeClient([{"bilingual_lines": [_line(2, "补译2")]}])
    chunk = _chunk()
    cached = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=chunk.start,
        end=chunk.end,
        bilingual_lines=[
            BilingualLine(
                segment_id=1,
                start=0,
                end=1,
                original="原文1",
                translation_zh="已有译文1",
            ),
            BilingualLine(
                segment_id=2,
                start=1,
                end=2,
                original="原文2",
                translation_zh="[翻译暂缺] 原文2",
                brief_note=FALLBACK_TRANSLATION_NOTE,
                translation_status=TRANSLATION_STATUS_MISSING,
            ),
            BilingualLine(
                segment_id=3,
                start=2,
                end=3,
                original="原文3",
                translation_zh="[翻译暂缺] 原文3",
                brief_note=FALLBACK_TRANSLATION_NOTE,
                asr_suspect=True,
            ),
        ],
    )

    result = _process_chunk(
        client,
        chunk,
        _options(tmp_path),
        "ja",
        RunLogger(tmp_path / "run.log", mirror_stdout=False),
        initial_result=cached,
    )

    assert client.calls == 1
    assert "原文1" not in client.prompts[0]
    assert "原文2" in client.prompts[0]
    assert "原文3" not in client.prompts[0]
    assert [line.translation_zh for line in result.bilingual_lines] == [
        "已有译文1",
        "补译2",
        "[翻译暂缺] 原文3",
    ]
    assert result.bilingual_lines[1].translation_status == "translated"
    assert result.bilingual_lines[2].translation_status == TRANSLATION_STATUS_BLOCKED_ASR_REVIEW
    assert _missing_segments(result, chunk) == []
    assert _result_has_fallback(result) is True


def _cache_key_for_test(options: AnalyzeOptions, chunk: AnalysisChunk) -> str:
    return cache_key(
        transcript_file=options.input_file,
        chunk=chunk,
        model=_cache_model_name(options),
        profile=options.profile,
        prompt_version=PROMPT_VERSION,
        parameters={
            "provider": options.provider,
            "source_language": options.source_language,
            "target_language": options.target_language,
            "temperature": options.temperature,
            "character_profile": options.character_profile,
            "summary": options.summary,
            "study_notes": options.study_notes,
            "media_context": "",
            "reasoning_effort": options.local_reasoning_effort,
            "ignore_user_config": options.local_ignore_user_config,
        },
    )


def _resume_document() -> TranscriptDocument:
    return TranscriptDocument(
        meta=TranscriptMeta(created_at="2026-09-20", run_id="resume-test", language="ja"),
        segments=[
            TranscriptSegment(
                id=index,
                start=float(index - 1),
                end=float(index),
                start_text=f"00:00:0{index - 1}.000",
                end_text=f"00:00:0{index}.000",
                text=f"原文{index}",
            )
            for index in range(1, 4)
        ],
    )


def test_run_analysis_resume_uses_shared_partial_cache_for_coverage_only(tmp_path: Path, monkeypatch) -> None:
    options = _options(tmp_path)
    options.resume = True
    options.summary = False
    options.study_notes = False
    chunk = _chunk()
    cached = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=chunk.start,
        end=chunk.end,
        bilingual_lines=[
            BilingualLine(
                segment_id=1,
                start=0,
                end=1,
                original="原文1",
                translation_zh="已有译文1",
            ),
            BilingualLine(
                segment_id=2,
                start=1,
                end=2,
                original="原文2",
                translation_zh="[翻译暂缺] 原文2",
                brief_note=FALLBACK_TRANSLATION_NOTE,
                translation_status=TRANSLATION_STATUS_MISSING,
            ),
            BilingualLine(
                segment_id=3,
                start=2,
                end=3,
                original="原文3",
                translation_zh="[翻译暂缺] 原文3",
                brief_note=FALLBACK_TRANSLATION_NOTE,
                asr_suspect=True,
            ),
        ],
    )
    shared_dir = tmp_path / "outputs" / "cache" / "analysis"
    shared_path = chunk_result_path(shared_dir, chunk, _cache_key_for_test(options, chunk))
    save_chunk_result(shared_path, cached)
    client = FakeClient([{"bilingual_lines": [_line(2, "补译2")]}])

    monkeypatch.setattr(analyzer_module, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.setattr(analyzer_module, "load_transcript", lambda _path: _resume_document())
    monkeypatch.setattr(analyzer_module, "make_chunks", lambda *_args, **_kwargs: [chunk])
    monkeypatch.setattr(analyzer_module, "_make_client", lambda *_args: client)

    result = run_analysis(options)

    assert client.calls == 1
    assert "原文1" not in client.prompts[0]
    assert "原文2" in client.prompts[0]
    assert "原文3" not in client.prompts[0]
    assert result["document"].meta.fallback_lines == 1
    assert [line.translation_zh for line in result["document"].chunks[0].bilingual_lines] == [
        "已有译文1",
        "补译2",
        "[翻译暂缺] 原文3",
    ]
    assert "skip cache with blocked ASR review" not in (
        result["output_dir"] / "run.log"
    ).read_text(encoding="utf-8")


def test_run_analysis_resume_retries_misaligned_cached_row_before_canonicalize(
    tmp_path: Path,
    monkeypatch,
) -> None:
    options = _options(tmp_path)
    options.resume = True
    options.summary = False
    options.study_notes = False
    chunk = _chunk()
    cached = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=chunk.start,
        end=chunk.end,
        bilingual_lines=[
            BilingualLine(
                segment_id=1,
                start=99,
                end=100,
                original="旧原文1",
                translation_zh="旧译文1",
            ),
            BilingualLine(
                segment_id=2,
                start=1,
                end=2,
                original="原文2",
                translation_zh="已有译文2",
            ),
            BilingualLine(
                segment_id=3,
                start=2,
                end=3,
                original="原文3",
                translation_zh="已有译文3",
            ),
        ],
    )
    shared_dir = tmp_path / "outputs" / "cache" / "analysis"
    shared_path = chunk_result_path(shared_dir, chunk, _cache_key_for_test(options, chunk))
    save_chunk_result(shared_path, cached)
    client = FakeClient([{"bilingual_lines": [_line(1, "新译文1")]}])

    monkeypatch.setattr(analyzer_module, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.setattr(analyzer_module, "load_transcript", lambda _path: _resume_document())
    monkeypatch.setattr(analyzer_module, "make_chunks", lambda *_args, **_kwargs: [chunk])
    monkeypatch.setattr(analyzer_module, "_make_client", lambda *_args: client)

    result = run_analysis(options)

    assert client.calls == 1
    assert "原文1" in client.prompts[0]
    assert "原文2" not in client.prompts[0]
    assert result["document"].chunks[0].bilingual_lines[0].translation_zh == "新译文1"
    assert "validate cache before canonicalize" in (
        result["output_dir"] / "run.log"
    ).read_text(encoding="utf-8")


def test_run_analysis_resume_reprocesses_all_missing_legacy_cache(tmp_path: Path, monkeypatch) -> None:
    options = _options(tmp_path)
    options.resume = True
    options.summary = False
    options.study_notes = False
    chunk = _chunk()
    cached = _fallback_chunk_result(chunk)
    shared_dir = tmp_path / "outputs" / "cache" / "analysis"
    shared_path = chunk_result_path(shared_dir, chunk, _cache_key_for_test(options, chunk))
    save_chunk_result(shared_path, cached)
    client = FakeClient([{
        "bilingual_lines": [_line(index, f"全量译文{index}") for index in range(1, 4)]
    }])

    monkeypatch.setattr(analyzer_module, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.setattr(analyzer_module, "load_transcript", lambda _path: _resume_document())
    monkeypatch.setattr(analyzer_module, "make_chunks", lambda *_args, **_kwargs: [chunk])
    monkeypatch.setattr(analyzer_module, "_make_client", lambda *_args: client)

    result = run_analysis(options)

    assert client.calls == 1
    assert "原文1" in client.prompts[0]
    assert "原文2" in client.prompts[0]
    assert "原文3" in client.prompts[0]
    assert result["document"].meta.fallback_lines == 0


def test_recovery_exception_preserves_successful_cached_rows(tmp_path: Path, monkeypatch) -> None:
    options = _options(tmp_path)
    options.resume = True
    options.summary = False
    options.study_notes = False
    chunk = _chunk()
    cached = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=chunk.start,
        end=chunk.end,
        bilingual_lines=[
            BilingualLine(
                segment_id=1,
                start=0,
                end=1,
                original="原文1",
                translation_zh="已有译文1",
            ),
            BilingualLine(
                segment_id=2,
                start=1,
                end=2,
                original="原文2",
                translation_zh="[翻译暂缺] 原文2",
                brief_note=FALLBACK_TRANSLATION_NOTE,
            ),
            BilingualLine(
                segment_id=3,
                start=2,
                end=3,
                original="原文3",
                translation_zh="[翻译暂缺] 原文3",
                brief_note=FALLBACK_TRANSLATION_NOTE,
                asr_suspect=True,
            ),
        ],
    )
    shared_dir = tmp_path / "outputs" / "cache" / "analysis"
    shared_path = chunk_result_path(shared_dir, chunk, _cache_key_for_test(options, chunk))
    save_chunk_result(shared_path, cached)

    def fail_recovery(*_args, **_kwargs):
        raise RuntimeError("synthetic coverage recovery failure")

    monkeypatch.setattr(analyzer_module, "project_root", lambda: tmp_path)
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.setattr(analyzer_module, "load_transcript", lambda _path: _resume_document())
    monkeypatch.setattr(analyzer_module, "make_chunks", lambda *_args, **_kwargs: [chunk])
    monkeypatch.setattr(analyzer_module, "_make_client", lambda *_args: object())
    monkeypatch.setattr(analyzer_module, "_process_chunk", fail_recovery)

    result = run_analysis(options)

    lines = result["document"].chunks[0].bilingual_lines
    assert result["document"].meta.failed_chunks == 1
    assert lines[0].translation_zh == "已有译文1"
    assert lines[1].translation_status == TRANSLATION_STATUS_MISSING
    assert lines[2].translation_status == TRANSLATION_STATUS_BLOCKED_ASR_REVIEW


def test_coverage_supplement_keeps_review_only_risk_and_deduplicates() -> None:
    chunk = _chunk()
    base = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=chunk.start,
        end=chunk.end,
        bilingual_lines=[_line_model(1, "译文1")],
    )
    risk = {
        "segment_id": 2,
        "reason_zh": "数字不确定",
        "risk_type": "翻译",
    }
    supplement = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=chunk.start,
        end=chunk.end,
        bilingual_lines=[_line_model(2, "译文2")],
        review_items=[risk, risk],
    )

    merged = _merge_supplement(base, supplement, chunk)

    assert [line.segment_id for line in merged.bilingual_lines] == [1, 2]
    assert len(merged.review_items) == 1
    assert merged.review_items[0].reason_zh == "数字不确定"
    assert merged.bilingual_lines[1].review_required is True
    assert merged.bilingual_lines[1].review_reason == "数字不确定"


def _line_model(segment_id: int, translation: str) -> BilingualLine:
    return BilingualLine(
        segment_id=segment_id,
        start=99,
        end=100,
        original="错误原文",
        translation_zh=translation,
    )


def test_study_notes_are_structured_and_do_not_duplicate_translation(tmp_path: Path) -> None:
    chunk = _chunk()
    result = _fallback_chunk_result(chunk)
    result.bilingual_lines[0].translation_zh = "翻译1"
    result.bilingual_lines[1].translation_zh = "翻译2"
    result.bilingual_lines[2].translation_zh = "翻译3"
    meta = AnalysisMeta(
        run_id="test",
        input_file="transcript.json",
        provider="gemini",
        model="test",
        profile="multilingual_study",
        source_language="ja",
        target_language="zh",
        chunk_minutes=2,
        max_segments_per_chunk=20,
        prompt_version="test",
        total_chunks=1,
    )
    output = tmp_path / "study_notes.md"

    export_combined_study_markdown(AnalysisDocument(meta=meta, chunks=[result]), output)

    text = output.read_text(encoding="utf-8")
    assert text.startswith("# 学习资料")
    assert "完整逐段原文与中文翻译请参见" in text
    assert "## 生词" in text
    assert "## 语法" in text
    assert "## 人工复查清单" in text
    assert "原文1原文2原文3" not in text
    assert "翻译1翻译2翻译3" not in text
    assert "**原文**" not in text
    assert "**自然翻译**" not in text
