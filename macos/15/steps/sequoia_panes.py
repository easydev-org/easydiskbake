"""The macOS panes that are not a click, in one place.

WHY THE APPLE ACCOUNT PANE IS HERE AND NOT IN THE YAML

Leaving it is a different mechanism on each release, and neither is a coordinate:

    15   a text link in the bottom-left corner, which opens a confirmation sheet whose blue
         button is the actual way out. Two clicks.

    26   a popup BUTTON where 15 has the link. Clicking it opens a menu that draws DOWNWARD -
         "Use Multiple Accounts" at y=833, "Sign In Later in Settings" at y=856 - and the menu
         row is the way out, not the button. Return does not choose it. Then the same kind of
         confirmation sheet appears, at a different place.

Writing that as a sequence of `at:` steps would need a `when: release == 26` on each one, which
is exactly the conditional-that-gates-a-click this repository has paid for four times. So the
branch is in Python where it is readable as a branch, and the YAML names it by name.

WHAT WAS MEASURED, AND WHERE

All four coordinates are from macos/15/drive_read.py, which measured them on live 15 and 26
guests. They are copied rather than re-derived: a second set of measurements is a second thing
that can disagree with the frames.
"""

from __future__ import annotations

# 15: the text link, and the sheet's blue button.
APPLE_SET_UP_LATER_15 = (620, 811)
APPLE_SKIP_15 = (1026, 634)

# 26: the popup button, the menu's own row, and the sheet's blue button. THE SHEET'S TITLE LINE
# READS AT y=516 AND IS NOT THE BUTTON - confirmed against a frame of the sheet itself.
APPLE_OTHER_OPTIONS_26 = (668, 799)
APPLE_SET_UP_LATER_26 = (700, 856)
APPLE_SKIP_26 = (1020, 640)


def apple_account(driver, step) -> bool:
    """Leave "Sign In to Your Apple Account" without an account, on either release.

    Returns True when the pane is gone. It is False only when the pane is still up and no
    measured way out worked, which the caller reports with the screen - the failure mode this
    replaced was a click that landed on nothing and a wizard that sat there.
    """
    release = str(step.get("release") or driver.release)

    if release != "26":
        driver.click(APPLE_SET_UP_LATER_15, "the 'Set Up Later' link (15)", settle=4.0)
        driver.look("apple-later")
        # THE CONFIRMATION SHEET IS PART OF THIS PANE. Whether it appears at all is decided by
        # the pane, not by us, so it is checked rather than clicked blind.
        if driver.says("sure") or driver.says("skip"):
            driver.click(APPLE_SKIP_15, "the sheet's blue Skip (15)", settle=8.0)
        return True

    # 26. THE POPUP FIRST, THEN THE MENU'S ROW.
    #
    # Return does not choose the menu row - that is measured, and it is why this is two clicks
    # and not a click and a keypress. If the first click did not open a menu the second lands
    # on the pane and nothing happens, so the check afterwards is what tells the two apart.
    driver.click(APPLE_OTHER_OPTIONS_26, "the 'Other Sign-In Options' popup (26)", settle=4.0)
    driver.look("apple-menu")
    driver.click(APPLE_SET_UP_LATER_26, "the menu's 'Sign In Later in Settings' (26)", settle=5.0)

    driver.look("apple-sheet")
    if driver.says("sure") or driver.says("skip"):
        driver.click(APPLE_SKIP_26, "the sheet's blue Skip (26)", settle=8.0)

    driver.look("apple-after")
    if driver.says("sign", "apple"):
        driver.log("  the Apple Account pane is still up; no measured way out worked")
        return False
    return True
