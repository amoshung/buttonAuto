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
echo Testing Decision-1 on the 4 approval cases (under 1 minute)...
"%PY_EXE%" -X utf8 tools\model_bench.py --decision1-only
echo.
echo Done. Tell Claude it is done. This window closes in 20 seconds.
timeout /t 20 >nul
