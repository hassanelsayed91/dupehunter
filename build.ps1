# Build standalone executables with PyInstaller.
#   .\build.ps1                 -> dist\DupeHunter.exe and dist\DupeHunter-CLI.exe
#   .\build.ps1 -SkipInstall    -> reuse an existing PyInstaller install
#
# Each build gets its own --distpath, then the single-file exes are copied to
# dist\ root. Windows file names are case-insensitive, so the CLI exe must not
# be called "dupehunter.exe" next to "DupeHunter.exe".
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$icon = Join-Path $PSScriptRoot "assets\dupehunter.ico"
if (-not (Test-Path $icon)) { throw "Icon not found: $icon" }

if (-not $SkipInstall) {
    python -m pip install --upgrade pyinstaller
}

function Invoke-PyInstaller {
    param([string[]]$ArgList)
    & python -m PyInstaller @ArgList
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
}

Invoke-PyInstaller @(
    "--noconfirm", "--clean", "--onefile", "--windowed",
    "--name", "DupeHunter", "--icon", $icon,
    "--distpath", "dist\gui", "--workpath", "build\gui", "--specpath", "build",
    "run_gui.py"
)

Invoke-PyInstaller @(
    "--noconfirm", "--clean", "--onefile", "--console",
    "--name", "DupeHunter-CLI", "--icon", $icon,
    "--distpath", "dist\cli", "--workpath", "build\cli", "--specpath", "build",
    "run_cli.py"
)

Copy-Item "dist\gui\DupeHunter.exe" "dist\DupeHunter.exe" -Force
Copy-Item "dist\cli\DupeHunter-CLI.exe" "dist\DupeHunter-CLI.exe" -Force
Remove-Item -Recurse -Force "dist\gui", "dist\cli"

Write-Host ""
Write-Host "Done:" -ForegroundColor Green
Get-ChildItem dist | ForEach-Object {
    Write-Host ("  dist\{0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB))
}
