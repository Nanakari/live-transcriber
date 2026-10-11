"""Maintenance must honor the configured data root and preserve source media."""
import json
from pathlib import Path

import pytest

from app.migrate_outputs import migrate_legacy_outputs
from scripts.consolidate_data import consolidate


def write(path: Path, content: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def transcript(path: Path, audio: Path) -> Path:
    return write(path, json.dumps({
        "meta": {"run_id": "demo", "source_audio_file": str(audio)}, "segments": [],
    }))


@pytest.mark.parametrize("absolute", [False, True])
def test_migration_leaves_organized_media_in_an_explicit_root_unchanged(tmp_path, absolute):
    root = tmp_path / "home"
    outputs = tmp_path / "external results" if absolute else root / "custom results"
    setting = str(outputs) if absolute else "custom results"
    write(root / "config.local.yaml", json.dumps({"app": {"output_dir": setting}}))
    audio = write(outputs / "media/kept group/audio/demo_source.wav", "audio")
    transcript(outputs / "media/kept group/transcripts/demo_transcript.json", audio)
    before = {p.relative_to(outputs): p.read_bytes() for p in outputs.rglob("*") if p.is_file()}

    for _ in range(2):
        result = migrate_legacy_outputs(root)
        assert result["moved"] == []
        assert {p.relative_to(outputs): p.read_bytes() for p in outputs.rglob("*") if p.is_file()} == before
        assert sorted(p.name for p in (outputs / "media").iterdir()) == ["kept group"]


def test_migration_never_moves_external_original_referenced_by_metadata(tmp_path):
    root = tmp_path / "home"
    source = write(tmp_path / "original recordings/keep.wav", "original")
    transcript(root / "outputs/transcripts/demo_transcript.json", source)

    result = migrate_legacy_outputs(root)

    assert source.read_text() == "original"
    assert all(Path(item["from"]).is_relative_to(root / "outputs") for item in result["moved"])
    assert len(list((root / "outputs/media").rglob("demo_transcript.json"))) == 1
    assert not migrate_legacy_outputs(root)["moved"]


@pytest.mark.parametrize("absolute", [False, True])
def test_consolidation_uses_configured_roots_and_repairs_imported_metadata(tmp_path, absolute):
    root, old = tmp_path / "new home", tmp_path / "old home"
    outputs = tmp_path / "external results" if absolute else root / "custom results"
    setting = str(outputs) if absolute else "custom results"
    write(root / "config.local.yaml", json.dumps({"app": {"output_dir": setting}}))
    write(old / "config.local.yaml", "app:\n  output_dir: old-results\n")
    cached = write(outputs / "analysis/_cache/chunk.json")
    audio = write(old / "old-results/media/example/audio/demo_source.wav", "media")
    imported = transcript(old / "old-results/media/example/transcripts/demo_transcript.json", audio)
    before = imported.read_bytes()

    planned = consolidate(root, [old])
    assert len(planned["moved"]) == 3
    assert audio.exists() and cached.exists() and imported.exists()
    result = consolidate(root, [old], apply=True)

    assert (outputs / "cache/analysis/chunk.json").read_text() == "{}"
    assert not cached.exists()
    target = outputs / "media/example/transcripts/demo_transcript.json"
    assert json.loads(target.read_text())["meta"]["source_audio_file"] == str(
        (outputs / "media/example/audio/demo_source.wav").resolve()
    )
    backups = list((root / "archive/migration").rglob("demo_transcript.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    assert len(result["metadata_updated"]) == 1
    assert not (root / "outputs").exists()
    assert not (old / "outputs").exists()
    assert not consolidate(root, [old], apply=True)["moved"]


def test_consolidation_rejects_nested_output_roots_before_moving(tmp_path):
    root = tmp_path / "home"
    source = write(root / "outputs/analysis/_cache/chunk.json", "cache")
    nested = root / "outputs/another-home"
    write(nested / "outputs/media/x.txt", "kept")

    with pytest.raises(ValueError, match="overlap|nested"):
        consolidate(root, [nested], apply=True)

    assert source.read_text() == "cache"


def test_consolidation_refuses_output_root_containing_a_data_home(tmp_path):
    root, old = tmp_path / "new", tmp_path / "old"
    write(root / "config.local.yaml", "app:\n  output_dir: outputs\n")
    write(old / "config.local.yaml", "app:\n  output_dir: '.'\n")
    original = write(old / "main.py", "# source, not a result")
    cached = write(old / "analysis/_cache/chunk.json", "cache")

    with pytest.raises(ValueError, match="overlaps a data directory"):
        consolidate(root, [old], apply=True)

    assert original.read_text() == "# source, not a result"
    assert cached.read_text() == "cache"
    assert not (root / "outputs").exists()


def link_directory(source: Path, destination: Path) -> None:
    try:
        source.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")


def test_migration_refuses_media_directory_pointing_outside_outputs(tmp_path):
    root = tmp_path / "home"
    source = write(root / "outputs/audio/demo_source.wav", "audio")
    outside = tmp_path / "outside"
    outside.mkdir()
    link_directory(root / "outputs/media", outside)

    with pytest.raises(ValueError):
        migrate_legacy_outputs(root)

    assert source.read_text() == "audio"
    assert not list(outside.iterdir())


def test_consolidation_validates_later_links_before_any_files_move(tmp_path):
    root, old = tmp_path / "home", tmp_path / "old"
    cached = write(root / "outputs/analysis/_cache/chunk.json", "cache")
    outside = tmp_path / "outside"
    write(outside / "source.wav", "original")
    (old / "outputs/media").mkdir(parents=True)
    link_directory(old / "outputs/media/link", outside)

    with pytest.raises(ValueError):
        consolidate(root, [old], apply=True)

    assert cached.read_text() == "cache"
    assert (outside / "source.wav").read_text() == "original"
