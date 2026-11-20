@echo off
setlocal
cd /d "%~dp0.."
chcp 65001 >nul
title AI Assistant - ACP trace
set "PY_EXE="
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_EXE=C:\Program Files\Python313\python.exe"
) else if exist "C:\ProgramData\miniconda3\python.exe" (
    set "PY_EXE=C:\ProgramData\miniconda3\python.exe"
) else (
    set "PY_EXE=python"
)
if not exist recon mkdir recon
echo ACP trace: records every JSON-RPC line and stderr of kiro-cli acp (default, v2, v3 cli) to recon\acp-trace-*.log
echo.
"%PY_EXE%" -X utf8 tools\acp_trace.py %*
echo.
echo Done. Send the log file path above. This window closes in 30 seconds.
timeout /t 30
