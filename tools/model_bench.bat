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
echo Comparing glm-5.3 (low / normal reasoning) and Decision-1 on real assistant tasks (about 2-3 minutes)...
"%PY_EXE%" -X utf8 tools\model_bench.py
echo.
echo Done. Tell Claude it is done. This window closes in 30 seconds.
timeout /t 30 >nul
