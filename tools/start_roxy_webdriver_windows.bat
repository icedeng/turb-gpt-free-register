@echo off
setlocal EnableExtensions

set "DRIVER=%APPDATA%\RoxyBrowser\chrome-bin\150\chromedriver.exe"
set "PORT=9515"
set "ALLOWED_IP=192.168.0.250"
set "RULE_NAME=Roxy ChromeDriver 9515"

rem Request administrator privileges for the Windows Firewall rule.
fltmc >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)

if not exist "%DRIVER%" (
    echo [ERROR] Roxy ChromeDriver was not found:
    echo %DRIVER%
    echo.
    echo Make sure RoxyBrowser Chrome core 150 is installed.
    pause
    exit /b 1
)

rem Do not start another process when port 9515 is already ready.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:%PORT%/status' -TimeoutSec 2; if ($r.value.ready -eq $true) { exit 0 } else { exit 1 } } catch { exit 1 }"
if not errorlevel 1 (
    echo [OK] Roxy ChromeDriver is already running:
    echo http://192.168.0.90:%PORT%/status
    pause
    exit /b 0
)

rem Recreate the firewall rule and only allow the application server.
netsh advfirewall firewall delete rule name="%RULE_NAME%" >nul 2>&1
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=%PORT% remoteip=%ALLOWED_IP% >nul
if errorlevel 1 (
    echo [ERROR] Failed to create the Windows Firewall rule.
    pause
    exit /b 1
)

echo [START] %DRIVER%
echo [PORT]  0.0.0.0:%PORT%
echo [ALLOW] %ALLOWED_IP%
echo.
echo Keep this window open while turb-gpt is running.
echo.

"%DRIVER%" --port=%PORT% --allowed-ips=%ALLOWED_IP%
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ChromeDriver exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
