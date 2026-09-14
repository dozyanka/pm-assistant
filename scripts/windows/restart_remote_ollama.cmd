@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "STARTER=%~dp0start_ollama.cmd"
if not exist "%STARTER%" set "STARTER=%~dp0..\..\scripts\windows\start_ollama.cmd"
if not exist "%STARTER%" (
  echo start_ollama.cmd was not found.
  pause
  exit /b 2
)

echo This will stop locally running ollama.exe processes and restart Ollama
 echo with PM Assistant local-only settings.
choice /C YN /N /M "Continue? [Y/N]: "
if errorlevel 2 exit /b 0

taskkill /F /IM ollama.exe >nul 2>nul
timeout /t 1 /nobreak >nul
start "PM Assistant Ollama" /min "%ComSpec%" /c call "%STARTER%"

echo Waiting for Ollama...
for /l %%I in (1,1,20) do (
  timeout /t 1 /nobreak >nul
  call :server_helper --check-ollama >nul 2>nul
  if not errorlevel 1 goto :ready
)

echo Ollama did not become ready. Check the Ollama window.
pause
exit /b 3

:ready
echo.
call :server_helper --check-ollama
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
