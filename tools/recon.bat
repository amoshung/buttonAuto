@echo off
setlocal
cd /d "%~dp0.."
title AI Assistant - UIA recon (read-only)
set "PY_EXE="
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_EXE=C:\Program Files\Python313\python.exe"
) else if exist "C:\ProgramData\miniconda3\python.exe" (
    set "PY_EXE=C:\ProgramData\miniconda3\python.exe"
) else (
    set "PY_EXE=python"
)
if not exist recon mkdir recon
echo Reading the UI structure of Kiro / Antigravity windows (read-only, no clicks or typing)...
"%PY_EXE%" -X utf8 tools\uia_probe.py %*
echo.
echo Checking Kiro CLI / Antigravity CLI (handshake only, no tasks sent)...
"%PY_EXE%" -X utf8 tools\acp_probe.py
echo.
echo Comparing models on real assistant tasks (gemma-4-31b vs GLM)...
"%PY_EXE%" -X utf8 tools\model_bench.py
echo.
echo Done. Results are in the recon folder. Tell Claude it is done. This window closes in 15 seconds.
timeout /t 15 >nul
