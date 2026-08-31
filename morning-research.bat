@echo off
REM Queue the morning research questions. A Claude Code session watching
REM data\research_queue.jsonl answers them; if none is open the file simply
REM accumulates and gets answered next time you open one. The rules-based
REM digest at 16:15 runs regardless and does not depend on this.
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
echo. >> "logs\research.log"
echo ===== %DATE% %TIME% ===== >> "logs\research.log"
".venv\Scripts\python.exe" -m lumbung.cli research >> "logs\research.log" 2>&1
echo exit code %ERRORLEVEL% >> "logs\research.log"
