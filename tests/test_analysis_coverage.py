from __future__ import annotations

import json
from pathlib import Path

from app.analysis.analyzer import (
    AnalyzeOptions,
    FALLBACK_TRANSLATION_NOTE,
    _chunk_coverage_issues,
    _fallback_chunk_result,
    _process_chunk,
)
from app.analysis.exporters import export_combined_study_markdown, export_translation_srt
from app.analysis.schemas import AnalysisChunk, AnalysisDocument, AnalysisMeta, AnalysisSegment
from app.utils import RunLogger


class FakeClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.calls = 0

    def generate_json_text(self, prompt: str, *, system_prompt: str) -> str:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return json.dumps(response, ensure_ascii=False)


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


def test_study_notes_interleave_original_and_translation(tmp_path: Path) -> None:
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
    assert text.index("原文：原文1") < text.index("中文：翻译1")
    assert text.index("中文：翻译1") < text.index("原文：原文2")
    assert text.index("原文：原文2") < text.index("中文：翻译2")
    assert text.index("中文：翻译2") < text.index("原文：原文3")
    assert "### 原文段落" not in text
    assert "### 自然中文" not in text
