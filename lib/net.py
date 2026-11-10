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

The addresses are easydev-org/easysandbox-macos's: 172.30.0.1/24 on the bridge, 172.30.0.2
for the guest, 172.30.0.254 reserved for the host side. They are deliberately not 10.0.2.0/24
so a guest configured by an old slirp run cannot half-work against the new link.

THE FORWARDED PORTS LIVE ON THE BRIDGE ADDRESS AND NOWHERE ELSE. `FORWARD_HOST` is that
address, and every client in this repository - every ssh, every rdp, every port probe - names
it instead of writing 127.0.0.1. A loopback address cannot be forwarded to a guest: DNAT
rewrites the destination but not the source, so the guest answers 127.0.0.1 to its own
loopback and the connection hangs until its timeout. See the OUTPUT rule below.

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


# THE GUEST'S MTU HAS TO FIT INSIDE THE CONTAINER'S, AND IT DID NOT.
#
# This was a constant 1500 while the container's own eth0 is 1450 - Kubernetes and the CNI
# underneath it take fifty bytes for their encapsulation. So the guest was TOLD 1500 over
# DHCP, sent 1500-byte packets, and every one of them was dropped silently on the way out
# because they did not fit. Nothing reports that: no ICMP comes back, because the drop is on
# a link the container has no visibility into.
#
# WHAT IT LOOKED LIKE. macOS 15 sat on the Setup Assistant's Screen Time pane for eight
# minutes with a spinner, holding three ESTABLISHED connections to an Apple CDN whose byte
# counter did not move once (MASQUERADE 285K -> 285K over twenty seconds). The TCP handshake
# had completed - those are small packets - and so had the TLS ClientHello, a few hundred
# bytes. The server's Certificate message is several kilobytes, does not fit, and never
# arrived, so TLS never finished and the pane waited for an answer that could not come.
#
# Run 42162859846's macOS 26 was blamed on "gdmf.apple.com being unreachable" and run
# 4220xxxxx's on "17.57.x.x being a broken Apple range". Both were this. Measured from the
# container, one size apart:
#
#     ping -M do -s 1422 17.157.64.66   (packet 1450)  OK
#     ping -M do -s 1472 17.157.64.66   (packet 1500)  DROPPED
#
# SO THE MTU IS READ FROM THE INTERFACE THAT LEAVES, not assumed. VM_MTU overrides it for a
# host whose uplink differs; the fallback is 1500 because a container with no eth0 has no
# link to fit inside.
def _uplink_mtu() -> int:
    """The MTU of the interface this container's traffic actually leaves through."""
    for dev in ("eth0", "enp0s1", "ens3"):
        path = f"/sys/class/net/{dev}/mtu"
        try:
            with open(path) as fh:
                value = int(fh.read().strip())
            if value >= 576:            # below IPv4's minimum is not a usable link
                return value
        except (OSError, ValueError):
            continue
    return 1500


MTU = int(os.environ.get("VM_MTU") or _uplink_mtu())

# THE ADDRESS A CLIENT INSIDE THE CONTAINER CONNECTS TO FOR A FORWARDED PORT. It is the
# bridge's own address, and it is the only one that works: see the OUTPUT rule in up().
FORWARD_HOST = BRIDGE_IP

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
    # ONLY WHEN IT IS NOT ALREADY OURS. `pkill -x dnsmasq` on every call is what made this
    # function NOT idempotent in the one way that matters, and it cost two macOS clean runs.
    #
    # `up()` says "Idempotent: a second call reuses what is there", and for the link, the tap and
    # the rules that is true. DHCP is not: killing dnsmasq drops every lease it handed out, and a
    # guest that has already booted has no way to ask again until its next renew - by which time
    # the wait for sshd has given up with
    #
    #     ssh: connect to host 172.30.0.1 port 2222: No route to host
    #
    # which is the HOST's routing refusing because the guest holds no address on the tap.
    #
    # Run 42890005370 shows it happening: `net.up()` ran at 20:03:55 (clean_step's own call) and
    # again at 20:04:03 (vm_session.start's), eight seconds apart, and the guest never appeared.
    # Its own screenshot is a fully booted login window - vmuser, the clock, the menu bar, a WiFi
    # icon - on a machine whose network had just been taken away.
    #
    # SO: if a dnsmasq of ours is already running against this bridge, leave it alone. The config
    # below is written every time either way, so a changed MTU or range still takes effect on the
    # next real restart.
    already = _sh("pgrep -x dnsmasq").returncode == 0
    if already:
        log("dnsmasq is already running; leaving the leases alone")
    else:
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
        f"pid-file=/var/run/dnsmasq.vm.pid;log-facility=/var/log/dnsmasq.vm.log;"
    )
    started = _sh(f"dnsmasq --conf-file=/dev/null {''.join('--' + opt + ' ' for opt in cfg.rstrip(';').split(';'))}")
    if started.returncode != 0:
        raise SystemExit(f"=== dnsmasq DID NOT START ===\n  {started.stderr.strip()[:300]}")

    # NO GEOIP HANDLING. The guest resolves and reaches whatever the network gives it.
    #
    # This repository used to answer every geolocation probe itself - first with a refusal,
    # then with a mock that said New York - because this cluster's egress address reports
    # China and every graphical installer reads it to choose a language and a time zone.
    # Both designs worked only for the installers that ask over plain HTTP, and chasing the
    # rest (a systemd-resolved guest bypassing /etc/resolv.conf, a Calamares that asks
    # https) produced more moving parts than the problem is worth.
    #
    # So it is gone. The installers see the same network they would on any other machine,
    # and the drivers that need a particular language or time zone set it themselves.

    # OUTBOUND: the guest's traffic leaves as this container does.
    #
    # MSS CLAMPING ON BOTH LEGS. The DHCP option above tells the guest the right MTU, and
    # that is the fix - but a guest that ignores it, or a path whose MTU changes after the
    # lease, fails the same way: full-size packets vanish and TLS stalls in its handshake.
    # Clamping rewrites the MSS in the SYN/SYN-ACK to fit this link, so the peer never offers
    # more than can be carried. It costs one rule and removes the silent failure for good.
    #
    # FORWARD covers the guest; OUTPUT covers this container's own connections, which share
    # the same uplink and were measured dropping 1500-byte packets too.
    #
    # THE INSTALL IS CHECKED, AND ITS FAILURE IS LOUD. It used to be a `-C` then `-A` with
    # both return codes discarded, which made a kernel without xt_TCPMSS look exactly like a
    # kernel with the rule installed. It was not: on this cluster
    #
    #     Warning: Extension TCPMSS revision 0 not supported, missing kernel module?
    #     iptables v1.8.11 (nf_tables): RULE_APPEND failed (No such file or directory)
    #
    # and `iptables -t mangle -L FORWARD` stayed empty for the whole life of the guest. A
    # macOS guest on a 1450-byte uplink then offered an MSS of 1460, every full segment was
    # dropped, and the Tahoe installer failed with "Failed to download required installer
    # asset" - a network error with no network error message. The guest-side MTU (net.MTU,
    # applied through virtio-net's host_mtu) is now the real fix; this clamp is the belt for
    # any guest that ignores it, and saying so out loud is how the next person finds out
    # which of the two is doing the work.
    for chain in ("FORWARD", "OUTPUT"):
        check = _sh(f"iptables -t mangle -C {chain} -p tcp --tcp-flags SYN,RST SYN "
                    f"-j TCPMSS --clamp-mss-to-pmtu")
        if check.returncode != 0:
            add = _sh(f"iptables -t mangle -A {chain} -p tcp --tcp-flags SYN,RST SYN "
                      f"-j TCPMSS --clamp-mss-to-pmtu")
            if add.returncode != 0:
                log(f"  ! no MSS clamp on {chain}: {add.stderr.strip()[:160]}")
                log("    (the guest's own MTU is still applied through virtio-net host_mtu)")

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
    # AND ON THE CONTAINER'S OWN ADDRESS, so a client inside the container reaches the guest
    # without leaving the machine. IT MUST BE SCOPED TO THE BRIDGE ADDRESS.
    #
    # Run 42188488710 was the first ubuntu bake to reach its second boot on a tap, and it
    # failed with
    #
    #     === SSH DID NOT ANSWER IN 600s ===
    #       last output: ssh: connect to host 127.0.0.1 port 2222: Connection timed out
    #
    # TEN MINUTES, although the installer had finished in 570s and the disk was fine. The
    # rule was `-A OUTPUT -p tcp -j VM_FWD` with no address, so it also caught the probe's
    # connection to 127.0.0.1:2222 and DNAT'd it to the guest. DNAT rewrites the DESTINATION
    # and leaves the SOURCE alone, so the guest saw a SYN from 127.0.0.1 and sent its reply
    # to what is, on the guest, its OWN loopback. The reply never arrived and nothing reset
    # the connection, so the probe sat on its connect timeout instead of being refused.
    #
    # Measured on a live guest from the same rule set, one address apart:
    #
    #     172.30.0.1:2222  -> refused in 3ms   (reply came back; the link is fine)
    #     127.0.0.1:2222   -> timed out at 4s   (reply went to the guest's loopback)
    #
    # SO THE MATCH IS THE BRIDGE ADDRESS, which is also what FORWARD_HOST names, and every
    # ssh and rdp client in this repository uses that name rather than a literal.
    _sh(f"iptables -t nat -C OUTPUT -d {BRIDGE_IP}/32 -p tcp -j VM_FWD")
    if _sh(f"iptables -t nat -C OUTPUT -d {BRIDGE_IP}/32 -p tcp -j VM_FWD").returncode != 0:
        _sh(f"iptables -t nat -A OUTPUT -d {BRIDGE_IP}/32 -p tcp -j VM_FWD")

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


def bridge_exists() -> bool:
    """Is the bridge address actually on an interface right now?

    ASKED RATHER THAN ASSUMED, because a caller that starts a service on a bridge which does
    not exist gets a server bound to an address the guest cannot route to - and the symptom is
    a client timeout, not a start failure. `up()` is idempotent; this is how a caller decides
    whether to say so.
    """
    result = _sh(f"ip -4 addr show dev {BRIDGE} 2>/dev/null | grep -c 'inet {BRIDGE_IP}/'")
    try:
        return int((result.stdout or "0").strip() or 0) > 0
    except ValueError:
        return False


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
