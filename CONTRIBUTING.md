# Contributing

Use Python 3.10–3.12 (3.12 recommended) and Node.js 22 LTS. Install the development
environment with `setup.ps1 -Dev`, or install `requirements-dev.txt` in a virtual
environment. See [Development](docs/development.md) for platform and build steps.

## Required checks

```powershell
python -m scripts.check
```

This runs Python regression tests, the Node frontend test and Python compilation.
CI and release builds use the same entry point. Installed local integrations are
opt-in with `--run-local`. Use isolated smoke tests when changing media tools,
player generation, packaged resources or release behavior.

## Change boundaries

- Follow the module responsibilities in [Architecture](docs/architecture.md).
- Keep shared defaults in `app/defaults.yaml`; personal overrides belong in the
  untracked `config.local.yaml` (see `config.local.example.yaml`).
- Keep user results under the configured output root; development artifacts
  belong in `build/`. Never commit credentials, private media, models or logs.
- Preserve existing CLI/API and output contracts, or document the migration.
- Update the PyInstaller spec and run release smoke when adding runtime resources.
- Add focused regression tests for behavior changes and describe validation in
  the pull request. Do not remove unrelated work from a dirty checkout.

Workspace and legacy data maintenance are documented in
[Maintenance](docs/maintenance.md).
