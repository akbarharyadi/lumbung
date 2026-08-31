@echo off
REM LIVE trading: real orders, real money. Requires .env with Indodax keys.
REM The engine asks you to confirm before it starts.
REM
REM IMPORTANT: while this window is closed, your stop-losses are NOT running.
REM Indodax has no server-side stop order.
cd /d "%~dp0"
title Lumbung - LIVE (do not close if a position is open)
echo.
echo  *** LIVE MODE - REAL MONEY ***
echo.
".venv\Scripts\python.exe" -m lumbung.cli run --mode live
pause
