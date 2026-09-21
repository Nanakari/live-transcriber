from __future__ import annotations

import json

import pytest

from app.analysis.analyzer import _canonicalize_result, _update_quality_metadata
from app.analysis.exporters import export_review_markdown
from app.analysis.repairs import (
    apply_high_confidence_repairs,
    effective_original,
    write_repair_log,
)
from app.analysis.schemas import (
    AnalysisDocument,
    AnalysisChunk,
    AnalysisSegment,
    AnalysisMeta,
    BilingualLine,
    ChunkAnalysisResult,
    ReviewItem,
)


def _document(*, confidence: float) -> AnalysisDocument:
    meta = AnalysisMeta(
        run_id="run",
        input_file="transcript.json",
        provider="local",
        model="test",
        profile="multilingual_study",
        source_language="ja",
        target_language="zh",
        chunk_minutes=3,
        max_segments_per_chunk=36,
        prompt_version="test",
        total_chunks=1,
    )
    line = BilingualLine(
        segment_id=1,
        start=0,
        end=1,
        original="�れさま",
        corrected_original="おつかれさま",
        repair_confidence=confidence,
        repair_reason="固定表达与上下文均一致",
        translation_zh="辛苦了",
        review_required=True,
        asr_suspect=True,
    )
    review = ReviewItem(
        segment_id=1,
        start=0,
        end=1,
        original="�れさま",
        corrected_original="おつかれさま",
        repair_confidence=confidence,
        repair_reason="固定表达与上下文均一致",
        reason_zh="ASR 字符异常",
        risk_type="ASR",
    )
    chunk = ChunkAnalysisResult(
        chunk_id="chunk_0000",
        start=0,
        end=1,
        bilingual_lines=[line],
        review_items=[review],
    )
    return AnalysisDocument(meta=meta, chunks=[chunk])


def test_high_confidence_repair_keeps_raw_text_and_updates_effective_text(tmp_path):
    document = _document(confidence=0.96)

    records = apply_high_confidence_repairs(document, threshold=0.90)
    line = document.chunks[0].bilingual_lines[0]

    assert records[0]["status"] == "applied"
    assert line.original == "�れさま"
    assert line.corrected_original == "おつかれさま"
    assert line.auto_repaired is True
    assert effective_original(line) == "おつかれさま"
    assert document.meta.auto_repaired_lines == 1

    log_path = tmp_path / "repair_log.json"
    write_repair_log(document, log_path)
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert payload["records"][0]["original"] == "�れさま"
    assert payload["records"][0]["corrected_original"] == "おつかれさま"


def test_low_confidence_repair_stays_pending_and_displays_raw_text():
    document = _document(confidence=0.89)

    records = apply_high_confidence_repairs(document, threshold=0.90)
    line = document.chunks[0].bilingual_lines[0]

    assert records[0]["status"] == "pending_manual_review"
    assert line.auto_repaired is False
    assert effective_original(line) == "�れさま"
    assert document.meta.auto_repaired_lines == 0


@pytest.mark.parametrize("invalid_confidence", [float("nan"), float("inf"), -0.1, 1.2])
def test_invalid_repair_confidence_stays_pending_even_at_zero_threshold(tmp_path, invalid_confidence):
    document = _document(confidence=invalid_confidence)

    records = apply_high_confidence_repairs(document, threshold=0.0)
    line = document.chunks[0].bilingual_lines[0]
    review = document.chunks[0].review_items[0]

    assert records[0]["status"] == "pending_manual_review"
    assert line.auto_repaired is False
    assert line.repair_confidence == 0.0
    assert "无效修复置信度" in line.repair_reason
    assert review.repair_confidence == 0.0
    assert "无效修复置信度" in review.repair_reason

    log_path = tmp_path / "repair_log.json"
    write_repair_log(document, log_path)
    text = log_path.read_text(encoding="utf-8")
    assert "NaN" not in text
    assert "Infinity" not in text
    payload = json.loads(text)
    assert payload["records"][0]["repair_confidence"] == 0.0
    assert payload["records"][0]["candidate_evidence"][0]["repair_confidence"] == 0.0


@pytest.mark.parametrize(
    ("line_confidence", "review_confidence"),
    [(0.90, 0.90), (0.91, 0.95)],
)
def test_conflicting_viable_candidates_stay_pending_without_length_tie_breaker(
    tmp_path,
    line_confidence,
    review_confidence,
):
    meta = AnalysisMeta(
        run_id="run",
        input_file="transcript.json",
        provider="local",
        model="test",
        profile="multilingual_study",
        source_language="ja",
        target_language="zh",
        chunk_minutes=3,
        max_segments_per_chunk=36,
        prompt_version="test",
        total_chunks=1,
    )
    chunk = AnalysisChunk(
        chunk_id="chunk_0000",
        index=0,
        start=0,
        end=1,
        start_text="00:00:00.000",
        end_text="00:00:01.000",
        segments=[AnalysisSegment(
            segment_id=1,
            start=0,
            end=1,
            start_text="00:00:00.000",
            end_text="00:00:01.000",
            text="�原文",
        )],
    )
    result = ChunkAnalysisResult(
        chunk_id=chunk.chunk_id,
        start=0,
        end=1,
        bilingual_lines=[BilingualLine(
            segment_id=1,
            original="�原文",
            translation_zh="译文",
            corrected_original="这是一个明显更长的候选",
            repair_confidence=line_confidence,
            repair_reason="line candidate",
            review_required=True,
            asr_suspect=True,
            auto_repaired=True,
        )],
        review_items=[ReviewItem(
            segment_id=1,
            original="�原文",
            corrected_original="短候选",
            repair_confidence=review_confidence,
            repair_reason="review candidate",
            reason_zh="ASR 候选冲突",
            risk_type="ASR",
            auto_repaired=True,
        )],
    )
    # Canonicalization may run more than once while merging/recovering caches.
    result = _canonicalize_result(_canonicalize_result(result, chunk), chunk)
    document = AnalysisDocument(meta=meta, chunks=[result])

    records = apply_high_confidence_repairs(document, threshold=0.90)

    assert records[0]["status"] == "pending_manual_review"
    assert document.chunks[0].bilingual_lines[0].auto_repaired is False
    assert {
        item.corrected_original for item in document.chunks[0].review_items
    } == {"这是一个明显更长的候选", "短候选"}
    assert all(item.auto_repaired is False for item in document.chunks[0].review_items)

    _update_quality_metadata(document)
    review_path = tmp_path / "review.md"
    export_review_markdown(document, review_path)
    review_text = review_path.read_text(encoding="utf-8")
    assert "这是一个明显更长的候选" in review_text
    assert "短候选" in review_text
    repair_path = tmp_path / "repair_log.json"
    write_repair_log(document, repair_path)
    payload = json.loads(repair_path.read_text(encoding="utf-8"))
    assert {
        item["corrected_original"] for item in payload["records"][0]["candidate_evidence"]
    } == {"这是一个明显更长的候选", "短候选"}


def test_model_auto_repaired_flag_is_ignored_for_lines_without_accepted_candidate():
    document = _document(confidence=0.89)
    document.chunks[0].bilingual_lines[0].auto_repaired = True

    apply_high_confidence_repairs(document, threshold=0.90)

    assert document.chunks[0].bilingual_lines[0].auto_repaired is False


def test_candidate_confidence_is_not_borrowed_from_another_candidate():
    document = _document(confidence=0.10)
    document.chunks[0].review_items.append(
        document.chunks[0].review_items[0].model_copy(update={
            "corrected_original": "正确的短候选",
            "repair_confidence": 0.95,
            "repair_reason": "上下文一致",
        })
    )

    records = apply_high_confidence_repairs(document, threshold=0.90)
    line = document.chunks[0].bilingual_lines[0]

    assert records[0]["corrected_original"] == "正确的短候选"
    assert records[0]["repair_confidence"] == 0.95
    assert line.corrected_original == "正确的短候选"
    assert line.auto_repaired is True
    assert all(
        evidence["corrected_original"] != "おつかれさま"
        or evidence["repair_confidence"] == 0.10
        for evidence in records[0]["candidate_evidence"]
    )


def test_translation_risk_does_not_authorize_source_repair():
    document = _document(confidence=0.99)
    line = document.chunks[0].bilingual_lines[0].model_copy(
        update={"original": "原文", "asr_suspect": False, "review_reason": "数字不确定"}
    )
    review = document.chunks[0].review_items[0].model_copy(
        update={"reason_zh": "数字不确定", "risk_type": "翻译"}
    )
    document.chunks[0].bilingual_lines = [line]
    document.chunks[0].review_items = [review]

    records = apply_high_confidence_repairs(document, threshold=0.90)

    assert records[0]["status"] == "pending_manual_review"
    assert document.chunks[0].bilingual_lines[0].auto_repaired is False


def test_review_flag_without_item_is_counted_in_metadata_and_report(tmp_path):
    meta = AnalysisMeta(
        run_id="run",
        input_file="transcript.json",
        provider="local",
        model="test",
        profile="multilingual_study",
        source_language="ja",
        target_language="zh",
        chunk_minutes=3,
        max_segments_per_chunk=36,
        prompt_version="test",
        total_chunks=1,
    )
    line = BilingualLine(
        segment_id=7,
        start=1,
        end=2,
        original="聞き取りにくい原文",
        translation_zh="需要复核的译文",
        review_required=True,
        review_reason="ASR 片段需要确认",
        asr_suspect=True,
    )
    chunk = ChunkAnalysisResult(
        chunk_id="chunk_0000",
        start=1,
        end=2,
        bilingual_lines=[line],
        review_items=[],
    )
    document = AnalysisDocument(meta=meta, chunks=[chunk])

    _update_quality_metadata(document)
    output = tmp_path / "review.md"
    export_review_markdown(document, output)

    assert document.meta.review_items == 1
    assert document.meta.review_segments == 1
    assert document.meta.unresolved_review_items == 1
    assert output.read_text(encoding="utf-8").count("## chunk_0000") == 1
