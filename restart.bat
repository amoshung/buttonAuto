@echo off
setlocal
cd /d "%~dp0"
title AI Engineering Assistant - restart

rem Same Python lookup as the old run.bat
set "PY_EXE="
set "PYW_EXE="
if exist "C:\Program Files\Python313\python.exe" (
    set "PY_EXE=C:\Program Files\Python313\python.exe"
    if exist "C:\Program Files\Python313\pythonw.exe" set "PYW_EXE=C:\Program Files\Python313\pythonw.exe"
) else if exist "C:\ProgramData\miniconda3\python.exe" (
    set "PY_EXE=C:\ProgramData\miniconda3\python.exe"
    if exist "C:\ProgramData\miniconda3\pythonw.exe" set "PYW_EXE=C:\ProgramData\miniconda3\pythonw.exe"
) else (
    set "PY_EXE=python"
    set "PYW_EXE=pythonw"
)
if "%PYW_EXE%"=="" set "PYW_EXE=%PY_EXE%"

rem When the web console's "restart" button calls us, the browser tab is already open
set "OPEN_FLAG=--open"
if /i "%~1"=="--from-server" set "OPEN_FLAG=--no-open"

echo.
echo [1/4] Stopping the old assistant (web backend and old desktop panel)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\assistant_ctl.ps1" stop

echo [2/4] Checking Python packages: "%PY_EXE%"
"%PY_EXE%" -c "import cv2, mss, PIL, numpy, uiautomation, requests" 2>nul
if errorlevel 1 (
    echo       Installing required packages...
    "%PY_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo       Package installation failed. See the messages above.
        pause
        exit /b 1
    )
)

echo [3/4] Starting the backend hidden in background (no console window)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PYW_EXE%' -ArgumentList '\"%~dp0server.py\"', '%OPEN_FLAG%' -WorkingDirectory '%~dp0' -WindowStyle Hidden"

echo [4/4] Waiting for the web console...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\assistant_ctl.ps1" wait
if errorlevel 1 (
    pause
    exit /b 1
)
timeout /t 2 >nul
exit /b 0
