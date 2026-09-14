@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "ROOT=%~dp0..\.."
cd /d "%ROOT%"
if exist "%ROOT%\.venv\Scripts\python.exe" (
  set "PY=%ROOT%\.venv\Scripts\python.exe"
) else (
  set "PY=py -3.13"
)
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
echo Offline application tests. No model inference or network downloads.
%PY% -m unittest discover -s tests -v
set "RESULT=%ERRORLEVEL%"
pause
exit /b %RESULT%
