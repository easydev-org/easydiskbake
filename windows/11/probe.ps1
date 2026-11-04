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
    #
    # THE PATH IS ASKED FOR, NOT ASSUMED. This probed `$env:ProgramFiles\PowerShell\7\pwsh.exe`
    # and reported MISSING in run 41782341308 while winget had just said
    #
    #     Successfully installed
    #
    # and `pwsh -v` answered PowerShell 7.6.9 in the very next step. So the file was there and
    # the guess was wrong - 7.4 and 7.5 use %ProgramFiles%\PowerShell\7, and an msix or a
    # per-user install does not.
    #
    # `Get-Command` answers where it actually is, which is also what the next stage will use.
    # The exe name alone distinguishes 7 from 5.1: Windows PowerShell is `powershell.exe`.
    "pwsh" {
        $cmd = Get-Command pwsh.exe -ErrorAction SilentlyContinue
        if ($cmd) {
            $v = & $cmd.Source -NoProfile -Command '$PSVersionTable.PSVersion.Major' 2>$null
            if ($v -and ([int]$v -ge 6)) {
                Write-Output ("present: " + $cmd.Source + " v" + $v)
                exit 0
            }
            Write-Output ("absent: pwsh runs but reports major version $v")
            exit 1
        }
        # THE MACHINE'S PATH, NOT THIS PROCESS'S.
        #
        # The MSI writes the new directory into the machine environment. An ssh session that
        # was already open - and this script runs under Windows PowerShell 5.1, started by
        # sshd - keeps the environment sshd captured at login, so `Get-Command` cannot see a
        # product that was installed five seconds ago. Run 41782341308 is the proof: this
        # probe said MISSING and the next step's `pwsh -v` answered PowerShell 7.6.9,
        # because ssh exec starts a NEW process and the new process reads the new PATH.
        $machine = @()
        try {
            $machine = ([Environment]::GetEnvironmentVariable("Path", "Machine") -split ";")
        } catch { }
        $user = @()
        try {
            $user = ([Environment]::GetEnvironmentVariable("Path", "User") -split ";")
        } catch { }

        $candidates = @(
            (Join-Path $env:ProgramFiles "PowerShell\7\pwsh.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "PowerShell\7\pwsh.exe"),
            (Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\pwsh.exe")
        ) + ($machine | Where-Object { $_ } | ForEach-Object { Join-Path $_ "pwsh.exe" }) `
          + ($user    | Where-Object { $_ } | ForEach-Object { Join-Path $_ "pwsh.exe" })

        foreach ($p in $candidates) {
            if ($p -and (Test-Path $p)) {
                $v = & $p -NoProfile -Command '$PSVersionTable.PSVersion.Major' 2>$null
                if ($v -and ([int]$v -ge 6)) { Write-Output ("present: " + $p + " v" + $v); exit 0 }
            }
        }
        Write-Output "absent: no pwsh.exe on the machine path or in the usual places"
        exit 1
    }

    default {
        Write-Output "unknown probe: $name"
        exit 2
    }
}
