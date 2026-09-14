@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "SRC=%~1"
if not defined SRC (
  echo Enter the full path to the source PM Assistant SQLite database.
  echo Example: C:\path\to\pm-assistant\app_data\project_memory.sqlite3
  set /p "SRC=Database path: "
)
if not exist "%SRC%" (
  echo Database not found: %SRC%
  pause
  exit /b 1
)
call :copy "%SRC%"
pause
exit /b %ERRORLEVEL%

:copy
if exist "%~dp0PM Assistant Remote Server.exe" (
  "%~dp0PM Assistant Remote Server.exe" --copy-db "%~1"
  exit /b %ERRORLEVEL%
)
if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" "%~dp0..\..\remote_server.py" --copy-db "%~1"
  exit /b %ERRORLEVEL%
)
py -3.13 "%~dp0..\..\remote_server.py" --copy-db "%~1"
exit /b %ERRORLEVEL%
