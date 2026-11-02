"""Press keys in a guest that is waiting for one.

    from bootkey import wait_for_boot_prompt
    wait_for_boot_prompt(console="console.log", qmp="/tmp/drive.sock.qmp")

WHY THIS EXISTS AT ALL

A Windows install ISO does not boot by itself. The El Torito boot image prints

    Press any key to boot from CD or DVD...

and waits about five seconds. If nothing is pressed it falls through to the next boot
option - and on a UEFI guest with no disk installed, that is PXE, then HTTP boot, then
nothing:

    BdsDxe: starting Boot0001 "UEFI QEMU DVD-ROM QM00001" ...
    BdsDxe: failed to start Boot0001 ...: Time out
    >>Start PXE over IPv4.
    PXE-E16: No valid offer received.

That is exactly what the first full bake recorded, for fifteen minutes, while the
arrival check waited for a Windows that had never started. The console log is what made
it visible - four thousand bytes of firmware output and not one line of Windows.

No amount of answer-file configuration avoids this: the prompt is in the boot sector,
before setup.exe runs, so Autounattend.xml cannot suppress it.

HOW DOCKUR DOES IT

dockur/windows watches its PTY for the prompt string and sends Return through the QEMU
monitor:

    if grep -Fq "Press any key to boot from" "$QEMU_PTY"; then
        sendKey ret 0 500
    fi

with a blind fallback for versions whose prompt never reaches the PTY: six Returns
100ms apart, 0.25s between repeats.

This module does the same thing through QMP rather than HMP, because lib/qmp.py already
speaks QMP and the bake already opens a QMP socket. The timing is dockur's: poll the
console, press on the marker, and keep pressing a few times afterwards because the
prompt is emitted more than once and the first Return can land before the guest is
listening.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import qmp as qmpmod  # noqa: E402


def log(msg: str) -> None:
    print(f"[bootkey] {msg}", flush=True)


# The string the El Torito boot image prints. Matched as a substring, so the locale
# variants ("Press any key to boot from CD/DVD...") and the trailing dots both hit.
PROMPT = "Press any key to boot from"

# Sent after the prompt is seen. dockur uses Return; a space also works and is less
# likely to be interpreted as "select the highlighted entry" if a boot menu is up.
BOOT_KEY = "ret"

# The prompt appears once per boot attempt and the guest needs a moment after the first
# press, so a few are sent rather than one.
REPEATS = 6
INTERVAL = 0.25


def wait_for_boot_prompt(console: str, qmp_path: str, *, timeout: int = 180,
                         blind_after: int = 60) -> bool:
    """Watch the console and press a key when the boot prompt appears.

    Returns True if a key was sent. A False return is not fatal by itself - some media
    boot without a prompt - so the caller decides what it means.

    BLIND FALLBACK: after `blind_after` seconds with no prompt in the console, the keys
    are sent anyway. dockur reached the same design for the same reason - some Windows
    versions do not emit the prompt to the serial console until much later, and waiting
    for it there would deadlock. Pressing Return at a guest that is not listening is
    harmless; not pressing it at one that is costs the whole run.

    The QMP socket may not exist yet when this is called, so connecting is retried.
    """
    started = time.time()
    deadline = started + timeout
    pressed = False

    while time.time() < deadline:
        text = ""
        try:
            with open(console, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            pass

        prompted = PROMPT in text
        elapsed = time.time() - started
        blind = elapsed >= blind_after

        if prompted or blind:
            if prompted:
                log(f"boot prompt seen after {elapsed:.0f}s")
            else:
                log(f"no prompt after {elapsed:.0f}s; pressing blind (dockur does the same)")

            if press(qmp_path):
                return True

            log("could not send the key yet; retrying")
            time.sleep(2)
            continue

        time.sleep(1)

    log(f"gave up after {timeout}s")
    return False


def press(qmp_path: str, key: str = BOOT_KEY, *, repeats: int = REPEATS,
          interval: float = INTERVAL) -> bool:
    """Send the key several times over one QMP connection.

    One connection, several presses: reconnecting between them puts a socket handshake
    in the middle of a five-second window.
    """
    if not os.path.exists(qmp_path):
        return False

    try:
        q = qmpmod.QMP(qmp_path)
    except (OSError, RuntimeError):
        return False

    try:
        for i in range(repeats):
            qmpmod.key(q, key)
            if i < repeats - 1:
                time.sleep(interval)
    except (OSError, RuntimeError) as exc:
        log(f"QMP failed mid-sequence: {exc}")
        return False
    finally:
        q.close()

    log(f"sent {repeats}x {key}")
    return True


def press_later(console: str, qmp_path: str, **kwargs) -> bool:
    """Blocking wrapper, for callers that want to do nothing else until it is done."""
    return wait_for_boot_prompt(console, qmp_path, **kwargs)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit("usage: bootkey.py <console.log> <qmp.sock> [timeout]")
    ok = wait_for_boot_prompt(
        sys.argv[1], sys.argv[2],
        timeout=int(sys.argv[3]) if len(sys.argv) > 3 else 180,
    )
    raise SystemExit(0 if ok else 1)
