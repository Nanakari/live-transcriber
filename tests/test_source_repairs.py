from __future__ import annotations

import json

from app.analysis.repairs import apply_high_confidence_repairs, effective_original, write_repair_log
from app.analysis.schemas import (
    AnalysisDocument,
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
