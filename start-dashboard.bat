@echo off
REM Phone dashboard (PWA). Open the printed link on your phone, then use
REM Chrome menu -> "Add to Home screen" to install it.
REM
REM Reachable over your home WiFi, or from anywhere via Tailscale.
cd /d "%~dp0"
title Lumbung - Dashboard
".venv\Scripts\python.exe" -m lumbung.cli dashboard
pause
