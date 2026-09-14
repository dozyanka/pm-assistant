@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
set "VERSION=1.0.0"
set "BUILD_PY=%ROOT%\.build-venv\Scripts\python.exe"
set "OUT_ROOT=%ROOT%\release\PM-Assistant-Remote-v%VERSION%"

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found. Install Python 3.13.
  pause
  exit /b 1
)
if not exist "%BUILD_PY%" py -3.13 -m venv "%ROOT%\.build-venv"
if errorlevel 1 goto :fail
"%BUILD_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
"%BUILD_PY%" -m pip install -r "%ROOT%\packaging\windows\requirements-remote-build.txt"
if errorlevel 1 goto :fail

set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
echo Running tests...
"%BUILD_PY%" -m unittest discover -s tests -v
if errorlevel 1 goto :fail

if exist "%ROOT%\build" rmdir /s /q "%ROOT%\build"
if exist "%ROOT%\dist" rmdir /s /q "%ROOT%\dist"
"%BUILD_PY%" -m PyInstaller --noconfirm --clean "%ROOT%\packaging\windows\PM_Assistant_Remote_Server.spec"
if errorlevel 1 goto :fail
if exist "%ROOT%\dist\PM Assistant Remote Server\_internal\samples" (
  echo ERROR: private sample data was bundled into the remote server package.
  goto :fail
)
"%BUILD_PY%" -m PyInstaller --noconfirm --clean "%ROOT%\packaging\windows\PM_Assistant_Remote_Client.spec"
if errorlevel 1 goto :fail

if exist "%OUT_ROOT%" rmdir /s /q "%OUT_ROOT%"
mkdir "%OUT_ROOT%\Server"
mkdir "%OUT_ROOT%\Client"
xcopy /e /i /y "%ROOT%\dist\PM Assistant Remote Server\*" "%OUT_ROOT%\Server\" >nul
xcopy /e /i /y "%ROOT%\dist\PM Assistant Remote\*" "%OUT_ROOT%\Client\" >nul
for %%F in (setup_remote_server.cmd start_remote_server.cmd start_ollama.cmd restart_remote_ollama.cmd disable_remote_access.cmd copy_data_to_remote_from_dev.cmd copy_data_to_remote_from_exe.cmd install_remote_server_autostart.cmd remove_remote_server_autostart.cmd) do copy /y "%ROOT%\scripts\windows\%%F" "%OUT_ROOT%\Server\%%F" >nul
copy /y "%ROOT%\scripts\windows\configure_remote_client.cmd" "%OUT_ROOT%\Client\configure_remote_client.cmd" >nul
copy /y "%ROOT%\docs\remote.md" "%OUT_ROOT%\README.md" >nul
copy /y "%ROOT%\SECURITY.md" "%OUT_ROOT%\SECURITY.md" >nul

echo.
echo Remote package completed: %OUT_ROOT%
echo Copy only Client to the client laptop. Keep Server on the AI host.
pause
exit /b 0
:fail
echo Remote build failed.
pause
exit /b 1
