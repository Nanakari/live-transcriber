param([string]$Version = '0.2.1', [switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
if ($Version -notmatch '^[0-9A-Za-z][0-9A-Za-z._-]*$') { throw 'Invalid version label.' }
Push-Location $projectDir
try {
    if (-not $SkipInstall) { & .\setup.ps1 -Dev }
    $buildPython = Join-Path $projectDir '.venv\Scripts\python.exe'
    & $buildPython -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed; build stopped.' }
    & $buildPython scripts\release_assets.py
    if ($LASTEXITCODE -ne 0) { throw 'Failed to prepare release resources.' }
    $versionDir = Join-Path 'dist' $Version
    & $buildPython -m PyInstaller --noconfirm --clean --distpath $versionDir live_transcriber.spec
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    $releaseDir = Join-Path $projectDir "$versionDir\LiveTranscriber"
    Copy-Item -LiteralPath start.bat,README.md,LICENSE,THIRD_PARTY_NOTICES.md -Destination $releaseDir
    & $buildPython scripts\smoke_release.py $releaseDir
    if ($LASTEXITCODE -ne 0) { throw 'Release smoke checks failed; package was not created.' }
    $archive = Join-Path $projectDir "dist\LiveTranscriber-$Version-windows-x64.zip"
    Compress-Archive -LiteralPath $releaseDir -DestinationPath $archive -Force
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($archive))" | Set-Content -Encoding ascii -LiteralPath "$archive.sha256"
    Write-Host "Release ready: $archive"
} finally { Pop-Location }
