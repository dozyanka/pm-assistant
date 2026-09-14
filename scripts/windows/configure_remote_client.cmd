@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
echo Enter the private HTTPS URL printed by setup_remote_server.cmd.
echo Example: https://my-pc.example-tailnet.ts.net
set /p "PM_REMOTE_URL=Server URL: "
if "%PM_REMOTE_URL%"=="" exit /b 1
if exist "%~dp0PM Assistant Remote.exe" (
  "%~dp0PM Assistant Remote.exe" --save-url "%PM_REMOTE_URL%"
  exit /b %ERRORLEVEL%
)
if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" "%~dp0..\..\remote_client.py" --save-url "%PM_REMOTE_URL%"
  exit /b %ERRORLEVEL%
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3.13 "%~dp0..\..\remote_client.py" --save-url "%PM_REMOTE_URL%"
  exit /b %ERRORLEVEL%
)
python "%~dp0..\..\remote_client.py" --save-url "%PM_REMOTE_URL%"
