$ErrorActionPreference = "Continue"

Write-Output "=== dotnet-check ==="

# WHERE IT IS, AND WHETHER IT RUNS.
#
# The ssh form of this check was
#
#     "C:\Program Files\dotnet\dotnet.exe" --version
#
# and in run 41775507806 it returned NOTHING - not a version, not an error, not a "not
# found" from cmd - while winget listed Microsoft.DotNet.SDK.10 as installed. An empty
# answer cannot be acted on, so this prints every step that produced it: the path, whether
# it exists, whether the directory is even there, and what the executable says when its
# output and exit code are captured separately.
#
# THE SDK LAYOUT IS ASKED FOR, NOT ASSUMED. `dotnet --list-sdks` names the versions that
# are actually usable by the muxer; a dotnet.exe with no SDK beside it answers --version
# with an error, and that distinction is the difference between a broken install and a
# check that ran in the wrong place.
#
# THE WORKLOAD IS PART OF THE VERDICT. A dev disk exists to build things, so the check
# compiles and runs a console app rather than only asking for a version number: an SDK that
# lists a version but cannot restore or build is not a toolchain.

$candidates = @(
    (Join-Path $env:ProgramFiles "dotnet\dotnet.exe"),
    "C:\Program Files\dotnet\dotnet.exe"
)

$found = $null
foreach ($p in $candidates) {
    Write-Output ("candidate: " + $p + " exists=" + (Test-Path $p))
    if ((Test-Path $p) -and (-not $found)) { $found = $p }
}

if ($found) {
    $dir = Split-Path $found
    Write-Output ("--- " + $dir + " ---")
    Get-ChildItem $dir -Force -ErrorAction SilentlyContinue |
        Select-Object -First 12 |
        ForEach-Object { Write-Output ("  " + $_.Name) }
    Write-Output ("--- sdk ---")
    Get-ChildItem (Join-Path $dir "sdk") -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Output ("  " + $_.Name) }
}

# THE VERSION, WITH ITS EXIT CODE AND ITS STDERR KEPT APART.
#
# 2>&1 merges them, and a merged stream is what made the ssh check unreadable: an error
# message written to stderr looked like an empty answer once something else consumed it.
if (-not $found) {
    Write-Output "DOTNET_MISSING"
    exit 1
}

Write-Output "--- dotnet --version ---"
$ver = & $found --version 2>$null
$verCode = $LASTEXITCODE
Write-Output ("  exit=" + $verCode + " out=[" + ($ver -join " ") + "]")

Write-Output "--- dotnet --list-sdks ---"
$sdks = & $found --list-sdks 2>&1
$sdkCode = $LASTEXITCODE
$sdks | ForEach-Object { Write-Output ("  " + $_) }
Write-Output ("  exit=" + $sdkCode)

# A BUILD, NOT A VERSION STRING. `dotnet new` + `dotnet run` is the smallest thing that
# proves the SDK can restore, compile and execute.
$work = Join-Path $env:USERPROFILE "dotnet-check"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $work | Out-Null

Write-Output "--- dotnet new console ---"
$newOut = & $found new console --output $work --force 2>&1
$newCode = $LASTEXITCODE
Write-Output ("  exit=" + $newCode)
$newOut | Select-Object -Last 4 | ForEach-Object { Write-Output ("  " + $_) }

Write-Output "--- dotnet build ---"
$buildOut = & $found build $work --nologo 2>&1
$buildCode = $LASTEXITCODE
Write-Output ("  exit=" + $buildCode)
$buildOut | Select-Object -Last 6 | ForEach-Object { Write-Output ("  " + $_) }

Write-Output "--- dotnet run ---"
$runOut = & $found run --project $work 2>&1
$runCode = $LASTEXITCODE
Write-Output ("  exit=" + $runCode)
$runOut | Select-Object -Last 4 | ForEach-Object { Write-Output ("  " + $_) }

if (($verCode -eq 0) -and ($buildCode -eq 0) -and ($runCode -eq 0)) {
    Write-Output "DOTNET_OK"
    exit 0
}

Write-Output "DOTNET_FAILED"
exit 1
