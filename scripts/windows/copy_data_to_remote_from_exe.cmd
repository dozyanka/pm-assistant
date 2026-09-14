@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "SRC=%LOCALAPPDATA%\PM Assistant\data\project_memory.sqlite3"
if not exist "%SRC%" (
  echo Existing Desktop PM Assistant database was not found: %SRC%
  pause
  exit /b 1
)
if exist "%~dp0PM Assistant Remote Server.exe" (
  "%~dp0PM Assistant Remote Server.exe" --copy-db "%SRC%"
) else if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" "%~dp0..\..\remote_server.py" --copy-db "%SRC%"
) else (
  py -3.13 "%~dp0..\..\remote_server.py" --copy-db "%SRC%"
)
pause
