@echo off
REM Run the local network web server (iPhone/Android browser access)
cd /d "%~dp0"
venv\Scripts\python.exe web_server.py %*
pause
