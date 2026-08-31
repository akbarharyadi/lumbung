@echo off
REM Publish the dashboard on https://lumbung.example.com
REM Only needed for a manual start -- the Startup folder already does this at
REM logon (Lumbung-Dashboard.vbs + Lumbung-Tunnel.vbs).
cd /d "%~dp0.."
title Lumbung - public dashboard (read-only)

if not exist "%USERPROFILE%\.cloudflared\cert.pem" (
  echo Not logged in to Cloudflare. See tools\setup-tunnel.md
  pause
  exit /b 1
)

REM Port 8788, bound to 127.0.0.1, read-only. See setup-tunnel.md for why.
echo Starting READ-ONLY dashboard on 127.0.0.1:8788 ...
start "Lumbung dashboard (readonly)" /min ".venv\Scripts\python.exe" -m lumbung.cli dashboard --readonly --host 127.0.0.1 --port 8788
timeout /t 8 /nobreak >nul

echo Controls (pause/flat/kill) are off on this deployment - home network only.
echo Starting Cloudflare tunnel...
"tools\cloudflared.exe" tunnel run lumbung
pause
