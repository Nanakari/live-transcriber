from __future__ import annotations

import shutil
import os
import json
from dataclasses import dataclass
from pathlib import Path

from .config import project_root
from .output_layout import ensure_media_subdirs, group_dir_from_artifact_path
from .utils import AppError, RunLogger, command_exists, format_srt_timestamp, generate_run_id, run_subprocess


@dataclass
class PreviewOptions:
    audio: Path
    subtitle: Path
    cover: Path | None
    output_dir: Path | None
    resolution: str
    subtitle_name: str
    video_name: str
    mode: str
    debug: bool = False


def create_potplayer_preview(options: PreviewOptions) -> dict[str, Path]:
    if options.mode != "potplayer":
        raise AppError("preview 当前只支持 --mode potplayer。")
    ensure_ffmpeg_for_preview()
    if not command_exists("ffmpeg"):
        raise AppError(
            "未找到 ffmpeg。\n"
            "请安装 ffmpeg 并加入 PATH，或确认项目 tools/ffmpeg.exe 所在目录已加入 PATH。"
        )

    audio = options.audio.expanduser()
    subtitle = options.subtitle.expanduser()
    cover = options.cover.expanduser() if options.cover else find_cover()
    if not audio.exists() or not audio.is_file():
        raise AppError(f"音频文件不存在或不可读取：{audio}")
    if not subtitle.exists() or not subtitle.is_file():
        raise AppError(f"字幕文件不存在或不可读取：{subtitle}")
    if cover is None or not cover.exists() or not cover.is_file():
        raise AppError("封面图不存在。请使用 --cover 指定 jpg/png/webp 封面图路径。")

    width, height = parse_resolution(options.resolution)
    run_id = generate_run_id()
    if options.output_dir:
        output_dir = options.output_dir.expanduser()
    else:
        group_dir = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
        if group_dir:
            output_dir = ensure_media_subdirs(group_dir)["previews"] / "potplayer"
        else:
            output_dir = project_root() / "outputs" / "previews" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = RunLogger(output_dir / "run.log", debug=options.debug)

    video_path = output_dir / options.video_name
    subtitle_path = output_dir / options.subtitle_name
    auto_subtitle_path = output_dir / f"{video_path.stem}.srt"
    cover_path = output_dir / "cover.jpg"
    readme_path = output_dir / "README_play.txt"

    prepare_cover(cover, cover_path, logger)
    shutil.copy2(subtitle, subtitle_path)
    build_preview_video(
        audio=audio,
        cover=cover_path,
        output=video_path,
        width=width,
        height=height,
        logger=logger,
    )
    ja_subtitle = maybe_copy_original_subtitle(subtitle, output_dir)
    bilingual_subtitle = maybe_write_bilingual_subtitle(subtitle_path, ja_subtitle, output_dir)
    study_subtitle = maybe_write_study_subtitle(subtitle, ja_subtitle, output_dir)
    if bilingual_subtitle:
        shutil.copy2(bilingual_subtitle, auto_subtitle_path)
    elif study_subtitle:
        shutil.copy2(study_subtitle, auto_subtitle_path)
    else:
        shutil.copy2(subtitle_path, auto_subtitle_path)
    learning_notes = copy_learning_notes(subtitle, output_dir)
    write_readme(readme_path, video_path.name, subtitle_path.name, learning_notes, study_subtitle)
    return {
        "output_dir": output_dir,
        "video": video_path,
        "subtitle": subtitle_path,
        "auto_subtitle": auto_subtitle_path,
        "bilingual_subtitle": bilingual_subtitle,
        "study_subtitle": study_subtitle,
        "vocabulary": learning_notes.get("vocabulary.md"),
        "grammar": learning_notes.get("grammar.md"),
        "cover": cover_path,
        "readme": readme_path,
        "log": output_dir / "run.log",
    }


def ensure_ffmpeg_for_preview() -> None:
    if command_exists("ffmpeg"):
        return
    bundled = project_root() / "tools" / "ffmpeg.exe"
    if bundled.exists():
        os.environ["PATH"] = str(bundled.parent.resolve()) + os.pathsep + os.environ.get("PATH", "")


def parse_resolution(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except Exception as exc:
        raise AppError("--resolution 格式应为 WIDTHxHEIGHT，例如 1280x720。") from exc
    if width <= 0 or height <= 0:
        raise AppError("--resolution 宽高必须大于 0。")
    return width, height


def find_cover() -> Path | None:
    candidates: list[Path] = []
    roots = [
        project_root() / "outputs" / "media",
        project_root() / "outputs" / "thumbnails",
    ]
    for root in roots:
        if not root.exists():
            continue
        for pattern in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            candidates.extend(root.rglob(pattern))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def prepare_cover(source: Path, target: Path, logger: RunLogger) -> None:
    suffix = source.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        shutil.copy2(source, target)
        return
    command = ["ffmpeg", "-y", "-i", str(source), "-frames:v", "1", str(target)]
    result = run_subprocess(command, logger)
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise AppError(
            "封面图转换为 jpg 失败。\n"
            "请确认封面图可打开；如果是 webp 且 ffmpeg 不支持，请手动转换为 jpg 后用 --cover 指定。\n"
            f"ffmpeg 输出：{(result.stderr or result.stdout).strip()}"
        )


def build_preview_video(
    *,
    audio: Path,
    cover: Path,
    output: Path,
    width: int,
    height: int,
    logger: RunLogger,
) -> None:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:in_range=pc:out_range=tv,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "format=yuv420p,setparams=range=tv"
    )
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-framerate",
        "1",
        "-i",
        str(cover),
        "-i",
        str(audio),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(output),
    ]
    result = run_subprocess(command, logger, stream_output=True)
    if result.returncode != 0:
        raise AppError(
            "生成 PotPlayer 预览 MP4 失败。\n"
            f"ffmpeg 输出：{(result.stderr or result.stdout).strip()}"
        )
    if not output.exists() or output.stat().st_size == 0:
        raise AppError("ffmpeg 已结束，但 live_preview.mp4 没有生成或为空。")


def maybe_copy_original_subtitle(chinese_subtitle: Path, output_dir: Path) -> Path | None:
    transcript_dirs: list[Path] = []
    group_dir = group_dir_from_artifact_path(chinese_subtitle)
    if group_dir:
        transcript_dirs.append(group_dir / "transcripts")
    transcript_dirs.append(project_root() / "outputs" / "transcripts")
    run_id = infer_transcript_run_id_from_analysis(chinese_subtitle)
    candidates: list[Path] = []
    for transcripts_dir in transcript_dirs:
        if not transcripts_dir.exists():
            continue
        if run_id:
            candidates.append(transcripts_dir / f"{run_id}_transcript.srt")
        candidates.extend(transcripts_dir.glob("*_transcript.srt"))
    existing = [path for path in candidates if path.exists() and path.is_file()]
    if not existing:
        return None
    source = existing[0]
    target = output_dir / "live_preview.ja.srt"
    if source.resolve() != chinese_subtitle.resolve():
        shutil.copy2(source, target)
        return target
    return None


def copy_learning_notes(chinese_subtitle: Path, output_dir: Path) -> dict[str, Path]:
    """Copy module-two learning notes into the PotPlayer preview package."""
    analysis_json = find_related_analysis_json(chinese_subtitle)
    analysis_dir = analysis_json.parent if analysis_json else chinese_subtitle.parent
    copied: dict[str, Path] = {}
    for name in ("vocabulary.md", "grammar.md"):
        source = analysis_dir / name
        if not source.exists() or not source.is_file():
            continue
        target = output_dir / name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        copied[name] = target
    return copied


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
        ja_text = compact_text((ja_block or {}).get("text") or line.get("original", ""), 90)
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
    original = str(line.get("original") or ja_text or "")
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
        time_line = next((line for line in lines if "-->" in line), "")
        text_lines = [line for line in lines if "-->" not in line and not line.isdigit()]
        if not time_line or not text_lines:
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


def write_readme(
    path: Path,
    video_name: str,
    subtitle_name: str,
    learning_notes: dict[str, Path] | None = None,
    study_subtitle: Path | None = None,
) -> None:
    notes = learning_notes or {}
    note_lines: list[str] = []
    if "vocabulary.md" in notes:
        note_lines.append("8. vocabulary.md 是模块二生成的生词讲解。")
    if "grammar.md" in notes:
        note_lines.append("9. grammar.md 是模块二生成的语法分析。")
    if study_subtitle:
        note_lines.append("10. live_preview.study.srt 是带当前句生词和语法提示的学习字幕，可在 PotPlayer 中手动切换。")
    extra = "\n".join(note_lines)
    if extra:
        extra += "\n"
    content = f"""PotPlayer 预览包使用说明

1. 用 PotPlayer 打开 {video_name}。
2. 默认会优先加载 live_preview.bilingual.srt 双语字幕；如果没有自动加载，把它拖入 PotPlayer 窗口。
3. 也可以在 PotPlayer 中右键 -> 字幕 -> 选择字幕，手动加载 {subtitle_name}、live_preview.study.srt 或 live_preview.bilingual.srt。
4. 如果字幕不同步，可以使用 PotPlayer 的字幕同步功能调整延迟。
5. {subtitle_name} 是外挂字幕，可以直接用文本编辑器或字幕工具修改。
6. live_preview.mp4 没有硬烧字幕，只包含静态封面画面和音频。
7. 如果存在 live_preview.ja.srt，它是模块一原文字幕，可按需手动加载。
{extra}预览包包含模块三播放文件，以及可直接打开的学习资料。
"""
    path.write_text(content, encoding="utf-8")

