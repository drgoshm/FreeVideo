@echo off
setlocal
title FreeVideo - Radeon RX 9070 XT
cd /d "%~dp0"
echo Starting FreeVideo. The browser will open when the server is ready.
echo Keep this window open while using FreeVideo. Press Ctrl+C to stop.
echo.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-comfy.ps1" -OpenBrowser
set "FreeVideoExit=%ERRORLEVEL%"
if not "%FreeVideoExit%"=="0" (
    echo.
    echo FreeVideo exited with an error. See the message above.
    pause
)
exit /b %FreeVideoExit%
