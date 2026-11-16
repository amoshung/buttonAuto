@echo off
cd /d "%~dp0"

set "PY_EXE="
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_EXE=C:\Program Files\Python313\python.exe"
) else if exist "C:\ProgramData\miniconda3\python.exe" (
    set "PY_EXE=C:\ProgramData\miniconda3\python.exe"
) else (
    set "PY_EXE=python"
)

echo [AI Engineering Assistant] Using Python: "%PY_EXE%"
"%PY_EXE%" -c "import cv2, mss, PIL, numpy, uiautomation, requests" 2>nul
if errorlevel 1 (
    echo [AI Engineering Assistant] Installing required packages...
    "%PY_EXE%" -m pip install -r requirements.txt
)

echo [AI Engineering Assistant] Starting control panel...
echo [AI Engineering Assistant] Keep this window minimized or close the GUI to exit.
"%PY_EXE%" "%~dp0main.py"
if errorlevel 1 (
    echo.
    echo [AI Engineering Assistant] Exited with an error. Press any key to close...
    pause >nul
)
