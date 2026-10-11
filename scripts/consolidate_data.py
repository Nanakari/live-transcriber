"""Consolidate known legacy output locations. Dry-run unless --apply is supplied."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import shutil
import stat
import sys
from datetime import datetime

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import output_root


def contained(path: Path, root: Path) -> Path:
    root = root.resolve()
    path.absolute().relative_to(root)
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.exists() or parent.is_symlink():
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError(f'Refusing link or junction: {parent}')
    resolved = path.resolve()
    resolved.relative_to(root)
    return resolved


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def consolidate(root: Path, imports: list[Path], *, apply: bool = False) -> dict:
    root = root.resolve()
    target_outputs = output_root(root=root)
    data_roots = list(dict.fromkeys(p.resolve() for p in [root, *imports]))
    source_outputs = list(dict.fromkeys(output_root(root=p) for p in data_roots))
    for base in source_outputs:
        if any(home.is_relative_to(base) for home in data_roots):
            raise ValueError(f'Output root overlaps a data directory; use a separate result directory: {base}')
    for index, base in enumerate(source_outputs):
        for other in source_outputs[index + 1:]:
            if base.is_relative_to(other) or other.is_relative_to(base):
                raise ValueError(f'Output roots overlap (nested directories): {base}, {other}')
    report = {'moved': [], 'duplicates_removed': [], 'metadata_updated': [], 'apply': apply}
    pairs = []
    relocated: dict[Path, Path] = {}
    for outputs in source_outputs:
        if not outputs.exists():
            continue
        for source in sorted(outputs.rglob('*')):
            contained(source, outputs)
            if not source.is_file():
                continue
            relative = source.relative_to(outputs)
            parts = relative.parts
            if parts[:2] == ('analysis', '_cache'):
                relative = Path('cache', 'analysis', *parts[2:])
            elif parts[:2] == ('media', '_staging'):
                relative = Path('_staging', *parts[2:])
            elif parts[:1] == ('web_jobs',):
                relative = Path('logs', 'web_jobs', *parts[1:])
            elif relative.name == 'web_launcher.log' and len(parts) == 1:
                relative = Path('logs', relative.name)
            destination = target_outputs / relative
            if source.resolve() != destination.resolve():
                pairs.append((source, destination, outputs))
    # Validate every planned source and destination before moving the first file.
    for source, destination, allowed in pairs:
        contained(source, allowed)
        contained(destination, target_outputs)
    for source, destination, allowed in pairs:
        contained(source, allowed)
        contained(destination, target_outputs)
        if destination.exists():
            if digest(source) == digest(destination):
                relocated[source.resolve()] = destination.resolve()
                report['duplicates_removed'].append(str(source))
                if apply:
                    source.unlink()
                continue
            # Never overwrite a distinct result, even when its legacy name collides.
            suffix = digest(source)[:12]
            destination = destination.with_name(f'{destination.stem}-import-{suffix}{destination.suffix}')
            contained(destination, target_outputs)
            if destination.exists():
                if digest(source) != digest(destination):
                    raise ValueError(f'Conflicting import: {destination}')
                report['duplicates_removed'].append(str(source))
                relocated[source.resolve()] = destination.resolve()
                if apply:
                    source.unlink()
                continue
        report['moved'].append({'from': str(source), 'to': str(destination)})
        relocated[source.resolve()] = destination.resolve()
        if apply:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
    if apply:
        # Repair only known metadata paths whose relocated target actually exists.
        backup = root / 'archive' / 'migration' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        for path in (target_outputs / 'media').rglob('*.json'):
            if path.name != 'analysis.json' and not path.name.endswith('_transcript.json'):
                continue
            try:
                contained(path, target_outputs)
                document = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                continue
            if not isinstance(document, dict) or not isinstance(document.get('meta', {}), dict):
                continue
            meta = document.get('meta', {})
            changed = False
            for key in ('input_file', 'source_audio_file', 'clean_audio_file'):
                value = meta.get(key)
                if not isinstance(value, str) or not value or Path(value).exists():
                    continue
                target = relocated.get(Path(value).resolve())
                if target is None:
                    # Retain support for stale Windows metadata imported on a
                    # different host, whose old root is no longer available.
                    parts = PureWindowsPath(value).parts
                    indexes = [i for i, part in enumerate(parts) if part.lower() == 'outputs']
                    if not indexes:
                        continue
                    target = target_outputs / Path(*parts[indexes[-1] + 1:])
                contained(target, target_outputs)
                if target.is_file():
                    meta[key] = str(target.resolve())
                    changed = True
            if changed:
                saved = backup / 'outputs' / path.relative_to(target_outputs)
                contained(saved, root)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
                path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
                report['metadata_updated'].append(str(path))
        for outputs in source_outputs:
            if outputs.exists():
                for directory in sorted(outputs.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                    contained(directory, outputs)
                    if directory.is_dir() and not any(directory.iterdir()):
                        directory.rmdir()
        log = target_outputs / 'logs' / 'maintenance'
        contained(log, target_outputs)
        log.mkdir(parents=True, exist_ok=True)
        (log / f'consolidation-{datetime.now():%Y%m%d-%H%M%S}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--import-from', type=Path, action='append', default=[])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = consolidate(args.root, args.import_from, apply=args.apply)
    print(json.dumps({key: len(value) if isinstance(value, list) else value for key, value in result.items()}))
