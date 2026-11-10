"""The three measured exceptions in kubuntu's walk, kept beside the YAML that names them.

WHY THESE THREE ARE PYTHON AND THE OTHER TWENTY STEPS ARE NOT

`advance()` IS THREE ATTEMPTS WITH A READING BETWEEN THEM. The reading does not choose WHERE
to click - every attempt presses the same measured (1757, 1058) - it chooses whether to TRY
AGAIN, and on this wizard that decision is load-bearing in both directions:

    A FIRST CI RUN CLICKED NINE TIMES AND MOVED NOTHING. The footer coordinate was eighteen
    pixels outside the button, and every step still reported itself visited; only the overall
    timeout ended it, ninety minutes later, with the installer still on its welcome pane.

    A SECOND UNCONDITIONAL CLICK IS A LIABILITY. Calamares advances in one to two seconds, so
    two blind clicks 0.8 s apart land the second one on the NEXT pane's Next button and skip a
    pane entirely - silently, having configured somewhere it did not mean to.

So the retry has to be conditional on the pane not having moved, and that is a branch. It is
here, in one function, rather than spread over three `at:` steps that would each fire blind.

`finish()` IS A LOOP WITH A STALL DETECTOR. A screen that has not changed in fifteen minutes
is a dead run and not a slow one: run 42364250527 spent eighty-seven of its ninety minutes
showing the same Summary text, one identical OCR line every twenty seconds. A `wait:` cannot
say that, and a `until:` cannot either - what it waits for is the ABSENCE of the summary.

Every coordinate below is the same number the driver used, and it came from a CI frame.
"""

from __future__ import annotations

import time

# MEASURED ON A CI FRAME, NOT A LOCAL ONE. The first version of these was measured by clicking
# around a local run until the wizard advanced, and it went to CI and did nothing at all.
#
# Calamares RIGHT-ALIGNS the footer, so these move when the label changes width - tesseract puts
# "Siguiente" at x 1729..1785, y 1052..1065, centre (1757, 1058). The old value was
# (1775, 1040): eighteen pixels right and eighteen up, which is above the button and on its
# right edge. A footer button is 35 px tall and those coordinates were outside all of it.
NEXT = (1757, 1058)

# CALAMARES' SIDEBAR IS 190 px WIDE AND LISTS EVERY PANE AT ALL TIMES, so anything that decides
# whether the wizard advanced has to look right of this - or it will read the name of the pane
# it is waiting for before the pane arrives.
#
# 190 IS MEASURED, NOT GUESSED: the dark panel ends at x=189 and the page background starts at
# 190. A looser value is not a harmless margin - at 220 it swallowed the first word of every
# form label, because the labels begin at x=196.
SIDEBAR_WIDTH = 190


def _content(driver) -> str:
    """The words right of the sidebar, lowercased - the only words a click on Next changes."""
    return " ".join(w[0].lower() for w in driver.words if w[1] > SIDEBAR_WIDTH)


def advance(driver, step) -> bool:
    """Leave this pane, three attempts, REFUSING TO CONTINUE if the screen never changed.

    THE ORDER IS THE ORDER A PERSON WOULD TRY: a click, the click again in case the first only
    focused the window, then Alt+N - which Calamares binds to Next on every pane. Alt+N is not
    first because on the Summary pane it starts the installation, and a shortcut fired into the
    wrong pane is worse than a repeated click.
    """
    settle = float(step.get("settle", 12.0))
    before = _content(driver)

    for attempt, how in ((1, "click"), (2, "click again"), (3, "alt+n")):
        if how == "alt+n":
            driver.log("alt+n - next, by keyboard")
            # steps.Driver carries qmpmod; a driver written before this one may not, so the
            # module is imported here rather than assumed on the object.
            key_combo = getattr(driver, "qmpmod", None)
            if key_combo is None:
                import qmp as key_combo
            key_combo.key_combo(driver.q, "alt", "n")
            time.sleep(settle)
        else:
            driver.click(NEXT, f"{step.name}: Next ({how})", settle=settle)
        driver.look(f"after {step.name}")
        now = _content(driver)
        if now and now != before:
            return True

    driver.log(f"=== {step.name.upper()} DID NOT ADVANCE, BY CLICK OR BY KEYBOARD ===")
    driver.log(f"  before: {before[:200]}")
    driver.log(f"  now   : {_content(driver)[:200]}")
    return False


def finish(driver, step) -> bool:
    """Until Calamares leaves the summary and reaches its last pane.

    "finish" IS NOT A MARKER: the sidebar lists every pane including the last, so the word
    "Finish" is on screen from the welcome pane onwards. The marker is the SUMMARY PANE GOING
    AWAY - copying begins when Calamares leaves it, and from then on the sidebar no longer
    highlights it.
    """
    timeout = float(step.get("timeout", 5400))
    started = time.time()
    left_summary = False
    stalled_since = time.time()
    previous = ""

    while time.time() - started < timeout:
        driver.look("finish")
        joined = _content(driver)
        if joined != previous:
            previous, stalled_since = joined, time.time()

        if "all done" in joined or "reboot now" in joined or "restart" in joined:
            driver.log("=== INSTALL COMPLETE ===")
            return True

        # A PANE WITH ALMOST NO WORDS AT THE END OF CALAMARES IS THE FINISH BUTTON. The last
        # screen is one sentence and one button; the summary it replaced had dozens.
        if left_summary and driver.words and len(driver.words) <= 6:
            driver.log("=== THE INSTALLER REACHED ITS LAST PANE ===")
            return True

        if "summary" not in joined:
            left_summary = True

        stalled = int(time.time() - stalled_since)
        if stalled > 900:
            driver.log(f"=== THE SCREEN HAS NOT CHANGED IN {stalled}s ===")
            driver.log("  the installer is not working; waiting longer will not change that")
            driver.log(f"  last screen: {joined[:200]}")
            return False
        time.sleep(20)

    driver.log("=== THE INSTALLER NEVER REPORTED COMPLETION ===")
    driver.log(f"  last screen: {_content(driver)[:200]}")
    return False
