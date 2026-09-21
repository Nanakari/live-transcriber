"""Readable display cues, separate from the fine-grained transcription."""
from __future__ import annotations

import math
import re
from pathlib import Path

from .utils import format_srt_timestamp


# ASS stores colours as ``&HAABBGGRR`` rather than CSS ``#RRGGBB``.
ASS_ORIGINAL_COLOR = "&H00FFFFFF"       # #FFFFFF, warm white source text
ASS_TRANSLATION_COLOR = "&H004DD8FF"   # #FFD84D, warm yellow translation text
ASS_OUTLINE_COLOR = "&H00141414"       # #141414, readable on bright footage
ASS_TRANSPARENT_COLOR = "&HFF000000"   # fully transparent subtitle background


def _join(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part.strip())


def _split_text_by_weights(text: str, weights: list[float]) -> list[str]:
    """Partition display text in the same order as timed word groups.

    Word timestamps provide reliable boundaries for timing, but repaired source
    text and Chinese translations do not necessarily have the same tokenization
    as the raw ASR words.  Proportional character cuts preserve the effective
    text while keeping each resulting cue aligned to its word-time group.
    """
    count = len(weights)
    value = str(text or "")
    if count <= 1:
        return [value]
    if not value:
        return [""] * count

    separators = set(" \t\n，,、。．.!！？?；;：:…)]）】」』")
    safe_weights = []
    for weight in weights:
        try:
            numeric = float(weight)
        except (TypeError, ValueError):
            numeric = 0.001
        safe_weights.append(numeric if math.isfinite(numeric) and numeric > 0 else 0.001)
    total = sum(safe_weights)
    boundaries: list[int] = []
    previous = 0
    cumulative = 0.0
    for index in range(1, count):
        cumulative += safe_weights[index - 1]
        target = round(len(value) * cumulative / total)
        # Empty pieces are preferable to dropping a character when there are
        # more timed groups than characters in a short translation.
        low = previous
        high = len(value) - (count - index - 1)
        target = max(low, min(target, high))
        # A punctuation mark far from the proportional boundary can collapse
        # most of a long translation into one cue.  Only use a nearby mark;
        # otherwise retain the proportional boundary as a conservative
        # approximation.
        punctuation_window = max(1, min(8, round(len(value) * 0.12)))
        candidates = [
            position
            for position in range(max(1, low, target - punctuation_window),
                                  min(high, target + punctuation_window) + 1)
            if value[position - 1] in separators
        ]
        if candidates:
            target = min(candidates, key=lambda position: abs(position - target))
        boundaries.append(target)
        previous = target

    start = 0
    parts: list[str] = []
    for boundary in (*boundaries, len(value)):
        parts.append(value[start:boundary])
        start = boundary
    return parts


def _normalise_alignment_text(value: str) -> str:
    return "".join(character for character in str(value or "") if not character.isspace())


def _split_source_by_word_text(text: str, groups: list[list[dict]]) -> list[str] | None:
    """Split source text at word boundaries when its text matches the words.

    Word timestamps are useful boundaries, but their tokenisation can differ
    from the repaired or exported source.  Exact matching after whitespace
    removal is deliberately required here; callers use proportional cuts when
    punctuation or words do not line up, which preserves the source without
    pretending to know a semantic alignment.
    """
    value = str(text or "")
    source = _normalise_alignment_text(value)
    if not source:
        return [""] * len(groups)
    normalised_positions = [index for index, character in enumerate(value) if not character.isspace()]
    cursor = 0
    boundaries: list[int] = []
    for group_index, group in enumerate(groups[:-1]):
        for word in group:
            token = _normalise_alignment_text(str(word.get("text", "")))
            if not token:
                continue
            if not source.startswith(token, cursor):
                return None
            cursor += len(token)
        if cursor:
            boundaries.append(normalised_positions[cursor - 1] + 1)
        else:
            boundaries.append(0)

    for word in groups[-1] if groups else []:
        token = _normalise_alignment_text(str(word.get("text", "")))
        if not token:
            continue
        if not source.startswith(token, cursor):
            return None
        cursor += len(token)
    if cursor != len(source):
        return None

    parts: list[str] = []
    start = 0
    for boundary in (*boundaries, len(value)):
        parts.append(value[start:boundary])
        start = boundary
    return parts


_MISSING_TRANSLATION_PREFIX = "[翻译暂缺]"
_BLOCKED_ASR_TRANSLATION = "[原文待复核，翻译暂缺]"
EMPTY_TRANSLATION_PLACEHOLDER = "[无独立译文]"
_LEGACY_MISSING_NOTE = "模型未返回翻译，已使用原文占位"


def _translation_status(cue: dict) -> tuple[str, bool, bool]:
    """Return ``(status, missing, blocked_asr)`` with legacy compatibility."""
    status = str(cue.get("translation_status") or "").strip().lower()
    translation = str(cue.get("translation") or "").strip()
    brief_note = str(cue.get("brief_note") or "").strip()
    blocked_marker = translation.startswith(_BLOCKED_ASR_TRANSLATION)
    missing_marker = translation.startswith(_MISSING_TRANSLATION_PREFIX) or blocked_marker
    missing = status in {"missing", "blocked_asr_review"} or missing_marker or _LEGACY_MISSING_NOTE in brief_note
    blocked = status == "blocked_asr_review" or blocked_marker or (missing and bool(cue.get("asr_suspect")))
    if blocked:
        status = "blocked_asr_review"
    elif missing:
        status = "missing"
    elif not status:
        status = "translated"
    return status, missing, blocked


def _strip_missing_translation_prefix(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith(_BLOCKED_ASR_TRANSLATION):
        return text[len(_BLOCKED_ASR_TRANSLATION):].strip()
    if text.startswith(_MISSING_TRANSLATION_PREFIX):
        return text[len(_MISSING_TRANSLATION_PREFIX):].strip()
    return text


def _normalise_display_cue(cue: dict) -> dict:
    """Normalize compatibility markers for every display cue path."""
    status, missing, blocked = _translation_status(cue)
    if not missing and "translation_status" not in cue:
        return cue
    normalized = dict(cue)
    translation = str(cue.get("translation") or "")
    if blocked:
        normalized["translation"] = _BLOCKED_ASR_TRANSLATION
        normalized["translation_status"] = "blocked_asr_review"
    elif missing:
        body = _strip_missing_translation_prefix(translation)
        normalized["translation"] = _MISSING_TRANSLATION_PREFIX + (f" {body}" if body else "")
        normalized["translation_status"] = "missing"
    elif status:
        normalized["translation_status"] = status
    return normalized


def _text_matches(left: str, right: str) -> bool:
    return _normalise_alignment_text(left) == _normalise_alignment_text(right)


def _display_translation_text(cue: dict) -> str:
    """Keep source-only display cues structurally bilingual in exported SRT."""
    translation = str(cue.get("translation") or "")
    if translation.strip() or not str(cue.get("original") or "").strip():
        return translation
    return EMPTY_TRANSLATION_PLACEHOLDER


def split_cues_by_word_timestamps(
    cues: list[dict],
    word_segments: list[list[dict]],
    *,
    max_duration: float = 9.0,
) -> list[dict]:
    """Split long ASR cues at existing word-level timestamp boundaries.

    The fine-grained SRT remains unchanged.  This helper is only for display
    exports, where a single 30–60 second ASR segment would otherwise become one
    unreadable subtitle block.  Cues without usable words are kept intact so a
    missing timestamp never drops transcript content.
    """
    if max_duration <= 0:
        raise ValueError("max_duration must be greater than zero")

    if word_segments and len(word_segments) != len(cues):
        # A positional fallback would attach another segment's timing to this
        # cue.  Disable enhancement for the whole batch instead.
        word_segments = []

    output: list[dict] = []
    for cue_index, cue in enumerate(cues):
        cue = _normalise_display_cue(cue)
        try:
            start = float(cue["start"])
            end = float(cue["end"])
        except (KeyError, TypeError, ValueError):
            output.append(cue)
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            output.append(cue)
            continue
        if end - start <= max_duration:
            output.append(cue)
            continue

        raw_words = word_segments[cue_index] if cue_index < len(word_segments) else []
        if not isinstance(raw_words, list):
            output.append(cue)
            continue
        words: list[dict[str, float | str]] = []
        invalid_words = False
        previous_start: float | None = None
        previous_positive_end: float | None = None
        for raw_word in raw_words or []:
            if not isinstance(raw_word, dict):
                invalid_words = True
                break
            try:
                raw_start = float(raw_word["start"])
                raw_end = float(raw_word["end"])
            except (KeyError, TypeError, ValueError):
                invalid_words = True
                break
            if not math.isfinite(raw_start) or not math.isfinite(raw_end) or raw_end < raw_start:
                invalid_words = True
                break
            if previous_start is not None and raw_start < previous_start:
                # Do not sort questionable input: doing so could associate a
                # word's text with the wrong time range.  Keeping the source
                # cue intact is the safe fallback for malformed ASR output.
                invalid_words = True
                break
            if raw_end > raw_start and previous_positive_end is not None and raw_end < previous_positive_end:
                invalid_words = True
                break
            previous_start = raw_start
            if raw_end > raw_start:
                previous_positive_end = raw_end

            if raw_end < start or raw_start > end:
                continue
            word_start = max(start, raw_start)
            word_end = min(end, raw_end)
            if word_end < word_start:
                continue
            words.append({
                "start": word_start,
                "end": word_end,
                "text": str(raw_word.get("word", raw_word.get("text", "")) or ""),
            })
        if invalid_words or not words:
            output.append(cue)
            continue

        timed_words = [word for word in words if float(word["end"]) > float(word["start"])]
        if not timed_words:
            # Zero-duration words are retained as text evidence, but cannot
            # provide a reliable display boundary on their own.
            output.append(cue)
            continue

        groups: list[list[dict[str, float | str]]] = []
        current: list[dict[str, float | str]] = []
        current_start: float | None = None
        for word in words:
            word_start = float(word["start"])
            word_end = float(word["end"])
            if current and word_end > word_start and current_start is not None and word_end - current_start > max_duration:
                groups.append(current)
                current = []
                current_start = None
            current.append(word)
            if word_end > word_start:
                if current_start is None:
                    current_start = word_start
        if current:
            groups.append(current)

        if len(groups) <= 1:
            output.append(cue)
            continue

        weights = []
        for group in groups:
            positive = [word for word in group if float(word["end"]) > float(word["start"])]
            weights.append(max(float(positive[-1]["end"]) - float(positive[0]["start"]), 0.001))

        original_text = str(cue.get("original", ""))
        original_parts = _split_source_by_word_text(original_text, groups)
        if original_parts is None:
            original_parts = _split_text_by_weights(original_text, weights)

        status, missing, blocked = _translation_status(cue)
        translation_text = str(cue.get("translation", ""))
        if blocked:
            translation_parts = [_BLOCKED_ASR_TRANSLATION] * len(groups)
        elif missing:
            translation_body = _strip_missing_translation_prefix(translation_text)
            if _text_matches(translation_body, original_text):
                body_parts = original_parts
            else:
                body_parts = _split_text_by_weights(translation_body, weights)
            translation_parts = [
                _MISSING_TRANSLATION_PREFIX + (f" {part.strip()}" if part.strip() else "")
                for part in body_parts
            ]
        else:
            translation_parts = _split_text_by_weights(translation_text, weights)
        for index, group in enumerate(groups):
            split_cue = dict(cue)
            positive = [word for word in group if float(word["end"]) > float(word["start"])]
            split_cue["start"] = float(positive[0]["start"])
            split_cue["end"] = float(positive[-1]["end"])
            split_cue["original"] = original_parts[index]
            split_cue["translation"] = translation_parts[index]
            split_cue["translation_status"] = status
            output.append(split_cue)

    return output


def group_display_cues(cues: list[dict]) -> list[dict]:
    groups: list[dict] = []
    current: list[dict] = []

    def status_key(cue: dict) -> tuple:
        status, missing, blocked = _translation_status(cue)
        return (
            status,
            missing,
            blocked,
            bool(cue.get("review_required")),
            bool(cue.get("asr_suspect")),
            bool(str(cue.get("translation") or "").strip()),
            str(cue.get("brief_note") or "").strip(),
            str(cue.get("review_reason") or "").strip(),
            str(cue.get("asr_issue") or "").strip(),
        )

    def flush():
        if current:
            first = current[0]
            group = {
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "original": _join([c["original"] for c in current]),
                "translation": _join([c["translation"] for c in current]),
                "translation_status": _translation_status(first)[0],
            }
            for key in ("brief_note", "review_required", "review_reason", "asr_suspect", "asr_issue"):
                if key in first:
                    group[key] = first[key]
            groups.append(group)
            current.clear()

    for cue in cues:
        cue = _normalise_display_cue(cue)
        try:
            cue_start = float(cue["start"])
            cue_end = float(cue["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(cue_start) or not math.isfinite(cue_end) or cue_end <= cue_start:
            continue
        if cue_start != cue["start"] or cue_end != cue["end"]:
            cue = dict(cue)
            cue["start"] = cue_start
            cue["end"] = cue_end
        if current:
            span = cue_end - current[0]["start"]
            gap = cue_start - current[-1]["end"]
            too_long = (len(_join([c["original"] for c in current] + [cue["original"]])) > 100
                        or len(_join([c["translation"] for c in current] + [cue["translation"]])) > 70)
            if (status_key(cue) != status_key(current[-1])
                    or gap > 0.8 or gap < -0.01 or span > 9 or too_long):
                flush()
        current.append(cue)
        span = current[-1]["end"] - current[0]["start"]
        if span >= 6 and re.search(r"[。！？!?]$", cue["translation"].strip()):
            flush()
    flush()
    return groups


def write_display_subtitles(cues: list[dict], srt: Path, ass: Path) -> None:
    groups = group_display_cues(cues)
    blocks = []
    for index, cue in enumerate(groups, 1):
        original = str(cue.get("original") or "")
        translation = _display_translation_text(cue)
        text_lines = [text for text in (original, translation) if text]
        if not text_lines:
            continue
        blocks.append(
            f"{index}\n{format_srt_timestamp(cue['start'])} --> {format_srt_timestamp(cue['end'])}\n"
            + "\n".join(text_lines)
        )
    srt.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Microsoft YaHei,28,&H00FFFFFF,&H00FFFFFF,&H00141414,&HFF000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def timestamp(seconds):
        centis = round(seconds * 100)
        return f"{centis // 360000}:{centis // 6000 % 60:02}:{centis // 100 % 60:02}.{centis % 100:02}"

    def escape(text):
        # Prevent transcript text from being interpreted as ASS style overrides.
        return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ")

    def colored_line(text: str, color: str) -> str:
        return f"{{\\c{color}&}}{escape(text)}"

    def dialogue_text(cue: dict) -> str:
        lines = []
        if cue["original"]:
            lines.append(colored_line(cue["original"], ASS_ORIGINAL_COLOR))
        translation = _display_translation_text(cue)
        if translation:
            lines.append(colored_line(translation, ASS_TRANSLATION_COLOR))
        return r"\N".join(lines)

    ass.write_text(header + "\n".join(
        f"Dialogue: 0,{timestamp(c['start'])},{timestamp(c['end'])},Default,,0,0,0,,{dialogue_text(c)}"
        for c in groups
    ) + "\n", encoding="utf-8")
