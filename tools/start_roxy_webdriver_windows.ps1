[CmdletBinding()]
param(
    [string]$DriverPath = "$env:APPDATA\RoxyBrowser\chrome-bin\150\chromedriver.exe",
    [int]$Port = 9515,
    [string]$AllowedIp = "192.168.0.250",
    [int]$HealthIntervalSeconds = 10,
    [int]$UnhealthyLimit = 12,
    [int]$RestartDelaySeconds = 3
)

$ErrorActionPreference = "Stop"
$RuleName = "Roxy ChromeDriver $Port"
$StatusUrl = "http://127.0.0.1:$Port/status"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Start-ElevatedCopy {
    $arguments = @(
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$PSCommandPath`"",
        "-DriverPath", "`"$DriverPath`"",
        "-Port", "$Port",
        "-AllowedIp", "`"$AllowedIp`"",
        "-HealthIntervalSeconds", "$HealthIntervalSeconds",
        "-UnhealthyLimit", "$UnhealthyLimit",
        "-RestartDelaySeconds", "$RestartDelaySeconds"
    )
    Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -Verb RunAs
}

function Ensure-FirewallRule {
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress $AllowedIp `
        -Profile Any | Out-Null
}

function Test-ChromeDriverHttp {
    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri $StatusUrl `
            -TimeoutSec 3
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Get-PortOwner {
    try {
        $connection = Get-NetTCPConnection `
            -LocalPort $Port `
            -State Listen `
            -ErrorAction Stop |
            Select-Object -First 1
        if ($null -ne $connection) {
            return (Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue)
        }
    }
    catch {
        return $null
    }
    return $null
}

function Wait-UntilReady {
    param([int]$TimeoutSeconds = 20)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-ChromeDriverHttp) {
            return $true
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Stop-OwnedChromeDriver {
    param([System.Diagnostics.Process]$Process)

    if ($null -eq $Process -or $Process.HasExited) {
        return
    }
    try {
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline -and (Get-Process -Id $Process.Id -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 200
        }
    }
    catch {
        Write-Warning "Failed to stop ChromeDriver PID $($Process.Id): $($_.Exception.Message)"
    }
}

if (-not (Test-Administrator)) {
    Write-Host "Requesting administrator privileges..." -ForegroundColor Yellow
    Start-ElevatedCopy
    exit 0
}

if (-not (Test-Path -LiteralPath $DriverPath -PathType Leaf)) {
    Write-Host "[ERROR] Roxy ChromeDriver was not found:" -ForegroundColor Red
    Write-Host $DriverPath
    Write-Host "Install RoxyBrowser Chrome core 150 and run this file again."
    Read-Host "Press Enter to close"
    exit 1
}

try {
    Ensure-FirewallRule
}
catch {
    Write-Host "[ERROR] Failed to configure Windows Firewall:" -ForegroundColor Red
    Write-Host $_.Exception.Message
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Roxy ChromeDriver supervisor" -ForegroundColor Cyan
Write-Host "Driver : $DriverPath"
Write-Host "Listen : 0.0.0.0:$Port"
Write-Host "Allow  : $AllowedIp"
Write-Host "Status : $StatusUrl"
Write-Host "Stop   : Press Ctrl+C"
Write-Host ""

while ($true) {
    $existing = Get-PortOwner
    if ($null -ne $existing) {
        if ($existing.ProcessName -notlike "chromedriver*") {
            Write-Host "[ERROR] Port $Port is already used by $($existing.ProcessName) (PID $($existing.Id))." -ForegroundColor Red
            Read-Host "Press Enter to close"
            exit 1
        }
        if (Test-ChromeDriverHttp) {
            Write-Host "[OK] Reusing ChromeDriver PID $($existing.Id)." -ForegroundColor Green
            $process = $existing
        }
        else {
            Write-Warning "ChromeDriver PID $($existing.Id) owns port $Port but does not answer. Restarting it."
            Stop-OwnedChromeDriver -Process $existing
            Start-Sleep -Seconds $RestartDelaySeconds
            $process = $null
        }
    }
    else {
        $process = $null
    }

    if ($null -eq $process) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Starting ChromeDriver..." -ForegroundColor Cyan
        $arguments = @(
            "--port=$Port",
            "--allowed-ips=$AllowedIp"
        )
        try {
            $process = Start-Process `
                -FilePath $DriverPath `
                -ArgumentList $arguments `
                -PassThru `
                -NoNewWindow
        }
        catch {
            Write-Warning "Failed to start ChromeDriver: $($_.Exception.Message)"
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }

        if (-not (Wait-UntilReady -TimeoutSeconds 20)) {
            Write-Warning "ChromeDriver did not become ready within 20 seconds. Restarting it."
            Stop-OwnedChromeDriver -Process $process
            Start-Sleep -Seconds $RestartDelaySeconds
            continue
        }
        Write-Host "[READY] ChromeDriver PID $($process.Id) is listening on port $Port." -ForegroundColor Green
    }

    $failedChecks = 0
    while (-not $process.HasExited) {
        Start-Sleep -Seconds $HealthIntervalSeconds
        if (Test-ChromeDriverHttp) {
            $failedChecks = 0
            continue
        }
        $failedChecks += 1
        Write-Warning "ChromeDriver health check failed ($failedChecks/$UnhealthyLimit)."
        if ($failedChecks -ge $UnhealthyLimit) {
            Write-Warning "ChromeDriver stayed unresponsive for too long. Restarting it."
            Stop-OwnedChromeDriver -Process $process
            break
        }
    }

    if ($process.HasExited) {
        $exitCode = "unknown"
        try {
            $exitCode = $process.ExitCode
        }
        catch {
        }
        Write-Warning "ChromeDriver exited with code $exitCode."
    }
    Start-Sleep -Seconds $RestartDelaySeconds
}
