from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


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
    start: float
    end: float
    original: str
    translation_zh: str
    literal_zh: str = ""
    brief_note: str = ""
    asr_suspect: bool = False
    asr_issue: str = ""
    confidence: float = 0.0


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
    reason_zh: str
    risk_type: str = ""


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


class FailedChunk(BaseModel):
    chunk_id: str
    error: str
    raw_response_file: str = ""


class AnalysisDocument(BaseModel):
    meta: AnalysisMeta
    chunks: list[ChunkAnalysisResult] = Field(default_factory=list)
    failed_chunks: list[FailedChunk] = Field(default_factory=list)
