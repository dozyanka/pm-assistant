@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

call :server_helper --check-ollama
set "AI_STATUS=%ERRORLEVEL%"

if "%AI_STATUS%"=="20" (
  echo.
  echo Ollama is not running. Starting local Ollama...
  if exist "%~dp0start_ollama.cmd" (
    start "PM Assistant Ollama" /min "%ComSpec%" /c call "%~dp0start_ollama.cmd"
  ) else if exist "%~dp0..\..\scripts\windows\start_ollama.cmd" (
    start "PM Assistant Ollama" /min "%ComSpec%" /c call "%~dp0..\..\scripts\windows\start_ollama.cmd"
  ) else (
    echo start_ollama.cmd was not found.
    exit /b 20
  )
  for /l %%I in (1,1,20) do (
    timeout /t 1 /nobreak >nul
    call :server_helper --check-ollama >nul 2>nul
    if not errorlevel 1 goto :ollama_ready
  )
  echo Ollama did not become ready in time.
  exit /b 20
)

if "%AI_STATUS%"=="21" (
  echo.
  echo Ollama is running, but one or both required models are missing.
  echo Run:
  echo   ollama pull qwen3.5:9b
  echo   ollama pull qwen3-embedding:0.6b
  exit /b 21
)

:ollama_ready
echo.
echo Ollama and required PM Assistant models are ready.

echo.
call :server_helper
exit /b %ERRORLEVEL%

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
