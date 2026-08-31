@echo off
REM One daily digest: crypto state, holdings, sell review, new IDX ideas.
REM Runs unattended from Task Scheduler at 16:15 WIB (after the IDX close).
REM
REM Output is appended to logs\daily.log. A scheduled job that prints into a
REM console nobody is watching is a job you cannot debug: when the digest stops
REM arriving, this file is the only way to find out why.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
echo. >> "logs\daily.log"
echo ===== %DATE% %TIME% ===== >> "logs\daily.log"
".venv\Scripts\python.exe" -m lumbung.cli daily >> "logs\daily.log" 2>&1
echo exit code %ERRORLEVEL% >> "logs\daily.log"
