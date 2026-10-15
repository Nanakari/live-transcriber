"""Publish a verified build, reusing only matching existing release assets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile


REQUIRED_FILES = (
    "LiveTranscriber.exe", "LiveTranscriberPlayer.exe", "start.bat",
    "_internal/tools/ffmpeg.exe", "_internal/tools/ffplay.exe",
    "_internal/tools/ffprobe.exe", "_internal/tools/node.exe",
    "_internal/app/assets/floating_subtitle_overlay.pyw",
)


def gh(*arguments: str) -> str:
    return subprocess.check_output(["gh", *arguments], text=True, encoding="utf-8")


def gh_json(*arguments: str):
    return json.loads(gh(*arguments))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_commit(run: dict, tag_commit: dict, checks: list[dict]) -> None:
    if run.get("conclusion") != "success" or run.get("workflowName") != "Windows release":
        raise ValueError("The source must be a successful Windows release build.")
    # targetCommitish can be 'main' on an existing tagged release. Resolve the
    # actual tag instead of comparing that release-creation hint to a SHA.
    commit = run.get("headSha")
    if not commit or tag_commit.get("sha") != commit:
        raise ValueError("Release tag does not point to the source build commit.")
    if not any(check.get("name") == "CI" and check.get("conclusion") == "success"
               and check.get("head_sha") == commit for check in checks):
        raise ValueError("No successful CI run exists for the source build commit.")


def prepare_files(incoming: Path, publish: Path, tag: str) -> list[Path]:
    archives = list(incoming.glob("*.zip"))
    if len(archives) != 1:
        raise ValueError("Expected exactly one Windows release archive.")
    archive = archives[0]
    digest = sha256(archive)
    checksum = Path(str(archive) + ".sha256").read_text(encoding="utf-8-sig").split()
    if len(checksum) != 2 or checksum[0].lower() != digest or checksum[1].lstrip("*") != archive.name:
        raise ValueError("Build archive does not match its checksum file.")
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("Build archive is corrupt.")
        names = set(bundle.namelist())
        for name in REQUIRED_FILES:
            if "LiveTranscriber/" + name not in names:
                raise ValueError(f"Missing runtime file: {name}")
        if any(Path(name.replace("\\", "/")).name in {"secrets.local.env", "config.local.yaml", ".env"}
               for name in names):
            raise ValueError("Build archive contains local secrets.")
    publish.mkdir(parents=True, exist_ok=True)
    target = publish / f"LiveTranscriber-{tag}-windows-x64.zip"
    shutil.copyfile(archive, target)
    target_checksum = Path(str(target) + ".sha256")
    target_checksum.write_text(f"{digest}  {target.name}\n", encoding="ascii", newline="\n")
    return [target, target_checksum]


def verify_existing(tag: str, release: dict, files: list[Path], directory: Path) -> list[Path]:
    """Return missing assets after checking all existing relevant assets."""
    expected = {path.name: path for path in files}
    assets = {asset["name"] for asset in release.get("assets", [])}
    unexpected = {name for name in assets if name.endswith((".zip", ".sha256"))} - expected.keys()
    if unexpected:
        raise ValueError("Release contains unverified archives/checksums: " + ", ".join(sorted(unexpected)))
    missing = []
    directory.mkdir(parents=True, exist_ok=True)
    for name, path in expected.items():
        if name not in assets:
            missing.append(path)
            continue
        gh("release", "download", tag, "--pattern", name, "--dir", str(directory))
        downloaded = directory / name
        if sha256(downloaded) != sha256(path):
            # Standard PowerShell drafts have CRLF checksum files; a BOM is
            # also harmless. Archives themselves must be byte-identical.
            equivalent_checksum = False
            if name.endswith(".sha256"):
                try:
                    equivalent_checksum = (downloaded.read_text(encoding="utf-8-sig").split()
                                           == path.read_text(encoding="ascii").split())
                except UnicodeError:
                    pass
            if not equivalent_checksum:
                raise ValueError(f"Existing release asset differs from verified build: {name}")
    if missing and not release.get("isDraft"):
        raise ValueError("Published release is missing verified assets; refusing to change it.")
    return missing


def publish_verified(run_id: str, tag: str, repository: str, workspace: Path) -> None:
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Invalid workflow run ID or release tag.")
    run = gh_json("run", "view", run_id, "--json", "conclusion,headSha,workflowName")
    tag_commit = gh_json("api", f"repos/{repository}/commits/{tag}")
    pages = gh_json("api", "--paginate", "--slurp",
                    f"repos/{repository}/actions/runs?head_sha={run['headSha']}&per_page=100")
    checks = [check for page in pages for check in page["workflow_runs"]]
    verify_commit(run, tag_commit, checks)
    incoming = workspace / "incoming"
    gh("run", "download", run_id, "--name", "windows-release", "--dir", str(incoming))
    files = prepare_files(incoming, workspace / "publish", tag)
    release = gh_json("release", "view", tag, "--json", "isDraft,assets")
    missing = verify_existing(tag, release, files, workspace / "existing")
    # Recheck the tag before mutations in case it moved during downloads.
    verify_commit(run, gh_json("api", f"repos/{repository}/commits/{tag}"), checks)
    for path in missing:
        gh("release", "upload", tag, str(path))
    release = gh_json("release", "view", tag, "--json", "isDraft,assets")
    if verify_existing(tag, release, files, workspace / "uploaded"):
        raise ValueError("Release is still missing verified assets after upload.")
    verify_commit(run, gh_json("api", f"repos/{repository}/commits/{tag}"), checks)
    if release.get("isDraft"):
        gh("release", "edit", tag, "--draft=false", "--latest")
        print(f"Published {tag} from verified build {run_id} ({run['headSha']}).")
    else:
        print(f"Release {tag} is already published with the verified assets.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="verified-release-") as temporary:
        publish_verified(args.run_id, args.tag, os.environ["GH_REPO"], Path(temporary))


if __name__ == "__main__":
    main()
