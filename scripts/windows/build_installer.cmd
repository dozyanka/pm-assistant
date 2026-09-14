@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
set "VERSION=1.0.0"
set "BUILD_DIR=%ROOT%\release\PM-Assistant-Windows-v%VERSION%"
set "OUTPUT_DIR=%ROOT%\release\installer"
set "ISCC="

if not exist "%BUILD_DIR%\PM Assistant.exe" (
  echo First run scripts\windows\build_desktop.cmd successfully.
  pause
  exit /b 1
)
where ISCC.exe >nul 2>nul
if not errorlevel 1 set "ISCC=ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC (
  echo Inno Setup 6 was not found. Portable build is still usable.
  pause
  exit /b 2
)
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
"%ISCC%" /DBuildDir="%BUILD_DIR%" /DOutputDir="%OUTPUT_DIR%" "%ROOT%\packaging\windows\PM_Assistant.iss"
if errorlevel 1 exit /b 1
echo Installer created in %OUTPUT_DIR%
pause
