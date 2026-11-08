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

# THE PASSWORD PANE, MEASURED ON ITS OWN FRAME. The entries start EMPTY, and a click 10-12 px
# off one lands on the artwork picker - see the header.
ACCOUNT_PASSWORD = {"password": (1188, 440), "confirm": (1188, 546)}


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
    """Full Name, then User Name, then Tab - the three things this pane needs.

    THE DISPLAY NAME IS NOT THE LOGIN. With both fields holding `docker` the footer's `Next`
    stays GREY: measured, three presses in a run at 9d4e83a left the pane unchanged. Typing
    `Docker User` instead makes the installer derive `dockeruser` and the button turns green
    at once. So the display name is `Docker User` and the login is `docker`.
    """
    user = str((getattr(driver, "values", {}) or {}).get("user") or "docker")
    display = user.capitalize() + " User"

    _click_type(driver, ACCOUNT_NAME["full name"], display)

    # `Full Name` COPIES INTO `User Name` AS IT IS TYPED, so this one IS cleared - by
    # backspaces, never ctrl+a - and then typed once. Measured: without that, `User Name`
    # read `dockerdocker`.
    _click_type(driver, ACCOUNT_NAME["user name"], user, clear=True, per_char=0.11)

    # THE FOCUS-OUT IS WHAT ENABLES `Next`. See the header.
    driver.qmpmod.key(driver.q, "tab")
    time.sleep(1.0)
    driver.look("account name filled")
    return True


def fill_account_password(driver, step) -> bool:
    """Password and confirm, on a pane whose entries are empty and whose picker is close.

    ESC FIRST, ALWAYS: if a click has already landed on the artwork picker, a GTK file chooser
    is up over the installer and every later keystroke goes into its search box. One Esc
    closes it, and it is a no-op on a pane with no dialog open.
    """
    password = str((getattr(driver, "values", {}) or {}).get("password") or "admin")

    driver.qmpmod.key(driver.q, "esc")
    time.sleep(1.5)

    _click_type(driver, ACCOUNT_PASSWORD["password"], password, per_char=0.12)
    _click_type(driver, ACCOUNT_PASSWORD["confirm"], password, per_char=0.12)
    driver.look("password filled")
    return True
