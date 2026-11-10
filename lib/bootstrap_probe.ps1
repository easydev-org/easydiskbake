# A bootstrap that proves the whole chain, in the PowerShell Windows already has.
#
#     put this in ~/shared/bootstrap.ps1 before a launch
#
# IT WRITES INTO THE SHARE, AND THAT IS THE POINT. The output is hello.txt beside this script, so
# it lands on the HOST - which is where a person can look at it without a guest running, and which
# means nothing it writes is ever part of the disk being published.
#
# "NOTHING IT WRITES IS EVER PART OF THE DISK" IS A PROPERTY THIS FILE HAS TO ENFORCE, NOT ASSUME.
# Windows has no mount point for this share - it is reached by UNC path - so there is no local
# directory to fall into by accident. What there IS is the launcher's own `net use`, and if that
# session could not be opened, Test-Path fails and this writes nothing. The refusal goes to
# ~/bootstrap-refused.txt instead, which IS on the disk: a refusal nobody can see is worse than a
# small file that explains it, and the clean step removes it.
#
# THE SAME KEYS AS lib/bootstrap_probe.sh, so the self-test reads one shape.

$ErrorActionPreference = 'Continue'

$Home_ = $env:USERPROFILE
$Share = Join-Path $Home_ 'shared'
$Out   = Join-Path $Share 'hello.txt'

# THE SHARE MUST BE REACHABLE, WHICH ON THIS PLATFORM MEANS THE UNC PATH ANSWERS.
#
# `Test-Path` on the script beside this one is the test: it is the same file the launcher waited
# for, so if that resolved, this writes to the share. If it did not, this writes nothing there.
$Stamp = [int][double]::Parse((Get-Date -UFormat %s))

if (-not (Test-Path $Share)) {
    @(
        'hello refused'
        'reason=share is not reachable'
        "path=$Share"
        "user=$env:USERNAME"
        "stamp=$Stamp"
    ) | Set-Content -Path (Join-Path $Home_ 'bootstrap-refused.txt') -Encoding ASCII
    Get-Content (Join-Path $Home_ 'bootstrap-refused.txt')
    exit 0
}

@(
    'hello.ps1'
    "user=$env:USERNAME"
    'shell=powershell'
    'share=mounted'
    "stamp=$Stamp"
) | Set-Content -Path $Out -Encoding ASCII

# AND ON STDOUT, so the launcher's log has it even if the share refused the write.
Get-Content $Out
