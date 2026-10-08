@echo off
rem Windows demo launcher: double-click this file or run "demo.cmd [--port 8001] [--no-open]".
rem Creates .venv on first run, installs the app, loads fixture feeds and opens the browser.
setlocal
cd /d "%~dp0"

set "PY="
py -3.12 -c "" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 12))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.12 or newer is required. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH", then run demo.cmd again.
  goto :fail
)

%PY% scripts\demo.py %*
if errorlevel 1 goto :fail
exit /b 0

:fail
if not defined CI pause
exit /b 1
