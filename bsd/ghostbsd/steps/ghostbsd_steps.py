"""The measured exceptions behind bsd/ghostbsd/steps/gbi.yaml.

WHY THESE TWO ARE PYTHON AND EVERYTHING ELSE IN THAT FILE IS DATA

The rule the walkthrough interpreter enforces is that a coordinate is always a number in the
file. What it cannot express is a question whose answer is a program:

    wait_for_live_desktop   "has MATE drawn its panel yet" has many right answers - the panel,
                            the installer's own window, a black frame while X starts - and the
                            Python driver answered it by polling three different markers. A
                            list of `until` words would be an opinion about one session rather
                            than a fact about the mechanism.

    kill_the_screensaver    Cinnamon's blank and MATE's blank are different mechanisms with the
                            same symptom, and both are reached through the session's own
                            settings store, not through the pointer.

Everything else in gbi.yaml is a step: a coordinate, a wait, a value, a confirmation. The
split is not "hard vs easy" - the ZFS disk tick is easy and is still data - it is "is this a
thing to do, or a question to answer".
"""

from __future__ import annotations


def wait_for_live_desktop(driver, step) -> bool:
    """Until MATE has drawn its panel, or the installer's own window is up.

    THE MARKERS ARE THREE BECAUSE THE SESSION IS NOT ONE THING. Measured on this ISO: the
    desktop comes up before its panel does, the installer's window can appear before the
    panel does when the autostart wins, and a frame taken too early is a black rectangle that
    reads as an empty screen. A single word would pass on two of those three.
    """
    import time

    timeout = float(step.get("timeout", 1800))
    deadline = time.time() + timeout

    while time.time() < deadline:
        driver.look("boot")
        joined = driver.text()
        if ("install" in joined and "ghostbsd" in joined) or "applications" in joined:
            driver.log("the live session is up")
            return True
        time.sleep(10)

    driver.log("=== THE LIVE DESKTOP DID NOT APPEAR ===")
    driver.log(f"  last screen: {' | '.join(w[0] for w in driver.words[:24])}")
    return False
