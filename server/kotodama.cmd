@echo off
rem Kotodama voice sidecar for Windows (experimental, not yet tested on Windows).
rem Double-click to start it, or run it from a console with a command:
rem   kotodama.cmd doctor
rem   kotodama.cmd test --name Ayame --race 2 "Welcome to Norg."
rem In its window, press R to restart it or Q to quit.
setlocal
cd /d "%~dp0"

set "PY="
rem 1. A virtual environment next to this file (server\.venv), if you made one.
if exist ".venv\Scripts\python.exe" set "PY=".venv\Scripts\python.exe""
rem 2. The py launcher that python.org's installer adds.
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
rem 3. python on PATH, skipping the Microsoft Store stub in WindowsApps,
rem    which only opens the Store.
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i /l /c:"WindowsApps"') do (
        if not defined PY set "PY="%%P""
    )
)
if not defined PY (
    echo Kotodama needs Python 3.10 or newer from https://www.python.org/downloads/
    echo Keep "py launcher" ticked in the installer, then run this again.
    pause
    exit /b 1
)

%PY% kotodama_server.py %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo Kotodama stopped with exit code %CODE%.
    pause
)
exit /b %CODE%
