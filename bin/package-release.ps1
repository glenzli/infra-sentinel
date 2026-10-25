param([switch]$SkipBuild)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$tauriConfig = Get-Content -LiteralPath (Join-Path $projectRoot 'ui\src-tauri\tauri.conf.json') -Raw | ConvertFrom-Json
$releaseVersion = [string]$tauriConfig.version
if ($releaseVersion -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?$') {
    throw "Unsupported release version: $releaseVersion"
}
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot 'build-desktop-app.ps1')
}

$installerPath = Join-Path $projectRoot "ui\src-tauri\target\release\bundle\nsis\Infra Sentinel_${releaseVersion}_x64-setup.exe"
$installer = Get-Item -LiteralPath $installerPath
if ($installer.VersionInfo.ProductVersion -ne $releaseVersion) {
    throw "Installer version does not match source version $releaseVersion. Rebuild before packaging."
}
$distDirectory = Join-Path $projectRoot 'dist'
[void](New-Item -ItemType Directory -Path $distDirectory -Force)
$assetName = "Infra-Sentinel-${releaseVersion}-windows-x64-unsigned-setup.exe"
$assetPath = Join-Path $distDirectory $assetName
Copy-Item -LiteralPath $installer.FullName -Destination $assetPath -Force
$digest = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(($assetPath + '.sha256'), ($digest + '  ' + $assetName + [Environment]::NewLine), [System.Text.UTF8Encoding]::new($false))
Write-Output "Packaged $assetPath"
Write-Output "SHA256 $digest"
