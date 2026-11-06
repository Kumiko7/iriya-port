@echo off
cd /d "%~dp0"
python iriya.py %*
if errorlevel 1 pause
