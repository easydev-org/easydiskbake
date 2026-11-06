"""One network for every guest: a bridged tap, DHCP from dnsmasq, and MASQUERADE.

    import net
    net.up()                      # bridge, tap, dnsmasq, NAT, port forwards
    ...
    net.down()                    # remove them, best effort

WHY THERE IS ONE MODE AND NOT TWO

qemu offers two ways to give a guest a network, and this repository used to pick per caller:

    -netdev user   a USERSPACE NAT. The guest's packets are terminated inside qemu and
                   re-issued by the QEMU PROCESS. hostfwd, smb= and a built-in DHCP server
                   all come free, and the guest is invisible to the host's network.
    -netdev tap    a real interface on a real link. The guest ARPs, DHCPs and routes like a
                   machine, and its packets leave through the HOST's stack.

TWO MODES MEANS TWO SETS OF BEHAVIOUR, and they are not interchangeable. Run 42162859846's
macos26 sat on "Update Mac Automatically" for six minutes - still spinning, Escape did not
interrupt it - because macOS was waiting for gdmf.apple.com and the slirp path could not reach
it:

    container -> proxy      apple.com 200 in 0.17s
    container -> direct     gdmf.apple.com FAILS
    guest -> slirp -> container -> direct   hangs forever

The guest's packets were re-issued by qemu, which does not carry the container's proxy
configuration, so the one host that could have routed them was never asked. With a tap the
packets leave through the container's own stack, and whatever policy routing that stack has
applies.

SO THE THREE THINGS SLIRP GAVE FOR FREE ARE BUILT HERE, ONCE, FOR EVERY SYSTEM:

    hostfwd 22 -> 2222      an iptables DNAT rule from the bridge address to the guest
    built-in DHCP           dnsmasq, bound to the bridge, handing out one address
    built-in DNS            dnsmasq, forwarding to the resolver the container has
    smb=DIR                 a real smbd on the bridge address (see lib/share.py)

The addresses are easydev-org/easysandbox-macos's: 172.30.0.1/24 on the bridge, 172.30.0.2
for the guest, 172.30.0.254 reserved for the host side. They are deliberately not 10.0.2.0/24
so a guest configured by an old slirp run cannot half-work against the new link.

WHAT THIS NEEDS FROM THE CONTAINER: NET_ADMIN and /dev/net/tun. A container without them
cannot run any of this, and up() says so in one sentence instead of failing at the first ip
command.
"""

from __future__ import annotations

import sys
import os
import subprocess
import time


def log(message: str) -> None:
    print(f"[net {time.strftime('%H:%M:%S')}] {message}", flush=True)


BRIDGE = os.environ.get("VM_BRIDGE", "qemubr0")
TAP = os.environ.get("VM_TAP", "qemutap0")
PREFIX = 24
BRIDGE_IP = os.environ.get("VM_BRIDGE_IP", "172.30.0.1")
GUEST_IP = os.environ.get("VM_GUEST_IP", "172.30.0.2")
HOST_IP = os.environ.get("VM_HOST_IP", "172.30.0.254")
MASK = "255.255.255.0"
MTU = 1500

# THE MAC IS FIXED, NOT RANDOM. dnsmasq is told which address to hand to which MAC, so a guest
# that comes up with a different one gets no lease - and macOS, which keeps its MAC in NVRAM,
# would otherwise be handed an address that changes between boots.
GUEST_MAC = os.environ.get("VM_GUEST_MAC", "52:54:00:56:4d:01")


def _run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def _sh(cmd: str, *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=check)


def available() -> tuple[bool, str]:
    """Whether this container can do it, and why not when it cannot."""
    if not os.path.exists("/dev/net/tun"):
        return False, ("/dev/net/tun does not exist. The pod needs a hostPath for it, and the "
                       "container needs NET_ADMIN.")
    # NET_ADMIN is what `ip link add ... type bridge` requires; testing it is cheaper than
    # parsing capabilities, and the test cleans up after itself.
    probe = _sh(f"ip tuntap add dev {TAP}-probe mode tap && ip link del {TAP}-probe")
    if probe.returncode != 0:
        return False, (f"cannot create a tap device ({probe.stderr.strip()[:200]}). "
                       f"The container needs cap_net_admin.")
    return True, ""


def up(*, ssh_port: int = 2222, rdp_port: int = 3389,
       forwards: dict[int, int] | None = None) -> None:
    """Create the link and the rules. Idempotent: a second call reuses what is there.

    `forwards` maps a HOST port to a GUEST port, and defaults to the two every system here
    uses - 2222 -> 22 and 3389 -> 3389. It replaces qemu's `hostfwd=tcp::HOST-:GUEST`, which
    does not exist on a tap.
    """
    ok, why = available()
    if not ok:
        raise SystemExit(
            f"=== NO BRIDGED NETWORK AVAILABLE ===\n"
            f"  {why}\n"
            f"  Every guest here runs on a tap because a userspace NAT cannot route the "
            f"way this cluster requires; see lib/net.py.")

    if forwards is None:
        forwards = {ssh_port: 22, rdp_port: 3389}

    _sh("sysctl -w net.ipv4.ip_forward=1")

    # THE BRIDGE FIRST, so the tap has somewhere to attach before it comes up.
    _sh(f"ip link add dev {BRIDGE} type bridge")
    _sh(f"ip address add {BRIDGE_IP}/{PREFIX} dev {BRIDGE}")
    _sh(f"ip link set {BRIDGE} mtu {MTU}")
    _sh(f"ip link set {BRIDGE} up")
    _sh(f"sysctl -w net.ipv6.conf.{BRIDGE}.disable_ipv6=1")

    _sh(f"ip tuntap add dev {TAP} mode tap")
    _sh(f"ip link set {TAP} mtu {MTU}")
    # PROMISCUOUS ON THE TAP IS NOT OPTIONAL: a tap only receives frames addressed to the
    # guest's MAC, and a bridge floods to ports whose MAC it has not learned yet. Without
    # promisc the guest's first ARP request is dropped and it never gets a lease.
    _sh(f"ip link set dev {TAP} up promisc on")
    _sh(f"sysctl -w net.ipv6.conf.{TAP}.disable_ipv6=1")
    _sh(f"ip link set dev {TAP} master {BRIDGE}")

    # DHCP AND DNS, from one dnsmasq bound to the bridge. `--dhcp-range=A,A` is a single
    # address: this is not a shared LAN, it is one guest, and a range would hand the same
    # address to anything else that asked.
    _sh("pkill -x dnsmasq")
    time.sleep(1)
    # NO MAC BINDING. The first version of this pinned the lease to GUEST_MAC, and every
    # stage afterwards handed the guest a DIFFERENT MAC - OpenCore generates the identity per
    # work directory, and `machine.shape()` passes it to the NIC - so dnsmasq logged
    #
    #     DHCPDISCOVER(qemubr0) 00:16:cb:25:ee:38 no address available
    #
    # every ten seconds forever, and the guest booted with no address, no route and no DNS.
    #
    # A ONE-ADDRESS RANGE INSTEAD: `--dhcp-range=A,A` hands A to the first client that asks,
    # whatever its MAC, and there is only ever one client on this bridge.
    cfg = (
        f"dhcp-authoritative;dhcp-range={GUEST_IP},{GUEST_IP};"
        f"dhcp-option=option:netmask,{MASK};"
        f"dhcp-option=option:router,{BRIDGE_IP};"
        # THE GUEST'S RESOLVER IS THIS BRIDGE, and dnsmasq forwards to whatever /etc/resolv.conf
        # in the container says - which is the only resolver that knows how to answer here.
        f"dhcp-option=option:dns-server,{BRIDGE_IP};"
        f"dhcp-option=option:mtu,{MTU};"
        f"interface={BRIDGE};bind-interfaces;no-ping;filter-AAAA;"
        f"pid-file=/var/run/dnsmasq.vm.pid;log-facility=/var/log/dnsmasq.vm.log"
    )
    started = _sh(f"dnsmasq --conf-file=/dev/null {''.join('--' + opt + ' ' for opt in cfg.rstrip(';').split(';'))}")
    if started.returncode != 0:
        raise SystemExit(f"=== dnsmasq DID NOT START ===\n  {started.stderr.strip()[:300]}")

    # OUTBOUND: the guest's traffic leaves as this container does.
    _sh(f"iptables -t nat -C POSTROUTING -s {BRIDGE_IP}/{PREFIX} -j MASQUERADE")
    if _sh(f"iptables -t nat -C POSTROUTING -s {BRIDGE_IP}/{PREFIX} -j MASQUERADE").returncode != 0:
        _sh(f"iptables -t nat -A POSTROUTING -s {BRIDGE_IP}/{PREFIX} "
            f"! -d {BRIDGE_IP}/{PREFIX} ! -o {BRIDGE} -j MASQUERADE")
    _sh(f"iptables -C FORWARD -s {BRIDGE_IP}/{PREFIX} -j ACCEPT")
    if _sh(f"iptables -C FORWARD -s {BRIDGE_IP}/{PREFIX} -j ACCEPT").returncode != 0:
        _sh(f"iptables -A FORWARD -s {BRIDGE_IP}/{PREFIX} -i {BRIDGE} ! -o {BRIDGE} -j ACCEPT")
    _sh(f"iptables -C FORWARD -d {GUEST_IP}/32 -j ACCEPT")
    if _sh(f"iptables -C FORWARD -d {GUEST_IP}/32 -j ACCEPT").returncode != 0:
        _sh(f"iptables -A FORWARD -d {GUEST_IP}/32 ! -i {BRIDGE} -o {BRIDGE} -j ACCEPT")

    # INBOUND: one DNAT rule per forwarded port, replacing hostfwd. It matches on the
    # destination ADDRESS rather than the interface, because the container's own eth0 and the
    # bridge are both local and `--dst-type LOCAL` would also catch the container's traffic.
    _sh("iptables -t nat -N VM_FWD")
    _sh("iptables -t nat -F VM_FWD")
    for host_port, guest_port in sorted(forwards.items()):
        _sh(f"iptables -t nat -A VM_FWD -p tcp --dport {host_port} -j DNAT "
            f"--to-destination {GUEST_IP}:{guest_port}")
    _sh(f"iptables -t nat -C PREROUTING -d {BRIDGE_IP}/32 -j VM_FWD")
    if _sh(f"iptables -t nat -C PREROUTING -d {BRIDGE_IP}/32 -j VM_FWD").returncode != 0:
        _sh(f"iptables -t nat -A PREROUTING -d {BRIDGE_IP}/32 -j VM_FWD")
    # AND ON THE CONTAINER'S OWN ADDRESS, so `localhost:2222` from inside the container works -
    # which is what every launcher and every ssh check in this repository does.
    _sh(f"iptables -t nat -C OUTPUT -p tcp -j VM_FWD")
    if _sh("iptables -t nat -C OUTPUT -p tcp -j VM_FWD").returncode != 0:
        _sh("iptables -t nat -A OUTPUT -p tcp -j VM_FWD")

    log(f"bridge {BRIDGE} {BRIDGE_IP}/{PREFIX}, tap {TAP}, guest {GUEST_IP} "
        f"mac {GUEST_MAC}, forwards {sorted(forwards)}")


def down() -> None:
    """Remove what up() created. Best effort: a missing rule is not an error."""
    _sh("pkill -x dnsmasq")
    _sh("iptables -t nat -F VM_FWD")
    _sh("iptables -t nat -X VM_FWD")
    _sh(f"iptables -t nat -D POSTROUTING -s {BRIDGE_IP}/{PREFIX} -j MASQUERADE")
    _sh(f"ip link set dev {TAP} down")
    _sh(f"ip link del dev {TAP}")
    _sh(f"ip link set dev {BRIDGE} down")
    _sh(f"ip link del dev {BRIDGE}")
    log("removed")


def guest_address() -> str:
    """The address the guest will have, for anything that needs to reach it directly."""
    return GUEST_IP


def main(argv: list[str]) -> int:
    """Bring the link up or down, and say whether this container can."""
    import argparse

    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("action", choices=["up", "down", "check"])
    p.add_argument("--ssh-port", type=int, default=2222)
    p.add_argument("--rdp-port", type=int, default=3389)
    args = p.parse_args(argv)

    if args.action == "check":
        ok, why = available()
        if ok:
            log(f"available: {BRIDGE} {BRIDGE_IP}/{PREFIX}, guest {GUEST_IP} mac {GUEST_MAC}")
            return 0
        log(f"NOT available: {why}")
        return 1
    if args.action == "up":
        up(ssh_port=args.ssh_port, rdp_port=args.rdp_port)
        return 0
    down()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
