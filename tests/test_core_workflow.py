from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import cli, config, transcriber, utils
from app.analysis import analyzer
from app.analysis.cache import cache_key, load_cached_result, save_chunk_result
from app.analysis.chunker import make_chunks
from app.analysis.exporters import export_all
from app.analysis.prompts import build_chunk_prompt
from app.analysis.schemas import AnalysisChunk, AnalysisDocument, AnalysisMeta, AnalysisSegment, ChunkAnalysisResult
from app.schemas import TranscriptDocument, TranscriptMeta, TranscriptSegment
from app.web import routes
from app.web.jobs import Job, JobManager
from app.web.server import create_app


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return tmp_path


def chunk():
    return make_chunks([AnalysisSegment(segment_id=1, start=0, end=1, start_text="0", end_text="1", text="Hello")],
                       chunk_minutes=2, max_segments_per_chunk=20)[0]


def test_profile_request_is_absent_unless_enabled():
    kwargs = dict(profile="multilingual_study", source_language="en", target_language="zh")
    plain = build_chunk_prompt(chunk(), **kwargs)
    assert "profile_observations" not in plain
    assert "人物侧写" not in plain
    assert '"vocabulary"' in plain and '"chunk_summary_zh"' in plain
    assert "profile_observations" in build_chunk_prompt(chunk(), character_profile=True, **kwargs)


def test_disabled_notes_are_not_requested():
    prompt = build_chunk_prompt(chunk(), profile="study", source_language="en", target_language="zh", study_notes=False, summary=False)
    assert '"vocabulary"' not in prompt and '"chunk_summary_zh"' not in prompt
    assert '"bilingual_lines"' in prompt


def test_cache_changes_with_text_language_and_features(tmp_path):
    original = chunk()
    args = dict(transcript_file=tmp_path / "source.json", model="model", profile="study", prompt_version="1")
    before = cache_key(chunk=original, parameters={"character_profile": False}, **args)
    changed = original.model_copy(deep=True)
    changed.segments[0].text = "Changed"
    assert before != cache_key(chunk=changed, parameters={"character_profile": False}, **args)
    assert before != cache_key(chunk=original, parameters={"character_profile": True}, **args)
    assert before != cache_key(chunk=original, parameters={"target_language": "ja"}, **args)


def test_corrupt_cache_is_a_miss_and_can_be_replaced(tmp_path):
    target = tmp_path / "cache.json"
    target.write_text("{", encoding="utf-8")
    assert load_cached_result(target) is None
    value = ChunkAnalysisResult(chunk_id="chunk_0000", start=0, end=1)
    save_chunk_result(target, value)
    assert load_cached_result(target) == value
    assert list(tmp_path.iterdir()) == [target]


def transcript_file(root):
    path = root / "input_transcript.json"
    document = TranscriptDocument(meta=TranscriptMeta(created_at="2026-09-05", run_id="sample", language="en"),
        segments=[TranscriptSegment(id=1, start=0, end=1, start_text="0", end_text="1", text="Hello")])
    path.write_text(document.model_dump_json(), encoding="utf-8")
    return path


def test_core_analysis_exports_four_results_without_profile(isolated, monkeypatch):
    class FakeClient:
        def generate_json_text(self, *args, **kwargs):
            return json.dumps({"bilingual_lines": [{"segment_id": 1, "start": 0, "end": 1,
                "original": "Hello", "translation_zh": "你好"}], "profile_observations": "ignored malformed optional data"})
    monkeypatch.setattr(analyzer, "_make_client", lambda *_: FakeClient())
    args = cli.build_parser().parse_args(["analyze", "--input", str(transcript_file(isolated))])
    result = cli.analyze_task(args)
    assert result["exit_code"] == 0
    assert all((result["output_dir"] / file).exists() for file in ("translation_zh.srt", "bilingual.md", "video_summary.md", "study_notes.md"))
    assert not (result["output_dir"] / "character_profile.md").exists()
    assert result["document"].chunks[0].profile_observations == []


def test_all_failed_analysis_returns_failure_but_preserves_outputs(isolated, monkeypatch):
    class FailingClient:
        def generate_json_text(self, *args, **kwargs):
            raise RuntimeError("synthetic failure")
    monkeypatch.setattr(analyzer, "_make_client", lambda *_: FailingClient())
    args = cli.build_parser().parse_args(["analyze", "--input", str(transcript_file(isolated))])
    result = cli.analyze_task(args)
    assert result["exit_code"] == 1
    assert result["document"].meta.failed_chunks == 1
    assert (result["output_dir"] / "review.md").exists()


def test_cpu_auto_and_explicit_gpu_policy(monkeypatch):
    monkeypatch.setattr(utils, "is_cuda_available", lambda: False)
    kwargs = dict(quality="high", requested_compute_type=None, default_compute_type="int8_float16")
    assert utils.normalize_device_compute(requested_device="auto", **kwargs)[:2] == ("cpu", "int8")
    assert utils.normalize_device_compute(requested_device="cuda", **kwargs)[:2] == ("cuda", "int8_float16")


def test_raw_zero_segment_id_is_preserved():
    assert transcriber._segment_to_model(SimpleNamespace(id=0, start=0, end=1), 1).id == 0


def test_frozen_data_does_not_depend_on_install_or_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVE_TRANSCRIBER_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    assert config.project_root() == tmp_path / "LiveTranscriber"


def test_default_pipeline_excludes_preview():
    args = cli.build_parser().parse_args(["pipeline", "--input", "test.wav"])
    assert args.modules == "transcribe,analyze"


def test_web_no_key_fails_before_start_and_transcribe_still_allowed(isolated, monkeypatch):
    started = []
    def start(module, command, **kwargs):
        started.append(command)
        return Job(job_id="test", module=module, command=command)
    monkeypatch.setattr(routes.jobs, "start", start)
    with TestClient(create_app()) as client:
        response = client.post("/api/pipeline", json={"input": "sample.wav", "module_transcribe": True, "module_analyze": True})
        assert response.status_code == 400 and not started
        response = client.post("/api/pipeline", json={"input": "sample.wav", "module_transcribe": True, "proxy": ""})
        assert response.status_code == 200
        command = started[0]
        assert command[command.index("--proxy") + 1] == ""
        assert command[command.index("--modules") + 1] == "transcribe"


def test_web_profile_flag_and_key_not_in_command(isolated, monkeypatch):
    captured = {}
    def start(module, command, **kwargs):
        captured.update(kwargs)
        captured["command"] = command
        return Job(job_id="test", module=module, command=command)
    monkeypatch.setattr(routes.jobs, "start", start)
    with TestClient(create_app()) as client:
        response = client.post("/api/analyze", json={"input": "sample.json", "gemini_api_key": "test-only-key", "character_profile": True})
        assert response.status_code == 200
        assert "--character-profile" in captured["command"]
        assert "test-only-key" not in " ".join(captured["command"])
        assert captured["env_overrides"]["GEMINI_API_KEY"] == "test-only-key"


def test_download_rejects_config_and_outside_files(isolated):
    (isolated / "config.yaml").write_text("{}", encoding="utf-8")
    result = isolated / "outputs" / "sample.md"
    result.parent.mkdir()
    result.write_text("sample", encoding="utf-8")
    with TestClient(create_app()) as client:
        assert client.get("/api/download", params={"path": str(isolated / "config.yaml")}).status_code == 403
        assert client.get("/api/download", params={"path": str(result)}).text == "sample"


def test_cross_origin_commands_are_rejected(isolated):
    with TestClient(create_app()) as client:
        response = client.post("/api/lifecycle/shutdown", headers={"Origin": "https://unrelated.example"})
        assert response.status_code == 403
        assert client.get("/", headers={"Host": "unrelated.example"}).status_code == 400


def test_javascript_has_executable_mime_type(isolated):
    with TestClient(create_app()) as client:
        response = client.get("/static/workbench.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/javascript")
        assert response.headers["x-content-type-options"] == "nosniff"


def test_cancel_pending_task_never_spawns(monkeypatch, isolated):
    manager = JobManager()
    job = Job(job_id="test", module="pipeline", command=["should-not-start"])
    manager._jobs[job.job_id] = job
    manager._heavy_running = job.job_id
    manager.stop(job.job_id)
    manager._run_job(job)
    assert job.status == "stopped" and job.proc is None
    assert not manager.has_running_jobs() and manager._heavy_running is None
