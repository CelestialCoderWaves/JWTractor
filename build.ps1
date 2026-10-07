# Builds JWTractor.exe - a single, self-contained Windows executable.
# Run from this folder:   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Output: dist\JWTractor.exe  (share this file with friends; nothing to install)

$ErrorActionPreference = "Stop"

Write-Host "Installing build dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
python -m pip install pyinstaller tkinterdnd2 pillow "websocket-client>=1.9,<2"
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }

Write-Host "Generating icon.ico..." -ForegroundColor Cyan
python make_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

Write-Host "Generating smooth UI graphics..." -ForegroundColor Cyan
python make_toggle_graphics.py
if ($LASTEXITCODE -ne 0) { throw "UI graphics generation failed." }

Write-Host "Building JWTractor.exe..." -ForegroundColor Cyan
# --collect-all tkinterdnd2 bundles the native tkdnd library the package needs.
# --icon sets the .exe's file icon; --add-data ships icon.ico so the running
# window can use it for its title-bar / taskbar icon too.
python -m PyInstaller `
    --noconfirm `
    --onefile `
    --windowed `
    --name JWTractor `
    --icon icon.ico `
    --add-data "icon.ico;." `
    --collect-all tkinterdnd2 `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

if (Test-Path "dist\JWTractor.exe") {
    Write-Host ""
    Write-Host "Done -> dist\JWTractor.exe" -ForegroundColor Green
} else {
    Write-Host "Build failed: dist\JWTractor.exe not found." -ForegroundColor Red
    exit 1
}
