param([switch]$NoBundle)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $projectRoot 'ui')
try {
    & npm.cmd ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Unable to install desktop build dependencies.' }
    if ($NoBundle) {
        & npm.cmd run tauri -- build --no-bundle
    } else {
        & npm.cmd run tauri -- build
    }
    if ($LASTEXITCODE -ne 0) { throw 'Windows desktop build failed.' }
} finally {
    Pop-Location
}
