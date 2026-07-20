@echo off
setlocal
cd /d "%~dp0"

set "APP_EXE="
for %%F in (*.exe) do set "APP_EXE=%%~fF"

if not defined APP_EXE (
    echo Application executable was not found.
    pause
    exit /b 1
)

start "Multilingual Media Analyzer" "%APP_EXE%"
