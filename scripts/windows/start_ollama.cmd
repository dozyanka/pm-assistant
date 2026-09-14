@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "OLLAMA_EXE="
where ollama.exe >nul 2>nul
if not errorlevel 1 set "OLLAMA_EXE=ollama.exe"
if not defined OLLAMA_EXE if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not defined OLLAMA_EXE if exist "%ProgramFiles%\Ollama\ollama.exe" set "OLLAMA_EXE=%ProgramFiles%\Ollama\ollama.exe"

if not defined OLLAMA_EXE (
  echo Ollama was not found.
  echo Install Ollama from the official distribution, then run:
  echo   ollama pull qwen3.5:9b
  echo   ollama pull qwen3-embedding:0.6b
  pause
  exit /b 1
)

set "OLLAMA_HOST=127.0.0.1:11434"
set "OLLAMA_NO_CLOUD=1"
set "OLLAMA_CONTEXT_LENGTH=8192"
set "OLLAMA_NUM_PARALLEL=1"
set "OLLAMA_MAX_LOADED_MODELS=1"

echo PM Assistant - LOCAL AI ONLY
echo Ollama: %OLLAMA_EXE%
echo Server: http://127.0.0.1:11434
echo Required models: qwen3.5:9b, qwen3-embedding:0.6b
echo.
echo Keep this window open. Press Ctrl+C to stop Ollama.
"%OLLAMA_EXE%" serve
set "RESULT=%ERRORLEVEL%"
echo Ollama exited with code %RESULT%.
pause
exit /b %RESULT%
