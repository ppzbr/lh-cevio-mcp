<#
.SYNOPSIS
    Remove the in-process MCP bridge from CeVIO AI.

.DESCRIPTION
    CeVIO's own files are never touched by the installer, so undoing it is a
    matter of deleting what was added: the bridge DLL, its build-info, the
    launcher and the Start Menu shortcut.  The shipped CeVIO shortcut still works
    throughout - the bridge only ever loaded through our own launcher.

    CeVIO must be closed first: Windows will not let a loaded DLL be deleted.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File uninstall.ps1
#>
[CmdletBinding()]
param(
    [string]$CeVioDir,
    [switch]$KeepLauncher
)

$ErrorActionPreference = 'Stop'

function Find-CeVio {
    foreach ($c in @(
            'C:\Program Files\CeVIO\CeVIO AI',
            'C:\Program Files (x86)\CeVIO\CeVIO AI')) {
        if (Test-Path (Join-Path $c 'CeVIO AI.exe')) { return $c }
        if (Test-Path (Join-Path $c 'CeVIO AI.orig.exe')) { return $c }
    }
    return $null
}
if (-not $CeVioDir) { $CeVioDir = Find-CeVio }
if (-not $CeVioDir) { throw "CeVIO AI not found. Pass -CeVioDir <install directory>." }
$CeVioDir = (Resolve-Path $CeVioDir).Path

$running = Get-Process -Name 'CeVIO AI', 'CeVIO AI.orig' -ErrorAction SilentlyContinue
if ($running) {
    Write-Warning "CeVIO AI is running (pid $($running.Id -join ', ')). Close it, then run this again."
    return
}

$targets = @(
    (Join-Path $CeVioDir 'LhCeVioBridge.dll'),
    (Join-Path $CeVioDir 'LhCeVioBridge.build-info.txt'),
    (Join-Path $CeVioDir 'LhCeVioBridge.boot.txt'),
    (Join-Path $CeVioDir 'LhCeVioBridge.status.json'),
    (Join-Path $CeVioDir 'LhCeVioBridge.launch.ps1'),
    (Join-Path $CeVioDir 'CeVIO AI (with bridge).cmd')
)
if (-not $KeepLauncher) {
    foreach ($path in $targets) {
        if (Test-Path $path) { Remove-Item $path -Force; Write-Host "removed $path" }
    }
} else {
    foreach ($path in $targets | Where-Object { $_ -notlike '*bridge).cmd' -and $_ -notlike '*.launch.ps1' }) {
        if (Test-Path $path) { Remove-Item $path -Force; Write-Host "removed $path" }
    }
}

$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\CeVIO AI (with MCP bridge).lnk'
if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed $lnk" }

Write-Host ""
Write-Host "CeVIO AI is back to its shipped state. Start it the normal way."
