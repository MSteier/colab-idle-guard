<#
.SYNOPSIS
    Removes Colab Idle Guard for the current Windows user.

.DESCRIPTION
    Stops the app and removes the program folder, Start-menu shortcuts, the Settings > Apps
    entry, the start-with-Windows entry and (unless -KeepVSCodeExtension) the VS Code extension.
    Your settings, pairing token and logs are kept unless you pass -RemoveUserData.
    The Chrome extension must be removed by you in chrome://extensions (Chrome does not allow
    scripts to remove extensions).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File uninstall.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveUserData
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\ColabIdleGuard"),
    [switch]$RemoveUserData,
    [switch]$KeepVSCodeExtension,
    [switch]$NoShortcuts,
    [switch]$NoRegister
)

$ErrorActionPreference = "Stop"
$AppName = "Colab Idle Guard"

$running = Get-Process -Name ColabIdleGuard -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$InstallDir*" }
if ($running) {
    Write-Host "Stopping $AppName..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# Start-with-Windows entry: only remove it if it points at this installation.
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$cmd = (Get-ItemProperty -Path $run -Name ColabIdleGuard -ErrorAction SilentlyContinue).ColabIdleGuard
if ($cmd -and $cmd -like "*$InstallDir*") {
    Remove-ItemProperty -Path $run -Name ColabIdleGuard
    Write-Host "Removed start-with-Windows entry."
}

if (-not $NoShortcuts) {
    $programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    foreach ($name in @("$AppName.lnk", "$AppName User Manual.lnk")) {
        $p = Join-Path $programs $name
        if (Test-Path $p) { Remove-Item $p -Force }
    }
}
if (-not $NoRegister) {
    Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ColabIdleGuard" -Recurse -Force -ErrorAction SilentlyContinue
}

if (-not $KeepVSCodeExtension -and (Get-Command code -ErrorAction SilentlyContinue)) {
    & code --uninstall-extension colab-idle-guard.colab-idle-guard 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Write-Host "VS Code extension removed." }
}

if (Test-Path $InstallDir) {
    # The script may be running from inside the folder: delete everything else first.
    Get-ChildItem $InstallDir -Force | Where-Object { $_.FullName -ne $PSCommandPath } | Remove-Item -Recurse -Force
    if ($PSCommandPath -like "$InstallDir*") {
        Start-Process -WindowStyle Hidden powershell.exe -ArgumentList "-NoProfile -Command `"Start-Sleep 2; Remove-Item -LiteralPath '$InstallDir' -Recurse -Force`""
    } else {
        Remove-Item $InstallDir -Recurse -Force
    }
    Write-Host "Removed $InstallDir."
}

if ($RemoveUserData) {
    foreach ($d in @((Join-Path $env:APPDATA "ColabIdleGuard"), (Join-Path $env:LOCALAPPDATA "ColabIdleGuard"))) {
        if (Test-Path $d) { Remove-Item $d -Recurse -Force; Write-Host "Removed $d." }
    }
} else {
    Write-Host "Settings, token and logs kept (use -RemoveUserData to delete them)."
}
Write-Host "$AppName uninstalled. Remove the Chrome extension in chrome://extensions." -ForegroundColor Green
