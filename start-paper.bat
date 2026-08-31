@echo off
REM Paper trading: no API keys needed, no real money at risk.
REM Prices against the live Indodax order book, fakes only the balance.
cd /d "%~dp0"
title Lumbung - PAPER
echo Starting Lumbung in PAPER mode. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m lumbung.cli run --mode paper
pause
