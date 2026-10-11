import hashlib
import zipfile

import pytest

from app import preview
from app.utils import AppError, RunLogger
from scripts import media_tools


def bundle(tmp_path, monkeypatch, *, omit=""):
    archive = tmp_path / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name in media_tools.TOOL_NAMES:
            if name != omit:
                output.writestr(f"ffmpeg/bin/{name}", f"synthetic-{name}".encode())
        output.writestr("ffmpeg/LICENSE", "license")
    monkeypatch.setattr(media_tools, "FFMPEG_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(media_tools.requests, "get", lambda *a, **kw: pytest.fail("valid cache should work offline"))
    return archive


def test_source_setup_fills_missing_tools_without_overwriting_local_ffmpeg(tmp_path, monkeypatch):
    archive = bundle(tmp_path, monkeypatch)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "ffmpeg.exe").write_bytes(b"keep-user-version")
    (tools / "ffplay.exe").touch()  # Interrupted/empty installs are recoverable.
    media_tools.install_media_tools(tools, archive)
    assert (tools / "ffmpeg.exe").read_bytes() == b"keep-user-version"
    assert (tools / "ffplay.exe").read_bytes() == b"synthetic-ffplay.exe"
    assert (tools / "ffprobe.exe").read_bytes() == b"synthetic-ffprobe.exe"
    assert (tools / "licenses/FFmpeg-LICENSE").is_file()
    archive.unlink()
    media_tools.install_media_tools(tools, archive)  # Fully installed tools need no network/cache.


def test_release_stages_all_tools_from_the_same_verified_archive(tmp_path, monkeypatch):
    archive = bundle(tmp_path, monkeypatch)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "ffmpeg.exe").write_bytes(b"stale-build")
    media_tools.install_media_tools(tools, archive, overwrite=True)
    for name in media_tools.TOOL_NAMES:
        assert (tools / name).read_bytes() == f"synthetic-{name}".encode()


def test_missing_archive_member_does_not_publish_partial_tools(tmp_path, monkeypatch):
    archive = bundle(tmp_path, monkeypatch, omit="ffprobe.exe")
    tools = tmp_path / "tools"
    with pytest.raises(RuntimeError, match="ffprobe"):
        media_tools.install_media_tools(tools, archive)
    assert not list(tools.glob("*.exe"))


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_download_or_checksum_never_installs_tools(tmp_path, monkeypatch, interrupted):
    archive = tmp_path / "cached.zip"
    archive.write_bytes(b"invalid-old-cache")
    class Download:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def raise_for_status(self):
            pass
        def iter_content(self, size):
            yield b"partial-or-corrupt-download"
            if interrupted:
                raise ConnectionError("interrupted")
    monkeypatch.setattr(media_tools.requests, "get", lambda *a, **kw: Download())
    with pytest.raises((ConnectionError, RuntimeError)):
        media_tools.install_media_tools(tmp_path / "tools", archive)
    assert archive.read_bytes() == b"invalid-old-cache"
    assert not list((tmp_path / "tools").glob("*.exe"))
    assert not list(tmp_path.glob("ffmpeg-download-*"))


def test_missing_ffplay_rejects_preview_instead_of_reporting_success(tmp_path, monkeypatch):
    audio = tmp_path / "audio.wav"
    subtitle = tmp_path / "subtitles.srt"
    audio.write_bytes(b"synthetic audio")
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nTest\n", encoding="utf-8")
    monkeypatch.setattr(preview, "_find_runtime_tool", lambda name: None)
    output = tmp_path / "package"
    with pytest.raises(AppError, match="ffplay.*prepare_tools",):
        preview.create_audio_preview(preview.PreviewOptions(audio=audio, subtitle=subtitle,
            cover=None, output_dir=output, resolution="1280x720", subtitle_name="live_preview.zh.srt",
            video_name="live_preview.mp4", mode="audio"))
    assert not (output / "run_audio_subtitle_player.cmd").exists()
    assert "analysis_fingerprint" not in (output / "assets/manifest.json").read_text(encoding="utf-8")


def test_preview_copies_both_engines_for_minimal_path_playback(tmp_path, monkeypatch):
    engine_dir = tmp_path / "tools"
    engine_dir.mkdir()
    for name in ("ffplay", "ffprobe"):
        (engine_dir / name).write_bytes(name.encode())
    monkeypatch.setattr(preview, "_find_runtime_tool", lambda name: engine_dir / name)
    assets = tmp_path / "assets"
    bundled = preview._bundle_audio_runtime_tools(assets, RunLogger(tmp_path / "run.log"))
    assert set(bundled) == {"ffplay", "ffprobe"}
    for name, target in bundled.items():
        assert target.parent == assets and target.read_bytes() == name.encode()


def test_diagnostics_reports_missing_player_even_when_ffmpeg_exists(tmp_path, monkeypatch):
    from app.web import routes
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.setattr(routes, "command_exists", lambda name: False)
    monkeypatch.setattr(routes, "tool_path", lambda name: tmp_path / name if name == "ffmpeg" else None)
    monkeypatch.setattr(routes, "_module_available", lambda *a: True)
    monkeypatch.setattr(routes, "is_cuda_available", lambda: False)
    status = routes.status()
    assert status["ffmpeg"] is True
    assert status["ffplay"] is False and status["ffprobe"] is False


def test_explicit_player_environment_remains_a_valid_dependency_source(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "resource_root", lambda: tmp_path)
    monkeypatch.setattr(config, "project_root", lambda: tmp_path)
    monkeypatch.setattr(config.sys, "executable", str(tmp_path / "python.exe"))
    ffplay = tmp_path / "custom player.exe"
    ffplay.write_bytes(b"synthetic player")
    monkeypatch.setenv("LIVE_TRANSCRIBER_FFPLAY", str(ffplay))
    assert preview._find_runtime_tool("ffplay") == ffplay
