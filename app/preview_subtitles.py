"""Read and align analysis/ASR metadata and emit subtitles for media previews.

Encoding media and assembling player packages belong to app.preview; cue timing
and rendering algorithms belong to app.subtitles.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from .analysis.repairs import effective_original
from .config import output_root, project_root
from .output_layout import group_dir_from_artifact_path
from .subtitles import split_cues_by_word_timestamps
from .utils import RunLogger, format_srt_timestamp


def split_display_cues(
    cues: list[dict],
    word_segments: list[list[dict]],
    logger: RunLogger,
) -> list[dict]:
    if word_segments and not word_segments_match_cues(cues, word_segments):
        logger.write("词级时间戳与字幕时间或原文不可验证，关闭词级拆分。")
        word_segments = []
    display_cues = split_cues_by_word_timestamps(cues, word_segments, max_duration=9.0)
    if len(display_cues) != len(cues):
        durations = []
        for cue in display_cues:
            try:
                durations.append(float(cue["end"]) - float(cue["start"]))
            except (KeyError, TypeError, ValueError):
                continue
        logger.write(
            f"按词级时间戳拆分长字幕：{len(cues)} 段 -> {len(display_cues)} 段，"
            f"最大时长={max(durations, default=0.0):.2f}s"
        )
    elif word_segments:
        logger.write(f"词级时间戳已加载：{len(word_segments)} 段，未发现需要拆分的长字幕。")
    else:
        logger.write("未找到可用词级时间戳，保留原始显示字幕时间轴。")
    return display_cues


def word_segments_match_cues(cues: list[dict], word_segments: list[list[dict]]) -> bool:
    if len(cues) != len(word_segments):
        return False
    for cue, words in zip(cues, word_segments):
        if not isinstance(words, list):
            return False
        try:
            cue_start = float(cue["start"])
            cue_end = float(cue["end"])
        except (KeyError, TypeError, ValueError):
            return False
        if not math.isfinite(cue_start) or not math.isfinite(cue_end) or cue_end <= cue_start:
            return False
        if not words:
            continue
        has_overlap = False
        has_text = False
        for word in words:
            if not isinstance(word, dict):
                return False
            try:
                word_start = float(word["start"])
                word_end = float(word["end"])
            except (KeyError, TypeError, ValueError):
                return False
            if not math.isfinite(word_start) or not math.isfinite(word_end) or word_end < word_start:
                return False
            if word_end >= cue_start and word_start <= cue_end:
                has_overlap = True
            if str(word.get("word", word.get("text", "")) or "").strip():
                has_text = True
        if not has_overlap or (str(cue.get("original") or "").strip() and not has_text):
            return False
    return True


def _normalise_srt_text(value: str) -> str:
    return " ".join(str(value or "").split())


def _exported_translation_text(line: dict) -> str | None:
    """Mirror ``export_translation_srt`` for metadata/SRT validation.

    Empty translations are intentionally exported as the original text plus
    ``[需要复查]``.  Treating the empty JSON value as an empty SRT line would
    reject otherwise valid blocked rows and disable all display metadata.
    """
    if "translation_zh" not in line:
        return None
    translation = str(line.get("translation_zh") or "")
    if translation.strip():
        return translation
    return f"{line.get('original', '')} [需要复查]"


def align_display_auxiliary_data(
    subtitle_blocks: list[dict],
    word_segments: list[list[dict]],
    line_metadata: list[dict],
    logger: RunLogger,
) -> tuple[list[list[dict]], list[dict]]:
    """Align timing/status data to parsed SRT blocks or disable enhancement.

    Analysis JSON is ordered by exported line order, while an externally
    supplied SRT can be shortened or reordered.  Indexing the two lists in
    that case silently assigns one line's timing or status to another.  Match
    by time and exported translation text; any ambiguity disables both
    enhancements for this preview.
    """
    if not subtitle_blocks:
        return [], []
    if len(line_metadata) != len(subtitle_blocks):
        logger.write("字幕块数量与分析行不一致，关闭词级拆分和状态增强。")
        return [], []
    if len(word_segments) not in {0, len(line_metadata)}:
        logger.write("词级时间戳数量与分析行不一致，关闭词级拆分。")
        word_segments = []

    seen_ids: set[int] = set()
    for line in line_metadata:
        segment_id = _as_segment_id(line.get("segment_id"))
        if segment_id is None or segment_id in seen_ids:
            logger.write("分析行 segment_id 缺失或重复，关闭词级拆分和状态增强。")
            return [], []
        seen_ids.add(segment_id)

    matches: list[int] = []
    for block in subtitle_blocks:
        try:
            block_start = float(block["start"])
            block_end = float(block["end"])
        except (KeyError, TypeError, ValueError):
            logger.write("字幕块时间戳不可验证，关闭词级拆分和状态增强。")
            return [], []
        if (
            not math.isfinite(block_start)
            or not math.isfinite(block_end)
            or block_end <= block_start
        ):
            logger.write("字幕块时间戳无效，关闭词级拆分和状态增强。")
            return [], []
        candidates: list[int] = []
        for index, line in enumerate(line_metadata):
            try:
                line_start = float(line["start"])
                line_end = float(line["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                not math.isfinite(line_start)
                or not math.isfinite(line_end)
                or line_end <= line_start
            ):
                continue
            if abs(line_start - block_start) > 0.02 or abs(line_end - block_end) > 0.02:
                continue
            exported_text = _exported_translation_text(line)
            if exported_text is None:
                continue
            if _normalise_srt_text(exported_text) != _normalise_srt_text(block.get("text", "")):
                continue
            candidates.append(index)
        if len(candidates) != 1:
            logger.write("字幕块无法与唯一分析行按时间和译文对应，关闭词级拆分和状态增强。")
            return [], []
        matches.append(candidates[0])

    if len(set(matches)) != len(matches):
        logger.write("字幕块映射到重复分析行，关闭词级拆分和状态增强。")
        return [], []
    aligned_metadata = [line_metadata[index] for index in matches]
    aligned_words = [word_segments[index] for index in matches] if word_segments else []
    return aligned_words, aligned_metadata


def maybe_copy_original_subtitle(chinese_subtitle: Path, output_dir: Path) -> Path | None:
    repaired = chinese_subtitle.parent / "repaired_transcript.srt"
    if repaired.exists() and repaired.is_file():
        target = output_dir / "live_preview.ja.srt"
        if repaired.resolve() != chinese_subtitle.resolve():
            shutil.copy2(repaired, target)
            return target

    related = _read_related_analysis(chinese_subtitle)
    if related is None:
        # A directory-wide glob cannot establish which transcript belongs to a
        # standalone subtitle, so leave the original-language track absent.
        return None
    analysis_json, analysis = related
    if not isinstance(analysis, dict):
        return None
    meta = analysis.get("meta", {})
    if not isinstance(meta, dict):
        return None

    group_dir = group_dir_from_artifact_path(chinese_subtitle)
    run_id = str(meta.get("transcript_run_id") or "").strip()
    input_file_text = str(meta.get("input_file") or "").strip()
    input_file = Path(input_file_text) if input_file_text else None
    candidates: list[Path] = []
    if input_file is not None:
        if not input_file.is_absolute():
            input_file = (analysis_json.parent / input_file).resolve()
        candidates.append(input_file.with_suffix(".srt"))

    names: list[str] = []
    if run_id:
        names.append(f"{run_id}_transcript.srt")
    if input_file is not None and input_file.name:
        names.append(input_file.with_suffix(".srt").name)
    transcript_dirs: list[Path] = []
    if group_dir:
        transcript_dirs.append(group_dir / "transcripts")
    transcript_dirs.append(output_root(root=project_root()) / "transcripts")
    for transcripts_dir in transcript_dirs:
        for name in names:
            candidates.append(transcripts_dir / name)

    existing: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.exists() or not resolved.is_file():
            continue
        seen.add(resolved)
        existing.append(resolved)
    if not existing:
        return None
    source = existing[0]
    target = output_dir / "live_preview.ja.srt"
    if source.resolve() != chinese_subtitle.resolve():
        shutil.copy2(source, target)
        return target
    return None


def infer_transcript_run_id_from_analysis(chinese_subtitle: Path) -> str:
    analysis_json = chinese_subtitle.parent / "analysis.json"
    if not analysis_json.exists():
        return ""
    try:
        import json

        meta = json.loads(analysis_json.read_text(encoding="utf-8")).get("meta", {})
        return str(meta.get("transcript_run_id") or "").strip()
    except Exception:
        return ""


def _read_related_analysis(chinese_subtitle: Path) -> tuple[Path, dict] | None:
    analysis_json = find_related_analysis_json(chinese_subtitle)
    if not analysis_json:
        return None
    try:
        return analysis_json, json.loads(analysis_json.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _analysis_lines_in_export_order(analysis: dict) -> list[dict]:
    """Flatten analysis lines in the same order used by translation_zh.srt."""
    lines: list[dict] = []
    chunks = analysis.get("chunks", []) if isinstance(analysis, dict) else []
    if not isinstance(chunks, list):
        return lines
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_lines = chunk.get("bilingual_lines", [])
        if not isinstance(chunk_lines, list):
            continue
        lines.extend(line for line in chunk_lines if isinstance(line, dict))
    return lines


def display_status_metadata(line: dict) -> dict:
    """Copy program-owned review fields onto a display cue when available."""
    if not isinstance(line, dict):
        return {}
    fields = (
        "translation_status",
        "brief_note",
        "review_required",
        "review_reason",
        "asr_suspect",
        "asr_issue",
    )
    return {field: line[field] for field in fields if field in line}


def load_analysis_line_metadata(chinese_subtitle: Path) -> list[dict]:
    """Return display metadata in exported subtitle order."""
    related = _read_related_analysis(chinese_subtitle)
    if related is None:
        return []
    _, analysis = related
    return _analysis_lines_in_export_order(analysis)


def _as_segment_id(value) -> int | None:
    try:
        # bool is an int subclass but is not a valid transcript identifier.
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def load_word_timed_segments(chinese_subtitle: Path) -> list[list[dict]]:
    """Load word timestamps by exported analysis ``segment_id`` order.

    The raw transcript may be reordered by a repair or resume path.  Returning
    words by list position would then attach one segment's timing to another's
    subtitle text, so a complete segment-id mapping is required before any
    display splitting is attempted.
    """
    related = _read_related_analysis(chinese_subtitle)
    if related is None:
        return []
    analysis_json, analysis = related
    if not isinstance(analysis, dict):
        return []
    lines = _analysis_lines_in_export_order(analysis)
    if not lines:
        return []
    try:
        meta = analysis.get("meta", {})
        if not isinstance(meta, dict):
            return []
        input_file = Path(str(meta.get("input_file") or ""))
        if not input_file.is_absolute():
            input_file = (analysis_json.parent / input_file).resolve()
        if not input_file.exists() or not input_file.is_file():
            return []
        transcript = json.loads(input_file.read_text(encoding="utf-8"))
        segments = transcript.get("segments", [])
        if not isinstance(segments, list):
            return []
        by_id: dict[int, dict] = {}
        for segment in segments:
            if not isinstance(segment, dict):
                return []
            raw_id = segment.get("id")
            if raw_id is None:
                raw_id = segment.get("segment_id")
            segment_id = _as_segment_id(raw_id)
            if segment_id is None or segment_id in by_id:
                return []
            by_id[segment_id] = segment

        ordered: list[list[dict]] = []
        seen_line_ids: set[int] = set()
        for line in lines:
            segment_id = _as_segment_id(line.get("segment_id"))
            if segment_id is None or segment_id in seen_line_ids or segment_id not in by_id:
                # Partial mappings are unsafe: returning no timing data keeps
                # every subtitle cue intact rather than guessing positions.
                return []
            seen_line_ids.add(segment_id)
            words = by_id[segment_id].get("words") or []
            if not isinstance(words, list):
                return []
            ordered.append([word for word in words if isinstance(word, dict)])
        return ordered
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return []


def maybe_write_bilingual_subtitle(chinese_subtitle: Path, japanese_subtitle: Path | None, output_dir: Path) -> Path | None:
    if japanese_subtitle is None or not japanese_subtitle.exists():
        return None
    zh_blocks = read_srt_blocks(chinese_subtitle)
    ja_blocks = read_srt_blocks(japanese_subtitle)
    if not zh_blocks or not ja_blocks:
        return None
    ja_lookup = build_srt_lookup(ja_blocks)
    blocks: list[str] = []
    for index, zh in enumerate(zh_blocks, start=1):
        ja = match_srt_block(zh, ja_lookup)
        if ja is None:
            continue
        text = "\n".join([ja["text"], zh["text"]]).strip()
        blocks.append(f"{index}\n{zh['time']}\n{text}")
    if not blocks:
        return None
    target = output_dir / "live_preview.bilingual.srt"
    target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return target


def maybe_write_study_subtitle(chinese_subtitle: Path, japanese_subtitle: Path | None, output_dir: Path) -> Path | None:
    analysis_json = find_related_analysis_json(chinese_subtitle)
    if not analysis_json:
        return None
    try:
        document = json.loads(analysis_json.read_text(encoding="utf-8"))
    except Exception:
        return None

    zh_blocks = read_srt_blocks(chinese_subtitle)
    ja_blocks = read_srt_blocks(japanese_subtitle) if japanese_subtitle and japanese_subtitle.exists() else []
    ja_lookup = build_srt_lookup(ja_blocks)
    line_entries: list[tuple[dict, dict]] = []
    for chunk in document.get("chunks", []):
        for line in chunk.get("bilingual_lines", []):
            line_entries.append((chunk, line))
    if not line_entries:
        return None

    blocks: list[str] = []
    count = min(len(line_entries), len(zh_blocks)) if zh_blocks else len(line_entries)
    for index in range(count):
        chunk, line = line_entries[index]
        zh_block = zh_blocks[index] if index < len(zh_blocks) else {}
        ja_block = match_srt_block(zh_block, ja_lookup) if zh_block else None
        time_text = zh_block.get("time") or f"{format_srt_timestamp(float(line.get('start', 0)))} --> {format_srt_timestamp(float(line.get('end', 0)))}"
        ja_text = compact_text(
            effective_original(line) if line else (ja_block or {}).get("text", ""),
            90,
        )
        zh_text = compact_text(zh_block.get("text") or line.get("translation_zh", ""), 90)
        study_lines = [value for value in (ja_text, zh_text) if value]
        notes = study_notes_for_line(chunk, line, ja_text)
        study_lines.extend(notes)
        blocks.append(f"{index + 1}\n{time_text}\n" + "\n".join(study_lines))

    target = output_dir / "live_preview.study.srt"
    target.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    return target


def find_related_analysis_json(chinese_subtitle: Path) -> Path | None:
    direct = chinese_subtitle.parent / "analysis.json"
    if direct.exists():
        return direct
    group_dir = group_dir_from_artifact_path(chinese_subtitle)
    if not group_dir:
        return None
    candidates = list((group_dir / "analysis").glob("*/analysis.json"))
    if not candidates:
        return None
    try:
        subtitle_bytes = chinese_subtitle.read_bytes()
        for analysis_json in sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True):
            translation = analysis_json.parent / "translation_zh.srt"
            if translation.exists() and translation.read_bytes() == subtitle_bytes:
                return analysis_json
    except Exception:
        pass
    return max(candidates, key=lambda path: path.stat().st_mtime)


def study_notes_for_line(chunk: dict, line: dict, ja_text: str) -> list[str]:
    original = effective_original(line) or ja_text or ""
    vocab_items = rank_items(
        original,
        ja_text,
        chunk.get("vocabulary", []),
        key_fields=("word", "example_original"),
    )[:2]
    grammar_items = rank_items(
        original,
        ja_text,
        chunk.get("grammar", []),
        key_fields=("pattern", "example_original"),
    )[:1]
    expression_items = rank_items(
        original,
        ja_text,
        chunk.get("fixed_expressions", []),
        key_fields=("expression", "example_original"),
    )[:1]

    notes: list[str] = []
    if vocab_items:
        parts = []
        for item in vocab_items:
            word = compact_text(str(item.get("word", "")), 18)
            reading = compact_text(str(item.get("reading", "")), 18)
            meaning = compact_text(str(item.get("meaning_zh", "")), 32)
            label = f"{word}({reading})" if reading else word
            if label and meaning:
                parts.append(f"{label}={meaning}")
        if parts:
            notes.append("生词：" + "；".join(parts))

    grammar_parts: list[str] = []
    for item in grammar_items:
        pattern = compact_text(str(item.get("pattern", "")), 24)
        explanation = compact_text(str(item.get("explanation_zh", "")), 42)
        if pattern and explanation:
            grammar_parts.append(f"{pattern}：{explanation}")
    for item in expression_items:
        expression = compact_text(str(item.get("expression", "")), 24)
        meaning = compact_text(str(item.get("meaning_zh", "")), 42)
        if expression and meaning:
            grammar_parts.append(f"{expression}：{meaning}")
    if grammar_parts:
        notes.append("语法：" + "；".join(grammar_parts[:2]))
    return notes


def rank_items(original: str, ja_text: str, items: list[dict], *, key_fields: tuple[str, ...]) -> list[dict]:
    scored: list[tuple[float, int, dict]] = []
    text = normalize_for_match(f"{original} {ja_text}")
    for index, item in enumerate(items):
        score = 0.0
        for field in key_fields:
            value = normalize_for_match(str(item.get(field, "")))
            if not value:
                continue
            if value in text or text in value:
                score = max(score, 3.0)
            else:
                score = max(score, overlap_score(text, value))
        if score >= 0.32:
            scored.append((score, -index, item))
    scored.sort(reverse=True)
    return [item for _, _, item in scored]


def normalize_for_match(value: str) -> str:
    return "".join(ch.lower() for ch in value if not ch.isspace() and ch not in "。、，,.！？!?「」『』（）()[]【】")


def overlap_score(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    if len(short) <= 2:
        return 1.0 if short in long else 0.0
    grams = {short[index : index + 2] for index in range(len(short) - 1)}
    if not grams:
        return 0.0
    hits = sum(1 for gram in grams if gram in long)
    return hits / len(grams)


def compact_text(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def read_srt_blocks(path: Path) -> list[dict[str, str]]:
    raw_blocks = path.read_text(encoding="utf-8", errors="replace").strip().split("\n\n")
    blocks: list[dict[str, str]] = []
    for raw in raw_blocks:
        lines = [line.strip("\ufeff") for line in raw.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if time_index < 0:
            continue
        time_line = lines[time_index]
        # Only the line before the time range is the SRT sequence number.
        # Numeric subtitle text after the time range is valid content and must
        # not be discarded (for example: "1", "2", "3").
        text_lines = lines[time_index + 1:]
        if not text_lines:
            continue
        start_text, _, end_text = time_line.partition("-->")
        try:
            start = parse_srt_seconds(start_text.strip())
            end = parse_srt_seconds(end_text.strip())
        except ValueError:
            continue
        blocks.append(
            {
                "time": time_line,
                "text": "\n".join(text_lines),
                "start": start,
                "end": end,
            }
        )
    return blocks


def parse_srt_seconds(value: str) -> float:
    hours_text, minutes_text, seconds_text = value.split(":")
    seconds_main, millis_text = seconds_text.split(",")
    hours = int(hours_text)
    minutes = int(minutes_text)
    seconds = int(seconds_main)
    millis = int(millis_text)
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def build_srt_lookup(blocks: list[dict[str, str]]) -> dict[str, dict[str, str] | list[dict[str, str]]]:
    by_time = {block["time"]: block for block in blocks}
    ordered = sorted(blocks, key=lambda block: (float(block["start"]), float(block["end"])))
    return {"by_time": by_time, "ordered": ordered}


def match_srt_block(block: dict[str, str], lookup: dict[str, dict[str, str] | list[dict[str, str]]]) -> dict[str, str] | None:
    if not block:
        return None
    by_time = lookup["by_time"]
    if block["time"] in by_time:
        return by_time[block["time"]]
    start = float(block["start"])
    end = float(block["end"])
    best: tuple[float, dict[str, str]] | None = None
    for candidate in lookup["ordered"]:
        distance = abs(float(candidate["start"]) - start) + abs(float(candidate["end"]) - end)
        if best is None or distance < best[0]:
            best = (distance, candidate)
    if best and best[0] <= 0.12:
        return best[1]
    return None
