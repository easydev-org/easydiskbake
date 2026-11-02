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

# THE FIRMWARE'S OWN MARKER, WHICH COMES FIRST.
#
# The boot prompt is printed by the El Torito image, and it is on screen for about five
# seconds. Waiting to *see* it on the serial console is unreliable: several Windows
# versions never emit it there at all, and the ones that do can take long enough that a
# first attempt sees nothing.
#
# dockur/windows solves it by watching for the firmware's own line instead - the moment
# OVMF lists the DVD device - and pressing one poll later:
#
#     elif bootKeyReady; then
#         (( keyWait += 1 ))
#         elif (( keyWait >= 1 )); then
#             sendKey ret 0 100 6 0.25
#
# bootKeyReady() greps for "UEFI QEMU .*DVD-ROM". That appears seconds before the
# prompt, in every UEFI boot, so it is the trigger that can be relied on.
FIRMWARE_DVD_MARKER = "UEFI QEMU"
DVD_MARKER = "DVD-ROM"
LEGACY_DVD_MARKER = "Booting from DVD/CD"

# Sent after the prompt is seen. dockur uses Return; a space also works and is less
# likely to be interpreted as "select the highlighted entry" if a boot menu is up.
BOOT_KEY = "ret"

# The prompt appears once per boot attempt and the guest needs a moment after the first
# press, so a few are sent rather than one.
REPEATS = 6
INTERVAL = 0.25

# dockur polls its PTY every 250ms (`sleep 0.25` at the end of waitForBoot). Matching
# it matters: the gap between the DVD marker and the end of the prompt is a few
# seconds, and a one-second poll can land on the wrong side of it.
POLL = 0.25


def wait_for_boot_prompt(console: str, qmp_path: str, *, timeout: int = 180,
                         blind_after: int = 60) -> bool:
    """Press a key when the firmware is about to hand off to the DVD.

    Returns True if a key was sent. False is not fatal by itself - some media boot
    without a prompt - so the caller decides what it means.

    THE TIMING IS dockur's, COPIED RATHER THAN GUESSED

    Its loop is:

        while isAlive "$pid"; do
            if (( ! keySent )) && needsBootKey; then
                if grep -Fq "Press any key to boot from" "$QEMU_PTY"; then
                    sendKey ret 0 500
                elif bootKeyReady; then
                    (( keyWait += 1 ))
                    elif (( keyWait >= 1 )); then
                        sendKey ret 0 100 6 0.25
            ...
            sleep 0.25        <- the poll is 250ms, not a second
        done

    and bootKeyReady() greps for `UEFI QEMU .*DVD-ROM`. So the sequence is: poll four
    times a second, notice the DVD listing, press one poll later.

    THE FIRST ATTEMPT FAILED ON EXACTLY THIS

    It waited for the prompt string alone and fell back to a blind press at 60 seconds.
    The prompt is on screen for about five seconds; by 60 the guest had fallen through
    to PXE and the run was over. The firmware's DVD line comes seconds earlier and is
    present in every UEFI boot, which is why dockur triggers on it.

    `blind_after` remains as the last resort, for a guest that prints neither marker.
    Once the firmware marker is seen the blind timer is irrelevant - the press happens
    in that first quarter-second poll after it appears.
    """
    started = time.time()
    deadline = started + timeout
    marker_seen_at: float | None = None

    while time.time() < deadline:
        text = ""
        try:
            with open(console, "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            pass

        elapsed = time.time() - started

        prompted = PROMPT in text
        firmware_dvd = (FIRMWARE_DVD_MARKER in text and DVD_MARKER in text) \
            or LEGACY_DVD_MARKER in text

        if prompted:
            log(f"boot prompt seen after {elapsed:.1f}s")
            if press(qmp_path):
                return True
            log("QMP not ready yet; retrying")
            time.sleep(POLL)
            continue

        if firmware_dvd:
            if marker_seen_at is None:
                marker_seen_at = time.time()
                log(f"firmware listed the DVD after {elapsed:.1f}s")
            # One more poll, as dockur does: the marker appears before the handoff has
            # moved far enough for a keypress to be swallowed.
            if time.time() - marker_seen_at >= POLL:
                if press(qmp_path):
                    return True
                log("QMP not ready yet; retrying")
            time.sleep(POLL)
            continue

        if elapsed >= blind_after:
            log(f"no marker after {elapsed:.0f}s; pressing blind (dockur keeps the same "
                f"fallback)")
            if press(qmp_path):
                return True
            log("QMP not ready yet; retrying")

        time.sleep(POLL)

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
