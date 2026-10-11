import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli, config, preview
from app.analysis import analyzer
from app.output_layout import group_dir_from_artifact_path, media_root
from app.schemas import TranscriptSegment
from app.web import jobs, routes
from app.web.server import create_app


@pytest.mark.parametrize("absolute", [False, True])
def test_custom_output_roundtrip_transcribe_analyze_preview_history_export_delete(tmp_path, monkeypatch, absolute):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(home))
    configured = tmp_path / "external results" if absolute else Path("custom results")
    (home / "config.local.yaml").write_text(
        json.dumps({"app": {"output_dir": str(configured)}, "features": {"summary": False, "study_notes": False}}),
        encoding="utf-8",
    )
    expected = configured if absolute else home / configured
    other_cwd = tmp_path / "different cwd"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    monkeypatch.setattr(cli, "normalize_device_compute", lambda **kw: ("cpu", "int8", []))
    monkeypatch.setattr(cli, "convert_to_clean_wav", lambda source, target, logger: target.write_bytes(b"clean"))
    monkeypatch.setattr(cli, "transcribe_audio", lambda **kw: ([TranscriptSegment(
        id=1, start=0, end=1, start_text="00:00:00.000", end_text="00:00:01.000", text="Hello there",
    )], {"detected_language": "en"}))
    monkeypatch.setattr(preview, "_bundle_audio_runtime_tools", lambda *a: {})
    class Client:
        def generate_json_text(self, *a, **kw):
            return '{"bilingual_lines":[{"segment_id":1,"translation_zh":"你好"}]}'
    monkeypatch.setattr(analyzer, "_make_client", lambda *a: Client())
    source = home / "input.wav"
    source.write_bytes(b"audio")
    transcribed = cli.transcribe_task(cli.build_parser().parse_args(["transcribe", "--input", str(source)]))
    group = transcribed["output_dir"]
    assert group.parent == expected / "media"
    assert media_root() == expected / "media"
    assert group_dir_from_artifact_path(transcribed["transcript"]) == group
    analyzed = cli.analyze_task(cli.build_parser().parse_args(["analyze", "--input", str(transcribed["transcript"])]))
    assert analyzed["output_dir"].parent == group / "analysis"
    assert list((expected / "cache" / "analysis").glob("chunk_*.json"))
    rendered = cli._create_preview(cli.build_parser().parse_args([
        "preview", "--audio", str(transcribed["audio"]), "--subtitle", str(analyzed["output_dir"] / "translation_zh.srt"),
    ]))
    assert rendered["output_dir"] == group / "audio"
    assert jobs.guess_output_dir("transcribe", datetime.now()) == str(expected)
    assert jobs.guess_output_dir("analyze", datetime.now()) == str(analyzed["output_dir"])
    assert jobs.guess_output_dir("preview", datetime.now()) == str(rendered["output_dir"])
    assert not list((expected / "_staging").glob("*/audio"))
    with monkeypatch.context() as no_process:
        no_process.setattr(jobs.threading.Thread, "start", lambda self: None)
        manager = jobs.JobManager()
        job = manager.start("analyze", ["unused"])
    assert Path(job.log_path).parent == expected / "logs" / "web_jobs"
    assert routes._default_preview_output_dir("", str(analyzed["output_dir"] / "translation_zh.srt"), str(transcribed["audio"])) == str(group / "audio")

    with TestClient(create_app()) as client:
        payload = client.get("/api/files/recent").json()
        assert len(payload["artifact_sets"]) == 1
        item = payload["artifact_sets"][0]
        assert item["group_path"] == str(group)
        assert item["deletable"] is True
        assert item["preview"]["audio_stale"] is False
        assert payload["latest_preview"]["audio_stale"] is False
        subtitle = analyzed["output_dir"] / "translation_zh.srt"
        assert "你好" in client.get("/api/file", params={"path": str(subtitle)}).json()["content"]
        assert client.get("/api/download", params={"path": str(subtitle)}).status_code == 200
        exported = client.get(f"/api/artifacts/{item['run_id']}/export")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            assert "translation_zh.srt" in archive.namelist()
        # Access is bounded to the configured root, not its parent or siblings.
        outside = tmp_path / "outside.md"
        outside.write_text("private", encoding="utf-8")
        assert client.get("/api/file", params={"path": str(outside)}).status_code == 403
        assert client.get("/api/download", params={"path": str(home / "config.local.yaml")}).status_code == 403
        assert client.delete(f"/api/artifacts/{item['run_id']}").status_code == 200
    assert not group.exists()
    assert source.exists()
    assert not (home / "outputs").exists()


def test_output_root_override_and_home_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    assert config.output_root("~/results") == Path("~/results").expanduser().resolve()
    assert config.output_root("relative") == tmp_path / "relative"
