from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.utils import RunLogger

from .schemas import (
    AnalysisDocument,
    BilingualLine,
    ReviewItem,
    has_invalid_repair_confidence_note,
    normalize_repair_confidence,
    repair_reason_with_confidence_note,
)


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


def collect_effective_review_items(
    document: AnalysisDocument,
) -> list[tuple[str, ReviewItem]]:
    """Return the single source of truth for review counts and reports.

    The model normally emits ``ReviewItem`` objects, but the normalized
    bilingual line also carries the authoritative ``review_required`` flag.
    Older or imperfect model responses can set that flag without emitting a
    matching item.  Synthesize a conservative item in that case so metadata,
    ``review.md`` and the combined study notes report the same set.
    """
    items: list[tuple[str, ReviewItem]] = []
    seen: set[tuple[object, str, str, str, str, float, str]] = set()
    for chunk in document.chunks:
        represented_segments = {
            item.segment_id
            for item in chunk.review_items
            if item.segment_id is not None
        }
        candidates = list(chunk.review_items)
        for line in chunk.bilingual_lines:
            if not line.review_required or line.segment_id in represented_segments:
                continue
            reason = (
                line.review_reason.strip()
                or line.asr_issue.strip()
                or "该片段已标记为需要人工复核。"
            )
            candidates.append(ReviewItem(
                segment_id=line.segment_id,
                start=line.start,
                end=line.end,
                original=line.original,
                corrected_original=line.corrected_original,
                repair_confidence=line.repair_confidence,
                repair_reason=line.repair_reason,
                auto_repaired=line.auto_repaired,
                reason_zh=reason,
                risk_type="ASR" if line.asr_suspect else "复核",
            ))

        for item in candidates:
            safe_confidence, safe_reason, _ = _safe_evidence_fields(
                item.repair_confidence,
                item.repair_reason,
            )
            if (
                safe_confidence != item.repair_confidence
                or safe_reason != item.repair_reason
            ):
                item = item.model_copy(update={
                    "repair_confidence": safe_confidence,
                    "repair_reason": safe_reason,
                })
            key = (
                item.segment_id,
                " ".join(item.original.split()),
                " ".join(item.reason_zh.split()),
                " ".join(item.risk_type.split()),
                " ".join(item.corrected_original.split()),
                round(safe_confidence, 6),
                " ".join(item.repair_reason.split()),
            )
            if key in seen:
                continue
            seen.add(key)
            items.append((chunk.chunk_id, item))
    return items


def _normalize_candidate(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


@dataclass(frozen=True)
class RepairCandidate:
    """One indivisible source-repair proposal and its evidence."""

    text: str
    confidence: float
    reason: str
    origin: str = ""
    confidence_valid: bool = True


@dataclass(frozen=True)
class RepairSelection:
    candidate: RepairCandidate | None
    candidates: tuple[RepairCandidate, ...] = ()
    conflicting: bool = False


def _safe_evidence_fields(confidence_value: Any, reason: Any) -> tuple[float, str, bool]:
    confidence, valid = normalize_repair_confidence(confidence_value)
    reason_text = str(reason or "").strip()
    if not valid:
        reason_text = repair_reason_with_confidence_note(reason_text, confidence_value)
    return (
        confidence,
        reason_text,
        valid and not has_invalid_repair_confidence_note(reason_text),
    )


def _candidate_evidence(line: BilingualLine, reviews: Iterable[ReviewItem]) -> list[RepairCandidate]:
    def candidate_from(text: str, confidence_value: Any, reason: Any, origin: str) -> RepairCandidate:
        confidence, reason_text, confidence_valid = _safe_evidence_fields(
            confidence_value,
            reason,
        )
        return RepairCandidate(
            text=_normalize_candidate(text),
            confidence=confidence,
            reason=reason_text,
            origin=origin,
            confidence_valid=confidence_valid,
        )

    candidates: list[RepairCandidate] = []
    if line.corrected_original.strip():
        candidates.append(candidate_from(
            line.corrected_original,
            line.repair_confidence,
            line.repair_reason,
            "bilingual_line",
        ))
    for item in reviews:
        if item.corrected_original.strip():
            candidates.append(candidate_from(
                item.corrected_original,
                item.repair_confidence,
                item.repair_reason.strip() or item.reason_zh.strip(),
                "review_item",
            ))
    # Multiple pieces of evidence for the same normalized text may be
    # consolidated, but the confidence and reason stay attached to the exact
    # candidate that supplied them.  In particular, do not borrow the score
    # from one candidate for another candidate's text.
    unique: list[RepairCandidate] = []
    positions: dict[str, int] = {}
    for candidate in candidates:
        if not candidate.text:
            continue
        previous_index = positions.get(candidate.text)
        if previous_index is None:
            positions[candidate.text] = len(unique)
            unique.append(candidate)
            continue
        previous = unique[previous_index]
        if (
            candidate.confidence > previous.confidence
            or (
                candidate.confidence == previous.confidence
                and candidate.confidence_valid
                and not previous.confidence_valid
            )
        ):
            unique[previous_index] = candidate
    return unique


def select_repair_candidate(
    line: BilingualLine,
    reviews: Iterable[ReviewItem],
    *,
    threshold: float | None = None,
) -> RepairSelection:
    candidates = tuple(_candidate_evidence(line, reviews))
    if not candidates:
        return RepairSelection(candidate=None)
    # Confidence is the only ranking signal.  max() is stable for equal
    # values, so an equal-confidence conflict remains an explicit conflict;
    # string length is never used as a tie breaker.
    selected = max(enumerate(candidates), key=lambda item: (item[1].confidence, -item[0]))[1]
    if threshold is None:
        viable = candidates
    else:
        viable = tuple(
            candidate
            for candidate in candidates
            if candidate.confidence_valid
            and candidate.confidence > 0.0
            and candidate.confidence >= threshold
        )
    conflicting = len({candidate.text for candidate in viable}) > 1
    return RepairSelection(
        candidate=selected,
        candidates=candidates,
        conflicting=conflicting,
    )


def _select_candidate(line: BilingualLine, reviews: list[ReviewItem]) -> RepairCandidate | None:
    """Compatibility helper for callers that only need the best proposal."""
    return select_repair_candidate(line, reviews).candidate


def _safe_to_apply(
    line: BilingualLine,
    candidate: str,
    confidence: float,
    threshold: float,
    *,
    conflicting: bool = False,
    confidence_valid: bool = True,
) -> bool:
    raw = line.original.strip()
    normalized_confidence, normalized = normalize_repair_confidence(confidence)
    if (
        conflicting
        or not candidate
        or candidate == raw
        or not confidence_valid
        or not normalized
        or normalized_confidence <= 0.0
        or normalized_confidence < threshold
    ):
        return False
    if "\ufffd" in candidate:
        return False
    lowered = candidate.casefold()
    if any(marker in lowered for marker in ("无法辨认", "听不清", "翻译暂缺", "unknown")):
        return False
    if len(candidate) > 2000:
        return False
    # A source repair must be tied to an ASR concern or visibly corrupt source
    # text.  A translation/number-risk review alone must never authorize an
    # automatic rewrite of the source transcript.
    return bool(line.asr_suspect or "\ufffd" in raw)


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
    threshold, threshold_valid = normalize_repair_confidence(threshold)
    if not threshold_valid:
        threshold = 0.80
    records: list[dict[str, Any]] = []
    updated_chunks = []

    for chunk in document.chunks:
        reviews_by_segment: dict[int, list[ReviewItem]] = {}
        for item in chunk.review_items:
            if item.segment_id is not None:
                reviews_by_segment.setdefault(item.segment_id, []).append(item)

        updated_lines: list[BilingualLine] = []
        selections_by_segment: dict[int, RepairSelection] = {}
        for line in chunk.bilingual_lines:
            related_reviews = reviews_by_segment.get(line.segment_id, [])
            selection = select_repair_candidate(
                line,
                related_reviews,
                threshold=threshold,
            )
            selected = selection.candidate
            selections_by_segment[line.segment_id] = selection
            if selected is None:
                # ``auto_repaired`` is a program decision.  Ignore a stale or
                # model-supplied true value even when no candidate is present.
                safe_confidence, safe_reason, _ = _safe_evidence_fields(
                    line.repair_confidence,
                    line.repair_reason,
                )
                updated_lines.append(line.model_copy(update={
                    "auto_repaired": False,
                    "repair_confidence": safe_confidence,
                    "repair_reason": safe_reason,
                }))
                continue

            candidate = selected.text
            confidence = selected.confidence
            reason = selected.reason or "模型提供了源语言修复候选。"
            applied = _safe_to_apply(
                line,
                candidate,
                confidence,
                threshold,
                conflicting=selection.conflicting,
                confidence_valid=selected.confidence_valid,
            )
            raw = line.original.strip()
            updated_line = line.model_copy(
                update={
                    "corrected_original": candidate,
                    "repair_confidence": confidence,
                    "repair_reason": reason,
                    "review_required": True,
                    "asr_suspect": line.asr_suspect or "\ufffd" in raw,
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
                "candidate_evidence": [
                    {
                        "corrected_original": item.text,
                        "repair_confidence": item.confidence,
                        "repair_reason": item.reason,
                        "origin": item.origin,
                    }
                    for item in selection.candidates
                ],
            })
            if applied and logger:
                logger.write(
                    f"auto repair applied {chunk.chunk_id} segment={line.segment_id} "
                    f"confidence={confidence:.3f}"
                )
            elif selection.conflicting and logger:
                logger.write(
                    f"auto repair pending conflicting candidates {chunk.chunk_id} "
                    f"segment={line.segment_id} candidates={len(selection.candidates)}"
                )

        line_by_segment = {line.segment_id: line for line in updated_lines}
        updated_reviews: list[ReviewItem] = []
        seen_review_candidates: set[tuple[int, str]] = set()
        for item in chunk.review_items:
            line = line_by_segment.get(item.segment_id) if item.segment_id is not None else None
            selection = (
                select_repair_candidate(
                    line,
                    reviews_by_segment.get(item.segment_id, []),
                    threshold=threshold,
                )
                if line is not None
                else RepairSelection(candidate=None)
            )
            candidate_key = _normalize_candidate(item.corrected_original)
            auto_repaired = False
            if line is not None and candidate_key:
                if (
                    selection.candidate is not None
                    and candidate_key == selection.candidate.text
                ):
                    auto_repaired = bool(line.auto_repaired and not selection.conflicting)
                seen_review_candidates.add((item.segment_id, candidate_key))
            # Preserve each review's own candidate/confidence/reason tuple;
            # only the program-owned auto flag is rewritten.
            item_confidence, item_reason, _ = _safe_evidence_fields(
                item.repair_confidence,
                item.repair_reason,
            )
            item = item.model_copy(update={
                "auto_repaired": auto_repaired,
                "repair_confidence": item_confidence,
                "repair_reason": item_reason,
            })
            updated_reviews.append(item)

        # If a model supplied a repair candidate on a bilingual line but did
        # not add an item for that exact candidate, create one so every piece
        # of evidence remains visible in review.md and the audit metadata.
        for line in updated_lines:
            selection = selections_by_segment.get(line.segment_id)
            evidence = selection.candidates if selection is not None else ()
            if not evidence and line.corrected_original.strip():
                evidence = (
                    RepairCandidate(
                        text=_normalize_candidate(line.corrected_original),
                        confidence=line.repair_confidence,
                        reason=line.repair_reason,
                        origin="bilingual_line",
                    ),
                )
            for candidate in evidence:
                candidate_key = _normalize_candidate(candidate.text)
                if not candidate_key or (line.segment_id, candidate_key) in seen_review_candidates:
                    continue
                updated_reviews.append(ReviewItem(
                    segment_id=line.segment_id,
                    start=line.start,
                    end=line.end,
                    original=line.original,
                    corrected_original=candidate.text,
                    repair_confidence=candidate.confidence,
                    repair_reason=candidate.reason,
                    auto_repaired=bool(
                        line.auto_repaired
                        and selection is not None
                        and selection.candidate is not None
                        and selection.candidate.text == candidate.text
                        and not selection.conflicting
                    ),
                    reason_zh=candidate.reason or line.review_reason,
                    risk_type="ASR" if line.asr_suspect else "复核",
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
            candidate_evidence: list[dict[str, Any]] = []
            seen_candidates: set[tuple[str, float, str]] = set()

            def add_candidate(text: str, confidence: float, reason: str, origin: str) -> None:
                normalized = _normalize_candidate(text)
                if not normalized:
                    return
                safe_confidence, safe_reason, _ = _safe_evidence_fields(
                    confidence,
                    reason,
                )
                evidence = (normalized, round(safe_confidence, 6), safe_reason)
                if evidence in seen_candidates:
                    return
                seen_candidates.add(evidence)
                candidate_evidence.append({
                    "corrected_original": normalized,
                    "repair_confidence": safe_confidence,
                    "repair_reason": safe_reason,
                    "origin": origin,
                })

            line_confidence, line_reason, _ = _safe_evidence_fields(
                line.repair_confidence,
                line.repair_reason,
            )
            add_candidate(
                line.corrected_original,
                line_confidence,
                line_reason,
                "bilingual_line",
            )
            for item in chunk.review_items:
                if item.segment_id == line.segment_id:
                    add_candidate(
                        item.corrected_original,
                        item.repair_confidence,
                        item.repair_reason or item.reason_zh,
                        "review_item",
                    )
            records.append({
                "chunk_id": chunk.chunk_id,
                "segment_id": line.segment_id,
                "start": line.start,
                "end": line.end,
                "original": line.original,
                "corrected_original": line.corrected_original,
                "repair_confidence": line_confidence,
                "repair_reason": line_reason,
                "status": "applied" if line.auto_repaired else "pending_manual_review",
                "candidate_evidence": candidate_evidence,
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
