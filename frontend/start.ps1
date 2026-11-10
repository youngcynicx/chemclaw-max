$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path (Split-Path -Parent $PSScriptRoot) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1).Source }
}
if (-not $python) { throw 'Python 3.10+ not found. Create ..\.venv or add Python to PATH.' }
& $python dev_server.py --port 4173
