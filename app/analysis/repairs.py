from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.utils import RunLogger

from .schemas import AnalysisDocument, BilingualLine, ReviewItem


AUTO_REPAIR_MODE = "high-confidence-source-repair-v1"


def effective_original(line: BilingualLine | dict[str, Any]) -> str:
    """Return the source text intended for repaired display artifacts."""
    if isinstance(line, dict):
        if line.get("auto_repaired") and str(line.get("corrected_original") or "").strip():
            return str(line["corrected_original"]).strip()
        return str(line.get("original") or "").strip()
    if line.auto_repaired and line.corrected_original.strip():
        return line.corrected_original.strip()
    return line.original.strip()


def _normalize_candidate(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def _select_candidate(line: BilingualLine, reviews: list[ReviewItem]) -> tuple[str, float, str] | None:
    candidates: list[tuple[str, float, str]] = []
    if line.corrected_original.strip():
        candidates.append((
            _normalize_candidate(line.corrected_original),
            float(line.repair_confidence or 0.0),
            line.repair_reason.strip(),
        ))
    for item in reviews:
        if item.corrected_original.strip():
            candidates.append((
                _normalize_candidate(item.corrected_original),
                float(item.repair_confidence or 0.0),
                item.repair_reason.strip() or item.reason_zh.strip(),
            ))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[1], len(item[0])), reverse=True)
    return candidates[0]


def _safe_to_apply(line: BilingualLine, candidate: str, confidence: float, threshold: float) -> bool:
    raw = line.original.strip()
    if not candidate or candidate == raw or confidence < threshold:
        return False
    if "\ufffd" in candidate:
        return False
    lowered = candidate.casefold()
    if any(marker in lowered for marker in ("无法辨认", "听不清", "翻译暂缺", "unknown")):
        return False
    if len(candidate) > 2000:
        return False
    # A source repair must be tied to a flagged ASR/ambiguity line. This
    # prevents a translation model from silently rewriting ordinary text.
    return bool(line.review_required or line.asr_suspect or "\ufffd" in raw)


def apply_high_confidence_repairs(
    document: AnalysisDocument,
    *,
    threshold: float,
    logger: RunLogger | None = None,
) -> list[dict[str, Any]]:
    """Apply only explicit, high-confidence source repairs.

    ``BilingualLine.original`` remains the raw ASR text. The repaired text is
    stored separately and only marked as effective when this conservative
    policy accepts it. Review items remain attached so every change is
    auditable.
    """
    threshold = max(0.0, min(1.0, float(threshold)))
    records: list[dict[str, Any]] = []
    updated_chunks = []

    for chunk in document.chunks:
        reviews_by_segment: dict[int, list[ReviewItem]] = {}
        for item in chunk.review_items:
            if item.segment_id is not None:
                reviews_by_segment.setdefault(item.segment_id, []).append(item)

        updated_lines: list[BilingualLine] = []
        for line in chunk.bilingual_lines:
            related_reviews = reviews_by_segment.get(line.segment_id, [])
            selected = _select_candidate(line, related_reviews)
            if selected is None:
                updated_lines.append(line)
                continue

            candidate, confidence, reason = selected
            reason = reason or "模型提供了源语言修复候选。"
            applied = _safe_to_apply(line, candidate, confidence, threshold)
            updated_line = line.model_copy(
                update={
                    "corrected_original": candidate,
                    "repair_confidence": confidence,
                    "repair_reason": reason,
                    "review_required": True,
                    "asr_suspect": True,
                    "auto_repaired": applied,
                }
            )
            updated_lines.append(updated_line)
            records.append({
                "chunk_id": chunk.chunk_id,
                "segment_id": line.segment_id,
                "start": line.start,
                "end": line.end,
                "original": line.original,
                "corrected_original": candidate,
                "repair_confidence": confidence,
                "repair_reason": reason,
                "status": "applied" if applied else "pending_manual_review",
            })
            if applied and logger:
                logger.write(
                    f"auto repair applied {chunk.chunk_id} segment={line.segment_id} "
                    f"confidence={confidence:.3f}"
                )

        line_by_segment = {line.segment_id: line for line in updated_lines}
        updated_reviews: list[ReviewItem] = []
        seen_review_segments: set[int] = set()
        for item in chunk.review_items:
            line = line_by_segment.get(item.segment_id) if item.segment_id is not None else None
            if line and line.corrected_original.strip():
                item = item.model_copy(
                    update={
                        "corrected_original": line.corrected_original,
                        "repair_confidence": line.repair_confidence,
                        "repair_reason": line.repair_reason,
                        "auto_repaired": line.auto_repaired,
                    }
                )
            if item.segment_id is not None:
                seen_review_segments.add(item.segment_id)
            updated_reviews.append(item)

        # If a model supplied a repair candidate on a bilingual line but
        # forgot to add a review item, create one so the change remains visible
        # in review.md and in the audit metadata.
        for line in updated_lines:
            if not line.corrected_original.strip() or line.segment_id in seen_review_segments:
                continue
            updated_reviews.append(ReviewItem(
                segment_id=line.segment_id,
                start=line.start,
                end=line.end,
                original=line.original,
                corrected_original=line.corrected_original,
                repair_confidence=line.repair_confidence,
                repair_reason=line.repair_reason,
                auto_repaired=line.auto_repaired,
                reason_zh=line.repair_reason,
                risk_type="ASR",
            ))

        updated_chunks.append(chunk.model_copy(update={
            "bilingual_lines": updated_lines,
            "review_items": updated_reviews,
        }))

    document.chunks = updated_chunks
    all_reviews = [item for chunk in document.chunks for item in chunk.review_items]
    applied_count = sum(1 for record in records if record["status"] == "applied")
    document.meta = document.meta.model_copy(update={
        "auto_repair_threshold": threshold,
        "auto_repaired_lines": applied_count,
        "unresolved_review_items": sum(1 for item in all_reviews if not item.auto_repaired),
    })
    if logger:
        logger.write(
            f"auto repair finished candidates={len(records)} applied={applied_count} "
            f"threshold={threshold:.3f}"
        )
    return records


def collect_repair_records(document: AnalysisDocument) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for chunk in document.chunks:
        for line in chunk.bilingual_lines:
            if not line.corrected_original.strip():
                continue
            records.append({
                "chunk_id": chunk.chunk_id,
                "segment_id": line.segment_id,
                "start": line.start,
                "end": line.end,
                "original": line.original,
                "corrected_original": line.corrected_original,
                "repair_confidence": line.repair_confidence,
                "repair_reason": line.repair_reason,
                "status": "applied" if line.auto_repaired else "pending_manual_review",
            })
    return records


def write_repair_log(document: AnalysisDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": AUTO_REPAIR_MODE,
        "enabled": document.meta.auto_repair_enabled,
        "threshold": document.meta.auto_repair_threshold,
        "auto_repaired_lines": document.meta.auto_repaired_lines,
        "unresolved_review_items": document.meta.unresolved_review_items,
        "records": collect_repair_records(document),
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
