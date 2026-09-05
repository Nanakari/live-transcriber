@echo off
setlocal
cd /d "%~dp0"
if exist "main.py" (
    if not exist ".venv\Scripts\python.exe" (
        echo First run: powershell -NoProfile -ExecutionPolicy Bypass -File setup.ps1
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" main.py web --open-browser
    if errorlevel 1 pause
    exit /b
)
if exist "LiveTranscriber.exe" (
    powershell -NoProfile -Command "Start-Process -FilePath (Join-Path (Get-Location) 'LiveTranscriber.exe') -ArgumentList 'web','--open-browser' -WindowStyle Hidden"
    exit /b
)
echo Application not found. Download the Windows ZIP from GitHub Releases and extract the whole folder.
pause
exit /b 1
