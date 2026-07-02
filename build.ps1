# Builds JWTractor.exe - a single, self-contained Windows executable.
# Run from this folder:   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Output: dist\JWTractor.exe  (share this file with friends; nothing to install)

$ErrorActionPreference = "Stop"

Write-Host "Installing build dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install pyinstaller tkinterdnd2

Write-Host "Building JWTractor.exe..." -ForegroundColor Cyan
# --collect-all tkinterdnd2 bundles the native tkdnd library the package needs.
python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name JWTractor `
    --collect-all tkinterdnd2 `
    app.py

if (Test-Path "dist\JWTractor.exe") {
    Write-Host ""
    Write-Host "Done -> dist\JWTractor.exe" -ForegroundColor Green
} else {
    Write-Host "Build failed: dist\JWTractor.exe not found." -ForegroundColor Red
    exit 1
}
