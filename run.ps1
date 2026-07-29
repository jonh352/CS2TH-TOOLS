$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "README.md pip install -r requirements.txt"
}

& $python (Join-Path $projectRoot "main.py")
