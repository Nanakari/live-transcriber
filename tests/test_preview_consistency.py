import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import preview
from app.output_layout import write_media_index
from app.preview_state import preview_is_current
from app.web import routes
from app.web.server import create_app


@pytest.fixture
def package(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRANSCRIBER_HOME", str(tmp_path))
    monkeypatch.setattr(preview, "_bundle_audio_runtime_tools", lambda *a: {})
    monkeypatch.setattr(preview, "ensure_ffmpeg_for_preview", lambda: None)
    monkeypatch.setattr(preview, "command_exists", lambda *a: True)
    monkeypatch.setattr(preview, "prepare_cover", lambda src, dst, log: dst.write_bytes(b"cover"))
    monkeypatch.setattr(preview, "build_preview_video", lambda **kw: kw["output"].write_bytes(b"video"))
    group = tmp_path / "outputs" / "media" / "sample"
    audio = group / "audio" / "sample_source.m4a"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    transcript = group / "transcripts" / "sample_transcript.json"
    transcript.parent.mkdir()
    transcript.write_text(json.dumps({"meta": {"run_id": "sample", "source_audio_file": str(audio)}, "segments": []}), encoding="utf-8")
    subtitle = new_analysis(group, "first", "旧译文", transcript)
    options = preview.PreviewOptions(audio=audio, subtitle=subtitle, cover=None, output_dir=None,
                                     resolution="1280x720", subtitle_name="live_preview.zh.srt",
                                     video_name="live_preview.mp4", mode="audio")
    return group, options


def new_analysis(group, run, text, transcript=None):
    directory = group / "analysis" / run
    directory.mkdir(parents=True)
    (directory / "analysis.json").write_text(json.dumps({
        "meta": {"run_id": run, "transcript_run_id": "sample", "input_file": str(transcript or "")},
        "chunks": [{"bilingual_lines": [{"segment_id": 1, "original": "Hello", "translation_zh": text,
                                         "start": 0, "end": 1}]}],
    }), encoding="utf-8")
    subtitle = directory / "translation_zh.srt"
    subtitle.write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n", encoding="utf-8")
    for name in ("bilingual.md", "review.md"):
        (directory / name).write_text(text, encoding="utf-8")
    # Force both analyses into the same displayed second, with different ordering.
    stamp = 1_790_000_000_100_000_000 + (100_000_000 if run == "second" else 0)
    os.utime(directory / "analysis.json", ns=(stamp, stamp))
    return subtitle


def test_reanalysis_marks_both_modes_stale_and_rebuilding_audio_only_preserves_video_warning(package, monkeypatch):
    group, options = package
    preview.create_video_preview(replace(options, mode="video"))
    assert preview_is_current(group / "video", options.subtitle)
    assert preview_is_current(group / "audio", options.subtitle)
    newest = new_analysis(group, "second", "新译文")
    write_media_index(group)
    index = (group / "README.md").read_text(encoding="utf-8")
    assert "已过期" in index and "[音频 + 悬挂字幕" not in index and "[播放字幕视频]" not in index
    monkeypatch.setattr(routes, "open_file", lambda path: {"path": path})
    with TestClient(create_app()) as client:
        payload = client.get("/api/files/recent").json()
        item = payload["artifact_sets"][0]
        assert Path(item["analysis"]["path"]) == newest.parent
        assert item["preview"]["audio_stale"] and item["preview"]["video_stale"]
        assert payload["latest_preview"]["stale"]
        for path in (group / "audio/run_audio_subtitle_player.cmd", group / "video/live_preview.mp4"):
            assert client.post("/api/open-file", json={"path": str(path)}).status_code == 409
            assert client.get("/api/download", params={"path": str(path)}).status_code == 409
        assert client.post("/api/open-potplayer", json={"video": str(group / "video/live_preview.mp4")}).status_code == 409
        # Latest analysis stays downloadable while old preview is unavailable.
        assert client.get("/api/download", params={"path": str(newest)}).status_code == 200
        preview.create_audio_preview(replace(options, subtitle=newest))
        payload = client.get("/api/files/recent").json()
        item = payload["artifact_sets"][0]
        assert item["preview"]["audio_stale"] is False
        assert item["preview"]["video_stale"] is True
        assert "live_preview.mp4" in item["preview"]["files"]
        assert client.post("/api/open-file", json={"path": str(group / "audio/run_audio_subtitle_player.cmd")}).status_code == 200
        assert client.post("/api/open-file", json={"path": str(group / "video/live_preview.mp4")}).status_code == 409
    assert "新译文" in (group / "audio/audio_mode.bilingual.srt").read_text(encoding="utf-8")
    assert (group / "video/live_preview.mp4").read_bytes() == b"video"
    index = (group / "README.md").read_text(encoding="utf-8")
    assert "[音频 + 悬挂字幕" in index and "字幕视频已过期" in index


def test_repairs_or_same_path_edits_invalidate_preview(package):
    group, options = package
    preview.create_audio_preview(options)
    assert preview_is_current(group / "audio", options.subtitle)
    analysis = options.subtitle.parent / "analysis.json"
    data = json.loads(analysis.read_text(encoding="utf-8"))
    data["chunks"][0]["bilingual_lines"][0]["corrected_original"] = "Repaired source"
    analysis.write_text(json.dumps(data), encoding="utf-8")
    assert not preview_is_current(group / "audio", options.subtitle)


@pytest.mark.parametrize("bad_manifest", [None, "{", "[]", "{}"])
def test_legacy_or_corrupt_manifest_is_stale(package, bad_manifest):
    group, options = package
    preview.create_audio_preview(options)
    manifest = group / "audio/assets/manifest.json"
    if bad_manifest is None:
        manifest.unlink()
    else:
        manifest.write_text(bad_manifest, encoding="utf-8")
    assert not preview_is_current(group / "audio", options.subtitle)
    item = routes._artifact_sets(group.parents[2])[0]
    assert item["preview"]["audio_stale"] is True


@pytest.mark.parametrize("mode", ["audio", "video"])
def test_failed_regeneration_cannot_reuse_previous_success_manifest(package, monkeypatch, mode):
    group, options = package
    options = replace(options, mode=mode)
    preview.create_preview(options)
    directory = group / mode
    assert preview_is_current(directory, options.subtitle)
    def fail(*a, **kw):
        raise RuntimeError("simulated interrupted write")
    monkeypatch.setattr(preview, "write_audio_subtitle_player" if mode == "audio" else "build_preview_video", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        preview.create_preview(options)
    assert not preview_is_current(directory, options.subtitle)
    assert "已过期或生成未完成" in (group / "README.md").read_text(encoding="utf-8")
