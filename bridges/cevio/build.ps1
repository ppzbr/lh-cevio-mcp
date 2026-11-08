# Builds LhCeVioBridge.dll - the in-process MCP bridge for CeVIO AI.
#
# The target is fixed on purpose. CeVIO AI is a .NET Framework 4.8 x64 WPF
# process, so the bridge is compiled against *that* runtime with the framework's
# own csc. Building it for a different runtime produces an assembly the CLR will
# refuse to load, and the diagnostics would not say why - so the target is pinned
# here and reported back in cevio_bridge_status.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# Options:
#   -CeVioDir  <path>   CeVIO AI install directory (auto-detected otherwise)
#   -OutDir    <path>   where the DLL is written (default: this directory)

[CmdletBinding()]
param(
    [string]$CeVioDir,
    [string]$OutDir
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $OutDir) { $OutDir = $here }

function Find-CeVio {
    $candidates = @(
        'C:\Program Files\CeVIO\CeVIO AI',
        'C:\Program Files (x86)\CeVIO\CeVIO AI'
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c 'CeVIO.Song.dll')) { return $c }
    }
    return $null
}

if (-not $CeVioDir) { $CeVioDir = Find-CeVio }
if (-not $CeVioDir -or -not (Test-Path (Join-Path $CeVioDir 'CeVIO.Song.dll'))) {
    throw "CeVIO.Song.dll not found. Pass -CeVioDir <CeVIO AI install directory>."
}

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) {
    throw "csc.exe not found at $csc. .NET Framework 4.x is required."
}

$song = Join-Path $CeVioDir 'CeVIO.Song.dll'
$json = Join-Path $CeVioDir 'Newtonsoft.Json.dll'
if (-not (Test-Path $json)) { throw "Newtonsoft.Json.dll not found in $CeVioDir" }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$out = Join-Path $OutDir 'LhCeVioBridge.dll'

Write-Host "CeVIO dir : $CeVioDir"
Write-Host "csc       : $csc"
Write-Host "output    : $out"

$args = @(
    '/nologo',
    '/target:library',
    '/platform:x64',
    '/optimize+',
    "/out:$out",
    "/r:$song",
    "/r:$json",
    "/r:System.dll",
    "/r:System.Core.dll",
    (Join-Path $here 'LhCeVioBridge.cs')
)

& $csc $args
if ($LASTEXITCODE -ne 0) { throw "compile failed ($LASTEXITCODE)" }

$info = [Reflection.AssemblyName]::GetAssemblyName($out)
$hash = (Get-FileHash $out -Algorithm SHA256).Hash
"built: $($info.FullName)"
"sha256: $hash"
"bytes : $((Get-Item $out).Length)"

# A build-info.txt beside the DLL, in the shape OpenUtau's bridge uses, so a
# client can see what it is talking to without loading it.
$buildInfo = @(
    "Bridge=LhCeVioBridge"
    "Version=0.1.0"
    "Protocol=1"
    "TargetFramework=net48"
    "Platform=x64"
    "BuiltAgainst=CeVIO.Song@$([Reflection.AssemblyName]::GetAssemblyName($song).Version)"
    "Built=$([DateTime]::UtcNow.ToString('o'))"
    "Sha256=$hash"
    "Loader=AppDomainManager|ExecuteInDefaultAppDomain"
)
$buildInfo -join "`n" | Set-Content -Path (Join-Path $OutDir 'LhCeVioBridge.build-info.txt') -Encoding UTF8
"wrote build-info.txt"

# ---------------------------------------------------------------------------
# The native loader, for attaching to a CeVIO that is *already running*.
#
# The AppDomainManager route above needs the assembly in CeVIO's own folder, so
# it needs administrator rights.  The loader does not: it is a plain DLL injected
# into the running editor, and it asks the CLR to run the managed bridge by
# absolute path.  Same bridge, same endpoint, no elevation, no restart.
# ---------------------------------------------------------------------------
function Resolve-Zig {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path $Explicit)) { return (Resolve-Path $Explicit).Path }
    if ($env:ZIG -and (Test-Path $env:ZIG)) { return (Resolve-Path $env:ZIG).Path }
    $onPath = Get-Command zig -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $candidate = & $python.Source -c "import os,ziglang;print(os.path.join(os.path.dirname(ziglang.__file__),'zig.exe'))" 2>$null
        if ($candidate -and (Test-Path $candidate)) { return $candidate.Trim() }
    }
    $cache = Join-Path $env:TEMP 'zig'
    $cached = Join-Path $cache 'zig.exe'
    if (Test-Path $cached) { return $cached }
    return $null
}

$zig = Resolve-Zig
if (-not $zig) {
    Write-Warning "zig not found - the managed bridge is built, but the loader was skipped."
    Write-Warning "  install zig with:  python -m pip install ziglang"
} else {
    $loaderSource = Join-Path $here 'native\LhCeVioLoader.cpp'
    $loaderOut = Join-Path $here 'native\LhCeVioLoader.dll'
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $loaderOut) | Out-Null
    if (Test-Path $loaderOut) {
        try {
            $stream = [IO.File]::Open($loaderOut, 'Open', 'ReadWrite', 'None'); $stream.Close()
        } catch {
            throw "$loaderOut is locked - eject it from CeVIO first"
        }
    }
    Write-Host "zig       : $zig"
    & $zig c++ -target x86_64-windows-gnu -std=c++17 -O2 -shared `
        -DUNICODE -D_UNICODE -D_WIN32_WINNT=0x0601 `
        -o $loaderOut $loaderSource -luser32 -ladvapi32
    if ($LASTEXITCODE -ne 0) { throw "zig c++ failed for the loader ($LASTEXITCODE)" }

    # zig emits a .lib and a .pdb beside a -shared build. Nothing here links
    # against the loader, and the symbols are not wanted in the tree.
    $loaderDir = Split-Path -Parent $loaderOut
    $loaderName = [IO.Path]::GetFileNameWithoutExtension($loaderOut)
    foreach ($extension in @('.lib', '.exp', '.pdb')) {
        Remove-Item (Join-Path $loaderDir ($loaderName + $extension)) -Force -ErrorAction SilentlyContinue
    }

    $loaderHash = (Get-FileHash $loaderOut -Algorithm SHA256).Hash
    "built    : $loaderOut"
    "sha256   : $loaderHash"

    $buildInfo = @(
        "Bridge=LhCeVioBridge"
        "Version=0.1.0"
        "Protocol=1"
        "TargetFramework=net48"
        "Platform=x64"
        "BuiltAgainst=CeVIO.Song@$([Reflection.AssemblyName]::GetAssemblyName($song).Version)"
        "Built=$([DateTime]::UtcNow.ToString('o'))"
        "Sha256=$hash"
        "Loader=LhCeVioLoader.dll"
        "LoaderSha256=$loaderHash"
        "LoaderKind=CLRCreateInstance/metahost -> ExecuteInDefaultAppDomain"
    )
    $buildInfo -join "`n" | Set-Content -Path (Join-Path $OutDir 'LhCeVioBridge.build-info.txt') -Encoding UTF8
    "wrote build-info.txt (with loader)"
}
