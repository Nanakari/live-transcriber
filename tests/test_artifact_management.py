from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.web import routes


def _create_media_group(root: Path) -> tuple[Path, str]:
    run_id = "20260807_140000_000_test"
    group = root / "outputs" / "media" / "sample-group"
    audio = group / "audio" / f"{run_id}_source.m4a"
    transcript = group / "transcripts" / f"{run_id}_transcript.json"
    analysis_file = group / "analysis" / "analysis-run" / "notes.md"
    preview_file = group / "previews" / "potplayer" / "live_preview.mp4"
    for path in (audio, transcript, analysis_file, preview_file):
        path.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio-data")
    analysis_file.write_bytes(b"analysis-data")
    preview_file.write_bytes(b"preview-data")
    transcript.write_text(
        json.dumps(
            {
                "meta": {
                    "run_id": run_id,
                    "title": "Sample transcription",
                    "source_audio_file": str(audio),
                    "clean_audio_file": "",
                },
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    return group, run_id


def test_artifact_set_reports_complete_group_storage(tmp_path: Path) -> None:
    group, run_id = _create_media_group(tmp_path)

    items = routes._artifact_sets(tmp_path)

    item = next(value for value in items if value["run_id"] == run_id)
    expected_files = [path for path in group.rglob("*") if path.is_file()]
    assert item["deletable"] is True
    assert Path(item["group_path"]) == group.resolve()
    assert item["storage_file_count"] == len(expected_files)
    assert item["storage_bytes"] == sum(path.stat().st_size for path in expected_files)


def test_delete_artifact_run_removes_only_selected_group(tmp_path: Path) -> None:
    group, run_id = _create_media_group(tmp_path)
    sibling = tmp_path / "outputs" / "media" / "keep-group" / "keep.txt"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("keep", encoding="utf-8")

    result = routes._delete_artifact_run(run_id, tmp_path)

    assert result["run_id"] == run_id
    assert result["deleted_file_count"] == 4
    assert not group.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_artifact_group_rejects_paths_outside_media_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "transcript.json"
    outside.parent.mkdir()
    outside.write_text("{}", encoding="utf-8")

    assert routes._artifact_group_dir([outside], tmp_path) is None


def test_delete_artifact_run_rejects_media_root_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media_root = tmp_path / "outputs" / "media"
    media_root.mkdir(parents=True)
    monkeypatch.setattr(
        routes,
        "_artifact_sets",
        lambda root: [
            {
                "run_id": "unsafe",
                "deletable": True,
                "group_path": str(media_root),
            }
        ],
    )

    with pytest.raises(HTTPException) as exc_info:
        routes._delete_artifact_run("unsafe", tmp_path)

    assert exc_info.value.status_code == 400
    assert media_root.exists()


def test_delete_endpoint_refuses_while_a_job_is_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes.jobs, "has_running_jobs", lambda: True)

    with pytest.raises(HTTPException) as exc_info:
        routes.delete_artifact("run-1")

    assert exc_info.value.status_code == 409
