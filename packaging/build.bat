@echo off
rem Builds dist\IriyaPort\ (see packaging\build.py). Uses a private venv in build\venv.
cd /d "%~dp0.."
if not exist build\venv\Scripts\python.exe python -m venv build\venv || goto :fail
build\venv\Scripts\python.exe -m pip install -q -r packaging\requirements.txt || goto :fail
build\venv\Scripts\python.exe packaging\build.py %* || goto :fail
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
