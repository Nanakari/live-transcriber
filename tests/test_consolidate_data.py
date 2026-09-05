from scripts.consolidate_data import consolidate


def test_consolidation_preserves_conflicts_and_is_repeatable(tmp_path):
    root = tmp_path / 'project'
    old = tmp_path / 'old'
    cached = root / 'outputs/analysis/_cache/chunk.json'
    cached.parent.mkdir(parents=True)
    cached.write_text('cache')
    imported = old / 'outputs/media/group/transcripts/a_transcript.json'
    imported.parent.mkdir(parents=True)
    imported.write_text('{"meta":{},"segments":[]}')
    current = root / 'outputs/media/group/transcripts/a_transcript.json'
    current.parent.mkdir(parents=True)
    current.write_text('{"meta":{},"segments":[1]}')
    report = consolidate(root, [old])
    assert cached.exists() and imported.exists() and len(report['moved']) == 2
    consolidate(root, [old], apply=True)
    assert (root / 'outputs/cache/analysis/chunk.json').read_text() == 'cache'
    assert current.read_text() == '{"meta":{},"segments":[1]}'
    assert len(list(current.parent.glob('*.json'))) == 2
    assert not consolidate(root, [old], apply=True)['moved']


def test_identical_import_can_be_removed(tmp_path):
    root, old = tmp_path / 'new', tmp_path / 'old'
    for base in (root, old):
        path = base / 'outputs/web_jobs/test.log'
        path.parent.mkdir(parents=True)
        path.write_text('same log')
    report = consolidate(root, [old], apply=True)
    assert len(report['duplicates_removed']) == 1
    assert (root / 'outputs/logs/web_jobs/test.log').read_text() == 'same log'
