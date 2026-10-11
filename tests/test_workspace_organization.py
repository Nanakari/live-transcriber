from pathlib import Path

import pytest

from scripts.organize_workspace import organize


def checkout(root: Path) -> None:
    (root / "app").mkdir()
    (root / "main.py").touch()
    (root / "app/config.py").touch()


def test_organization_preserves_results_and_can_be_repeated(tmp_path):
    checkout(tmp_path)
    for name in ("outputs/qa/sample/frame.png", "outputs/release/v1/release.zip",
                 "outputs/media/sample/transcripts/a.json", "outputs/cache/analysis/a.json"):
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.write_bytes(name.encode())
    report = organize(tmp_path)
    assert len(report["moves"]) == 2
    assert (tmp_path / "outputs/qa/sample/frame.png").exists()
    assert not (tmp_path / "build").exists()
    applied = organize(tmp_path, apply=True)
    assert all(item["verified"] for item in applied["moves"])
    assert (tmp_path / "build/qa/sample/frame.png").read_bytes() == b"outputs/qa/sample/frame.png"
    assert (tmp_path / "archive/releases/legacy-outputs/v1/release.zip").read_bytes() == b"outputs/release/v1/release.zip"
    assert (tmp_path / "outputs/media/sample/transcripts/a.json").read_bytes() == b"outputs/media/sample/transcripts/a.json"
    assert (tmp_path / "outputs/cache/analysis/a.json").exists()
    assert not organize(tmp_path, apply=True)["moves"]


def test_organization_refuses_conflicts_before_any_move(tmp_path):
    checkout(tmp_path)
    (tmp_path / "outputs/qa").mkdir(parents=True)
    (tmp_path / "outputs/release").mkdir()
    (tmp_path / "archive/releases/legacy-outputs").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        organize(tmp_path, apply=True)
    assert (tmp_path / "outputs/qa").exists()
    assert not (tmp_path / "build/qa").exists()


def test_organization_rejects_a_link_inside_the_move(tmp_path):
    checkout(tmp_path)
    source = tmp_path / "outputs/qa"
    source.mkdir(parents=True)
    outside = tmp_path / "private"
    outside.mkdir()
    try:
        (source / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="link or junction"):
        organize(tmp_path, apply=True)
    assert outside.exists()
