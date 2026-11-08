"""The two account panes of Pop!_OS' installer, where a field must NOT be cleared.

WHY THIS IS PYTHON AND NOT THREE `at:`/`type:` STEPS, AND THE RUN THAT PROVED IT

Run 42516547644's frame at t=150s shows the Create User Account pane with

    Full Name    docker          <- the DIM PLACEHOLDER, not typed text
    User Name    docker|
    Next                         <- grey

- and the installer derives the login from the display name, so a Full Name equal to the
login is exactly the state in which `Next` stays disabled. The walk had just typed
`Docker User` there, so the typing had not landed.

THE REASON IS THE CLEARING, NOT THE CLICK. `Driver.type_into` is click, ctrl+a, sixty
BackSpaces, then type - and that is right for a pane whose entries sit alone. THIS PANE'S
ENTRIES SIT BESIDE THE ARTWORK PICKER: a `ctrl+a` or a BackSpace that lands on the wrong
control selects the avatar instead of the text, and the installer's own image picker opens
over the form. The original driver's docstring records the same measurement twice:

    a click 10-12 pixels off the entry opened `Choose Icon`, a GTK file chooser, and the
    password went into ITS search box (`adminadmin` in the frame)

So both panes here are CLICK, THEN TYPE, with no clearing at all: the fields start empty, and
one field is emptied by backspaces only when the installer may have pre-filled it. That is a
different action from `type_into`, which is why it cannot be spelled with the generic helper.

THE FOCUS-OUT IS THE OTHER HALF, AND IT IS MEASURED TOO. Run 42502695338 typed both fields
correctly and the footer stayed grey for forty-two minutes because nothing moved the focus
after the last character - a GTK entry validates on focus-out. One Tab leaves the field,
which is the event the pane listens for, and it moves the focus to the footer so it cannot
type into another control.
"""

from __future__ import annotations

import time

# MEASURED ON A DOM FRAME OF THE ACCOUNT PANE (see the header):
#
#     Full Name    y=477..511 (centre 494), x=488..1432
#     User Name    y=546..575 (centre 560)
#
# The old constants were 495 and 561, which are inside those boxes - the click was never the
# problem. They are written here as the measured centres anyway.
ACCOUNT_NAME = {"full name": (1188, 494), "user name": (1188, 560)}

# THE PASSWORD PANE, MEASURED ON ITS OWN DOM FRAME - AND THE OLD NUMBERS WERE IN THE GAPS.
#
# Frame pq4/f_145 (the pane that actually drew at 21:14:45 on run 42523566171):
#
#     Choose Account Password   entry y=390..427 (centre 408), x=950..1433
#     Confirm Password          entry y=477..532 (centre 504)
#
# The constants here used to be 440 and 546, which are the rows BETWEEN those two entries:
# a press at 440 lands on the GAP under the password box and a press at 546 on the gap under
# the confirm box. That is why clicking there opened the artwork picker on the pane BEHIND -
# `Choose Icon`, with `adminadmin` in its search box - and why the installer never started.
ACCOUNT_PASSWORD = {"password": (1188, 408), "confirm": (1188, 504)}


def _click_type(driver, xy, text: str, *, clear: bool = False, per_char: float = 0.09) -> None:
    """Click the entry and type, WITHOUT ctrl+a - see the header for why that matters."""
    import keys
    driver.click(xy, f"type {text!r}", settle=1.5)
    if clear:
        # Backspaces only, and only where the installer may have pre-filled the box. ctrl+a
        # is what selects the artwork picker on this pane.
        for _ in range(40):
            driver.qmpmod.key(driver.q, "backspace")
            time.sleep(0.02)
    # type the text, reporting anything that has no qcode
    unsupported = keys.type_text(driver.q, text, per_char=per_char)
    if unsupported:
        driver.log(f"  COULD NOT TYPE {unsupported!r}")
    time.sleep(1.2)


def fill_account_name(driver, step) -> bool:
    """Full Name only - because the installer DERIVES User Name, and it derived it correctly.

    THE RUN THAT PROVED IT. Run 42522057017's frames, ten seconds apart:

        t=150   Full Name  [Docker User]        <- typed, caret in it
                User Name  dockeruser           <- AUTO-DERIVED, grey, correct
                Next                            <- ENABLED
        t=155   Full Name  docker               <- the placeholder again
                User Name  docker|              <- my click + forty BackSpaces emptied the
                Next                            <- derived value, and Next went GREY

    So the pane was complete before this code touched User Name. The original driver emptied
    and retyped it because `dockerdocker` was once measured - but that measurement was for
    TYPING INTO BOTH FIELDS, which is not what happens here: one field is filled and the other
    is derived.

    WORSE, THE CLICK ON USER NAME CHANGED THE FOCUS, and the following Tab then walked out of
    the form onto the ARTWORK PICKER: t=165 in the same recording is a `Choose Icon` file
    chooser with `admin` typed into its search box, which is why the disk never started and
    why the password pane was never reached.

    A GTK entry validates on focus-out, so the Tab is still needed - but it comes from Full
    Name, the only field this step types into.
    """
    user = str((getattr(driver, "values", {}) or {}).get("user") or "docker")
    display = user.capitalize() + " User"

    _click_type(driver, ACCOUNT_NAME["full name"], display)

    # THE FOCUS-OUT IS WHAT ENABLES `Next`: a GTK entry validates on focus-out, and nothing
    # here moved the focus after the last character until this was added (run 42502695338 sat
    # with Next grey for forty-two minutes). It is the ONLY thing this step does after typing.
    driver.qmpmod.key(driver.q, "tab")
    time.sleep(1.0)
    driver.look("account name filled")
    return True


def fill_account_password(driver, step) -> bool:
    """Password and confirm, at their measured coordinates, with Esc to close a chooser.

    THE EARLIER FAILURE WAS NOT THESE COORDINATES. Run 42522057017's frames at t=165 show a
    `Choose Icon` chooser with `admin` in its search box - but the click that opened it was on
    the PREVIOUS pane: the account step had emptied User Name, `Next` went grey, and the
    "password pane" the walk was clicking was still Create User Account, whose artwork picker
    sits at (1188,440). With that step fixed to leave the derived login alone, `Next` is
    enabled and this pane is actually reached.

    A CLICK 10-12 PIXELS OFF AN ENTRY OPENED THE PICKER ONCE, measured, so Esc bookends the
    typing: it is a no-op on a clean pane and it closes a chooser opened by a stray press. The
    reading afterwards is the check - if the chooser is up, the installer cannot be advanced
    and saying so now is worth more than clicking Next into a dialog and waiting forty minutes
    for the disk to explain it.
    """
    password = str((getattr(driver, "values", {}) or {}).get("password") or "admin")
    import keys

    driver.qmpmod.key(driver.q, "esc")
    time.sleep(1.5)

    for label in ("password", "confirm"):
        driver.qmpmod.click(driver.q, *ACCOUNT_PASSWORD[label])
        time.sleep(1.5)
        unsupported = keys.type_text(driver.q, password, per_char=0.12)
        if unsupported:
            driver.log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(1.2)
        # A CHOOSER OPENED BY THIS PRESS IS CLOSED BEFORE THE NEXT ONE, so the second entry
        # cannot be typed into its search box.
        driver.qmpmod.key(driver.q, "esc")
        time.sleep(1.0)

    driver.look("password filled")
    text = driver.text()
    if "choose" in text and "icon" in text:
        driver.log("=== THE ARTWORK CHOOSER IS UP; THE PASSWORD PANE DID NOT TAKE ===")
        return False
    return True
