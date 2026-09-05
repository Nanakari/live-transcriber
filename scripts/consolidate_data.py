"""Consolidate known legacy output locations. Dry-run unless --apply is supplied."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PureWindowsPath
import shutil
from datetime import datetime


def contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved.relative_to(root.resolve())
    if path.is_symlink():
        raise ValueError(f"Refusing symbolic link: {path}")
    return resolved


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def consolidate(root: Path, imports: list[Path], *, apply: bool = False) -> dict:
    root = root.resolve()
    report = {'moved': [], 'duplicates_removed': [], 'metadata_updated': [], 'apply': apply}
    pairs = []
    for source_root in [root, *[p.resolve() for p in imports if p.resolve() != root]]:
        outputs = source_root / 'outputs'
        if not outputs.exists():
            continue
        for source in sorted(outputs.rglob('*')):
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
            destination = root / 'outputs' / relative
            if source.resolve() != destination.resolve():
                pairs.append((source, destination, outputs))
    for source, destination, allowed in pairs:
        contained(source, allowed)
        contained(destination, root / 'outputs')
        if destination.exists():
            if digest(source) == digest(destination):
                report['duplicates_removed'].append(str(source))
                if apply:
                    source.unlink()
                continue
            # Never overwrite a distinct result, even when its legacy name collides.
            suffix = digest(source)[:12]
            destination = destination.with_name(f'{destination.stem}-import-{suffix}{destination.suffix}')
            if destination.exists():
                if digest(source) != digest(destination):
                    raise ValueError(f'Conflicting import: {destination}')
                report['duplicates_removed'].append(str(source))
                if apply:
                    source.unlink()
                continue
        report['moved'].append({'from': str(source), 'to': str(destination)})
        if apply:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
    if apply:
        # Repair only known metadata paths whose relocated target actually exists.
        backup = root / 'archive' / 'migration' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        for path in (root / 'outputs' / 'media').rglob('*.json'):
            if path.name != 'analysis.json' and not path.name.endswith('_transcript.json'):
                continue
            try:
                document = json.loads(path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                continue
            meta = document.get('meta', {})
            changed = False
            for key in ('input_file', 'source_audio_file', 'clean_audio_file'):
                value = meta.get(key)
                if not isinstance(value, str) or not value or Path(value).exists():
                    continue
                parts = PureWindowsPath(value).parts
                indexes = [i for i, part in enumerate(parts) if part.lower() == 'outputs']
                if not indexes:
                    continue
                target = root / 'outputs' / Path(*parts[indexes[-1] + 1:])
                contained(target, root / 'outputs')
                if target.is_file():
                    meta[key] = str(target.resolve())
                    changed = True
            if changed:
                saved = backup / path.relative_to(root)
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
                path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
                report['metadata_updated'].append(str(path))
        for source_root in [root, *imports]:
            outputs = source_root / 'outputs'
            if outputs.exists():
                for directory in sorted(outputs.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                    contained(directory, outputs)
                    if directory.is_dir() and not any(directory.iterdir()):
                        directory.rmdir()
        log = root / 'outputs' / 'logs' / 'maintenance'
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
