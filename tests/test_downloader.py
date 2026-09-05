from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import downloader
from app.downloader import DownloadSection, _time_to_seconds, make_download_section
from app.utils import AppError
from app.utils import RunLogger


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("90", 90.0),
        ("1:30", 90.0),
        ("05:02:03", 18123.0),
        ("5:2:3", 18123.0),
        ("05：02：03", 18123.0),
        ("５：２：３", 18123.0),
        (" 5 ： 2 ： 3 ", 18123.0),
    ],
)
def test_time_to_seconds_accepts_compatible_formats(value: str, expected: float) -> None:
    assert _time_to_seconds(value, "时间") == expected


@pytest.mark.parametrize("value", ["", "1:2:3:4", "abc", "-1", "nan", "inf"])
def test_time_to_seconds_rejects_invalid_values(value: str) -> None:
    with pytest.raises(AppError):
        _time_to_seconds(value, "时间")


def test_empty_range_means_complete_media() -> None:
    assert make_download_section(None, None) is None


def test_start_only_means_from_start_to_media_end_without_metadata() -> None:
    assert make_download_section("5：2：3", None) == DownloadSection("18123", None)


def test_start_only_uses_known_media_end() -> None:
    assert make_download_section("1:30", "", 300.0) == DownloadSection("90", "300")


def test_end_only_means_from_media_start() -> None:
    assert make_download_section("", "5：2：3") == DownloadSection("0", "18123")


def test_yt_dlp_uses_portable_module_invocation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(downloader, "project_root", lambda: tmp_path)

    assert downloader._yt_dlp_command() == [str(python), "-m", "yt_dlp"]


def test_thumbnail_uses_default_when_ytdlp_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fallback = tmp_path / "fallback.png"
    fallback.write_bytes(b"default-image")
    thumbnails = tmp_path / "thumbnails"
    monkeypatch.setattr(downloader, "_yt_dlp_command", lambda: None)
    monkeypatch.setattr(downloader, "default_thumbnail_path", lambda: fallback)

    result = downloader.download_thumbnail(
        "https://example.invalid/video",
        thumbnails,
        "run-1",
        None,
        RunLogger(tmp_path / "run.log"),
    )

    assert result == thumbnails / "run-1.png"
    assert result.read_bytes() == b"default-image"


def _configure_audio_download_test(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downloader, "_yt_dlp_command", lambda: ["yt-dlp"])
    monkeypatch.setattr(downloader, "fetch_video_info", lambda *args, **kwargs: {})
    monkeypatch.setattr(downloader, "_add_ffmpeg_location", lambda command: None)
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: None)


def test_audio_download_retries_403_with_fresh_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_audio_download_test(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(command: list[str], logger: RunLogger, *, stream_output: bool = False):
        calls.append(command.copy())
        if len(calls) < 3:
            return subprocess.CompletedProcess(command, 1, "HTTP Error 403: Forbidden", "")
        (tmp_path / "un-1_source.m4a").write_bytes(b"audio")
        return subprocess.CompletedProcess(command, 0, "downloaded", "")

    monkeypatch.setattr(downloader, "run_subprocess", fake_run)

    result, _ = downloader.download_audio(
        "https://www.youtube.com/watch?v=test",
        tmp_path,
        "run-1",
        "http://127.0.0.1:7897",
        RunLogger(tmp_path / "run.log"),
    )

    assert result == tmp_path / "un-1_source.m4a"
    assert len(calls) == 3
    assert calls[0] == calls[1] == calls[2]
    log = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "重新提取签名 URL" in log


def test_audio_download_reports_plain_403_after_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_audio_download_test(monkeypatch)
    calls = 0

    def fake_run(command: list[str], logger: RunLogger, *, stream_output: bool = False):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 1, "ERROR: HTTP Error 403: Forbidden", "")

    monkeypatch.setattr(downloader, "run_subprocess", fake_run)

    with pytest.raises(AppError) as exc_info:
        downloader.download_audio(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            "run-1",
            None,
            RunLogger(tmp_path / "run.log"),
        )

    message = str(exc_info.value)
    assert calls == 3
    assert "HTTP 403" in message
    assert "共尝试 3 次" in message
    assert "不代表 JS challenge 一定失败" in message


def test_audio_download_reports_explicit_n_challenge_without_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_audio_download_test(monkeypatch)
    calls = 0

    def fake_run(command: list[str], logger: RunLogger, *, stream_output: bool = False):
        nonlocal calls
        calls += 1
        output = "WARNING: n challenge solving failed; HTTP Error 403: Forbidden"
        return subprocess.CompletedProcess(command, 1, output, "")

    monkeypatch.setattr(downloader, "run_subprocess", fake_run)

    with pytest.raises(AppError) as exc_info:
        downloader.download_audio(
            "https://www.youtube.com/watch?v=test",
            tmp_path,
            "run-1",
            None,
            RunLogger(tmp_path / "run.log"),
        )

    assert calls == 1
    assert "n challenge solving failed" in str(exc_info.value)
