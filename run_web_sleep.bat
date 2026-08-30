@echo off
REM Sleep-friendly mode: schedules run from Windows Task Scheduler (which wakes
REM the PC), so the in-server scheduler is off and the PC is allowed to sleep.
REM Register the tasks first:  venv\Scripts\python.exe schedule_tasks.py --sync
cd /d "%~dp0"
venv\Scripts\python.exe web_server.py --no-scheduler --allow-sleep %*
pause
