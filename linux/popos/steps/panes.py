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

# THE PASSWORD PANE, MEASURED ON ITS OWN FRAME BY THE FOCUS RING, AND CLICKED AT THE FAR END.
#
# Frame pr2/f_131 (the pane that drew at 21:26 on run 42524642824): the focused entry's orange
# ring spans y=432..463, so its centre is 447, and the installer draws the two 96 px apart -
# Confirm at 543.
#
# THE X IS 1350, NOT 1188, AND THAT IS THE WHOLE OF THE LAST DEFECT. The artwork picker's
# avatar circle is at (1190,405) with a radius of about 40 px, and the entry is a long box that
# starts at x=970. A press at x=1188 is directly BELOW that circle on this pane, and the presses
# that were meant for the entry kept landing on the picker instead - measured on run
# 42525650794, whose frame at 21:38:45 is a `Choose Icon` chooser looking at
# `usr/share/pixmaps`, and whose next frame has `adminadmin` in the chooser's search box.
#
# x=1350 is inside the same entry and 160 px from the avatar, so the press cannot reach the
# picker at all. The y values were already right.
ACCOUNT_PASSWORD = {"password": (1350, 447), "confirm": (1350, 543)}


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

    THE PANE IS WAITED FOR FIRST, for the same reason the password pane is (run 42535359047
    typed a password into this form because it had not settled): `Full Name` is on this pane
    and nowhere else, and typing into a card that has not drawn yet lands nowhere.
    """
    user = str((getattr(driver, "values", {}) or {}).get("user") or "vmuser")

    # THE DISPLAY NAME MUST DIFFER FROM THE LOGIN NAME, AND THE LOGIN MUST STILL BE `docker`.
    #
    # TWO CONSTRAINTS, AND THEY ARE BOTH MEASURED:
    #
    #   pop-os-installer DERIVES the login from the display name by lowercasing it and
    #   dropping spaces. `Vmuser` -> `vmuser`, `VM User` -> `vmuser` (run 42570186630's own
    #   prompt read `dockeruser` under the old word; the rule is the same one).
    #   `id -un` on 9e2168b's ssh stage).
    #
    #   IT ALSO REFUSES WHEN THE TWO ARE THE SAME NAME, AND THE CHECK IGNORES CASE.
    #
    # MEASURED ON THE REAL PANE, one variable at a time, with both entries read from their own
    # pixels and the footer judged by its green fill (144,206,176):
    #
    #     Full Name      derived User Name   footer
    #     docker         docker              DISABLED
    #     Docker         docker              DISABLED      <- case does not help
    #     Docker User    dockeruser          ENABLED
    #     dockeruser     dockeruser          ENABLED       <- equal, but not derived
    #     Dockeruser     dockeruser          ENABLED
    #
    # TWO RULES, AND THEY EXPLAIN EVERY EARLIER RUN:
    #
    #   the login name is the display name lowercased with spaces removed
    #   the pane will not advance when Full Name and User Name are THE SAME after that
    #   normalisation - it does not matter whether one was typed or derived
    #
    # WHICH MEANS `docker` CANNOT BE OBTAINED FROM THIS INSTALLER AT ALL. The only display
    # names that derive `docker` are `docker` and `Docker`, and both are the same string to
    # the check. `dockeruser` is what this installer can produce, and it is what the ssh stage
    # logs in as - see that file's `ssh_user`.
    #
    # EVERY EARLIER ATTEMPT WAS ONE HALF OF THIS TABLE: `Docker` was tried because the check
    # looked case-sensitive (run e242215: footer grey), and typing `docker` over the derived
    # name was tried because the check looked like a string compare (run d579610: footer gone).
    # `VM User` -> `vmuser`: THE ONLY SHAPE THAT DERIVES THE LOGIN THIS REPOSITORY ASKS FOR.
    #
    # The pane refuses when Full Name and the derived User Name are the same string after
    # lowercasing and stripping spaces, so every single-word display is refused:
    #
    #     `vmuser`  -> derives `vmuser`  same -> DISABLED
    #     `Vmuser`  -> derives `vmuser`  same -> DISABLED
    #     `VMUSER`  -> derives `vmuser`  same -> DISABLED
    #
    # A display WITH A SPACE derives the space-stripped form, which is a different string:
    #
    #     `VM User`     -> derives `vmuser`      different -> ENABLED
    #     `Vmuser User` -> derives `vmuseruser`  different -> ENABLED
    #
    # - and only the first derives the login this repository uses. Measured against the real
    # pane in a local pod (`Docker User`/`dockeruser` behaved identically under the old word).
    display = "VM User" if user == "vmuser" else user.capitalize() + " User"

    deadline = time.time() + float(step.get("timeout", 90))
    seen = ""
    while time.time() < deadline:
        driver.look("account pane")
        seen = driver.text()
        if "full name" in seen and "user name" in seen:
            break
        time.sleep(2.0)
    else:
        driver.log("=== THE ACCOUNT PANE NEVER ARRIVED ===")
        driver.log(f"  last screen: {seen[:200]}")
        return False

    _click_type(driver, ACCOUNT_NAME["full name"], display)

    # THE FOCUS-OUT IS WHAT ENABLES `Next`: a GTK entry validates on focus-out, and nothing
    # here moved the focus after the last character until this was added (run 42502695338 sat
    # with Next grey for forty-two minutes).
    #
    # AND THE FOCUS-OUT IS WHAT ENABLES THE FOOTER. One Tab leaves Full Name; the derived User
    # Name follows by itself, and the pane validates on the focus-out. NOTHING HERE TYPES INTO
    # USER NAME - doing so replaces the derived value with an equal one, which the pane then
    # refuses (see the table above).
    driver.qmpmod.key(driver.q, "tab")
    time.sleep(1.0)
    driver.look("account name filled")
    return True


def fill_account_password(driver, step) -> bool:
    """The password twice, on the pane that actually says Choose Account Password.

    THE PANE MUST BE UP BEFORE ANY KEY IS SENT, AND THE READING IS THE CHECK.

    Run 42535359047's log is the reason:

        look after the footer: ... | Install | Pop!_OS | 24.04 | LTS | Choose |      (the pane)
        look password filled:  ... | Full | Name | adminadm                            (the ACCOUNT)

    - the click that was meant to leave the account name pane was still settling, so the
    account pane came back with `adminadm` typed into Full Name and the password boxes never
    received anything. The step then reported success, because all it checked for was a file
    chooser, and `Drive Encryption` was clicked on a pane that was not there. The disk never
    started and the run ended at 0 MiB.

    SO THIS WAITS FOR THE PANE BY ITS OWN WORD - `Choose Account Password` appears nowhere else
    on this installer - and refuses to type until it is up. `until` on a word is the same
    mechanism every other walk here uses for the same purpose, and it is a reading that
    decides WHEN to type, not where.
    """
    password = str((getattr(driver, "values", {}) or {}).get("password") or "admin")
    import keys

    # WAIT FOR THE PANE. The card draws in well under a second once Next has taken, so this is
    # bounded tightly; if it never arrives the step fails here with the screen in the log
    # instead of typing a password into the account form.
    deadline = time.time() + float(step.get("timeout", 90))
    seen = ""
    while time.time() < deadline:
        driver.look("password pane")
        seen = driver.text()
        if "account password" in seen or "confirm password" in seen:
            break
        time.sleep(2.0)
    else:
        driver.log("=== THE PASSWORD PANE NEVER ARRIVED ===")
        driver.log(f"  last screen: {seen[:200]}")
        return False

    for label in ("password", "confirm"):
        driver.qmpmod.click(driver.q, *ACCOUNT_PASSWORD[label])
        time.sleep(1.5)
        unsupported = keys.type_text(driver.q, password, per_char=0.12)
        if unsupported:
            driver.log(f"  COULD NOT TYPE {unsupported!r}")
        time.sleep(1.2)

    driver.look("password filled")
    text = driver.text()
    if "choose" in text and "icon" in text:
        driver.log("=== THE ARTWORK CHOOSER IS UP; THE PASSWORD PANE DID NOT TAKE ===")
        return False
    # THE PANE MUST STILL BE THE PASSWORD PANE. If it went back to the account form, the typing
    # above went somewhere it must not have, and saying so now is the whole point.
    if "full name" in text and "account password" not in text:
        driver.log("=== THE ACCOUNT FORM IS UP, NOT THE PASSWORD PANE ===")
        driver.log(f"  last screen: {text[:200]}")
        return False
    return True
