from __future__ import annotations

import math
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


TRANSLATION_STATUS_TRANSLATED = "translated"
TRANSLATION_STATUS_MISSING = "missing"
TRANSLATION_STATUS_BLOCKED_ASR_REVIEW = "blocked_asr_review"
TranslationStatus = Literal[
    TRANSLATION_STATUS_TRANSLATED,
    TRANSLATION_STATUS_MISSING,
    TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
]
_LEGACY_FALLBACK_PREFIX = "[翻译暂缺]"
_LEGACY_FALLBACK_NOTE = "模型未返回翻译，已使用原文占位"
INVALID_REPAIR_CONFIDENCE_MARKER = "无效修复置信度"


def normalize_repair_confidence(value: Any) -> tuple[float, bool]:
    """Return a finite repair confidence and whether the input was valid.

    Model output must remain parseable even when a provider emits ``NaN``, an
    infinity, or a value outside the documented range.  Invalid values become
    zero (no evidence), while callers can use the boolean to keep the repair
    pending and the reason field records the original value.
    """
    if isinstance(value, bool):
        return 0.0, False
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0, False
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        return 0.0, False
    return normalized, True


def invalid_repair_confidence_note(value: Any) -> str:
    try:
        display = repr(value)
    except Exception:
        display = "<unprintable>"
    if len(display) > 80:
        display = display[:77] + "..."
    return f"{INVALID_REPAIR_CONFIDENCE_MARKER}（原值：{display}）"


def repair_reason_with_confidence_note(reason: Any, value: Any) -> str:
    text = str(reason or "").strip()
    if INVALID_REPAIR_CONFIDENCE_MARKER in text:
        return text
    note = invalid_repair_confidence_note(value)
    return f"{note}；{text}" if text else note


def has_invalid_repair_confidence_note(reason: Any) -> bool:
    return INVALID_REPAIR_CONFIDENCE_MARKER in str(reason or "")


def _asr_suspect_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().casefold() in {"true", "1", "yes", "是"}


def is_missing_translation_payload(translation_zh: Any, brief_note: Any = "") -> bool:
    text = str(translation_zh or "")
    note = str(brief_note or "")
    return (
        not text.strip()
        or _has_legacy_fallback_marker(text, note)
    )


def _has_legacy_fallback_marker(translation_zh: Any, brief_note: Any = "") -> bool:
    text = str(translation_zh or "")
    note = str(brief_note or "")
    return (
        text.lstrip().startswith(_LEGACY_FALLBACK_PREFIX)
        or note.strip() == _LEGACY_FALLBACK_NOTE
    )


def _legacy_translation_status(data: Mapping[str, Any]) -> TranslationStatus:
    """Infer status only for input that predates the program-owned field.

    The presence of the field is the provenance boundary.  Explicit
    ``missing`` is retryable even when ASR is suspect; only old fallback rows
    with no status field are quarantined as blocked when ASR is suspect.
    """
    if "translation_status" in data:
        value = data.get("translation_status")
        if isinstance(value, str) and value in {
            TRANSLATION_STATUS_TRANSLATED,
            TRANSLATION_STATUS_MISSING,
            TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
        }:
            return value
        return TRANSLATION_STATUS_MISSING
    if not is_missing_translation_payload(data.get("translation_zh"), data.get("brief_note")):
        return TRANSLATION_STATUS_TRANSLATED
    if not _has_legacy_fallback_marker(data.get("translation_zh"), data.get("brief_note")):
        return TRANSLATION_STATUS_MISSING
    return (
        TRANSLATION_STATUS_BLOCKED_ASR_REVIEW
        if _asr_suspect_value(data.get("asr_suspect"))
        else TRANSLATION_STATUS_MISSING
    )


class _StringCoerceModel(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _none_to_empty_string(cls, value, info):
        annotations = cls.__annotations__
        field_annotation = annotations.get(info.field_name)
        if value is None and field_annotation in {str, "str"}:
            return ""
        return value


class AnalysisSegment(BaseModel):
    segment_id: int
    start: float
    end: float
    start_text: str
    end_text: str
    text: str


class AnalysisChunk(BaseModel):
    chunk_id: str
    index: int
    start: float
    end: float
    start_text: str
    end_text: str
    segments: list[AnalysisSegment] = Field(default_factory=list)


class BilingualLine(_StringCoerceModel):
    segment_id: int
    start: float = 0.0
    end: float = 0.0
    original: str = ""
    corrected_original: str = ""
    repair_confidence: float = 0.0
    repair_reason: str = ""
    auto_repaired: bool = False
    translation_zh: str
    literal_zh: str = ""
    brief_note: str = ""
    review_required: bool = False
    review_reason: str = ""
    asr_suspect: bool = False
    asr_issue: str = ""
    confidence: float = 0.0
    # This field is owned by the analysis program.  It is intentionally
    # removed from model output schemas and ignored on fresh model responses;
    # persisted caches use it to distinguish recoverable omissions from ASR
    # lines that must stay blocked pending human review.
    translation_status: TranslationStatus = TRANSLATION_STATUS_TRANSLATED

    @model_validator(mode="before")
    @classmethod
    def _normalize_program_owned_fields(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if "translation_status" not in normalized:
            normalized["translation_status"] = _legacy_translation_status(normalized)
        raw_confidence = normalized.get("repair_confidence", 0.0)
        confidence, valid = normalize_repair_confidence(raw_confidence)
        normalized["repair_confidence"] = confidence
        if not valid:
            normalized["repair_reason"] = repair_reason_with_confidence_note(
                normalized.get("repair_reason", ""),
                raw_confidence,
            )
        return normalized

    @field_validator("translation_status", mode="before")
    @classmethod
    def _normalize_translation_status(cls, value):
        if isinstance(value, str) and value in {
            TRANSLATION_STATUS_TRANSLATED,
            TRANSLATION_STATUS_MISSING,
            TRANSLATION_STATUS_BLOCKED_ASR_REVIEW,
        }:
            return value
        # A malformed persisted value is retryable.  In particular, do not
        # turn an invalid value into an implicit ASR quarantine.
        return TRANSLATION_STATUS_MISSING

    @field_validator("repair_confidence", mode="before")
    @classmethod
    def _normalize_repair_confidence(cls, value):
        return normalize_repair_confidence(value)[0]


class VocabularyItem(_StringCoerceModel):
    word: str
    reading: str = ""
    meaning_zh: str
    part_of_speech: str = ""
    example_original: str = ""
    example_zh: str = ""
    level: str = "intermediate"


class GrammarItem(_StringCoerceModel):
    pattern: str
    explanation_zh: str
    example_original: str = ""
    example_zh: str = ""
    importance: str = "medium"


class FixedExpressionItem(_StringCoerceModel):
    expression: str
    meaning_zh: str
    usage_note_zh: str = ""
    example_original: str = ""
    example_zh: str = ""


class ReviewItem(_StringCoerceModel):
    segment_id: Optional[int] = None
    start: Optional[float] = None
    end: Optional[float] = None
    original: str = ""
    corrected_original: str = ""
    repair_confidence: float = 0.0
    repair_reason: str = ""
    auto_repaired: bool = False
    reason_zh: str
    risk_type: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_repair_evidence(cls, data):
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        raw_confidence = normalized.get("repair_confidence", 0.0)
        confidence, valid = normalize_repair_confidence(raw_confidence)
        normalized["repair_confidence"] = confidence
        if not valid:
            normalized["repair_reason"] = repair_reason_with_confidence_note(
                normalized.get("repair_reason", ""),
                raw_confidence,
            )
        return normalized

    @field_validator("repair_confidence", mode="before")
    @classmethod
    def _normalize_repair_confidence(cls, value):
        return normalize_repair_confidence(value)[0]


class ProfileObservation(_StringCoerceModel):
    speaker_label: str = "主要说话人"
    category: str
    observation_zh: str
    evidence_zh: str
    confidence: float = 0.5


class ChunkAnalysisResult(BaseModel):
    chunk_id: str
    start: float
    end: float
    chunk_summary_zh: str = ""
    key_points_zh: list[str] = Field(default_factory=list)
    content_importance: float = 0.0
    bilingual_lines: list[BilingualLine] = Field(default_factory=list)
    vocabulary: list[VocabularyItem] = Field(default_factory=list)
    grammar: list[GrammarItem] = Field(default_factory=list)
    fixed_expressions: list[FixedExpressionItem] = Field(default_factory=list)
    tone_notes: list[str] = Field(default_factory=list)
    content_tags: list[str] = Field(default_factory=list)
    speaker_notes: list[str] = Field(default_factory=list)
    context_notes: list[str] = Field(default_factory=list)
    task_requirements: list[str] = Field(default_factory=list)
    vtuber_context: list[str] = Field(default_factory=list)
    profile_observations: list[ProfileObservation] = Field(default_factory=list)
    review_items: list[ReviewItem] = Field(default_factory=list)
    learning_value: float = 0.0


class AnalysisMeta(BaseModel):
    character_profile: bool = False
    summary: bool = True
    study_notes: bool = True
    run_id: str
    input_file: str
    transcript_run_id: str = ""
    provider: str
    model: str
    fallback_model: str = ""
    reasoning_effort: str = ""
    profile: str
    source_language: str
    target_language: str
    chunk_minutes: float
    max_segments_per_chunk: int
    prompt_version: str
    total_chunks: int
    succeeded_chunks: int = 0
    failed_chunks: int = 0
    skipped_chunks: int = 0
    fallback_chunks: int = 0
    fallback_lines: int = 0
    quality_status: str = "complete"
    review_items: int = 0
    review_segments: int = 0
    auto_repair_enabled: bool = True
    auto_repair_threshold: float = 0.80
    auto_repaired_lines: int = 0
    unresolved_review_items: int = 0


class FailedChunk(BaseModel):
    chunk_id: str
    error: str
    raw_response_file: str = ""


class VideoSummaryPoint(BaseModel):
    text: str = Field(min_length=1)
    source_chunk_ids: list[str] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary text must not be blank")
        return value.strip()


class VideoSummary(BaseModel):
    points: list[VideoSummaryPoint] = Field(min_length=1)


class AnalysisDocument(BaseModel):
    meta: AnalysisMeta
    chunks: list[ChunkAnalysisResult] = Field(default_factory=list)
    failed_chunks: list[FailedChunk] = Field(default_factory=list)
    video_summary: VideoSummary | None = None
    video_summary_error: str = ""
