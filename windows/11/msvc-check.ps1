$ErrorActionPreference = "Continue"

Write-Output "=== msvc-check ==="
Write-Output ("OS: " + [System.Environment]::OSVersion.Version.ToString())

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { Write-Output "NO_VSWHERE"; exit 1 }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
Write-Output ("vs path: " + $vs)
if (-not $vs) { Write-Output "NO_VCTOOLS"; exit 1 }

$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
if (-not (Test-Path $vcvars)) { Write-Output "NO_VCVARS"; exit 1 }
Write-Output ("vcvars: " + $vcvars)

# IS cl.exe THERE AT ALL, and what does it say?
$cl = Join-Path $vs "VC\Tools\MSVC"
Write-Output "--- MSVC toolset directories ---"
Get-ChildItem $cl -Directory -ErrorAction SilentlyContinue | ForEach-Object { Write-Output ("  " + $_.Name) }
$clExe = Get-ChildItem $cl -Recurse -Filter cl.exe -ErrorAction SilentlyContinue | Select-Object -First 2
if ($clExe) { $clExe | ForEach-Object { Write-Output ("  cl.exe: " + $_.FullName) } } else { Write-Output "  no cl.exe under VC\Tools\MSVC" }

# Compile through a BATCH FILE, so the vcvars environment and the cl invocation are in one
# cmd context with no quoting between powershell and cmd.
$dir = Join-Path $env:USERPROFILE "msvc-check"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$src = Join-Path $dir "hello.c"
$code = "#include <stdio.h>" + [char]10 + "int main(void){printf(`"hello-from-msvc`");return 0;}" + [char]10
Set-Content -Path $src -Value $code -Encoding Ascii

$bat = Join-Path $dir "build.bat"
$lines = @("@echo off", "call `"$vcvars`"", "cd /d `"$dir`"", "cl /nologo `"$src`"", "echo CL_EXIT=%ERRORLEVEL%", "hello.exe", "echo RUN_EXIT=%ERRORLEVEL%")
Set-Content -Path $bat -Value $lines -Encoding Ascii
Write-Output ("--- build.bat ---")
Get-Content $bat | ForEach-Object { Write-Output ("  " + $_) }

Write-Output "--- running build.bat ---"
$out = & cmd /c $bat 2>&1
$out | ForEach-Object { Write-Output ("  " + $_) }

if ($out -match "hello-from-msvc") { Write-Output "COMPILE_OK" } else { Write-Output "COMPILE_FAILED" }
