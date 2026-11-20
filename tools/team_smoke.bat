@echo off
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
title AI Assistant - office smoke test (real CLIs)
set "PY_EXE="
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_EXE=C:\Program Files\Python313\python.exe"
) else if exist "C:\ProgramData\miniconda3\python.exe" (
    set "PY_EXE=C:\ProgramData\miniconda3\python.exe"
) else (
    set "PY_EXE=python"
)
if not exist recon mkdir recon
echo Office smoke test: each real CLI creates one file in a throwaway sandbox.
echo Usage: tools\team_smoke.bat [--only kiro^|agy^|claude] [--seat glm] [--probe-all] [--sandbox PATH]
echo.
"%PY_EXE%" -X utf8 tools\team_smoke.py %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code %RC% (0 = all passed). Results are in the recon folder. This window closes in 30 seconds.
timeout /t 30
exit /b %RC%
