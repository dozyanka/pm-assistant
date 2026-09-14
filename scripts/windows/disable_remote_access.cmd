@echo off
setlocal
set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not exist "%TS%" set "TS=tailscale.exe"
echo Disabling PM Assistant Tailscale Serve HTTPS endpoint...
"%TS%" serve --https=443 off
if errorlevel 1 (
  echo Could not change Tailscale Serve configuration.
  pause
  exit /b 1
)
echo Remote access disabled. Local PM Assistant data was not changed.
pause
