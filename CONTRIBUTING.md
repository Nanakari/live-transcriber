# Contributing

## Setup

1. Create and activate a Python 3.10–3.12 virtual environment (3.12 recommended).
2. Install development dependencies with `python -m pip install -r requirements-dev.txt`.
3. Copy `config.local.example.yaml` to `config.local.yaml` only when local overrides are needed.

Never commit credentials, private media, generated transcripts, logs, or local configuration.

## Checks

Run these commands before opening a pull request:

```powershell
python -m pytest -q
python -m compileall -q app main.py
```

Keep pull requests focused and describe any user-visible configuration or output changes.
