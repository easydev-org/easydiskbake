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


def _still_on_the_pane(driver) -> bool:
    """Is "Sign In to Your Apple Account" still on screen?

    THE TITLE, NOT A PHRASE. The account pane's body says "used to log in to [your Apple
    Account]", so `says("sign", "apple")` is true there too - and a check that cannot tell the
    two apart reports the wrong thing twice: success on a pane that is still up, and failure on
    a pane that was never the Apple Account one.
    """
    return driver.says("sign", "in", "to", "your", "apple", "account")


def apple_account(driver, step) -> bool:
    """Leave "Sign In to Your Apple Account" without an account, on either release.

    Returns True when the pane is gone. It is False only when the pane is still up and no
    measured way out worked, which the caller reports with the screen - the failure mode this
    replaced was a click that landed on nothing and a wizard that sat there.
    """
    # macOS 15 ONLY. There is no 26 branch here and there must not be one: 26 was measured
    # and is not supported (see macos/README.md), and a branch for a release this repository
    # does not finish is a branch nothing can test.
    #
    # THE LINK ALONE IS NOT THE WAY OUT. `Set Up Later` raises a confirmation sheet, and if
    # the sheet has not drawn yet ONE RETURN is what raises it - drive.py's own 15 branch does
    # exactly this four-action sequence, with its timings:
    #
    #     click Set Up Later   settle 2
    #     key   Return         settle 3      the sheet's default button needs it
    #     click sheet Skip     settle 2      only if the sheet is up
    #     key   Return         settle 2      Skip focused it, so Return fires it
    #
    # The last two are unconditional in drive.py and its comment says why: "the sheet only
    # exists if the click worked. When the pane has already gone, this coordinate is empty
    # space on the next pane - harmless - and when the sheet is up it is its Skip button."
    driver.click(APPLE_SET_UP_LATER_15, "the 'Set Up Later' link (15)", settle=2.0)
    driver.press("ret", gap=3.0)
    driver.click(APPLE_SKIP_15, "the sheet's Skip, if there is one", settle=2.0)
    driver.press("ret", gap=2.0)

    # THE VERDICT IS THE SCREEN, NOT "I CLICKED". Returning True unconditionally is how a pane
    # is answered three times in a row and then reported STUCK. The match is the pane's TITLE:
    # its body says "used to log in to your Apple Account", so a loose check reports success
    # while the wizard is still on a different pane.
    driver.look("apple-after")
    if _still_on_the_pane(driver):
        driver.log("  the Apple Account pane is still up; no measured way out worked")
        return False
    return True
