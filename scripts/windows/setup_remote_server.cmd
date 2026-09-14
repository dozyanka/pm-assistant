@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not exist "%TS%" set "TS=%ProgramW6432%\Tailscale\tailscale.exe"
if not exist "%TS%" (
  where tailscale.exe >nul 2>nul
  if not errorlevel 1 set "TS=tailscale.exe"
)
if not exist "%TS%" if /i not "%TS%"=="tailscale.exe" (
  echo Tailscale was not found. Install it, sign in, then retry.
  pause
  exit /b 1
)

call :server_helper --configure
if errorlevel 1 goto :fail

echo.
echo Configuring PRIVATE Tailscale Serve on HTTPS 443...
echo PM Assistant itself remains bound to 127.0.0.1:8765.
"%TS%" serve --bg --https=443 8765
if errorlevel 1 goto :fail
"%TS%" serve status
if errorlevel 1 goto :fail

echo.
echo Remote access configured. No router port forwarding is required.
echo Do NOT enable Tailscale Funnel for PM Assistant.
pause
exit /b 0

:server_helper
if exist "%~dp0PM Assistant Remote Server.exe" (
  "%~dp0PM Assistant Remote Server.exe" %*
  exit /b %ERRORLEVEL%
)
if exist "%~dp0..\..\.venv\Scripts\python.exe" (
  "%~dp0..\..\.venv\Scripts\python.exe" "%~dp0..\..\remote_server.py" %*
  exit /b %ERRORLEVEL%
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3.13 "%~dp0..\..\remote_server.py" %*
  exit /b %ERRORLEVEL%
)
python "%~dp0..\..\remote_server.py" %*
exit /b %ERRORLEVEL%

:fail
echo Remote setup failed. Existing PM Assistant data was not changed.
pause
exit /b 1
