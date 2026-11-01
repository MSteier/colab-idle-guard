<#
.SYNOPSIS
    Builds Colab Idle Guard: tests, Windows executable, extensions, PDF manual, release ZIP.

.DESCRIPTION
    Output:
      dist\ColabIdleGuard\ColabIdleGuard.exe        PyInstaller one-folder build (windowed)
      dist\colab-idle-guard-<ver>.vsix              VS Code extension
      dist\colab-idle-guard-chrome-<ver>.zip        Chrome extension (for "Load unpacked")
      dist\Colab-Idle-Guard-User-Manual.pdf         PDF user manual
      release\colab-idle-guard-<ver>.zip            Everything, plus full source

    Requires the development environment: .venv (requirements-dev.txt), Node.js/npm,
    vscode_extension\node_modules (npm install) and Google Chrome (for the PDF).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -SkipTests
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipExe,
    [switch]$SkipExtensions,
    [switch]$SkipManual,
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Dist = Join-Path $Root "dist"
$Build = Join-Path $Root "build"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Invoke-Checked([string]$what, [scriptblock]$block) {
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

if (-not (Test-Path $Py)) { throw "Python environment not found at $Py. Create it: py -3.12 -m venv .venv; .venv\Scripts\pip install -r requirements-dev.txt" }
Push-Location $Root
try {
    $Version = (& $Py -c "import colab_idle_guard; print(colab_idle_guard.__version__)").Trim()
    Write-Host "Colab Idle Guard $Version" -ForegroundColor Green
    New-Item -ItemType Directory -Force $Dist, $Build | Out-Null

    Step "Static checks (compile all sources, mypy)"
    Invoke-Checked "compileall" { & $Py -m compileall -q colab_idle_guard scripts tests }
    Invoke-Checked "mypy" { & $Py -m mypy colab_idle_guard --ignore-missing-imports --check-untyped-defs }

    if (-not $SkipTests) {
        Step "Python tests"
        Invoke-Checked "pytest" { & $Py -m pytest -q }
        Step "Chrome extension tests"
        Invoke-Checked "node tests" { node --test "tests/js/*.test.*" }
        Step "VS Code extension tests"
        Push-Location vscode_extension
        try { Invoke-Checked "npm test" { npm test --silent } } finally { Pop-Location }
    }

    if (-not $SkipExe) {
        Step "Windows executable (PyInstaller)"
        $Icon = Join-Path $Build "icon.ico"
        Invoke-Checked "icon" { & $Py scripts\make_icon.py $Icon }
        $DefaultConfig = Join-Path $Root "config\default.json"
        Invoke-Checked "pyinstaller" {
            & $Py -m PyInstaller --noconfirm --clean --windowed --name ColabIdleGuard `
                --icon $Icon --add-data "$DefaultConfig;config" `
                --hidden-import pystray._win32 `
                --distpath $Dist --workpath (Join-Path $Build "pyinstaller") --specpath $Build `
                scripts\pyinstaller_entry.py
        }
    }

    if (-not $SkipExtensions) {
        Step "VS Code extension (.vsix)"
        Push-Location vscode_extension
        try { Invoke-Checked "vsce" { npm run package --silent } } finally { Pop-Location }
        Copy-Item "vscode_extension\colab-idle-guard-$Version.vsix" $Dist -Force

        Step "Chrome extension (.zip)"
        $ChromeZip = Join-Path $Dist "colab-idle-guard-chrome-$Version.zip"
        if (Test-Path $ChromeZip) { Remove-Item $ChromeZip }
        Compress-Archive -Path "chrome_extension\*" -DestinationPath $ChromeZip
    }

    if (-not $SkipManual) {
        Step "PDF user manual"
        Invoke-Checked "manual" { & $Py scripts\build_manual.py (Join-Path $Dist "Colab-Idle-Guard-User-Manual.pdf") }
    }

    if (-not $SkipZip) {
        Step "Release ZIP"
        $Stage = Join-Path $Build "release\colab-idle-guard-$Version"
        if (Test-Path $Stage) { Remove-Item $Stage -Recurse -Force }
        New-Item -ItemType Directory -Force $Stage | Out-Null
        $exe = Join-Path $Dist "ColabIdleGuard"
        if (-not (Test-Path (Join-Path $exe "ColabIdleGuard.exe"))) { throw "Executable missing; run without -SkipExe first." }
        Copy-Item $exe (Join-Path $Stage "ColabIdleGuard") -Recurse
        Copy-Item chrome_extension (Join-Path $Stage "chrome_extension") -Recurse
        Copy-Item (Join-Path $Dist "colab-idle-guard-$Version.vsix") $Stage
        Copy-Item (Join-Path $Dist "Colab-Idle-Guard-User-Manual.pdf") $Stage
        Copy-Item scripts\install.ps1, scripts\uninstall.ps1, README.md, CHANGELOG.md $Stage
        Copy-Item docs (Join-Path $Stage "docs") -Recurse

        # Full source, without environments, build output or VCS data.
        $Src = Join-Path $Stage "source"
        $exclude = @(".venv", ".git", "node_modules", "out", "build", "dist", "release", "__pycache__", ".pytest_cache", ".mypy_cache", ".coverage")
        robocopy $Root $Src /E /XD $exclude /XF *.vsix .coverage /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
        $global:LASTEXITCODE = 0

        $Release = Join-Path $Root "release"
        New-Item -ItemType Directory -Force $Release | Out-Null
        $Zip = Join-Path $Release "colab-idle-guard-$Version.zip"
        if (Test-Path $Zip) { Remove-Item $Zip }
        Compress-Archive -Path "$Stage\*" -DestinationPath $Zip -CompressionLevel Optimal
        $hash = (Get-FileHash $Zip -Algorithm SHA256).Hash
        Set-Content -Path "$Zip.sha256" -Value "$hash  colab-idle-guard-$Version.zip" -Encoding ascii
        Write-Host ("Release: {0} ({1:N1} MB)`nSHA-256: {2}" -f $Zip, ((Get-Item $Zip).Length / 1MB), $hash) -ForegroundColor Green
    }
    Write-Host "`nBuild finished." -ForegroundColor Green
}
finally {
    Pop-Location
}
