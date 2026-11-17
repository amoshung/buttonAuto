@echo off
setlocal
cd /d "%~dp0.."
set "PY_EXE="
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_EXE=C:\Program Files\Python313\python.exe"
) else if exist "C:\ProgramData\miniconda3\python.exe" (
    set "PY_EXE=C:\ProgramData\miniconda3\python.exe"
) else (
    set "PY_EXE=python"
)
echo where kiro-cli:
where kiro-cli
echo.
echo Checking agy / kiro-cli / claude (handshake only, no tasks sent)...
"%PY_EXE%" -X utf8 tools\acp_probe.py
echo.
echo Done. Tell Claude it is done. This window closes in 15 seconds.
timeout /t 15 >nul
