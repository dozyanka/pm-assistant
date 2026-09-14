@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if not exist "%~dp0start_remote_server.cmd" (
  echo start_remote_server.cmd was not found.
  pause
  exit /b 1
)
schtasks /Create /TN "PM Assistant Remote Server" /TR "\"%ComSpec%\" /c \"\"%~dp0start_remote_server.cmd\"\"" /SC ONLOGON /F
if errorlevel 1 (
  echo Could not create the startup task. You can still start the server manually.
  pause
  exit /b 1
)
echo Autostart installed. The full startup chain will check/start Ollama before the server.
pause
