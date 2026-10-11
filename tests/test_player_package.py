import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import player_package
from app.preview_state import preview_identity, preview_is_current
from app.utils import AppError
from app.web import routes
from app.web.server import create_app


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    group = tmp_path / "outputs/media/example"
    audio_dir = group / "audio"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "example_source.wav"
    audio.write_bytes(b"synthetic audio")
    transcript = group / "transcripts/example_transcript.json"
    transcript.parent.mkdir()
    transcript.write_text(json.dumps({"meta": {"run_id": "example", "source_audio_file": str(audio)}}))
    analysis = group / "analysis/old"
    analysis.mkdir(parents=True)
    (analysis / "analysis.json").write_text(json.dumps({"meta": {"transcript_run_id": "example"}}))
    for name in ("bilingual.md", "review.md"):
        (analysis / name).write_text("synthetic")
    subtitle = analysis / "translation_zh.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    launcher = audio_dir / "run_audio_subtitle_player.cmd"
    launcher.write_text("synthetic launcher")
    (audio_dir / "audio_subtitle_player.pyw").write_text("# synthetic player")
    (audio_dir / "audio_mode.bilingual.srt").write_bytes(subtitle.read_bytes())
    assets = audio_dir / "assets"
    assets.mkdir()
    manifest = {**preview_identity(subtitle), "transcript_run_id": "example", "audio": str(audio)}
    (assets / "manifest.json").write_text(json.dumps(manifest))
    os.utime(analysis, (100, 100))
    return group, audio, subtitle, launcher


def updated_analysis(group, subtitle):
    new = group / "analysis/new"
    new.mkdir()
    for file in subtitle.parent.iterdir():
        (new / file.name).write_bytes(file.read_bytes())
    result = new / "translation_zh.srt"
    result.write_text("1\n00:00:00,000 --> 00:00:01,000\n更新译文\n", encoding="utf-8")
    os.utime(new, (200, 200))
    return result


def test_reanalysis_marks_old_player_stale_and_rejects_playback(media, monkeypatch):
    group, audio, subtitle, launcher = media
    assert not routes._artifact_sets(routes.project_root())[0]["preview"]["stale"]
    new_subtitle = updated_analysis(group, subtitle)
    item = routes._artifact_sets(routes.project_root())[0]
    assert Path(item["analysis"]["path"]).name == "new"
    assert item["preview"]["audio_stale"]
    monkeypatch.setattr(routes, "open_file", lambda _: pytest.fail("Opened stale player"))
    with pytest.raises(HTTPException) as error:
        routes.open_output_file({"path": str(launcher)})
    assert error.value.status_code == 409
    # Simulate regenerating the player against the current analysis.
    manifest_path = audio.parent / "assets/manifest.json"
    manifest_path.write_text(json.dumps({**preview_identity(new_subtitle), "transcript_run_id": "example"}))
    assert not routes._artifact_sets(routes.project_root())[0]["preview"]["audio_stale"]


def test_identity_includes_repair_metadata_and_legacy_needs_regeneration(media):
    _, audio, subtitle, _ = media
    assert preview_is_current(audio.parent, subtitle)
    metadata = subtitle.parent / "analysis.json"
    metadata.write_text(metadata.read_text() + "\n")
    assert not preview_is_current(audio.parent, subtitle)
    (audio.parent / "assets/manifest.json").write_text("{}")
    assert not preview_is_current(audio.parent, subtitle)


def fake_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "build-runtime"
    internal = runtime / "_internal"
    internal.mkdir(parents=True)
    executable = runtime / "LiveTranscriberPlayer.exe"
    executable.write_bytes(b"synthetic executable")
    (internal / "python310.dll").write_bytes(b"synthetic dependency")
    tools = runtime / "tools"
    tools.mkdir()
    for name in ("ffplay", "ffprobe"):
        (tools / f"{name}.exe").write_bytes(name.encode())
    monkeypatch.setattr(player_package, "_player_runtime", lambda: (executable, internal))
    monkeypatch.setattr(player_package, "tool_path", lambda name: tools / f"{name}.exe")


def test_player_export_contains_runtime_media_and_relative_launcher(media, tmp_path, monkeypatch):
    fake_runtime(tmp_path, monkeypatch)
    group, audio, subtitle, _ = media
    with TestClient(create_app()) as client:
        response = client.get("/api/artifacts/example/player-export")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"media/source.wav", "subtitles.srt", "runtime/LiveTranscriberPlayer.exe",
                "runtime/_internal/python310.dll", "assets/ffplay.exe", "assets/ffprobe.exe",
                "run_audio_subtitle_player.cmd", "README_play.txt"} <= names
        assert archive.read("media/source.wav") == audio.read_bytes()
        launcher = archive.read("run_audio_subtitle_player.cmd").decode()
        assert "%~dp0runtime\\LiveTranscriberPlayer.exe" in launcher
        assert "%~dp0media\\source.wav" in launcher
        assert str(tmp_path) not in launcher
    assert not list((tmp_path / "outputs/_staging").glob("player-export-*"))


def test_stale_player_export_is_rejected_before_build(media, monkeypatch):
    group, _, subtitle, _ = media
    updated_analysis(group, subtitle)
    monkeypatch.setattr(routes, "build_player_archive", lambda *args: pytest.fail("Exported stale player"))
    with TestClient(create_app()) as client:
        assert client.get("/api/artifacts/example/player-export").status_code == 409


def test_failed_export_cleans_staging(media, monkeypatch, tmp_path):
    def fail(*args):
        raise AppError("missing runtime")
    monkeypatch.setattr(routes, "build_player_archive", fail)
    with TestClient(create_app()) as client:
        assert client.get("/api/artifacts/example/player-export").status_code == 422
    assert not list((tmp_path / "outputs/_staging").glob("player-export-*"))


def test_export_uses_the_audio_selected_when_player_was_generated(media, tmp_path, monkeypatch):
    fake_runtime(tmp_path, monkeypatch)
    _, audio, _, _ = media
    selected_audio = audio.parent / "alternate.wav"
    selected_audio.write_bytes(b"selected player audio")
    manifest_path = audio.parent / "assets/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["audio"] = str(selected_audio)
    manifest_path.write_text(json.dumps(manifest))
    with TestClient(create_app()) as client:
        response = client.get("/api/artifacts/example/player-export")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("media/source.wav") == b"selected player audio"


def test_audio_and_video_freshness_are_independent(media):
    group, audio, subtitle, _ = media
    video = group / "video"
    (video / "assets").mkdir(parents=True)
    (video / "live_preview.mp4").write_bytes(b"synthetic")
    (video / "assets/manifest.json").write_text(json.dumps({
        **preview_identity(subtitle), "transcript_run_id": "example",
    }))
    new_subtitle = updated_analysis(group, subtitle)
    (audio.parent / "assets/manifest.json").write_text(json.dumps({
        **preview_identity(new_subtitle), "transcript_run_id": "example",
    }))
    preview = routes._preview_result(video)
    analysis = routes._latest_analysis_result(new_subtitle.parent)
    routes._mark_preview_freshness(preview, analysis)
    assert not preview["audio_stale"]
    assert preview["video_stale"]
