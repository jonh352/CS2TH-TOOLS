# Backward-compatible entry: prefer Setup (<100MB). Use -OneFile for portable exe.
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1 -OneFile

param([switch]$OneFile)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($OneFile) {
  & powershell -ExecutionPolicy Bypass -File .\build_setup.ps1 -OneFile
  exit $LASTEXITCODE
}

& powershell -ExecutionPolicy Bypass -File .\build_setup.ps1
exit $LASTEXITCODE
