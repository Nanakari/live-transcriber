from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class TranscriptWord(BaseModel):
    word: str
    start: float
    end: float
    probability: Optional[float] = None


class TranscriptSegment(BaseModel):
    id: int
    start: float
    end: float
    start_text: str
    end_text: str
    text: str
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    words: list[TranscriptWord] = Field(default_factory=list)


class TranscriptMeta(BaseModel):
    title: str = ""
    source_url: str = ""
    input_file: str = ""
    download_start: str = ""
    download_end: str = ""
    source_audio_file: str = ""
    clean_audio_file: str = ""
    language: str = "ja"
    detected_language: str = ""
    language_probability: Optional[float] = None
    quality: str = "high"
    model: str = "large-v3-turbo"
    device: str = "auto"
    compute_type: str = "int8_float16"
    beam_size: int = 5
    vad_filter: bool = True
    word_timestamps: bool = True
    condition_on_previous_text: bool = True
    initial_prompt_used: str = ""
    created_at: str
    run_id: str


class TranscriptDocument(BaseModel):
    meta: TranscriptMeta
    segments: list[TranscriptSegment] = Field(default_factory=list)
