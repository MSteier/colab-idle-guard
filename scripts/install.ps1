<#
.SYNOPSIS
    Installs (or upgrades) Colab Idle Guard for the current Windows user. No administrator rights needed.

.DESCRIPTION
    - Copies the application to %LOCALAPPDATA%\Programs\ColabIdleGuard (or -InstallDir).
    - Copies the Chrome extension next to it, ready for chrome://extensions > "Load unpacked".
    - Installs the VS Code extension if the "code" command is available (skip with -SkipVSCode).
    - Creates Start-menu shortcuts and an entry in Settings > Apps (skip with -NoShortcuts / -NoRegister).
    - Never touches your settings, pairing token or logs (%APPDATA% / %LOCALAPPDATA%\ColabIdleGuard),
      so upgrading keeps your configuration and the extensions stay paired.

    Run it from the folder that contains ColabIdleGuard\ColabIdleGuard.exe (the release ZIP),
    or from a source checkout after scripts\build.ps1 (it then uses dist\).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\ColabIdleGuard"),
    [switch]$SkipVSCode,
    [switch]$NoShortcuts,
    [switch]$NoRegister,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$AppName = "Colab Idle Guard"
$Here = $PSScriptRoot

function Find-Package {
    foreach ($base in @($Here, (Join-Path $Here "..\dist"), (Join-Path $Here "dist"))) {
        $exe = Join-Path $base "ColabIdleGuard\ColabIdleGuard.exe"
        if (Test-Path $exe) { return (Resolve-Path $base).Path }
    }
    throw "ColabIdleGuard\ColabIdleGuard.exe not found next to this script. Use the release ZIP, or build first (scripts\build.ps1)."
}

function Find-First([string[]]$candidates) {
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return (Resolve-Path $c).Path } }
    return $null
}

$Pkg = Find-Package
$SourceRoot = if (Test-Path (Join-Path $Pkg "chrome_extension")) { $Pkg } else { (Resolve-Path (Join-Path $Pkg "..")).Path }
$ChromeSrc = Find-First @((Join-Path $Pkg "chrome_extension"), (Join-Path $SourceRoot "chrome_extension"))
$Vsix = Get-ChildItem -Path $Pkg -Filter "colab-idle-guard-*.vsix" -ErrorAction SilentlyContinue | Select-Object -First 1
$Manual = Find-First @((Join-Path $Pkg "Colab-Idle-Guard-User-Manual.pdf"))
$Version = if ($Vsix -and $Vsix.BaseName -match '^colab-idle-guard-(.+)$') { $Matches[1] } else { "unknown" }

Write-Host "Installing $AppName to $InstallDir" -ForegroundColor Cyan

# Stop a running copy of this installation so files can be replaced.
$running = Get-Process -Name ColabIdleGuard -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$InstallDir*" }
if ($running) {
    Write-Host "Stopping the running $AppName..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

New-Item -ItemType Directory -Force $InstallDir | Out-Null
robocopy (Join-Path $Pkg "ColabIdleGuard") (Join-Path $InstallDir "app") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying the application failed (robocopy $LASTEXITCODE)" }
if ($ChromeSrc) {
    robocopy $ChromeSrc (Join-Path $InstallDir "chrome_extension") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying the Chrome extension failed (robocopy $LASTEXITCODE)" }
}
$global:LASTEXITCODE = 0
if ($Manual) { Copy-Item $Manual $InstallDir -Force }
Copy-Item (Join-Path $Here "uninstall.ps1") $InstallDir -Force -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $InstallDir "uninstall.ps1"))) {
    Copy-Item (Join-Path $SourceRoot "scripts\uninstall.ps1") $InstallDir -Force
}
$Exe = Join-Path $InstallDir "app\ColabIdleGuard.exe"

if (-not $NoShortcuts) {
    $programs = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Join-Path $programs "$AppName.lnk"))
    $lnk.TargetPath = $Exe
    $lnk.WorkingDirectory = Split-Path $Exe
    $lnk.Description = "Releases idle Google Colab runtimes"
    $lnk.Save()
    if ($Manual) {
        $m = $shell.CreateShortcut((Join-Path $programs "$AppName User Manual.lnk"))
        $m.TargetPath = Join-Path $InstallDir (Split-Path $Manual -Leaf)
        $m.Save()
    }
    Write-Host "Start-menu shortcuts created."
}

if (-not $NoRegister) {
    $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ColabIdleGuard"
    New-Item -Path $key -Force | Out-Null
    $uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallDir 'uninstall.ps1')`""
    $values = @{
        DisplayName = $AppName; DisplayVersion = $Version; Publisher = "Mist Technologies"
        InstallLocation = $InstallDir; DisplayIcon = $Exe; UninstallString = $uninstall
    }
    foreach ($k in $values.Keys) { Set-ItemProperty -Path $key -Name $k -Value $values[$k] }
    Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
    Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord
    Write-Host "Registered in Settings > Apps."
}

if (-not $SkipVSCode) {
    $code = Get-Command code -ErrorAction SilentlyContinue
    if (-not $Vsix) {
        Write-Warning "VS Code extension package (.vsix) not found; skipped."
    } elseif (-not $code) {
        Write-Warning "'code' command not found; install $($Vsix.Name) manually (VS Code > Extensions > ... > Install from VSIX)."
    } else {
        # Remove the copy published under the old "colab-idle-guard" publisher ID, if present.
        & code --uninstall-extension colab-idle-guard.colab-idle-guard 2>$null | Out-Null
        & code --install-extension $Vsix.FullName --force 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Host "VS Code extension installed (reload VS Code windows to activate)." }
        else { Write-Warning "VS Code extension install failed; install $($Vsix.Name) manually." }
    }
}

if (-not $NoLaunch) {
    Start-Process -FilePath $Exe
    Write-Host "$AppName started (look for its icon in the system tray)."
}

Write-Host ""
Write-Host "Done. Next step for Chrome:" -ForegroundColor Green
Write-Host "  1. Open chrome://extensions and turn on Developer mode."
Write-Host "  2. Click 'Load unpacked' and choose: $(Join-Path $InstallDir 'chrome_extension')"
Write-Host "     (If you loaded it from another folder before, remove that copy first.)"
Write-Host "  3. Tray icon > Copy pairing token, then paste it in the extension's settings page."
Write-Host "Automatic release is OFF until you enable it in Settings. See the user manual."
