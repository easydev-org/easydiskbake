"""Share a host directory with the guest over SMB.

    from share import Share
    s = Share("/path/on/host", readonly=True)
    args = s.netdev_args(ssh_port=2222, rdp_port=3389)

WHY SMB FOR EVERY PLATFORM

Every guest this repository bakes can mount an SMB share:

    Windows   net use Z: \\\\10.0.2.4\\qemu          (native)
    Linux     mount -t cifs //10.0.2.4/qemu /mnt   (cifs-utils)
    macOS     mount_smbfs //10.0.2.4/qemu /mnt     (native)
    BSD       mount_smbfs //10.0.2.4/qemu /mnt     (native on FreeBSD)

virtiofs cannot do that: it exists for Linux and (with WinFsp plus a service) Windows,
and not at all for macOS or Android. 9p exists for Linux and BSD and nowhere else. SMB
is the only mechanism all four share, and it is also the one a guest can use without
a kernel module.

Android is the exception: its emulator does not implement virtiofs or 9p, and Android
has no SMB client. Files reach it with `adb push`. That is a copy, not a mount, and it
is documented as such in android/README.md.

WHY qemu's smb= AND NOT A SEPARATE smbd

qemu's `-netdev user,...,smb=DIR` forks smbd itself, with a generated config, and
tears it down when qemu exits. Nothing has to be started, supervised or cleaned up,
and the port is internal to the user-mode network - so no host port is opened and no
credentials cross the boundary.

It does need smbd installed: qemu checks CONFIG_SMBD_COMMAND at startup and refuses to
create the netdev if it is missing. `samba` is in the Containerfile for that reason.

WHY THE GUEST SEES 10.0.2.4

User-mode networking gives the guest 10.0.2.15, the host at 10.0.2.2, and the SMB
server at the fourth address in the range, 10.0.2.4. qemu's documentation says so:

    By default the 4th IP in the guest network is used, i.e. x.x.x.4.

Windows does not resolve a name for it, so `\\\\10.0.2.4\\qemu` is what a mount command
uses. A LMHOSTS entry would give it a name; that is not worth a guest-side file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# qemu's user-mode network: the guest is .15, the host is .2, and smbd is .4.
SMB_SERVER = "10.0.2.4"
SMB_SHARE = "qemu"


def log(msg: str) -> None:
    print(f"[share] {msg}", flush=True)


class ShareError(RuntimeError):
    pass


@dataclass
class Share:
    """A host directory exposed to the guest as \\\\10.0.2.4\\qemu.

    `readonly` is qemu's smb= ... there is no readonly flag on the emulated server
    itself; permissions are the host directory's. This field exists so a caller can say
    what it intends and a check can verify the directory matches, rather than silently
    getting something else.
    """

    path: str
    readonly: bool = True
    tag: str = "hostshare"   # unused by SMB; kept for a future 9p path

    def __post_init__(self) -> None:
        if not os.path.isdir(self.path):
            raise ShareError(
                f"share path is not a directory: {self.path}\n"
                f"qemu's smb= needs an existing directory; it does not create one."
            )
        self.path = os.path.abspath(self.path)

        if not os.access(self.path, os.R_OK):
            raise ShareError(f"share path is not readable: {self.path}")
        if not self.readonly and not os.access(self.path, os.W_OK):
            raise ShareError(
                f"share path is not writable but readonly=False was requested: "
                f"{self.path}"
            )

    def netdev_args(self, *, ssh_port: int = 2222, rdp_port: int = 3389,
                    host_ports: bool = True) -> list[str]:
        """The -netdev/-device pair, with SMB enabled.

        The forwards are kept: an SSH or RDP port that was reachable before the share
        was added must stay reachable, or adding a feature breaks a sandbox.
        """
        if not _smbd_available():
            raise ShareError(
                "smbd is not installed, so qemu cannot start its SMB server.\n"
                "qemu checks for it at startup and refuses the netdev if it is missing.\n"
                "The bake image installs `samba`; a host running this needs it too."
            )

        forward = ""
        if host_ports:
            forward = f",hostfwd=tcp::{ssh_port}-:22,hostfwd=tcp::{rdp_port}-:3389"

        return [
            "-netdev", f"user,id=n0{forward},smb={self.path}",
            "-device", "virtio-net-pci,netdev=n0",
        ]

    def guest_path_hint(self, platform: str) -> str:
        """How the guest should mount it. A string for the log, not an action.

        Nothing here mounts anything: doing so needs a command inside the guest, and
        that is the caller's business. This exists so a bake log can say what the guest
        will see, and so tests can assert the address without duplicating it.
        """
        unc = f"\\\\{SMB_SERVER}\\{SMB_SHARE}"
        return {
            "windows": f"net use Z: {unc}",
            "linux": f"mount -t cifs //{SMB_SERVER}/{SMB_SHARE} /mnt/share -o guest",
            "macos": f"mount_smbfs //guest@{SMB_SERVER}/{SMB_SHARE} /mnt/share",
            "bsd": f"mount_smbfs //guest@{SMB_SERVER}/{SMB_SHARE} /mnt/share",
            "android": "not supported - use adb push",
        }.get(platform, "unknown platform")


def _smbd_available() -> bool:
    """qemu looks for CONFIG_SMBD_COMMAND, normally /usr/sbin/smbd."""
    import shutil

    for candidate in ("smbd", "/usr/sbin/smbd", "/usr/local/sbin/smbd"):
        if shutil.which(candidate) or os.path.exists(candidate):
            return True
    return False


def describe(share: Share, platform: str) -> None:
    log(f"{share.path} -> {share.guest_path_hint(platform)}")
