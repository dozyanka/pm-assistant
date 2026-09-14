@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
set "VERSION=1.0.0"
set "BUILD_PY=%ROOT%\.build-venv\Scripts\python.exe"
set "OUT_DIR=%ROOT%\release\PM-Assistant-Windows-v%VERSION%"

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
"%BUILD_PY%" -m pip install -r "%ROOT%\packaging\windows\requirements-build.txt"
if errorlevel 1 goto :fail

set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
echo Running tests...
"%BUILD_PY%" -m unittest discover -s tests -v
if errorlevel 1 goto :fail

if exist "%ROOT%\build" rmdir /s /q "%ROOT%\build"
if exist "%ROOT%\dist" rmdir /s /q "%ROOT%\dist"
"%BUILD_PY%" -m PyInstaller --noconfirm --clean "%ROOT%\packaging\windows\PM_Assistant.spec"
if errorlevel 1 goto :fail
if exist "%ROOT%\dist\PM Assistant\_internal\samples" (
  echo ERROR: private sample data was bundled into the desktop package.
  goto :fail
)

if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"
mkdir "%OUT_DIR%"
xcopy /e /i /y "%ROOT%\dist\PM Assistant\*" "%OUT_DIR%\" >nul
copy /y "%ROOT%\docs\windows.md" "%OUT_DIR%\README.md" >nul

echo.
echo Build completed: %OUT_DIR%\PM Assistant.exe
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
