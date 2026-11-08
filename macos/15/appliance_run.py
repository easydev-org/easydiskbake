"""Boot the embedded macOS 15 disk. This is the appliance image's entry point.

    docker run --device /dev/kvm --device /dev/net/tun --cap-add NET_ADMIN \
               -p 6080:6080 -p 2222:2222 easydiskbake:macos15-appliance

WHAT THIS CONTAINER IS

A macOS 15 virtual machine that starts from one command. Everything it boots from is inside
the image and nothing is fetched at run time:

    /assets/recovery.dmg          Apple's recovery image; the APFS driver comes out of it
    /assets/opencore/*            OpenCore, the template, OVMF_CODE and OVMF_VARS
    /assets/disk/part-NNNN        macos15-ssh, split; joined on first start

/ASSETS IS READ-ONLY ON PURPOSE. A start that could write there would be a start whose second
boot differs from its first, and the disk is the one thing here that must not drift.

THE VOLUME, AND WHY THE DISK IS NOT BOOTED FROM /ASSETS

The embedded disk is a finished install: it is the artifact, not a scratch volume. Booting it
directly would let any change a guest makes become part of the image's expectations for the
next boot - and it would be read-only anyway, which macOS treats as a failing system disk.

So the appliance keeps two files on the volume it is given:

    /data/macos15-ssh.qcow2       the joined base, byte-identical to what was published
    /data/run.qcow2               a qcow2 overlay whose backing file is the base
    /data/work/                   OpenCore assets, the firmware vars, identity.json

and boots the overlay. The base stays exactly what the image shipped; the guest's writes
accumulate in the overlay, which is why the volume is worth keeping. Delete the volume and the
next start is a factory boot again.

THE OVERLAY IS REUSED, NOT RECREATED. lib/overlay.py's Overlay.open() deletes a leftover
overlay, which is right for a bake - every run is supposed to start from the same bytes - and
wrong here, where the point of attaching a volume is that the guest's changes survive. This is
the one place that deliberately differs from the bake path, so it is done directly rather than
by giving Overlay a flag that the bakes would then also have.

ONE NETWORK MODE, THE SAME AS CI: A TAP ON A BRIDGE WITH dnsmasq AND MASQUERADE

Not qemu's `-netdev user`. lib/net.py carries the measurement - a slirp guest sat on "Update
Mac Automatically" for six minutes because qemu re-issued its packets without the container's
proxy configuration - and this appliance uses that module rather than a second implementation.
There is no slirp path to fall back to and no environment variable that turns one on.

    guest   172.30.0.2        DHCP from dnsmasq, DNS to the bridge
    bridge  172.30.0.1        MASQUERADE out, DNAT in for 2222 -> 22
    tap     qemutap0

KVM IS REQUIRED AND THERE IS NO SOFTWARE-EMULATION FALLBACK. lib/vm.py states the reason for
the repository and it applies here word for word: TCG is twenty to fifty times slower, so a
guest that boots under it says nothing about the guest that matters. A container without
/dev/kvm stops here with one sentence.

WHAT RUNS AS PID 1

qemu. The entry point execs it, so a signal sent to the container reaches qemu directly and
there is no supervisor to keep alive. websockify is started first, as a detached child, so the
noVNC page is up by the time qemu's VNC server starts listening.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

REPO = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "macos", "15"))

import appliance      # noqa: E402  - this directory
import compress       # noqa: E402
import disk_parts     # noqa: E402
import machine        # noqa: E402
import net            # noqa: E402
import opencore       # noqa: E402
import vm             # noqa: E402


def log(message: str) -> None:
    print(f"[appliance {time.strftime('%H:%M:%S')}] {message}", flush=True)


def need_kvm() -> None:
    """Refuse to start without KVM, rather than starting a machine nobody measured."""
    if os.path.exists("/dev/kvm"):
        return
    raise SystemExit(
        "=== NO /dev/kvm IN THIS CONTAINER ===\n"
        "  macOS cannot be installed or booted without hardware virtualisation, and there is\n"
        "  no software-emulation path here on purpose: a TCG guest runs twenty to fifty times\n"
        "  slower, so every timing this disk was baked against would be wrong.\n"
        "  Pass --device /dev/kvm (and the host needs kvm_intel or kvm_amd loaded).")

def join_base(assets: str, data: str) -> str:
    """A disk baked into the image, joined from its parts, or "" when there is none.

    THIS IS THE OLD PATH, KEPT BECAUSE AN IMAGE MAY STILL CARRY ONE. The default build does not
    - see appliance.py - and `--disk` is how a disk is chosen now. But a Containerfile that
    copies parts into /assets/disk produces a valid image too, and a start against it should
    boot what it shipped rather than failing because the modern path expects a pull.
    """
    directory = os.path.join(assets, "disk")
    if not os.path.isfile(os.path.join(directory, disk_parts.MANIFEST)):
        return ""
    dest = os.path.join(data, os.path.basename(_baked_disk_name(assets)))
    log("=== a disk is baked into this image ===")
    return disk_parts.join(directory, dest)


def _baked_disk_name(assets: str) -> str:
    """What the baked disk calls itself, from its own manifest.

    `parts.json` records `source`, which is the file the parts were cut from - `macos15-ssh`.
    Using it means the volume holds a file named for what it actually is, rather than a generic
    `base.qcow2` that says nothing in `ls`.
    """
    import json
    path = os.path.join(assets, "disk", disk_parts.MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("source") or "disk.qcow2"
    except (OSError, ValueError):
        return "disk.qcow2"


def open_overlay(base: str, data: str, *, plain: bool = False) -> str:
    """Create the boot disk's overlay once, then reuse it.

    THE BASE IS OPENED READ-ONLY BY QEMU while a backing file is in use, so nothing this guest
    does can touch the artifact. That is the same guarantee lib/overlay.py relies on; only the
    lifetime differs.

    `plain` IS FOR A SETUP DISK, WHICH IS AN INSTALLER. machine.TARGET_DRIVE_PLAIN records the
    macos26 run that lost 17 GiB mid-install: macOS writes ~36 GiB, takes an APFS snapshot,
    discards its staging volume and switches the root, and with discard=unmap every TRIM is
    honoured immediately - so the clusters the snapshot still refers to are freed under it and
    the installer stops at "About 48 minutes remaining". A finished disk wants the opposite, so
    the flag follows the stage rather than the container.
    """
    name = "install.qcow2" if plain else "run.qcow2"
    path = os.path.join(data, name)

    if os.path.exists(path):
        info = subprocess.run(
            [compress.qemu_img(), "info", "-U", "--output=json", path],
            capture_output=True, text=True,
        )
        if info.returncode == 0:
            backing = (info.stdout or "").lower()
            if os.path.basename(base).lower() in backing:
                log(f"overlay {path} reused ({os.path.getsize(path) >> 20} MiB written so far)")
                return path
            log(f"overlay {path} backs onto something else; recreating it")
        else:
            log(f"overlay {path} cannot be read ({info.stderr.strip()[:120]}); recreating it")
        os.unlink(path)

    log(f"overlay {path} (backing {os.path.basename(base)})")
    result = subprocess.run(
        [compress.qemu_img(), "create", "-f", "qcow2", "-F", "qcow2",
         "-b", base, path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"could not create the overlay: {(result.stderr or result.stdout).strip()[:300]}")
    return path


def expose_uplink(ssh_port: int) -> None:
    """Make the forwarded ports reachable on the container's own address, not only the bridge.

    lib/net.py DNATs on the BRIDGE address, which is what every client inside a bake uses. A
    container that is published with `-p 2222:2222` is reached on its eth0 address instead, and
    that address has no DNAT rule - so ssh would be refused at the port the runbook names.

    THIS IS THE SAME RULE, ON THE OTHER ADDRESS. It is not a second network mode: VM_FWD is
    net.up()'s own chain, and this adds one PREROUTING jump for one more destination. The
    OUTPUT jump is deliberately NOT added, because that is the rule whose absence from
    127.0.0.1 was measured in lib/net.py - a DNAT'd loopback connection is answered to the
    guest's own loopback and hangs.

    A container with no address of its own is a container with no networking, which net.up()
    has already refused, so a lookup that finds nothing is reported and skipped rather than
    guessed at.
    """
    route = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                           capture_output=True, text=True)
    addresses = [line.split()[3].split("/")[0]
                 for line in (route.stdout or "").splitlines() if len(line.split()) > 3]
    if not addresses:
        log("! no uplink address found; ssh and rdp are reachable on the bridge only")
        return

    for address in addresses:
        check = subprocess.run(["iptables", "-t", "nat", "-C", "PREROUTING", "-d",
                                f"{address}/32", "-p", "tcp", "-j", "VM_FWD"],
                               capture_output=True, text=True)
        if check.returncode == 0:
            continue
        add = subprocess.run(["iptables", "-t", "nat", "-A", "PREROUTING", "-d", f"{address}/32",
                              "-p", "tcp", "-j", "VM_FWD"], capture_output=True, text=True)
        if add.returncode == 0:
            log(f"forwards reachable on {address}:{ssh_port} too")
        else:
            log(f"! could not add the uplink forward for {address}: {add.stderr.strip()[:120]}")


def start_novnc(port: int, vnc: int) -> None:
    """Serve the noVNC page, detached, so it outlives this process being replaced by qemu.

    A MISSING websockify IS NOT A FAILURE TO START. The guest is the product; the browser page
    is how you look at it. A run without the page still has VNC on 5900, which is a real way to
    reach it, so this says what is missing and continues rather than taking the VM down over a
    console script.
    """
    web = "/usr/share/novnc"
    if not os.path.isdir(web):
        log(f"! no {web}; the VM is reachable on VNC port {vnc} and nowhere else")
        return
    if not shutil.which("websockify"):
        log(f"! websockify is not installed; the VM is reachable on VNC port {vnc} only")
        return

    argv = ["websockify", "--web", web, str(port), f"127.0.0.1:{vnc}"]
    log(f"novnc  http://0.0.0.0:{port}/vnc.html  (websockify -> 127.0.0.1:{vnc})")
    subprocess.Popen(argv, stdout=sys.stdout, stderr=subprocess.STDOUT,
                     start_new_session=True)


def _has_baked_disk(assets: str) -> bool:
    return os.path.isfile(os.path.join(assets, "disk", disk_parts.MANIFEST))


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--assets", default=os.environ.get("ASSETS", "/assets"))
    p.add_argument("--data", default=os.environ.get("DATA", "/data"))
    p.add_argument("--work", default="", help="OpenCore work directory (default <data>/work)")
    # THE CHOICE THIS CONTAINER EXISTS FOR. See appliance.DISKS for the three names; NO_DISK is
    # a real answer and not an absence - it is how a person gets to Recovery.
    p.add_argument("--disk", default=os.environ.get("VM_DISK", "none"),
                   help="none (Recovery), setup, desktop, ssh, or a published tag")
    p.add_argument("--mem", type=int, default=int(os.environ.get("VM_MEM", "8192")))
    p.add_argument("--cpus", type=int, default=int(os.environ.get("VM_CPUS", "4")))
    p.add_argument("--ssh-port", type=int, default=int(os.environ.get("VM_SSH_PORT", "2222")))
    p.add_argument("--rdp-port", type=int, default=int(os.environ.get("VM_RDP_PORT", "3389")))
    p.add_argument("--novnc-port", type=int, default=int(os.environ.get("VM_NOVNC_PORT", "6080")))
    p.add_argument("--no-novnc", action="store_true",
                   help="do not start websockify; VNC stays raw on 5900")
    p.add_argument("--dry-run", action="store_true",
                   help="stage everything and print the qemu argv instead of starting it")
    a = p.parse_args(argv)

    need_kvm()
    work = a.work or os.path.join(a.data, "work")
    os.makedirs(a.data, exist_ok=True)
    os.makedirs(work, exist_ok=True)

    log("=== OpenCore, firmware, APFS driver ===")
    appliance.stage(a.assets, work)
    assets = opencore.build("15", work)
    log(f"identity {assets['identity']['serial']} / {assets['identity']['mac']}")

    # THE DISK, IN ONE OF THREE WAYS, IN THE ORDER A USER MEANS THEM.
    #
    #   --disk none     no disk at all, and the firmware boots OpenCore into Recovery
    #   --disk <name>   pulled from this repository's package onto the volume, once
    #   (no --disk)     a disk baked into the image, if the image has one
    #
    # The baked-in case is checked last on purpose: an explicit choice must win over whatever
    # the image happens to carry, or someone with the old 16 GiB image could not boot their own
    # shell of choice out of it.
    base = ""
    if a.disk and a.disk != appliance.NO_DISK:
        base = appliance.pull_disk_named(a.disk, a.data)
    elif a.disk == appliance.NO_DISK:
        base = join_base(a.assets, a.data) if _has_baked_disk(a.assets) else ""

    if base:
        log(f"base {base} ({os.path.getsize(base) >> 20} MiB)")
        # THE DISK IS CONNECTED WITH discard=unmap. The bake's installer must not have it - see
        # machine.TARGET_DRIVE_PLAIN for the macos26 run that lost 17 GiB mid-install - but a
        # finished system is exactly what discard exists for. A `setup` disk IS an installer,
        # so it gets the plain drive.
        disk = open_overlay(base, a.data, plain=a.disk == "setup")
    else:
        log("no disk: booting the firmware with nothing attached, which reaches Recovery")
        disk = ""

    shape = machine.shape(assets, cpus=a.cpus, mem_mb=a.mem)
    shape.ssh_port = a.ssh_port
    shape.rdp_port = a.rdp_port
    # THE VNC SERVER IS THE CONTAINER'S OTHER PORT, so it is bound where a published port can
    # reach it. A bake binds 127.0.0.1 because nothing outside its job talks to it.
    shape.vnc_bind = "0.0.0.0"

    if not a.no_novnc:
        start_novnc(a.novnc_port, 5900 + shape.vnc_display)

    extra = [
        "-drive", f"file={assets['boot']},format=raw,if=none,id=oc",
        "-device", "virtio-blk-pci,drive=oc,bootindex=0",
    ]
    # THE TARGET DISK IS OPTIONAL, AND THAT IS THE POINT OF --disk none.
    #
    # With no target the firmware still loads OpenCore from boot.img, and OpenCore's own boot
    # entry list has nothing to chain to - which is the Recovery environment. That is a real
    # thing to want: it is how a person erases a disk, restores from a backup, or installs a
    # release this repository does not publish.
    if disk:
        template = machine.TARGET_DRIVE_PLAIN if a.disk == "setup" else machine.TARGET_DRIVE
        extra += [
            "-drive", template.format(disk=disk),
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
        ]
    extra += ["-device", f"isa-applesmc,osk={machine.OSK_PLAIN}"]

    # net.up() IS WHAT MAKES THE ONE NETWORK MODE EXIST - bridge, tap, DHCP, MASQUERADE and
    # the forwards. It is called here rather than by vm.Guest because this entry point execs
    # qemu itself; the argument vector below is the same one Guest.start() builds.
    log("=== network: tap on a bridge, dnsmasq, MASQUERADE ===")
    net.up(ssh_port=a.ssh_port, rdp_port=a.rdp_port)
    shape.tap_ifname = shape.tap_ifname or net.TAP
    shape.tap_mac = shape.tap_mac or net.GUEST_MAC
    shape.net_mtu = shape.net_mtu or net.MTU
    expose_uplink(a.ssh_port)

    args = vm.build_args(work, extra, shape)

    log(f"ssh    {net.FORWARD_HOST}:{a.ssh_port} -> guest 172.30.0.2:22")
    log(f"qmp    {vm.Paths(work).drive}")
    if a.dry_run:
        print(" ".join(args))
        return 0

    log("starting qemu as pid 1")
    os.makedirs(work, exist_ok=True)
    console = open(os.path.join(work, "console.log"), "ab")
    os.dup2(console.fileno(), sys.stdout.fileno())
    os.dup2(console.fileno(), sys.stderr.fileno())
    os.execvp(args[0], args)
    return 0  # not reached


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
