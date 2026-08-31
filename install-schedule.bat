@echo off
REM Registers the daily digest with Windows Task Scheduler (16:15 every day).
cd /d "%~dp0"
schtasks /create /tn "Lumbung-Daily" /sc daily /st 16:15 /f /tr "\"%~dp0daily-report.bat\""
echo.
echo Registered. Check it with:  schtasks /query /tn Lumbung-Daily
echo Remove it with:             schtasks /delete /tn Lumbung-Daily /f
pause
