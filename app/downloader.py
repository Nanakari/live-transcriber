from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import project_root
from .utils import AppError, RunLogger, run_subprocess


def _yt_dlp_command() -> list[str] | None:
    root = project_root()
    local_exe = root / ".venv" / "Scripts" / "yt-dlp.exe"
    if local_exe.exists():
        return [str(local_exe)]

    if not getattr(sys, "frozen", False):
        try:
            import yt_dlp  # noqa: F401

            return [sys.executable, "-m", "yt_dlp"]
        except Exception:
            pass

    found = shutil.which("yt-dlp")
    return [found] if found else None


def _ffmpeg_location() -> str | None:
    root = project_root()
    local = root / "tools" / "ffmpeg.exe"
    if local.exists():
        return str(local.parent)
    found = shutil.which("ffmpeg")
    return str(Path(found).parent) if found else None


def _ffmpeg_command() -> str | None:
    root = project_root()
    local = root / "tools" / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg")

def _add_ffmpeg_location(command: list[str]) -> None:
    location = _ffmpeg_location()
    if location:
        command.extend(["--ffmpeg-location", location])


def _safe_filename_part(value: str, *, max_len: int = 56) -> str:
    value = re.sub(r'[\/:*?"<>|]+', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:max_len].strip(" ._") if value else ""


def _compact_date(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return ""


def _build_media_stem(info: dict[str, object], run_id: str) -> str:
    channel = _safe_filename_part(str(info.get("channel") or info.get("uploader") or ""), max_len=32)
    title = _safe_filename_part(str(info.get("title") or ""), max_len=72)
    date = _compact_date(str(info.get("upload_date") or info.get("release_date") or ""))
    parts = [part for part in (channel, date, title, run_id[-4:]) if part]
    return "_".join(parts) if parts else run_id


def _add_cookie_options(command: list[str], cookies_from_browser: str | None, cookies: str | None) -> None:
    # Prefer an explicit cookies.txt file. Browser-cookie extraction can fail on Windows DPAPI.
    if cookies:
        command.extend(["--cookies", cookies])
        return
    if cookies_from_browser:
        command.extend(["--cookies-from-browser", cookies_from_browser])


def _add_remote_components(command: list[str], remote_components: str | None) -> None:
    if remote_components:
        command.extend(["--remote-components", remote_components])

class DownloadSection(NamedTuple):
    start: str
    end: str


def _time_to_seconds(value: str, label: str) -> float:
    text = value.strip()
    if not text:
        raise AppError(f"{label} 不能为空。")
    parts = text.split(":")
    try:
        if len(parts) == 1:
            seconds = float(parts[0])
        elif len(parts) == 2:
            minutes = int(parts[0])
            seconds = minutes * 60 + float(parts[1])
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = hours * 3600 + minutes * 60 + float(parts[2])
        else:
            raise ValueError
    except ValueError as exc:
        raise AppError(f"{label} 格式无效：{value}。请使用秒数或 HH:MM:SS，例如 90、1:30、00:01:30。") from exc
    if seconds < 0:
        raise AppError(f"{label} 不能小于 0。")
    return seconds


def _format_section_time(seconds: float) -> str:
    seconds = float(seconds)
    if seconds.is_integer():
        return str(int(seconds))
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def _format_duration_label(seconds: float) -> str:
    total = max(0, int(seconds))
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"

def _metadata_duration_seconds(info: dict[str, object]) -> float | None:
    value = info.get("duration")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    text = str(info.get("duration_string") or "").strip()
    if text:
        try:
            return _time_to_seconds(text, "视频总时长")
        except AppError:
            return None
    return None


def make_download_section(start: str | None, end: str | None, duration_seconds: float | None = None) -> DownloadSection | None:
    start_text = (start or "").strip()
    end_text = (end or "").strip()
    if not start_text and not end_text:
        return None
    start_seconds = _time_to_seconds(start_text, "下载开始时间") if start_text else 0.0
    if end_text:
        end_seconds = _time_to_seconds(end_text, "下载结束时间")
    elif duration_seconds and duration_seconds > 0:
        end_seconds = duration_seconds
    else:
        raise AppError("只填写下载开始时间时，未能获取视频总时长。请同时填写下载结束时间。")
    if end_seconds <= start_seconds:
        raise AppError("下载结束时间必须大于开始时间。")
    if duration_seconds and duration_seconds > 0:
        duration_label = _format_duration_label(duration_seconds)
        if start_seconds >= duration_seconds:
            raise AppError(
                f"下载开始时间 {_format_duration_label(start_seconds)} 超过视频总时长 {duration_label}。"
                "请确认填的是当前视频内的时间点。"
            )
        if end_seconds > duration_seconds:
            raise AppError(
                f"下载结束时间 {_format_duration_label(end_seconds)} 超过视频总时长 {duration_label}。"
                "请把结束时间调到视频范围内。"
            )
    return DownloadSection(start=_format_section_time(start_seconds), end=_format_section_time(end_seconds))


def _add_download_section(command: list[str], section: DownloadSection | None) -> None:
    if not section:
        return
    command.extend(["--download-sections", f"*{section.start}-{section.end}", "--force-keyframes-at-cuts"])


def _section_duration(section: DownloadSection) -> str:
    duration = float(section.end) - float(section.start)
    if duration <= 0:
        raise AppError("下载结束时间必须大于开始时间。")
    return _format_section_time(duration)

def _duration_from_ffmpeg_output(output: str) -> float | None:
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _probe_media_duration(path: Path, logger: RunLogger) -> float | None:
    ffmpeg = _ffmpeg_command()
    if not ffmpeg:
        return None
    result = run_subprocess([ffmpeg, "-hide_banner", "-i", str(path)], logger)
    output = "\n".join(part for part in (result.stderr, result.stdout) if part)
    duration = _duration_from_ffmpeg_output(output)
    if duration is not None:
        logger.write(f"downloaded media duration={duration:.2f}s")
    else:
        logger.write("无法从 ffmpeg 输出读取下载文件时长。")
    return duration


def _section_is_complete(actual_seconds: float, expected_seconds: float) -> bool:
    # Encoders and container timestamps can differ slightly at cut boundaries.
    tolerance = max(5.0, expected_seconds * 0.02)
    return actual_seconds + tolerance >= expected_seconds


def _output_tail(output: str, *, max_chars: int = 2000) -> str:
    text = output.strip()
    return text[-max_chars:] if len(text) > max_chars else text


def _strip_start_time_query(url: str) -> str:
    parts = urlsplit(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in {"t", "start", "time_continue"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment))

def fetch_video_info(
    url: str,
    proxy: str | None,
    logger: RunLogger,
    *,
    cookies_from_browser: str | None = None,
    cookies: str | None = None,
    remote_components: str | None = None,
) -> dict[str, object]:
    """Best-effort metadata fetch from yt-dlp."""
    yt_dlp = _yt_dlp_command()
    if not yt_dlp:
        return {}

    command = [*yt_dlp, "--dump-single-json", "--skip-download", "--no-playlist", url]
    _add_ffmpeg_location(command)
    _add_cookie_options(command, cookies_from_browser, cookies)
    _add_remote_components(command, remote_components)
    if proxy:
        command.extend(["--proxy", proxy])
    result = run_subprocess(command, logger)
    if result.returncode != 0:
        logger.write("获取视频信息失败：" + (result.stderr or result.stdout))
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.write(f"解析视频信息失败：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def fetch_title(
    url: str,
    proxy: str | None,
    logger: RunLogger,
    *,
    cookies_from_browser: str | None = None,
    cookies: str | None = None,
    remote_components: str | None = None,
) -> str:
    """Best-effort title fetch from yt-dlp."""
    return str(
        fetch_video_info(
            url,
            proxy,
            logger,
            cookies_from_browser=cookies_from_browser,
            cookies=cookies,
            remote_components=remote_components,
        ).get("title")
        or ""
    )



def _download_audio_section_with_ffmpeg(
    yt_dlp: list[str],
    url: str,
    output_path: Path,
    proxy: str | None,
    logger: RunLogger,
    section: DownloadSection,
    *,
    cookies_from_browser: str | None = None,
    cookies: str | None = None,
    remote_components: str | None = None,
) -> None:
    ffmpeg = _ffmpeg_command()
    if not ffmpeg:
        raise AppError("未找到 ffmpeg，无法下载指定时间段。请确认 tools\\ffmpeg.exe 存在或 ffmpeg 在 PATH 中。")

    expected_seconds = float(_section_duration(section))
    max_attempts = 3
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        logger.write(f"section download attempt={attempt}/{max_attempts}")
        output_path.unlink(missing_ok=True)

        # Refresh the signed URL on every attempt; YouTube media URLs are short-lived.
        get_url_command = [*yt_dlp, "-f", "bestaudio/best", "--no-playlist", "-g", url]
        _add_ffmpeg_location(get_url_command)
        _add_cookie_options(get_url_command, cookies_from_browser, cookies)
        _add_remote_components(get_url_command, remote_components)
        if proxy:
            get_url_command.extend(["--proxy", proxy])
        result = run_subprocess(get_url_command, logger)
        if result.returncode != 0:
            last_error = (result.stderr or result.stdout).strip()
            logger.write(f"获取音频直连失败 attempt={attempt}: {_output_tail(last_error)}")
        else:
            media_url = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
            if not media_url:
                last_error = "yt-dlp 没有返回可用于 ffmpeg 的音频直连地址。"
            else:
                command = [
                    ffmpeg, "-y", "-ss", section.start,
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_at_eof", "1",
                    "-reconnect_delay_max", "10",
                ]
                if proxy:
                    command.extend(["-http_proxy", proxy])
                command.extend([
                    "-i", media_url,
                    "-t", _format_section_time(expected_seconds),
                    "-progress", "pipe:1", "-nostats",
                    "-vn", "-c:a", "aac", "-b:a", "192k", str(output_path),
                ])
                result = run_subprocess(command, logger, stream_output=True)
                ffmpeg_output = "\n".join(part for part in (result.stderr, result.stdout) if part)
                if result.returncode != 0:
                    last_error = _output_tail(ffmpeg_output)
                    logger.write(f"ffmpeg 下载失败 attempt={attempt}: {last_error}")
                elif not output_path.exists() or output_path.stat().st_size < 1024:
                    last_error = "下载文件不存在或小于 1KB。"
                    logger.write(f"下载文件无效 attempt={attempt}: {last_error}")
                else:
                    actual_seconds = _probe_media_duration(output_path, logger)
                    if actual_seconds is None:
                        last_error = "无法确认下载文件的实际时长。"
                    elif _section_is_complete(actual_seconds, expected_seconds):
                        logger.write(
                            f"section download complete expected={expected_seconds:.2f}s "
                            f"actual={actual_seconds:.2f}s"
                        )
                        return
                    else:
                        last_error = (
                            f"下载提前结束：请求 {expected_seconds:.2f} 秒，"
                            f"实际仅 {actual_seconds:.2f} 秒。"
                        )
                        logger.write(
                            f"{last_error}\nffmpeg output tail:\n{_output_tail(ffmpeg_output)}"
                        )

        output_path.unlink(missing_ok=True)
        if attempt < max_attempts:
            delay = attempt * 2
            logger.write(f"将在 {delay} 秒后重新获取直链并重试。")
            time.sleep(delay)

    raise AppError(
        f"指定时间段下载不完整，已重试 {max_attempts} 次。\n"
        f"目标区间：{section.start}-{section.end}（{expected_seconds:.2f} 秒）。\n"
        f"最后错误：{last_error}\n"
        "请检查代理稳定性，或临时关闭代理后重试。"
    )

def download_audio(
    url: str,
    audio_dir: Path,
    run_id: str,
    proxy: str | None,
    logger: RunLogger,
    *,
    cookies_from_browser: str | None = None,
    cookies: str | None = None,
    remote_components: str | None = None,
    download_start: str | None = None,
    download_end: str | None = None,
) -> tuple[Path, str]:
    """Download best audio with yt-dlp and return the created source file and title."""
    yt_dlp = _yt_dlp_command()
    if not yt_dlp:
        raise AppError(
            "未找到 yt-dlp。\n"
            "请先安装：python -m pip install yt-dlp\n"
            "如果已安装，请确认 yt-dlp 在 PATH 中。"
        )

    info = fetch_video_info(
        url,
        proxy,
        logger,
        cookies_from_browser=cookies_from_browser,
        cookies=cookies,
        remote_components=remote_components,
    )
    title = str(info.get("title") or "")
    section = make_download_section(download_start, download_end, _metadata_duration_seconds(info))
    download_url = _strip_start_time_query(url)
    if section:
        logger.write(f"download section={section.start}-{section.end}")
    output_stem = _build_media_stem(info, run_id)
    if section:
        section_output = audio_dir / f"{output_stem}_source.m4a"
        _download_audio_section_with_ffmpeg(
            yt_dlp,
            download_url,
            section_output,
            proxy,
            logger,
            section,
            cookies_from_browser=cookies_from_browser,
            cookies=cookies,
            remote_components=remote_components,
        )
        return section_output, title

    output_template = str(audio_dir / f"{output_stem}_source.%(ext)s")
    command = [
        *yt_dlp,
        "-f",
        "bestaudio/best",
        "--no-playlist",
        "--newline",
        "--progress",
        "--progress-delta",
        "1",
        "-x",
        "--audio-format",
        "m4a",
        "-o",
        output_template,
        download_url,
    ]
    _add_ffmpeg_location(command)
    _add_cookie_options(command, cookies_from_browser, cookies)
    _add_remote_components(command, remote_components)
    if proxy:
        command.extend(["--proxy", proxy])

    result = run_subprocess(command, logger, stream_output=True)
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        if "Failed to decrypt with DPAPI" in output:
            raise AppError(
                "浏览器 Cookie 解密失败。\n"
                "这是 Windows DPAPI 限制导致 yt-dlp 无法读取 Edge/Chrome 登录态，不是转写模块错误。\n"
                "请把浏览器 Cookie 导出为 Netscape 格式的 cookies.txt，然后在高级设置里选择 cookies.txt，并把 Cookies from browser 设为“不使用”。\n"
                f"yt-dlp 输出：{output}"
            )
        if "n challenge solving failed" in output or "remote-components" in output or "HTTP Error 403" in output:
            raise AppError(
                "YouTube 下载被 JS challenge 拦截。\n"
                "这不是转写模型错误，而是 yt-dlp 在下载视频数据时没有解出 YouTube 的 n challenge。\n"
                "请在高级设置里把 YouTube JS challenge solver 设为“GitHub solver”，或在命令行添加：--remote-components ejs:github。\n"
                f"yt-dlp 输出：{output}"
            )
        raise AppError(
            "URL 下载失败。\n"
            "可能原因：URL 不正确、视频不可访问、需要登录、网络问题、需要代理、yt-dlp 版本过旧，或 ffmpeg 不可用。\n"
            f"yt-dlp 输出：{output}"
        )

    candidates = sorted(audio_dir.glob(f"{output_stem}_source.*"))
    if not candidates:
        raise AppError("yt-dlp 运行结束，但没有找到下载后的音频文件。请检查 yt-dlp 输出和写入权限。")
    return candidates[0], title


def download_thumbnail(
    url: str,
    thumbnails_dir: Path,
    run_id: str,
    proxy: str | None,
    logger: RunLogger,
    *,
    cookies_from_browser: str | None = None,
    cookies: str | None = None,
    remote_components: str | None = None,
) -> Path | None:
    """Best-effort thumbnail download for preview workflows without changing audio download."""
    yt_dlp = _yt_dlp_command()
    if not yt_dlp:
        raise AppError("未找到 yt-dlp，无法下载封面缩略图。请运行：python -m pip install yt-dlp")
    thumbnails_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(thumbnails_dir / f"{run_id}.%(ext)s")
    command = [
        *yt_dlp,
        "--skip-download",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "-o",
        output_template,
        url,
    ]
    _add_ffmpeg_location(command)
    _add_cookie_options(command, cookies_from_browser, cookies)
    _add_remote_components(command, remote_components)
    if proxy:
        command.extend(["--proxy", proxy])
    result = run_subprocess(command, logger)
    if result.returncode != 0:
        logger.write("thumbnail download failed: " + (result.stderr or result.stdout))
        return None
    candidates = sorted(thumbnails_dir.glob(f"{run_id}.*"))
    return candidates[0] if candidates else None





