"""Relocate known development artifacts, preserving every byte. Dry-run by default.

This is separate from consolidate_data: it never migrates media tasks or edits
their metadata, and never deletes models, results, caches or archived releases.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat


RELOCATIONS = (
    ("outputs/qa", "build/qa"),
    ("outputs/release", "archive/releases/legacy-outputs"),
    ("output", "build/legacy-output"),
)


def _checked_path(path: Path, root: Path) -> Path:
    """Check containment and reject symlinks/junctions before a recursive move."""
    path.relative_to(root)
    for parent in (path, *path.parents):
        if parent == root:
            break
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ValueError(f"Refusing link or junction: {parent}")
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"Path is outside the workspace: {path}")
    return resolved


def _inventory(directory: Path, root: Path) -> dict:
    files = {}
    total = 0
    for base, dirs, names in os.walk(directory, followlinks=False):
        for name in dirs + names:
            _checked_path(Path(base) / name, root)
        for name in sorted(names):
            path = Path(base) / name
            if not path.is_file():
                raise ValueError(f"Not a regular file: {path}")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            size = path.stat().st_size
            files[path.relative_to(directory).as_posix()] = {"bytes": size, "sha256": digest.hexdigest()}
            total += size
    return {"file_count": len(files), "bytes": total, "files": files}


def organize(root: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    if not (root / "main.py").is_file() or not (root / "app/config.py").is_file():
        raise ValueError("Expected a live-transcriber source checkout")
    report = {"root": str(root), "applied": apply, "moves": []}
    for old, new in RELOCATIONS:
        source = _checked_path(root / old, root)
        destination = _checked_path(root / new, root)
        if not source.exists():
            continue
        if not source.is_dir():
            raise ValueError(f"Expected directory: {source}")
        if destination.exists():
            raise FileExistsError(f"Destination already exists; refusing to merge: {destination}")
        report["moves"].append({"from": old, "to": new, **_inventory(source, root)})
    if not apply or not report["moves"]:
        return report

    log_dir = _checked_path(root / "build/maintenance", root)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"organization-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
    report["manifest"] = str(log)
    try:
        for item in report["moves"]:
            source = _checked_path(root / item["from"], root)
            destination = _checked_path(root / item["to"], root)
            if destination.exists() or _inventory(source, root)["files"] != item["files"]:
                raise RuntimeError(f"Files changed after planning: {source}")
            log.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            item["moved"] = True
            if _inventory(destination, root)["files"] != item["files"]:
                raise RuntimeError(f"Post-move verification failed: {destination}")
            item["verified"] = True
    finally:
        log.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = organize(args.root, apply=args.apply)
    # Full hashes are saved in the manifest; keep terminal output readable.
    report["moves"] = [{key: value for key, value in move.items() if key != "files"} for move in report["moves"]]
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
