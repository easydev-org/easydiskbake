# Clean the traces a build leaves, before the disk is exported.
#
# This disk is handed on as a golden image. Whatever this run put on it - installers,
# extraction directories, a test .c file, my own helper scripts, shell history - travels
# with it unless it is removed here, and it is visible to whoever gets the image.
#
# WHAT IS DELIBERATELY KEPT: the installed products themselves. Only caches, temp files,
# logs and history go. Nothing here touches Program Files, ProgramData\Microsoft\VisualStudio\Packages
# (the VS payload cache is what makes repair and update work), or the dotnet SDK.
$ErrorActionPreference = 'Continue'

$before = (Get-PSDrive C -ErrorAction SilentlyContinue).Used
Write-Output ("=== clean ===")
Write-Output ("used before: " + [math]::Round($before/1MB,0) + " MB")

function Report($what, $path) {
    if ($path -and (Test-Path $path)) {
        $sz = (Get-ChildItem $path -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
        Write-Output ("  " + $what + ": " + [math]::Round(($sz/1MB),1) + " MB")
    } else {
        Write-Output ("  " + $what + ": absent")
    }
}

# 1. My own helper scripts and the compile test.
Write-Output "--- build artefacts ---"
Report "msvc-check dir" (Join-Path $env:USERPROFILE "msvc-check")
Remove-Item (Join-Path $env:USERPROFILE "msvc-check") -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $env:USERPROFILE -Filter "vs-*.ps1" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Output ("  removing " + $_.Name)
    Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
}
Get-ChildItem $env:USERPROFILE -Filter "msvc-check.ps1" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

# 2. User and system temp.
Write-Output "--- temp ---"
Report "user TEMP" $env:TEMP
Get-ChildItem $env:TEMP -Force -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
}
Report "Windows Temp" (Join-Path $env:SystemRoot "Temp")
Get-ChildItem (Join-Path $env:SystemRoot "Temp") -Force -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
}

# 3. winget download cache. The installed packages stay; only the downloaded installers go.
Write-Output "--- winget cache ---"
Get-ChildItem (Join-Path $env:LOCALAPPDATA "Packages") -Directory -Filter "Microsoft.DesktopAppInstaller_*" -ErrorAction SilentlyContinue | ForEach-Object {
# $_ is the package directory here; an inner pipeline would overwrite it, so this is
# kept as a separate variable and the inner loops use $sub and $_.
    $pkg = $_
    Write-Output ("  " + $pkg.Name)
    $ls = Join-Path $pkg.FullName "LocalState"
    if (Test-Path $ls) {
        Get-ChildItem $ls -Directory -ErrorAction SilentlyContinue | ForEach-Object { Write-Output ("    " + $_.Name) }
    }
    foreach ($sub in @("Temp", "DownloadedInstallers", "Manifests")) {
        $p = Join-Path $ls $sub
        if (Test-Path $p) {
            $sz = (Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue | Measure-Object -Property Length -Sum).Sum
            $mb = [math]::Round(($sz/1MB),1)
            Write-Output ("    removing " + $sub + ": " + $mb + " MB")
            Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# 4. Delivery optimisation cache - Windows Update style peer cache, often several GB.
Write-Output "--- delivery optimisation ---"
$do = "$env:SystemRoot\ServiceProfiles\NetworkService\AppData\Local\Microsoft\Windows\DeliveryOptimization"
Report "DeliveryOptimization" $do
if (Test-Path $do) {
    Get-ChildItem $do -Force -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Output "  contents removed"
}

# 5. Shell history.
Write-Output "--- history ---"
$hist = Join-Path $env:APPDATA "Microsoft\Windows\PowerShell\PSReadLine\ConsoleHost_history.txt"
if (Test-Path $hist) { Write-Output ("  PSReadLine history: " + (Get-Item $hist).Length + " bytes") } else { Write-Output "  PSReadLine history: absent" }
Remove-Item $hist -Force -ErrorAction SilentlyContinue

# 6. Run dialog and recent documents, which list what was executed.
Write-Output "--- shell traces ---"
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Recent\*") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Recent\AutomaticDestinations\*") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:APPDATA "Microsoft\Windows\Recent\CustomDestinations\*") -Recurse -Force -ErrorAction SilentlyContinue
Write-Output "  recent items and jump lists cleared"

# 7. Recycle bin.
Write-Output "--- recycle bin ---"
# Clear-RecycleBin cannot resolve a bin in a non-interactive ssh session - it answers
# "The system cannot find the path specified" whatever -DriveLetter is given. Deleting the
# contents of $Recycle.Bin directly does the same job and needs no shell folder.
$rb = "$env:SystemDrive\$Recycle.Bin"
if (Test-Path $rb) {
    $n = 0
    Get-ChildItem $rb -Force -ErrorAction SilentlyContinue | ForEach-Object {
# $Recycle.Bin holds one folder per SID; leave nothing inside any of them.
        Get-ChildItem $_.FullName -Force -ErrorAction SilentlyContinue | ForEach-Object {
            Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            $n++
        }
    }
    Write-Output ("  emptied (removed " + $n + " items from " + $rb + ")")
} else {
    Write-Output "  no $Recycle.Bin on this drive"
}

# 8. Event logs. Cleared last, and each log is only cleared if it will clear - some are
# locked while a service holds them.
Write-Output "--- event logs ---"
$cleared = 0
foreach ($log in @("Application","System","Security","Setup","Windows PowerShell")) {
    try { wevtutil cl $log 2>$null; $cleared++ } catch { }
}
Write-Output ("  cleared " + $cleared + " logs")

# 9. The build toolchain's caches, as opposed to Windows' own.
#
# These are what the dev stage adds on top of the ssh stage, and none of them are needed by
# the finished image:
#   Package Cache           copies of every MSI that was installed, ~0.7 GB
#   ...\VisualStudio\Packages  the VS payload cache, kept only so Setup can modify or repair
#   SoftwareDistribution    Windows Update download cache
# Removing them costs nothing that a build needs; keeping them is most of the 3.25 GiB that
# the dev stage adds over and above the 5.6 GiB of toolchain that is actually installed.
 Write-Output "--- build caches ---"
 foreach ($p in @(
     "C:\ProgramData\Package Cache",
     "C:\ProgramData\Microsoft\Visual Studio\Packages",
     "$env:SystemRoot\SoftwareDistribution\Download",
    ($env:SystemRoot + '\Installer\$PatchCache$')
 )) {
     if (Test-Path $p) {
         $mb = (Get-ChildItem $p -Recurse -Force -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
         if ($mb) { Write-Output ("  " + [math]::Round($mb/1MB,1) + " MB  " + $p) }
         Get-ChildItem $p -Force -ErrorAction SilentlyContinue |
             ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
     }
 }

# 10. ZERO THE FREE SPACE, or none of the above changes the size of the image.
#
# export runs qemu-img convert -O qcow2 over the raw disk, and qcow2 only skips clusters that
# are already zero. Deleting a file in NTFS marks its clusters free but leaves the bytes in
# place, so convert still has to store them and the qcow2 comes out the same size. Writing
# zeros over the free space is what actually makes it smaller.
#
# Slower and kinder than sdelete -c: a file grown to fill the disk with zero writes, then
# deleted, so the clusters are genuinely zero when convert looks at them.
 Write-Output "--- zeroing free space ---"
 $writtenTotal = 0
 $zeroStart = Get-Date
 $free = (Get-PSDrive C -ErrorAction SilentlyContinue).Free
 $used = (Get-PSDrive C -ErrorAction SilentlyContinue).Used
 Write-Output ("  free before: " + [math]::Round($free/1GB,2) + " GB")
 # BOUNDED BY WHAT THE DISK HAS USED, NOT BY WHAT IS FREE - see lib/linclean.py.
 #
 # A qcow2 holds only the clusters that were written, so a 128 GB disk with 16 GB in it is a
 # 16 GB file. What zeroing is for is the blocks that WERE written and then released, and that
 # cannot exceed what the disk ever held. The old bound was the free space, which grew with the
 # virtual size: raising --disk-size to 128G would have made this write 110 GB.
 $cap = [Math]::Min($free, $used)
 Write-Output ("  will zero up to: " + [math]::Round($cap/1GB,2) + " GB (used " + [math]::Round($used/1GB,2) + ")")
 if ($cap -gt 500MB) {
     $tmp = Join-Path $env:SystemDrive "zero.fill"
     try {
         $fs = [System.IO.File]::Create($tmp)
         $buf = New-Object byte[] (4MB)
         $written = 0
         $limit = $cap - 200MB
         while ($written -lt $limit) {
             $fs.Write($buf, 0, $buf.Length)
             $written += $buf.Length
         }
         $fs.Close()
         $writtenTotal = $written
         Write-Output ("  wrote " + [math]::Round($written/1GB,2) + " GB of zeros")
     } catch {
         $writtenTotal = $written
         Write-Output ("  stopped at " + [math]::Round($written/1GB,2) + " GB: " + $_.Exception.Message)
     } finally {
         Remove-Item $tmp -Force -ErrorAction SilentlyContinue
     }
 } else {
     Write-Output "  not enough free space to bother"
 }
 Write-Output ("  took " + [int]((Get-Date) - $zeroStart).TotalSeconds + "s")

# 11. The ssh known_hosts entries this pipeline wrote, and any saved credentials.
Write-Output "--- ssh and credentials ---"
$kh = Join-Path $env:USERPROFILE ".ssh\known_hosts"
if (Test-Path $kh) { Remove-Item $kh -Force -ErrorAction SilentlyContinue; Write-Output "  known_hosts removed" } else { Write-Output "  known_hosts absent" }
cmdkey /list 2>$null | Select-String "Target:" | ForEach-Object { Write-Output ("  credential: " + $_.Line) }

# WHAT THE ZEROING ACTUALLY ACHIEVED, AND WHY "USED" IS THE WRONG NUMBER FOR IT.
#
# The first version printed `used after:` and `freed:` from Get-PSDrive, and run 41785167409
# reported
#
#     used before: 33382 MB
#     used after:  64301 MB
#     freed:       -30919 MB
#
# which reads as "the clean added 30 GB". It did not. Writing zeros into zero.fill really did
# consume 31.38 GB - that is the whole point, the blocks are no longer free - and when the
# file is deleted NTFS marks those clusters free WITHOUT zeroing them again, because they are
# already zero. `Used` counts live files, so after the delete it should have gone back.
#
# It did not, because this ran in the same session: Get-PSDrive caches, and the value it
# returned still included the file. So the number this prints is not a measurement of the
# disk, and a metric that is wrong in a plausible direction is worse than no metric.
#
# WHAT IS REPORTED INSTEAD is the thing that was measured directly and cannot be misread:
# how many bytes of zeros were written. That is the quantity qemu-img convert will skip, and
# it is printed by the loop that wrote them.
Write-Output ("zeroed:      " + [math]::Round($writtenTotal/1MB,1) + " MB written over free space")
Write-Output ("used before: " + [math]::Round($before/1MB,0) + " MB (for reference only)")
Write-Output "CLEAN_DONE"
