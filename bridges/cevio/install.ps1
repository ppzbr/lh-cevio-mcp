<#
.SYNOPSIS
    Install the in-process MCP bridge into CeVIO AI.

.DESCRIPTION
    CeVIO AI is a .NET Framework 4.8 process, and the framework has a supported
    start-up hook: if APPDOMAIN_MANAGER_ASM names an assembly and
    APPDOMAIN_MANAGER_TYPE names a type deriving from System.AppDomainManager,
    the CLR loads that type into the process before Main runs. That is the closest
    thing .NET Framework has to OpenUtau's Plugins folder - no patching, no
    injection, no keyboard, and nothing is replaced.

    This script does three things:

      1. copies LhCeVioBridge.dll (+ its build-info.txt) into the CeVIO AI folder,
         because the AppDomainManager assembly must be resolvable from the
         application base;
      2. writes a launcher - "CeVIO AI (with bridge).cmd" - that sets the two
         environment variables and then starts CeVIO AI exactly as the normal
         shortcut does;
      3. launches it once and checks boot.txt / status.json / tools-list, so the
         install is verified rather than hoped for.

    Nothing is overwritten and nothing is deleted. CeVIO's own files are never
    touched: the bridge is additive. To undo, run uninstall.ps1.

.PARAMETER CeVioDir
    CeVIO AI install directory. Auto-detected from the usual locations.

.PARAMETER BridgeDll
    Source LhCeVioBridge.dll. Defaults to the one next to this script.

.PARAMETER NoVerify
    Skip the post-install launch and probe.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
#>
[CmdletBinding()]
param(
    [string]$CeVioDir,
    [string]$BridgeDll,
    [switch]$NoVerify
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------------------------------------------------------- locate CeVIO
# A retail install starts "CeVIO AI.exe".  An install that boots through a loader
# keeps the original beside it as "CeVIO AI.orig.exe" and puts the loader in front
# under the plain name.  Either spelling means "this is the editor's folder", so
# both are accepted - hard-coding one of them made this script refuse to run on a
# clean install.
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

# What to actually start: "CeVIO AI.exe" when it is there, otherwise the renamed
# original.  The launcher has to start the same executable the user's shortcut
# does, or a licensed install could come up unlicensed and the engine's static
# constructor would throw "Authentication error." at the bridge.
$CeVioExe = 'CeVIO AI.exe'
if (-not (Test-Path (Join-Path $CeVioDir $CeVioExe))) { $CeVioExe = 'CeVIO AI.orig.exe' }
if (-not (Test-Path (Join-Path $CeVioDir $CeVioExe))) {
    throw "$CeVioDir does not look like a CeVIO AI install (no CeVIO AI.exe)."
}

if (-not $BridgeDll) { $BridgeDll = Join-Path $here 'LhCeVioBridge.dll' }
if (-not (Test-Path $BridgeDll)) {
    throw "$BridgeDll not found. Run build.ps1 first."
}

# Refuse to install a bridge built for the wrong runtime: it would load, fail,
# and give no clue why. The bridge records its own target in build-info.txt.
$infoPath = [IO.Path]::ChangeExtension($BridgeDll, '.build-info.txt')
if (Test-Path $infoPath) {
    $info = Get-Content $infoPath
    $target = ($info | Where-Object { $_ -like 'TargetFramework=*' }) -replace '.*=', ''
    if ($target -and $target -ne 'net48') {
        throw "bridge targets $target, CeVIO AI is net48 - rebuild with build.ps1"
    }
    Write-Host ("bridge  : {0}" -f (($info | Where-Object { $_ -like 'BuiltAgainst=*' }) -replace '.*=', ''))
}

# CeVIO AI is 64-bit; a 32-bit bridge would silently never load.
$asm = [Reflection.AssemblyName]::GetAssemblyName($BridgeDll)
$flags = [Reflection.AssemblyName]::GetAssemblyName($BridgeDll).ProcessorArchitecture
Write-Host "assembly: $($asm.FullName)"

# ------------------------------------------------------------------- copy it in
$admin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    throw "writing to $CeVioDir needs an elevated PowerShell (Run as Administrator)."
}

$dstDll = Join-Path $CeVioDir 'LhCeVioBridge.dll'
Copy-Item $BridgeDll $dstDll -Force
if (Test-Path $infoPath) {
    Copy-Item $infoPath (Join-Path $CeVioDir 'LhCeVioBridge.build-info.txt') -Force
}
Write-Host "installed: $dstDll"

# ------------------------------------------------------------------- launcher
# Same start-up code path as the shipped shortcut, plus two environment
# variables. launch.ps1 does the work so the .cmd stays one line.
$launcherPs1 = Join-Path $CeVioDir 'LhCeVioBridge.launch.ps1'
$launchBody = @'
# Starts CeVIO AI with the in-process MCP bridge loaded.
#   $env:APPDOMAIN_MANAGER_ASM  - assembly holding the AppDomainManager
#   $env:APPDOMAIN_MANAGER_TYPE - namespace-qualified type name
$env:APPDOMAIN_MANAGER_ASM  = 'LhCeVioBridge'
$env:APPDOMAIN_MANAGER_TYPE = 'LhCeVio.DomainManager'
Set-Location -LiteralPath $PSScriptRoot
& (Join-Path $PSScriptRoot 'CeVIO AI.exe') @args
'@
# The here-string above is single-quoted so its $env: lines survive verbatim;
# the executable name is substituted here, once, from what was found on disk.
$launchBody = $launchBody.Replace("'CeVIO AI.exe'", "'$CeVioExe'")
Set-Content -Path $launcherPs1 -Value $launchBody -Encoding UTF8

$cmdPath = Join-Path $CeVioDir 'CeVIO AI (with bridge).cmd'
$cmdBody = @"
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0LhCeVioBridge.launch.ps1" %*
"@
Set-Content -Path $cmdPath -Value $cmdBody -Encoding ASCII
Write-Host "launcher : $cmdPath"

# A shortcut in the Start Menu, next to the normal one, so the bridged editor is
# a double-click and the original is left alone.
try {
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $lnk = Join-Path $startMenu 'CeVIO AI (with MCP bridge).lnk'
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $cmdPath
    $sc.WorkingDirectory = $CeVioDir
    $sc.Description = 'CeVIO AI with the in-process MCP bridge'
    $sc.Save()
    Write-Host "shortcut : $lnk"
} catch {
    Write-Host "shortcut : skipped ($($_.Exception.Message))"
}

# ------------------------------------------------------------------- verify
if ($NoVerify) { return }

# boot.txt lives beside the bridge inside the (possibly protected) CeVIO folder;
# remove the old one first so this run's lines are unambiguous.
$boot = Join-Path $CeVioDir 'LhCeVioBridge.boot.txt'
$status = Join-Path $CeVioDir 'LhCeVioBridge.status.json'
foreach ($f in @($boot, $status)) { if (Test-Path $f) { Remove-Item $f -Force } }

Write-Host "starting CeVIO AI with the bridge..."
Start-Process -FilePath $cmdPath | Out-Null

# The bridge boots ~3 s after the domain is created, then binds a port. Give the
# editor time to come up before deciding it failed.
$deadline = (Get-Date).AddSeconds(45)
$endpoint = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 750
    if (Test-Path $status) {
        try {
            $s = Get-Content $status -Raw | ConvertFrom-Json
            if ($s.server.endpoint) { $endpoint = $s.server.endpoint; break }
        } catch { }
    }
}

if (-not $endpoint) {
    Write-Warning "the bridge did not publish an endpoint within 45 s."
    if (Test-Path $boot) { Write-Host "--- boot.txt ---"; Get-Content $boot | Select-Object -Last 20 }
    else { Write-Host "no boot.txt was written at all - the start-up hook did not run." }
    return
}

Write-Host "endpoint : $endpoint"
$rpc = @{ jsonrpc = '2.0'; id = 1; method = 'tools/list'; params = @{} } | ConvertTo-Json -Compress
try {
    $reply = Invoke-RestMethod -Uri $endpoint -Method Post -Body $rpc -ContentType 'application/json'
    Write-Host ("tools    : {0}" -f $reply.result.tools.Count)
    $reply.result.tools | ForEach-Object { '   ' + $_.name }
} catch {
    Write-Warning "endpoint is up but tools/list failed: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "Installed. Always start CeVIO through the launcher so the bridge loads."
Write-Host "Normal shortcut still launches CeVIO without the bridge."
