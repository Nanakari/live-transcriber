from __future__ import annotations

import re
import statistics

from .schemas import TranscriptSegment
from .utils import format_timestamp


_SUSPICIOUS_SEGMENT_MIN_SECONDS = 10.0
_SUSPICIOUS_WORD_MIN_SECONDS = 6.0
_SUSPICIOUS_SECONDS_PER_CHARACTER = 1.2
_REPAIRED_TAIL_PADDING_SECONDS = 0.5
_MAX_SUSPICIOUS_TAIL_WORDS = 4
_LOW_CONFIDENCE_LEADING_WORD_MAX_PROBABILITY = 0.1
_SUSPICIOUS_LEADING_GAP_MIN_SECONDS = 3.0
_MAX_LEADING_ANCHOR_SPAN_SECONDS = 1.5
_MAX_LOW_CONFIDENCE_LEADING_WORDS = 3
_REPAIRED_START_PADDING_SECONDS = 0.2
_FRAGMENT_WINDOW_SECONDS = 60.0
_FRAGMENT_WINDOW_STEP_SECONDS = 30.0
_FRAGMENT_WINDOW_MIN_SEGMENTS = 36
_FRAGMENT_MAX_MEDIAN_DURATION_SECONDS = 0.75
_FRAGMENT_MAX_WORDS = 2
_FRAGMENT_MIN_SHORT_SEGMENT_RATIO = 0.8
_FRAGMENT_MIN_WORD_TIMESTAMP_RATIO = 0.9
_MERGE_MAX_GAP_SECONDS = 0.8
_MERGE_MAX_DURATION_SECONDS = 10.0
_MERGE_MAX_WORDS = 24
_MERGE_MAX_CHARACTERS = 120
_STRONG_TERMINAL_PUNCTUATION = (".", "!", "?", "。", "！", "？")
_NO_SPACE_BEFORE = set(",.!?;:%)]}\u3001\u3002\uff0c\uff01\uff1f\uff1b\uff1a\uff09\u3011\u3009\u300b")
_NO_SPACE_AFTER = set("([{\uff08\u3010\u3008\u300a")


def clean_segments(segments: list[TranscriptSegment], no_speech_prob_threshold: float = 0.9) -> list[TranscriptSegment]:
    """Apply conservative cleanup without changing transcript meaning.

    Whisper can occasionally assign a very long timestamp to the final few
    tokens of a short segment.  Only extreme, tail-only cases are repaired;
    ordinary long speech segments and segments without word timestamps are
    left unchanged.
    """
    cleaned: list[TranscriptSegment] = []
    for segment in segments:
        text = re.sub(r"\s+", " ", segment.text.strip())
        if text == "":
            continue
        if segment.no_speech_prob is not None and segment.no_speech_prob > no_speech_prob_threshold and text.strip() == "":
            continue
        new_id = len(cleaned) + 1
        cleaned_segment = segment.model_copy(
            update={
                "id": new_id,
                "text": text,
                "start_text": format_timestamp(segment.start),
                "end_text": format_timestamp(segment.end),
            }
        )
        cleaned_segment = _repair_low_confidence_leading_timestamp(cleaned_segment)
        cleaned.append(_repair_extreme_tail_timestamp(cleaned_segment))
    return _merge_fragmented_segments(cleaned)


def _merge_fragmented_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Merge only dense runs of tiny Whisper segments using word timestamps.

    Isolated short replies are intentionally untouched.  A segment becomes
    eligible only when it belongs to a sliding one-minute window dominated by
    one- or two-word timestamped segments.  Even inside such a window, strong
    punctuation, pauses, invalid timestamps, and subtitle size limits remain
    hard boundaries.
    """
    fragmented_indexes = _fragmented_segment_indexes(segments)
    if not fragmented_indexes:
        return segments

    merged: list[TranscriptSegment] = []
    current: TranscriptSegment | None = None
    current_is_fragmented = False

    for index, segment in enumerate(segments):
        is_fragmented = index in fragmented_indexes
        if current is None:
            current = segment
            current_is_fragmented = is_fragmented
            continue

        if (
            current_is_fragmented
            and is_fragmented
            and _can_merge_adjacent_segments(current, segment)
        ):
            current = _merge_adjacent_segments(current, segment)
            continue

        merged.append(current)
        current = segment
        current_is_fragmented = is_fragmented

    if current is not None:
        merged.append(current)

    return [
        segment.model_copy(update={"id": index})
        for index, segment in enumerate(merged, start=1)
    ]


def _fragmented_segment_indexes(segments: list[TranscriptSegment]) -> set[int]:
    if len(segments) < _FRAGMENT_WINDOW_MIN_SEGMENTS:
        return set()

    starts = [segment.start for segment in segments]
    first_window = (starts[0] // _FRAGMENT_WINDOW_STEP_SECONDS) * _FRAGMENT_WINDOW_STEP_SECONDS
    last_start = starts[-1]
    fragmented: set[int] = set()
    left = 0
    right = 0
    window_start = first_window

    while window_start <= last_start:
        window_end = window_start + _FRAGMENT_WINDOW_SECONDS
        while left < len(segments) and starts[left] < window_start:
            left += 1
        if right < left:
            right = left
        while right < len(segments) and starts[right] < window_end:
            right += 1

        indexes = list(range(left, right))
        if _is_fragmented_window(segments, indexes):
            fragmented.update(indexes)
        window_start += _FRAGMENT_WINDOW_STEP_SECONDS

    return fragmented


def _is_fragmented_window(segments: list[TranscriptSegment], indexes: list[int]) -> bool:
    if len(indexes) < _FRAGMENT_WINDOW_MIN_SEGMENTS:
        return False

    window_segments = [segments[index] for index in indexes]
    timestamped = [segment for segment in window_segments if _valid_words(segment)]
    if len(timestamped) / len(window_segments) < _FRAGMENT_MIN_WORD_TIMESTAMP_RATIO:
        return False

    durations = [max(0.0, segment.end - segment.start) for segment in timestamped]
    short_count = sum(len(segment.words) <= _FRAGMENT_MAX_WORDS for segment in timestamped)
    return (
        statistics.median(durations) <= _FRAGMENT_MAX_MEDIAN_DURATION_SECONDS
        and short_count / len(timestamped) >= _FRAGMENT_MIN_SHORT_SEGMENT_RATIO
    )


def _valid_words(segment: TranscriptSegment) -> bool:
    return bool(segment.words) and all(
        word.end >= word.start >= 0.0 for word in segment.words
    )


def _can_merge_adjacent_segments(left: TranscriptSegment, right: TranscriptSegment) -> bool:
    if not _valid_words(left) or not _valid_words(right):
        return False
    if not _has_safe_internal_word_gaps(left) or not _has_safe_internal_word_gaps(right):
        return False
    if left.text.rstrip().endswith(_STRONG_TERMINAL_PUNCTUATION):
        return False

    last_word = left.words[-1]
    first_word = right.words[0]
    gap = first_word.start - last_word.end
    if gap < 0.0 or gap > _MERGE_MAX_GAP_SECONDS:
        return False

    merged_duration = right.words[-1].end - left.words[0].start
    if merged_duration > _MERGE_MAX_DURATION_SECONDS:
        return False
    if len(left.words) + len(right.words) > _MERGE_MAX_WORDS:
        return False

    merged_text = _join_segment_text(left.text, right.text)
    return len(re.sub(r"\s+", "", merged_text)) <= _MERGE_MAX_CHARACTERS


def _has_safe_internal_word_gaps(segment: TranscriptSegment) -> bool:
    return all(
        0.0 <= following.start - previous.end <= _MERGE_MAX_GAP_SECONDS
        for previous, following in zip(segment.words, segment.words[1:])
    )


def _merge_adjacent_segments(left: TranscriptSegment, right: TranscriptSegment) -> TranscriptSegment:
    left_weight = max(1, len(left.words))
    right_weight = max(1, len(right.words))
    avg_logprob = _weighted_optional_average(
        left.avg_logprob,
        left_weight,
        right.avg_logprob,
        right_weight,
    )
    no_speech_prob = _max_optional(left.no_speech_prob, right.no_speech_prob)
    return left.model_copy(
        update={
            "end": right.end,
            "end_text": format_timestamp(right.end),
            "text": _join_segment_text(left.text, right.text),
            "avg_logprob": avg_logprob,
            "no_speech_prob": no_speech_prob,
            "words": [*left.words, *right.words],
        }
    )


def _join_segment_text(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return right
    if not right:
        return left
    if (
        right[0] in _NO_SPACE_BEFORE
        or left[-1] in _NO_SPACE_AFTER
        or _is_cjk_character(left[-1])
        or _is_cjk_character(right[0])
    ):
        return left + right
    return f"{left} {right}"


def _is_cjk_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3040 <= codepoint <= 0x30FF
        or 0x3400 <= codepoint <= 0x9FFF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def _weighted_optional_average(
    left: float | None,
    left_weight: int,
    right: float | None,
    right_weight: int,
) -> float | None:
    if left is None:
        return right
    if right is None:
        return left
    return (left * left_weight + right * right_weight) / (left_weight + right_weight)


def _max_optional(left: float | None, right: float | None) -> float | None:
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _repair_low_confidence_leading_timestamp(segment: TranscriptSegment) -> TranscriptSegment:
    """Trim an obviously false word-timestamp anchor at the segment start.

    Music and other non-speech audio can occasionally produce one or more
    extremely low-confidence leading tokens followed by a multi-second gap.
    Only a short, low-confidence prefix at the very start is eligible.  Normal
    high-confidence pauses and gaps later in a sentence are left unchanged.
    The raw words and text stay intact; only the subtitle boundary is moved.
    """
    if len(segment.words) < 2:
        return segment

    max_prefix_words = min(_MAX_LOW_CONFIDENCE_LEADING_WORDS, len(segment.words) - 1)
    for prefix_count in range(1, max_prefix_words + 1):
        leading_words = segment.words[:prefix_count]
        probabilities = [word.probability for word in leading_words]
        if any(
            probability is None or probability > _LOW_CONFIDENCE_LEADING_WORD_MAX_PROBABILITY
            for probability in probabilities
        ):
            break

        last_leading_word = leading_words[-1]
        next_word = segment.words[prefix_count]
        leading_span = last_leading_word.end - segment.start
        gap = next_word.start - last_leading_word.end
        if (
            leading_span > _MAX_LEADING_ANCHOR_SPAN_SECONDS
            or gap < _SUSPICIOUS_LEADING_GAP_MIN_SECONDS
        ):
            continue

        repaired_start = max(segment.start, next_word.start - _REPAIRED_START_PADDING_SECONDS)
        if repaired_start >= segment.end:
            return segment
        return segment.model_copy(
            update={
                "start": repaired_start,
                "start_text": format_timestamp(repaired_start),
            }
        )

    return segment


def _repair_extreme_tail_timestamp(segment: TranscriptSegment) -> TranscriptSegment:
    """Trim only an obviously stretched final word-timestamp tail.

    The threshold is intentionally high.  The repair requires all of the
    following: word timestamps, a long segment, an unusually long word, a
    high seconds-per-character ratio, and the anomaly being in the final few
    words.  The text and raw word evidence are preserved; only the segment
    boundary used by subtitle exports is corrected.
    """
    if not segment.words:
        return segment

    segment_duration = segment.end - segment.start
    character_count = len(re.sub(r"\s+", "", segment.text))
    if (
        segment_duration < _SUSPICIOUS_SEGMENT_MIN_SECONDS
        or character_count == 0
        or segment_duration / character_count < _SUSPICIOUS_SECONDS_PER_CHARACTER
    ):
        return segment

    suspicious_indexes = [
        index
        for index, word in enumerate(segment.words)
        if word.end > word.start and word.end - word.start >= _SUSPICIOUS_WORD_MIN_SECONDS
    ]
    if not suspicious_indexes:
        return segment

    first_suspicious_index = min(suspicious_indexes)
    if first_suspicious_index == 0 or len(segment.words) - first_suspicious_index > _MAX_SUSPICIOUS_TAIL_WORDS:
        return segment

    previous_word = segment.words[first_suspicious_index - 1]
    repaired_end = min(segment.end, previous_word.end + _REPAIRED_TAIL_PADDING_SECONDS)
    if segment.end - repaired_end < _SUSPICIOUS_WORD_MIN_SECONDS:
        return segment

    return segment.model_copy(
        update={
            "end": repaired_end,
            "end_text": format_timestamp(repaired_end),
        }
    )
