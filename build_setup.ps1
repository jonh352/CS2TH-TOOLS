# Build onedir + Inno Setup installer (target: Setup < 100MB, like CS2CT)
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\build_setup.ps1
# Optional portable onefile (~110MB+):
#   powershell -ExecutionPolicy Bypass -File .\build_setup.ps1 -OneFile

param(
  [switch]$OneFile,
  [switch]$SkipInno
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$versionSource = Get-Content .\core\version.py -Raw
$versionMatch = [regex]::Match(
  $versionSource,
  '__version__\s*=\s*["''](?<version>[^"'']+)["'']'
)
if (-not $versionMatch.Success) {
  throw "Unable to read __version__ from core\version.py"
}
$appVersion = $versionMatch.Groups["version"].Value

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
  python -m venv .venv
}

Write-Host "==> Installing build dependencies..."
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  throw "Node.js/npm is required to build the local Steam trade-up component"
}
Write-Host "==> Installing local Steam trade-up component..."
npm ci --cache .\.npm-cache --ignore-scripts
if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }

$dist = Join-Path $PSScriptRoot "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null

if ($OneFile) {
  Write-Host "==> PyInstaller onefile (portable; usually >100MB)..."
  & .\.venv\Scripts\pyinstaller.exe --noconfirm --clean .\CS2TH-Tools-onefile.spec
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller onefile failed" }
  $exe = Join-Path $dist "CS2TH-Tools.exe"
  if (-not (Test-Path $exe)) { throw "Missing $exe" }
  $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
  Write-Host "Portable exe: $exe ($mb MB)"
  Write-Host "Note: onefile is already zlib-packed; further zip/xz barely shrinks it."
  exit 0
}

Write-Host "==> PyInstaller onedir (input for Inno LZMA)..."
& .\.venv\Scripts\pyinstaller.exe --noconfirm --clean .\CS2TH-Tools.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller onedir failed" }

$appDir = Join-Path $dist "CS2TH-Tools"
$exe = Join-Path $appDir "CS2TH-Tools.exe"
if (-not (Test-Path $exe)) { throw "Missing $exe" }

$dirMB = [math]::Round(
  ((Get-ChildItem $appDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB),
  1
)
Write-Host "Onedir ready: $appDir ($dirMB MB uncompressed on disk)"

function Find-ISCC {
  $candidates = @(
    "${env:LocalAppData}\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
  )
  foreach ($p in $candidates) {
    if ($p -and (Test-Path $p)) { return $p }
  }
  $hit = Get-ChildItem "$env:ProgramFiles","${env:ProgramFiles(x86)}","$env:LocalAppData\Programs" `
    -Recurse -Filter ISCC.exe -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
  return $hit
}

if ($SkipInno) {
  Write-Host "SkipInno set; not building Setup."
  exit 0
}

$iscc = Find-ISCC
if (-not $iscc) {
  Write-Host ""
  Write-Host "Inno Setup 6 not found. Install from https://jrsoftware.org/isinfo.php"
  Write-Host "Then re-run: powershell -ExecutionPolicy Bypass -File .\build_setup.ps1"
  Write-Host "Or compile manually: ISCC.exe /DMyAppVersion=$appVersion installer.iss"
  exit 0
}

Write-Host "==> Compiling Setup with Inno: $iscc"
& $iscc "/DMyAppVersion=$appVersion" .\installer.iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }

$setup = Get-ChildItem (Join-Path $dist "CS2TH-Tools_Setup_*.exe") | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { throw "Setup exe not found in dist\" }

$setupMB = [math]::Round($setup.Length / 1MB, 1)
Write-Host ""
Write-Host "Done: $($setup.FullName)  ($setupMB MB)"
Write-Host "Distribute this Setup (same idea as CS2CT _Setup_*.exe)."
if ($setupMB -gt 100) {
  Write-Host "Warning: Setup still >100MB; check Inno compression / payload size."
}
