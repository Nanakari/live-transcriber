import hashlib
import io
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from scripts import publish_verified as publisher


class GitHubRelease:
    """Exercise the full release flow without any GitHub mutation or network."""
    def __init__(self):
        self.tag = "v1.2.3"
        self.commit = "a" * 40
        self.tag_commit = self.commit
        self.ci_commit = self.commit
        self.conclusion = "success"
        self.draft = True
        self.mutations = []
        self.fail_upload = ""
        self.archive_name = f"LiveTranscriber-{self.tag}-windows-x64.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name in publisher.REQUIRED_FILES:
                bundle.writestr("LiveTranscriber/" + name, b"test runtime")
        archive = buffer.getvalue()
        checksum = f"{hashlib.sha256(archive).hexdigest()}  {self.archive_name}\r\n".encode()
        self.build = {self.archive_name: archive, self.archive_name + ".sha256": checksum}
        self.assets = dict(self.build)

    def gh(self, *args):
        if args[:2] == ("run", "view"):
            return json.dumps({"conclusion": self.conclusion, "headSha": self.commit,
                               "workflowName": "Windows release"})
        if args[0] == "api":
            if "/commits/" in args[-1]:
                return json.dumps({"sha": self.tag_commit})
            return json.dumps([{"workflow_runs": [{"name": "CI", "conclusion": "success",
                                                     "head_sha": self.ci_commit}]}])
        if args[:2] == ("run", "download"):
            directory = Path(args[args.index("--dir") + 1])
            directory.mkdir(parents=True)
            for name, data in self.build.items():
                (directory / name).write_bytes(data)
            return ""
        if args[:2] == ("release", "view"):
            return json.dumps({"isDraft": self.draft, "targetCommitish": "main",
                               "assets": [{"name": name} for name in self.assets]})
        if args[:2] == ("release", "download"):
            name = args[args.index("--pattern") + 1]
            directory = Path(args[args.index("--dir") + 1])
            (directory / name).write_bytes(self.assets[name])
            return ""
        if args[:2] == ("release", "upload"):
            path = Path(args[3])
            assert len(args) == 4, "Uploads must not use --clobber"
            assert path.name not in self.assets
            if path.name == self.fail_upload:
                raise subprocess.CalledProcessError(1, args)
            self.assets[path.name] = path.read_bytes()
            self.mutations.append(("upload", path.name))
            return ""
        if args[:2] == ("release", "edit"):
            self.draft = False
            self.mutations.append(("publish", args[2]))
            return ""
        raise AssertionError(args)

    def publish(self, monkeypatch, workspace):
        monkeypatch.setattr(publisher, "gh", self.gh)
        publisher.publish_verified("123", self.tag, "owner/repo", workspace)


def test_standard_tag_draft_reuses_existing_assets_and_publish_retry_is_noop(tmp_path, monkeypatch):
    release = GitHubRelease()
    original = dict(release.assets)
    release.publish(monkeypatch, tmp_path / "first")
    assert release.mutations == [("publish", release.tag)]
    assert release.assets == original  # Includes the build.ps1 CRLF checksum.
    release.mutations.clear()
    release.publish(monkeypatch, tmp_path / "retry")
    assert release.mutations == []


def test_interrupted_upload_retries_only_missing_asset(tmp_path, monkeypatch):
    release = GitHubRelease()
    release.assets.clear()
    release.fail_upload = release.archive_name + ".sha256"
    with pytest.raises(subprocess.CalledProcessError):
        release.publish(monkeypatch, tmp_path / "interrupted")
    assert release.draft
    assert release.mutations == [("upload", release.archive_name)]
    release.fail_upload = ""
    release.mutations.clear()
    release.publish(monkeypatch, tmp_path / "retry")
    assert release.mutations == [("upload", release.archive_name + ".sha256"), ("publish", release.tag)]


@pytest.mark.parametrize("field", ["tag_commit", "ci_commit", "conclusion"])
def test_mismatched_commit_or_unsuccessful_build_cannot_publish(field, tmp_path, monkeypatch):
    release = GitHubRelease()
    setattr(release, field, "wrong")
    with pytest.raises(ValueError):
        release.publish(monkeypatch, tmp_path)
    assert release.mutations == []


def test_existing_different_binary_is_never_overwritten(tmp_path, monkeypatch):
    release = GitHubRelease()
    release.assets[release.archive_name] = b"different build"
    with pytest.raises(ValueError, match="differs"):
        release.publish(monkeypatch, tmp_path)
    assert release.assets[release.archive_name] == b"different build"
    assert release.mutations == []


def test_corrupt_build_checksum_prevents_release_changes(tmp_path, monkeypatch):
    release = GitHubRelease()
    release.build[release.archive_name + ".sha256"] = b"incorrect checksum"
    with pytest.raises(ValueError, match="checksum"):
        release.publish(monkeypatch, tmp_path)
    assert release.mutations == []


def test_published_incomplete_release_is_not_modified(tmp_path, monkeypatch):
    release = GitHubRelease()
    release.draft = False
    release.assets.pop(release.archive_name + ".sha256")
    with pytest.raises(ValueError, match="Published release is missing"):
        release.publish(monkeypatch, tmp_path)
    assert release.mutations == []


def test_unverified_extra_binary_prevents_publication(tmp_path, monkeypatch):
    release = GitHubRelease()
    release.assets["old-version.zip"] = b"unverified"
    with pytest.raises(ValueError, match="unverified archives"):
        release.publish(monkeypatch, tmp_path)
    assert release.mutations == []


def test_tag_moved_during_asset_verification_is_not_published(tmp_path, monkeypatch):
    release = GitHubRelease()
    original_gh = release.gh
    commit_reads = 0
    def move_tag(*args):
        nonlocal commit_reads
        if args[0] == "api" and "/commits/" in args[-1]:
            commit_reads += 1
            if commit_reads == 3:
                release.tag_commit = "b" * 40
        return original_gh(*args)
    monkeypatch.setattr(publisher, "gh", move_tag)
    with pytest.raises(ValueError, match="tag does not point"):
        publisher.publish_verified("123", release.tag, "owner/repo", tmp_path)
    assert release.mutations == []
