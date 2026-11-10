# A bootstrap that proves the whole chain, in the PowerShell Windows already has.
#
#     put this in ~/shared/bootstrap.ps1 before a launch
#
# IT IS THE TEST, NOT A FIXTURE. The chain it has to prove is longer than "something ran":
#
#     the logon task fired
#     the share was mapped by then
#     the disk's own PowerShell was found
#     the script ran as the user
#
# So it writes ~/hello.txt - INSIDE THE GUEST, NOT INTO THE SHARE - and that is deliberate. A
# file written to the share would appear on the host whether or not the guest ever ran anything
# (a mapping is not proof of execution). ~/hello.txt can only exist if the guest ran this.
#
# THE SAME KEYS AS lib/bootstrap_probe.sh, so the self-test reads one shape.

$ErrorActionPreference = 'Continue'

$Home_ = $env:USERPROFILE
$Out = Join-Path $Home_ 'hello.txt'

# THE SHARE IS READ, NOT WRITTEN. If it is missing the launch still proves the task and the
# shell; it only fails to prove the mapping, and it says which.
$Share = Join-Path $Home_ 'shared'
$ShareState = 'MISSING'
if (Test-Path $Share) {
    if (Test-Path (Join-Path $Share 'bootstrap.ps1')) { $ShareState = 'readable' }
}

$Stamp = [int][double]::Parse((Get-Date -UFormat %s))

@(
    'hello.ps1'
    "user=$env:USERNAME"
    'shell=powershell'
    "share=$ShareState"
    "stamp=$Stamp"
) | Set-Content -Path $Out -Encoding ASCII

# AND ON STDOUT, so the launcher's log has it even if the file write was refused.
Get-Content $Out
