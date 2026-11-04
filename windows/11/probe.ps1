$ErrorActionPreference = "Continue"

# ============================================================================
# probe.ps1 <name>  -  is this package ACTUALLY on the machine?
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File probe.ps1 dotnet
#
# EXITS 0 WHEN PRESENT, 1 WHEN ABSENT, AND THAT IS THE WHOLE CONTRACT.
#
# WHY THIS EXISTS
#
# The stage decided whether to install a package by grepping `winget list` for the package
# id, and that answer was wrong in run 41778185290:
#
#     === Microsoft.DotNet.SDK.10 already installed ===      skipped
#     ...
#     candidate: C:\Program Files\dotnet\dotnet.exe exists=False
#     DOTNET_MISSING
#
# winget listed the id and nothing was on disk. The match was a two-segment prefix -
# "Microsoft.DotNet" - so any package under that prefix satisfied it, and a package that
# winget records but whose files are not beside the muxer reads as installed.
#
# Asking winget a second time was never going to fix that: the same question gets the same
# wrong answer. These probes ask the machine instead - does the executable exist, does it
# run, is the directory there - which is the claim the verification step makes later. If
# the probe and the verification disagree, the disk is broken and this is where it shows.
#
# EACH PROBE IS THE THING THE NEXT STAGE NEEDS. Not "a registry key exists": the compiler
# that compiles, the muxer that answers, the shell that runs.

$name = $args[0]
if (-not $name) { Write-Output "usage: probe.ps1 <name>"; exit 2 }

switch ($name) {

    # THE COMPILER. cl.exe under VC\Tools\MSVC in the BuildTools install, found the same way
    # msvc-check.ps1 finds it, because "installed" has to mean "there is a compiler".
    "msvc" {
        $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
        if (-not (Test-Path $vswhere)) { Write-Output "absent: no vswhere"; exit 1 }
        $vs = & $vswhere -latest -products * `
            -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath 2>$null
        if (-not $vs) { Write-Output "absent: no VC tools workload"; exit 1 }
        $cl = Join-Path $vs "VC\Tools\MSVC"
        $exe = Get-ChildItem $cl -Recurse -Filter cl.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like "*Hostx64\x64*" } | Select-Object -First 1
        if (-not $exe) { Write-Output "absent: no cl.exe under $cl"; exit 1 }
        Write-Output ("present: " + $exe.FullName)
        exit 0
    }

    # THE WINDOWS SDK. The versioned include directory is the thing a build needs; the
    # unversioned folder exists even on a machine where the SDK was removed.
    "winsdk" {
        $root = "${env:ProgramFiles(x86)}\Windows Kits\10\Include"
        if (-not (Test-Path $root)) { Write-Output "absent: no Include directory"; exit 1 }
        $ver = Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^\d+\.\d+' } | Sort-Object Name -Descending |
            Select-Object -First 1
        if (-not $ver) { Write-Output "absent: no versioned Include"; exit 1 }
        Write-Output ("present: " + $ver.Name)
        exit 0
    }

    # THE .NET SDK, and the distinction matters: dotnet.exe is the MUXER and it can exist
    # with no SDK beside it, in which case `dotnet --version` fails while
    # "%ProgramFiles%\dotnet" looks installed. Both are required.
    "dotnet" {
        $exe = Join-Path $env:ProgramFiles "dotnet\dotnet.exe"
        if (-not (Test-Path $exe)) { Write-Output "absent: no $exe"; exit 1 }
        $sdk = Join-Path $env:ProgramFiles "dotnet\sdk"
        $versions = @()
        if (Test-Path $sdk) {
            $versions = Get-ChildItem $sdk -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^\d+\.\d+' } | ForEach-Object { $_.Name }
        }
        if ($versions.Count -eq 0) { Write-Output "absent: muxer with no SDK under $sdk"; exit 1 }
        Write-Output ("present: SDK " + ($versions -join " "))
        exit 0
    }

    # POWERSHELL 7, not Windows PowerShell 5.1, which is in the image already and would
    # make this probe pass on every machine.
    "pwsh" {
        $exe = Join-Path $env:ProgramFiles "PowerShell\7\pwsh.exe"
        if (-not (Test-Path $exe)) { Write-Output "absent: no $exe"; exit 1 }
        Write-Output ("present: " + $exe)
        exit 0
    }

    default {
        Write-Output "unknown probe: $name"
        exit 2
    }
}
