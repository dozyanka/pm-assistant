@echo off
schtasks /Delete /TN "PM Assistant Remote Server" /F
if errorlevel 1 exit /b 1
echo Autostart removed.
pause
