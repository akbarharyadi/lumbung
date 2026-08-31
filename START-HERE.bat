@echo off
REM ============================================================
REM  Starts everything in PAPER mode: safe, no keys, no real money.
REM  Opens two windows - leave both open. Close them to stop.
REM    1. Dashboard    -> your phone
REM    2. Paper engine -> watches the market, simulates trades
REM
REM  For real money later, close the engine window and run
REM  start-live.bat instead. Keep the dashboard window as it is.
REM ============================================================
cd /d "%~dp0"
title Lumbung launcher

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: virtual environment missing. Run this first:
  echo    uv venv --python 3.13
  echo    uv pip install -e ".[dev]"
  pause
  exit /b 1
)

echo Checking setup...
echo.
call ".venv\Scripts\python.exe" -m lumbung.cli doctor

echo.
echo ============================================================
echo  Opening dashboard and paper engine in separate windows...
echo ============================================================
echo.

start "Lumbung Dashboard" "%~dp0start-dashboard.bat"
timeout /t 5 /nobreak >nul
start "Lumbung Paper Engine" "%~dp0start-paper.bat"

echo Both windows opened.
echo.
echo   *** LEAVE BOTH WINDOWS OPEN ***
echo   They ARE the server and the engine. Closing a window stops it,
echo   and the phone will then show ERR_CONNECTION_REFUSED.
echo.
echo The dashboard window shows the link to open on your phone.
echo.
pause
