from __future__ import annotations

import shutil
import os
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .subtitles import (
    ASS_ORIGINAL_COLOR,
    ASS_OUTLINE_COLOR,
    ASS_TRANSPARENT_COLOR,
    write_display_subtitles,
)
from .config import output_root, project_root, tool_path
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
# Re-export existing helpers while their implementations live in one module.
from .preview_subtitles import (
    split_display_cues,
    word_segments_match_cues,
    align_display_auxiliary_data,
    maybe_copy_original_subtitle,
    infer_transcript_run_id_from_analysis,
    display_status_metadata,
    load_analysis_line_metadata,
    load_word_timed_segments,
    maybe_write_bilingual_subtitle,
    maybe_write_study_subtitle,
    find_related_analysis_json,
    study_notes_for_line,
    rank_items,
    normalize_for_match,
    overlap_score,
    compact_text,
    read_srt_blocks,
    parse_srt_seconds,
    build_srt_lookup,
    match_srt_block,
)
from .preview_state import invalidate_preview, preview_identity
from .media_assets import default_thumbnail_path
from .output_layout import group_dir_from_artifact_path, write_media_index
from .utils import AppError, RunLogger, command_exists, generate_run_id, run_subprocess


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

    source_identity = preview_identity(subtitle)
    run_id = generate_run_id()
    group = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
    if options.output_dir:
        output_dir = options.output_dir.expanduser()
    elif group:
        output_dir = group / "audio"
    else:
        output_dir = output_root(root=project_root()) / "previews" / run_id / "audio"
    output_dir.mkdir(parents=True, exist_ok=True)
    support_dir = output_dir / "assets"
    subtitle_dir = output_dir / "subtitles"
    support_dir.mkdir(exist_ok=True)
    subtitle_dir.mkdir(exist_ok=True)
    invalidate_preview(output_dir)
    if group:
        write_media_index(group)
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
        **source_identity,
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

    source_identity = preview_identity(subtitle)
    width, height = parse_resolution(options.resolution)
    run_id = generate_run_id()
    group = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
    if options.output_dir:
        output_dir = options.output_dir.expanduser()
    else:
        group_dir = group_dir_from_artifact_path(subtitle) or group_dir_from_artifact_path(audio)
        if group_dir:
            output_dir = group_dir / "video"
        else:
            output_dir = output_root(root=project_root()) / "previews" / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    support_dir = output_dir / "assets"
    subtitle_dir = output_dir / "subtitles"
    support_dir.mkdir(exist_ok=True)
    subtitle_dir.mkdir(exist_ok=True)
    invalidate_preview(output_dir)
    if group:
        write_media_index(group)
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
    invalidate_preview(audio_output_dir)
    if group:
        write_media_index(group)
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
    (audio_support_dir / "manifest.json").write_text(json.dumps({
        **source_identity, "mode": "audio",
        "transcript_run_id": infer_transcript_run_id_from_analysis(subtitle),
        "audio": str(audio.resolve()), "audio_subtitle": str(audio_mode_subtitle.resolve()),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
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
        **source_identity,
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
    """Require the audio engine before advertising a playable package."""
    ffplay = _find_runtime_tool("ffplay")
    if ffplay is None or not ffplay.is_file() or ffplay.stat().st_size == 0:
        raise AppError("未找到 ffplay 音频引擎，无法生成可播放的音频包。Windows 源码版请运行 "
                       ".\\.venv\\Scripts\\python.exe scripts\\prepare_tools.py（或重新运行 setup.ps1）；"
                       "发行版请重新完整解压安装包。")
    support_dir.mkdir(parents=True, exist_ok=True)
    bundled: dict[str, Path] = {}
    for name in ("ffplay", "ffprobe"):
        source = ffplay if name == "ffplay" else _find_runtime_tool(name)
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
        output_root(root=project_root()) / "media",
        output_root(root=project_root()) / "thumbnails",
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
