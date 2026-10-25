@echo off
rem TweetPilot: install the Windows scheduled task that syncs config\ to the server every 5 minutes.
rem Double-click once. Safe to run again.
set "ROOT=%~dp0"
schtasks /create /f /tn "TweetPilot-Sync" /sc minute /mo 5 /tr "wscript.exe \"%ROOT%scripts\windows\run-hidden.vbs\"" >nul
if errorlevel 1 (
  echo [FAILED] Could not create the scheduled task.
  pause
  exit /b 1
)
schtasks /run /tn "TweetPilot-Sync" >nul
echo.
echo [OK] TweetPilot sync installed. It checks config\ every 5 minutes.
echo      Edit config\tweetpilot.env any time; changes reach the server automatically.
echo.
pause
