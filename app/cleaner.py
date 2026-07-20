from __future__ import annotations

import re

from .schemas import TranscriptSegment
from .utils import format_timestamp


def clean_segments(segments: list[TranscriptSegment], no_speech_prob_threshold: float = 0.9) -> list[TranscriptSegment]:
    """Apply conservative cleanup without changing transcript meaning."""
    cleaned: list[TranscriptSegment] = []
    for segment in segments:
        text = re.sub(r"\s+", " ", segment.text.strip())
        if text == "":
            continue
        if segment.no_speech_prob is not None and segment.no_speech_prob > no_speech_prob_threshold and text.strip() == "":
            continue
        new_id = len(cleaned) + 1
        cleaned.append(
            segment.model_copy(
                update={
                    "id": new_id,
                    "text": text,
                    "start_text": format_timestamp(segment.start),
                    "end_text": format_timestamp(segment.end),
                }
            )
        )
    return cleaned
