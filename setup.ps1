param([string]$Python = "", [switch]$Gpu, [switch]$Dev)
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
try {
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        if ($Python) { & $Python -m venv .venv }
        elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3.12 -m venv .venv }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv .venv }
        else { throw 'Install Python 3.12, then run setup.ps1 again.' }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the environment. Install Python 3.12 or pass -Python PATH.' }
    }
    & $venvPython -c 'import sys; assert (3,10) <= sys.version_info < (3,13), "Use Python 3.10, 3.11 or 3.12"'
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported Python version.' }
    $requirementsFile = if ($Dev) { 'requirements-dev.txt' } else { 'requirements.txt' }
    & $venvPython -m pip install -r $requirementsFile
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network access and rerun setup.ps1.' }
    if ($Gpu) {
        & $venvPython -m pip install -r requirements-gpu.txt
        if ($LASTEXITCODE -ne 0) { throw 'GPU dependency installation failed.' }
    }
    & $venvPython scripts\prepare_tools.py
    if ($LASTEXITCODE -ne 0) { throw 'Tool setup failed.' }
    Write-Host 'Setup complete. Run start.bat. Transcription models download on first use.'
} finally { Pop-Location }
