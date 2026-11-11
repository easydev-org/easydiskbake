"""Write the files a login hook needs, so a walkthrough can copy them one at a time.

    - name: write the launcher
      python: bootstrap_files:launcher
      family: macos
      python_screen: false
      timeout: 60

    - name: copy the launcher in
      scp: [bootstrap-launcher.sh, /tmp/bootstrap-launcher.sh]

WHY THE ARTIFACTS ARE SEPARATE STEPS NOW

Installing a login hook is five things, and one `bootstrap_install:` step used to do all of them:

    1. write the launcher on the host
    2. scp it into the guest
    3. write the mount daemon (macOS) or the task script (Windows)
    4. scp that in too
    5. run the installer and check the result

When any of them failed, the log said `the launcher install exited 1` and named neither the step
nor the file. Worse, the five cannot be checked one at a time: a copy that half-worked left a step
that reported success and a hook that did not run.

SO THE WALKTHROUGH SAYS WHAT IT DOES, ONE FILE AT A TIME

    write the launcher      this module, on the host
    copy the launcher       `scp:`
    write the mount daemon  this module
    copy the mount daemon   `scp:`
    install                 `ssh:`, which is the steps that make it run

Every one of those is a step that can fail on its own and say which file it was.

WHAT THIS MODULE DOES NOT DO: touch the guest. It writes host-side files and returns their paths,
and the `scp:` step after it is what moves them.
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (REPO, os.path.join(REPO, "lib")):
    if path not in sys.path:
        sys.path.insert(0, path)


def log(message: str) -> None:
    print(f"[bootstrap-files] {message}", flush=True)


def _family(step) -> str:
    family = str(getattr(step, "get", lambda k, d=None: d)("family") or "")
    if family not in ("macos", "windows", "linux"):
        raise SystemExit(
            f"{getattr(step, 'name', 'this step')}: needs `family: macos`, `family: windows` or "
            f"`family: linux`, got {family!r}")
    return family


def _work(driver) -> str:
    work = getattr(driver, "work", "") or "."
    os.makedirs(work, exist_ok=True)
    return work


def _write(work: str, name: str, body: str) -> str:
    path = os.path.join(work, name)
    with open(path, "w") as fh:
        fh.write(body)
    log(f"  {name} ({len(body)} bytes)")
    return path


# EVERY VALUE THE LAUNCHER NEEDS COMES FROM hostshare AND net, NOT FROM THE WALKTHROUGH.
#
# The file has to name the server because nothing it could look at survives a reboot, and the
# share's own account because that is what it mounts with. Reading them from the modules that
# create them is what keeps one disk's launcher from naming another disk's server.
def launcher(driver, step, **_kwargs) -> bool:
    """Write the login launcher. macOS and Linux get .sh, Windows .ps1, by the same rule."""
    import bootstrap
    import hostshare
    import net

    family = _family(step)
    work = _work(driver)
    label = getattr(step, "name", "launcher")
    body = bootstrap.launcher_body(
        family, host=net.BRIDGE_IP, user=hostshare.SMB_USER,
        share=hostshare.SHARE_NAME, password=hostshare.SMB_PASSWORD)
    name = bootstrap.launcher_name(family)
    _write(work, name, body)
    log(f"  {label}: wrote {name}; the `scp:` step after this copies it in")
    return True


def smbnetfs_conf(driver, step, **_kwargs) -> bool:
    """Write the two files smbnetfs insists on, for `scp:` to copy into the guest.

    WHY A FILE AND NOT `-o`, WHICH IS WHAT EVERY OTHER CLIENT HERE USES

    Because FUSE refuses an option it does not know and smbnetfs never sees it:

        fuse: unknown option `smb_server=172.30.0.1'

    - measured on run 6d67231. And smbnetfs says what it wants instead:

        This directory should contain at least two files: smb.conf and smbnetfs.conf.

    WHY IT IS WRITTEN HERE AND NOT IN THE SSH BODY

    The body of a `share_mount:` command runs inside `sudo sh -c '...'`, where a heredoc needs
    quotes of its own and a second layer of quoting eats the newlines. Three spellings of that
    command all produced a file smbnetfs read as `Invalid input line : auth` - the line truncated
    at its first word. A file written on the HOST has no shell in the way at all, and `scp:` moves
    it in as bytes.

    smb.conf IS DELIBERATELY MINIMAL: it is libsmbclient's own configuration, and this guest is
    given one server on one bridge. Everything that matters is in smbnetfs.conf.
    """
    import hostshare
    import net

    work = _work(driver)
    label = getattr(step, "name", "smbnetfs")

    _write(work, "smb.conf", (
        "[global]\n"
        "   workgroup = WORKGROUP\n"
        "   client min protocol = SMB2\n"
        "   client max protocol = SMB3\n"
    ))
    # `auth <user>` WITH NO PASSWORD FIELD - the server accepts an empty one and this parser
    # rejects a trailing space as `Invalid input`.
    _write(work, "smbnetfs.conf", (
        f"auth {hostshare.SMB_USER}\n"
        f"smb_server={net.BRIDGE_IP}\n"
        f"smb_share={hostshare.SHARE_NAME}\n"
    ))
    log(f"  {label}: wrote smb.conf and smbnetfs.conf for the `scp:` steps after this")
    return True


def macos_plist(driver, step, **_kwargs) -> bool:
    """The LaunchAgent that runs the launcher at login. macOS only."""
    import bootstrap

    work = _work(driver)
    # `__LAUNCHER__` IS SUBSTITUTED IN THE GUEST, by a `sed` in the install step, because the
    # path is under the account's HOME and this side does not know it. The account arrives in
    # the walkthrough, not here.
    _write(work, "bootstrap.plist", bootstrap.macos_plist("__LAUNCHER__"))
    return True


def macos_mount_script(driver, step, **_kwargs) -> bool:
    """The LaunchDaemon's script: mount the share at boot, as root.

    ROOT BECAUSE A MOUNT IS PRIVILEGED, and a LaunchAgent (the login hook) runs as the user. See
    the note in lib/hostshare.py's mount_commands for why that split exists.
    """
    import bootstrap
    import hostshare
    import net

    work = _work(driver)
    # WHICH HOME THE MOUNT LANDS IN. `~/shared` on macOS is `/Users/<account>/shared`, and the
    # account is a walkthrough var - so it is read from the driver, which is where run_file puts
    # the resolved values.
    values = getattr(driver, "values", {}) or {}
    account = values.get("user") or "vmuser"
    _write(work, "smbmount.sh", bootstrap.macos_mount_script(
        net.BRIDGE_IP, hostshare.SMB_USER, hostshare.SHARE_NAME,
        home=f"/Users/{account}"))
    return True


def macos_mount_plist(driver, step, **_kwargs) -> bool:
    """The LaunchDaemon that runs the mount script at boot."""
    import bootstrap

    work = _work(driver)
    _write(work, "smbmount.plist", bootstrap.macos_mount_plist("__SCRIPT__"))
    return True


def windows_task(driver, step, **_kwargs) -> bool:
    """The `schtasks` script that registers the logon task. Windows only."""
    import bootstrap

    work = _work(driver)
    values = getattr(driver, "values", {}) or {}
    account = values.get("user") or "vmuser"
    launcher_path = f"C:/Users/{account}/{bootstrap.launcher_name('windows')}"
    _write(work, "bootstrap-install.ps1", bootstrap.windows_ps1(launcher_path))
    return True
