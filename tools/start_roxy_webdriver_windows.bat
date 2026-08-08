@echo off
setlocal

set "SCRIPT=%~dp0start_roxy_webdriver_windows.ps1"

if not exist "%SCRIPT%" (
    echo [ERROR] PowerShell launcher was not found:
    echo %SCRIPT%
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo PowerShell launcher exited with code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
