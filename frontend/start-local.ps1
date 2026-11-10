param(
    [string]$Db = '',
    [int]$CorePort = 8765,
    [int]$FrontendPort = 4173
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create ..\.venv and install the package first.' }
if (-not $Db) { $Db = Join-Path $root 'data\chemclaw_lab.db' }
if (-not (Test-Path -LiteralPath $Db)) {
    throw "Core database not found: $Db. Run 'chemclaw init-db --db ...' first."
}
$env:PYTHONPATH = Join-Path $root 'src'
$coreArgs = "-m chemclaw_lab.cli serve-api --db `"$Db`" --port $CorePort"
$core = Start-Process -FilePath $python -ArgumentList $coreArgs -NoNewWindow -PassThru
try {
    Write-Host "Core API: http://127.0.0.1:$CorePort"
    Write-Host "Frontend: http://127.0.0.1:$FrontendPort"
    Push-Location $PSScriptRoot
    try { & $python dev_server.py --port $FrontendPort }
    finally { Pop-Location }
}
finally {
    if (-not $core.HasExited) { Stop-Process -Id $core.Id -Force }
}
