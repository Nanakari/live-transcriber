from __future__ import annotations

import shutil
import os
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .subtitles import (
    ASS_ORIGINAL_COLOR,
    ASS_OUTLINE_COLOR,
    ASS_TRANSPARENT_COLOR,
    split_cues_by_word_timestamps,
    write_display_subtitles,
)
from .config import project_root, tool_path
from .floating_overlay import (
    AUDIO_MODE_SUBTITLE_FILENAME,
    AUDIO_PLAYER_LAUNCHER_FILENAME,
    AUDIO_PLAYER_FILENAME,
    OVERLAY_LAUNCHER_FILENAME,
    OVERLAY_FILENAME,
    write_audio_subtitle_player,
    write_floating_subtitle_overlay,
    write_overlay_launcher,
)
from .analysis.repairs import effective_original
from .media_assets import default_thumbnail_path
from .output_layout import ensure_media_subdirs, group_dir_from_artifact_path, write_media_index
from .utils import AppError, RunLogger, command_exists, format_srt_timestamp, generate_run_id, run_subprocess


SUBTITLE_FORCE_STYLE = (
    "FontName=Microsoft YaHei,FontSize=26,Bold=1,"
    f"PrimaryColour={ASS_ORIGINAL_COLOR},OutlineColour={ASS_OUTLINE_COLOR},"
    f"BackColour={ASS_TRANSPARENT_COLOR},BorderStyle=1,Outline=2,Shadow=1,"
    "Alignment=2,MarginV=36"
)


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
    floating_overlay_name: str = OVERLAY_FILENAME


def create_preview(options: PreviewOptions) -> dict[str, Path]:
    """Create the selected preview package; audio is the default lightweight mode."""
    if options.mode == "audio":
        return create_audio_preview(options)
    return create_video_preview(options)


def create_audio_preview(options: PreviewOptions) -> dict[str, Path]:
    """Create an audio-only player package without encoding a video file."""
    if options.mode != "audio":
        raise AppError("音频预览需要 --mode audio。")

    audio = options.audio.expanduser()
    subtitle = options.subtitle.expanduser()
    if not audio.exists() or not audio.is_file():
        raise AppError(f"音频文件不存在或不可读取：{audio}")
    if not subtitle.exists() or not subtitle.is_file():
        raise AppError(f"字幕文件不存在或不可读取：{subtitle}")

    run_id = generate_run_id()
    group = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
    if options.output_dir:
        output_dir = options.output_dir.expanduser()
    elif group:
        output_dir = group / "audio"
    else:
        output_dir = project_root() / "outputs" / "previews" / run_id / "audio"
    output_dir.mkdir(parents=True, exist_ok=True)
    support_dir = output_dir / "assets"
    subtitle_dir = output_dir / "subtitles"
    support_dir.mkdir(exist_ok=True)
    subtitle_dir.mkdir(exist_ok=True)
    logger = RunLogger(support_dir / "run.log", debug=options.debug)
    runtime_tools = _bundle_audio_runtime_tools(support_dir, logger)
    word_segments = load_word_timed_segments(subtitle)
    line_metadata = load_analysis_line_metadata(subtitle)

    subtitle_path = subtitle_dir / options.subtitle_name
    shutil.copy2(subtitle, subtitle_path)
    ja_subtitle = maybe_copy_original_subtitle(subtitle, subtitle_dir)
    bilingual_subtitle = maybe_write_bilingual_subtitle(subtitle_path, ja_subtitle, subtitle_dir)
    original_blocks = read_srt_blocks(ja_subtitle) if ja_subtitle else []
    original_lookup = build_srt_lookup(original_blocks)
    cues = []
    subtitle_blocks = read_srt_blocks(subtitle_path)
    word_segments, metadata_for_blocks = align_display_auxiliary_data(
        subtitle_blocks, word_segments, line_metadata, logger
    )
    for index, block in enumerate(subtitle_blocks):
        original = match_srt_block(block, original_lookup)
        cue = {
            "start": block["start"],
            "end": block["end"],
            "original": original["text"] if original else "",
            "translation": block["text"],
        }
        cue.update(display_status_metadata(metadata_for_blocks[index] if metadata_for_blocks else {}))
        cues.append(cue)

    display_cues = split_display_cues(cues, word_segments, logger)
    audio_mode_subtitle = output_dir / AUDIO_MODE_SUBTITLE_FILENAME
    audio_mode_ass = support_dir / "audio_mode.bilingual.ass"
    write_display_subtitles(display_cues, audio_mode_subtitle, audio_mode_ass)
    audio_player_path = output_dir / AUDIO_PLAYER_FILENAME
    write_audio_subtitle_player(audio_player_path, audio, audio_mode_subtitle)
    audio_player_launcher_path = output_dir / AUDIO_PLAYER_LAUNCHER_FILENAME
    write_overlay_launcher(audio_player_launcher_path, audio_player_path.name, subtitle=audio_mode_subtitle, audio=audio)
    audio_readme_path = output_dir / "README_play.txt"
    write_audio_readme(
        audio_readme_path,
        audio.name,
        audio_player_path.name,
        audio_mode_subtitle.name,
        audio_player_launcher_path.name,
        runtime_tools=runtime_tools,
    )
    manifest_path = support_dir / "manifest.json"
    manifest_path.write_text(json.dumps({
        "mode": "audio",
        "transcript_run_id": infer_transcript_run_id_from_analysis(subtitle),
        "audio": str(audio.resolve()),
        "analysis_subtitle": str(subtitle.resolve()),
        "audio_player": str(audio_player_path.resolve()),
        "audio_launcher": str(audio_player_launcher_path.resolve()),
        "audio_subtitle": str(audio_mode_subtitle.resolve()),
        "runtime_tools": {
            name: f"assets/{path.name}" for name, path in runtime_tools.items()
        },
        "display_cues_before_split": len(cues),
        "display_cues_after_split": len(display_cues),
        "audio_controls": ["play_pause", "timeline_seek", "seek_minus_10", "seek_plus_10", "keyboard_seek"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if group:
        write_media_index(group)
    return {
        "output_dir": output_dir,
        "audio": audio,
        "subtitle": subtitle_path,
        "bilingual_subtitle": bilingual_subtitle,
        "audio_subtitle": audio_mode_subtitle,
        "readme": audio_readme_path,
        "audio_player": audio_player_path,
        "audio_launcher": audio_player_launcher_path,
        "audio_readme": audio_readme_path,
        "log": support_dir / "run.log",
    }


def create_video_preview(options: PreviewOptions) -> dict[str, Path]:
    if options.mode not in {"video", "potplayer"}:
        raise AppError("preview 支持 --mode video。")
    ensure_ffmpeg_for_preview()
    if not command_exists("ffmpeg"):
        raise AppError(
            "未找到 ffmpeg。\n"
            "请安装 ffmpeg 并加入 PATH，或确认项目 tools/ffmpeg.exe 所在目录已加入 PATH。"
        )

    audio = options.audio.expanduser()
    subtitle = options.subtitle.expanduser()
    cover = options.cover.expanduser() if options.cover else find_cover()
    if cover is None or not cover.exists() or not cover.is_file():
        cover = default_thumbnail_path()
    if not audio.exists() or not audio.is_file():
        raise AppError(f"音频文件不存在或不可读取：{audio}")
    if not subtitle.exists() or not subtitle.is_file():
        raise AppError(f"字幕文件不存在或不可读取：{subtitle}")
    if cover is None or not cover.exists() or not cover.is_file():
        raise AppError("封面图不存在，且内置默认缩略图不可用。")

    width, height = parse_resolution(options.resolution)
    run_id = generate_run_id()
    if options.output_dir:
        output_dir = options.output_dir.expanduser()
    else:
        group_dir = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
        if group_dir:
            output_dir = group_dir / "video"
        else:
            output_dir = project_root() / "outputs" / "previews" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    support_dir = output_dir / "assets"
    subtitle_dir = output_dir / "subtitles"
    support_dir.mkdir(exist_ok=True)
    subtitle_dir.mkdir(exist_ok=True)
    logger = RunLogger(support_dir / "run.log", debug=options.debug)
    word_segments = load_word_timed_segments(subtitle)
    line_metadata = load_analysis_line_metadata(subtitle)

    video_path = output_dir / options.video_name
    subtitle_path = subtitle_dir / options.subtitle_name
    auto_subtitle_path = output_dir / f"{video_path.stem}.srt"
    cover_path = support_dir / "cover.jpg"
    readme_path = output_dir / "README_play.txt"

    prepare_cover(cover, cover_path, logger)
    shutil.copy2(subtitle, subtitle_path)
    ja_subtitle = maybe_copy_original_subtitle(subtitle, subtitle_dir)
    bilingual_subtitle = maybe_write_bilingual_subtitle(subtitle_path, ja_subtitle, subtitle_dir)
    original_blocks = read_srt_blocks(ja_subtitle) if ja_subtitle else []
    original_lookup = build_srt_lookup(original_blocks)
    display_srt = subtitle_dir / "display.bilingual.srt"
    burn_subtitle = subtitle_dir / "display.bilingual.ass"
    cues = []
    subtitle_blocks = read_srt_blocks(subtitle_path)
    word_segments, metadata_for_blocks = align_display_auxiliary_data(
        subtitle_blocks, word_segments, line_metadata, logger
    )
    for index, block in enumerate(subtitle_blocks):
        original = match_srt_block(block, original_lookup)
        cue = {
            "start": block["start"],
            "end": block["end"],
            "original": original["text"] if original else "",
            "translation": block["text"],
        }
        cue.update(display_status_metadata(metadata_for_blocks[index] if metadata_for_blocks else {}))
        cues.append(cue)
    display_cues = split_display_cues(cues, word_segments, logger)
    write_display_subtitles(display_cues, display_srt, burn_subtitle)
    floating_overlay_path = output_dir / options.floating_overlay_name
    write_floating_subtitle_overlay(floating_overlay_path, display_srt)
    floating_overlay_launcher_path = output_dir / OVERLAY_LAUNCHER_FILENAME
    write_overlay_launcher(floating_overlay_launcher_path, floating_overlay_path.name, subtitle=display_srt)
    group = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
    audio_output_dir = (group / "audio") if group else (output_dir / "audio")
    audio_output_dir.mkdir(parents=True, exist_ok=True)
    audio_support_dir = audio_output_dir / "assets"
    audio_support_dir.mkdir(parents=True, exist_ok=True)
    runtime_tools = _bundle_audio_runtime_tools(audio_support_dir, logger)
    audio_mode_subtitle = audio_output_dir / AUDIO_MODE_SUBTITLE_FILENAME
    shutil.copy2(display_srt, audio_mode_subtitle)
    audio_player_path = audio_output_dir / AUDIO_PLAYER_FILENAME
    write_audio_subtitle_player(audio_player_path, audio, audio_mode_subtitle)
    audio_player_launcher_path = audio_output_dir / AUDIO_PLAYER_LAUNCHER_FILENAME
    write_overlay_launcher(audio_player_launcher_path, audio_player_path.name, subtitle=audio_mode_subtitle, audio=audio)
    audio_readme_path = audio_output_dir / "README_play.txt"
    write_audio_readme(
        audio_readme_path,
        audio.name,
        audio_player_path.name,
        audio_mode_subtitle.name,
        audio_player_launcher_path.name,
        runtime_tools=runtime_tools,
    )
    build_preview_video(
        audio=audio,
        cover=cover_path,
        subtitle=burn_subtitle,
        output=video_path,
        width=width,
        height=height,
        logger=logger,
    )
    # Only remove the legacy auto-loaded subtitle after the burned video succeeds.
    auto_subtitle_path.unlink(missing_ok=True)
    write_readme(
        readme_path,
        video_path.name,
        "subtitles/" + subtitle_path.name,
        floating_overlay_path.name,
        audio_player_path=audio_player_path,
        floating_launcher_name=floating_overlay_launcher_path.name,
        audio_launcher_path=audio_player_launcher_path,
    )
    (support_dir / "manifest.json").write_text(json.dumps({
        "transcript_run_id": infer_transcript_run_id_from_analysis(subtitle),
        "audio": str(audio.resolve()), "analysis_subtitle": str(subtitle.resolve()),
        "floating_overlay": str(floating_overlay_path.resolve()),
        "floating_overlay_launcher": str(floating_overlay_launcher_path.resolve()),
        "audio_player": str(audio_player_path.resolve()),
        "audio_launcher": str(audio_player_launcher_path.resolve()),
        "audio_subtitle": str(audio_mode_subtitle.resolve()),
        "audio_runtime_tools": {
            name: f"audio/assets/{path.name}" for name, path in runtime_tools.items()
        },
        "display_cues_before_split": len(cues),
        "display_cues_after_split": len(display_cues),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if group:
        write_media_index(group)
    return {
        "output_dir": output_dir,
        "video": video_path,
        "subtitle": subtitle_path,
        "bilingual_subtitle": bilingual_subtitle,
        "display_subtitle": display_srt,
        "cover": cover_path,
        "readme": readme_path,
        "floating_overlay": floating_overlay_path,
        "floating_overlay_launcher": floating_overlay_launcher_path,
        "audio_player": audio_player_path,
        "audio_launcher": audio_player_launcher_path,
        "audio_subtitle": audio_mode_subtitle,
        "audio_readme": audio_readme_path,
        "log": support_dir / "run.log",
    }


def ensure_ffmpeg_for_preview() -> None:
    if command_exists("ffmpeg"):
        return
    bundled = tool_path("ffmpeg")
    if bundled:
        os.environ["PATH"] = str(bundled.parent.resolve()) + os.pathsep + os.environ.get("PATH", "")


def _find_runtime_tool(name: str) -> Path | None:
    bundled = tool_path(name)
    if bundled:
        return bundled
    discovered = shutil.which(name)
    return Path(discovered) if discovered else None


def _bundle_audio_runtime_tools(support_dir: Path, logger: RunLogger) -> dict[str, Path]:
    """Copy ffplay/ffprobe beside generated players when available.

    The generated player still retains PATH and explicit-override fallbacks,
    but a preview package created on this machine no longer needs those tools
    to be installed globally on the machine where it is played.
    """
    support_dir.mkdir(parents=True, exist_ok=True)
    bundled: dict[str, Path] = {}
    for name in ("ffplay", "ffprobe"):
        source = _find_runtime_tool(name)
        if source is None or not source.exists() or not source.is_file():
            logger.write(f"warning: 未找到 {name}.exe，播放器将保留 PATH 回退。")
            continue
        target_name = f"{name}.exe" if os.name == "nt" else name
        target = support_dir / target_name
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        bundled[name] = target
        logger.write(f"已内置播放器运行时：{target.name}")
    return bundled


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
    subtitle: Path,
    output: Path,
    width: int,
    height: int,
    logger: RunLogger,
) -> None:
    subtitle_filter = build_burn_subtitle_filter(subtitle)
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:in_range=pc:out_range=tv,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"{subtitle_filter},"
        "format=yuv420p,setparams=range=tv"
    )
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-framerate",
        "25",
        "-i",
        str(cover),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-tune",
        "stillimage",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
    ]
    audio_duration = probe_media_duration(audio, logger)
    if audio_duration is None or audio_duration <= 0:
        raise AppError(
            "无法读取原始音频时长，已停止生成预览，避免生成被截短的视频。"
        )
    # The subtitle timeline normally ends at the last spoken line, while the
    # audio may contain trailing silence. Use the audio duration explicitly;
    # never use -shortest here because the burned subtitle filter can finish
    # before the audio does.
    command.extend(["-t", f"{audio_duration:.3f}"])
    command.extend(
        [
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(output),
        ]
    )
    result = run_subprocess(command, logger, stream_output=True)
    if result.returncode != 0:
        raise AppError(
            "生成字幕视频 MP4 失败。\n"
            f"ffmpeg 输出：{(result.stderr or result.stdout).strip()}"
        )
    if not output.exists() or output.stat().st_size == 0:
        raise AppError("ffmpeg 已结束，但 live_preview.mp4 没有生成或为空。")
    validate_preview_video(output, audio_duration, logger)


def build_burn_subtitle_filter(subtitle: Path) -> str:
    """Build a libass filter that burns readable subtitles into the video."""
    subtitle_path = escape_filter_path(subtitle)
    if subtitle.suffix.lower() == ".ass":
        return f"ass=filename={subtitle_path}"
    return f"subtitles=filename={subtitle_path}:force_style='{SUBTITLE_FORCE_STYLE}'"


def escape_filter_path(path: Path) -> str:
    """Escape a Windows/Unicode path for ffmpeg's filtergraph parser."""
    value = path.expanduser().resolve().as_posix()
    # Escape the option value first, then the surrounding filtergraph. Avoid
    # quoted strings: an apostrophe inside them terminates ffmpeg's quoting.
    value = re.sub(r"([\\':])", r"\\\1", value)
    return re.sub(r"([\\'\[\],;\s])", r"\\\1", value)


def probe_media_duration(path: Path, logger: RunLogger) -> float | None:
    """Read media duration without decoding the whole input."""
    text = probe_media_text(path, logger)
    match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_media_text(path: Path, logger: RunLogger) -> str:
    result = run_subprocess(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-t", "0", "-f", "null", os.devnull],
        logger,
    )
    return "\n".join(part for part in (result.stderr, result.stdout) if part)


def validate_preview_video(path: Path, expected_duration: float, logger: RunLogger) -> None:
    """Fail loudly when the generated file is empty, truncated, or not media."""
    text = probe_media_text(path, logger)
    actual_duration = parse_media_duration(text)
    if actual_duration is None:
        raise AppError(f"预览成品校验失败：无法读取视频时长：{path}")

    tolerance = max(1.0, expected_duration * 0.001)
    if abs(actual_duration - expected_duration) > tolerance:
        raise AppError(
            "预览成品校验失败：视频时长与原始音频不一致。\n"
            f"原始音频：{expected_duration:.3f}s；成品视频：{actual_duration:.3f}s。"
        )
    if not re.search(r"Stream #\d+:\d+.*Video:", text):
        raise AppError("预览成品校验失败：MP4 中没有视频流。")
    if not re.search(r"Stream #\d+:\d+.*Audio:", text):
        raise AppError("预览成品校验失败：MP4 中没有音频流。")
    logger.write(
        f"预览成品校验通过：duration={actual_duration:.3f}s，"
        "视频流和音频流均存在，字幕已烧录到视频画面。"
    )


def parse_media_duration(text: str) -> float | None:
    match = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


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
    transcript_dirs.append(project_root() / "outputs" / "transcripts")
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


def write_readme(
    path: Path,
    video_name: str,
    subtitle_name: str,
    floating_overlay_name: str | None = OVERLAY_FILENAME,
    audio_player_path: Path | None = None,
    learning_notes: dict[str, Path] | None = None,
    study_subtitle: Path | None = None,
    floating_launcher_name: str | None = None,
    audio_launcher_path: Path | None = None,
) -> None:
    content = f"""字幕视频使用说明

直接打开 {video_name}，使用系统默认播放器或任何支持 MP4 的播放器。
字幕已经烧录，播放时无需加载外挂字幕。

目录：
- {video_name}：最终视频（封面背景、音频、日中字幕）。
- subtitles/：细分原文／译文字幕，以及按连续语音合并的 display.bilingual.srt / .ass。
- assets/：封面、生成日志与关联信息。
"""
    if floating_overlay_name:
        content += f"""
悬挂字幕框：
- {floating_launcher_name or floating_overlay_name}：直接点击启动独立的 Windows Tkinter 悬挂字幕框，样式对齐 Gemini Live Translator 的 compact 两行模式。
  从视频时间轴开头显示；若视频从第 N 秒开始播放，可用 python {floating_overlay_name} --start N。
- {floating_overlay_name}：播放器脚本本体；如果系统已关联 Python，也可以直接双击。
  默认读取 subtitles/display.bilingual.srt，可用 --srt 指定其他字幕文件。
"""
    if audio_player_path:
        audio_relative = Path("..") / "audio" / audio_player_path.name
        audio_subtitle_relative = Path("..") / "audio" / AUDIO_MODE_SUBTITLE_FILENAME
        content += f"""
音频模式：
- {(audio_relative.parent / audio_launcher_path.name).as_posix() if audio_launcher_path else audio_relative.as_posix()}：直接点击启动独立音频 + 下方悬挂双语字幕框，不打开视频画面，也不依赖 PotPlayer/VLC。
- {audio_relative.as_posix()}：播放器脚本本体；音频文件和字幕位于 ../audio/。
- {audio_subtitle_relative.as_posix()}：音频模式使用的双语时间轴字幕文件。
  播放器支持播放/暂停、进度条拖动、-10s/+10s 快进后退；空格暂停，方向键快进/后退。
"""
    content += """

显示字幕按连续语音合并，最多约9秒一组，明显停顿处断开；时间相对所选音频片段。
需要改字幕时，请编辑字幕并重新生成视频，播放器的字幕偏移不能修改已烧录的画面。
总结与学习笔记在同一媒体任务的 analysis/ 中，源音频保留在 audio/ 中。
"""
    path.write_text(content, encoding="utf-8")


def write_audio_readme(
    path: Path,
    audio_name: str,
    player_name: str,
    subtitle_name: str,
    launcher_name: str | None = None,
    runtime_tools: dict[str, Path] | None = None,
) -> None:
    bundled_runtime = ""
    if runtime_tools:
        names = ", ".join(path.name for path in runtime_tools.values())
        bundled_runtime = f"\n- assets/：播放器已随包内置 {names}，不依赖目标机器的 FFmpeg PATH。"
    else:
        bundled_runtime = "\n- assets/：如果包内没有 ffplay/ffprobe，播放器会回退到系统 PATH 或 --ffplay 指定路径。"
    path.write_text(
        f"""音频 + 悬挂字幕使用说明

直接双击 {launcher_name or player_name}，程序会播放 {audio_name}，同时在屏幕下方显示独立的双语悬挂字幕框。
不打开视频画面，也不依赖 PotPlayer/VLC；音频由隐藏的 ffplay 音频引擎播放。

文件：
- {audio_name}：源音频。
- {subtitle_name}：音频模式双语时间轴。
- {launcher_name or player_name}：Windows 直接启动入口，不要求系统关联 .pyw 文件。
- {player_name}：独立 Python 图形窗口播放器。
{bundled_runtime}

播放器控制：
- 播放/暂停按钮：暂停或继续音频。
- 进度条：拖动到任意位置后跳转。
- -10s / +10s：后退或快进 10 秒。
- 空格：播放/暂停；左右方向键：后退/快进 5 秒；Shift+左右方向键：后退/快进 30 秒。

可选命令：
- python {player_name} --start 125：从第 125 秒开始。
- python {player_name} --srt 其他字幕.srt：改用其他时间轴。
- python {player_name} --ffplay D:\\path\\ffplay.exe：指定 ffplay 路径。

空白字幕时段是原始时间轴中的无语音区间，音频仍会继续播放。
""",
        encoding="utf-8",
    )


# Keep older CLI integrations working.
create_potplayer_preview = create_video_preview
