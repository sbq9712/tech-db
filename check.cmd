@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\check_project.py
) else (
  py -3 scripts\check_project.py
)
if errorlevel 1 pause
