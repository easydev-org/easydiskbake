"""The one macOS ssh step that is not a command: the System Settings sequence.

WHY THIS IS A THIN DELEGATION AND NOT A REWRITE

macos/15/ssh.py's enable_remote_login() is thirty lines of clicks and about two hundred lines of
comment explaining why each one is where it is - and the comments are the valuable part:

    the search field is clicked after Spotlight has drawn, because a click that arrives first
    leaves the pane on the Sharing OVERVIEW and every later click is one screen off

    the full-disk-access switch IS ALREADY ON when Remote Login is turned on, and pressing it
    again turns it off - which is the bug that made the clean fail with `diskutil apfs trim WAS
    REFUSED (exit 1)`, six runs after the stage started working

    `Done` closes the pane so the daemon settles rather than holding the change in a sheet

None of that is expressible as `at:` steps, because two of the three are "wait for a redraw that
has no marker" and one is "look at this switch and do NOT press it". So the sequence stays in
ssh.py and the YAML names it.

WHY IT TAKES A guest AND NOT THE STEP DRIVER

enable_remote_login(guest) is the existing signature and it reads guest.paths.drive. Rather than
change that function's interface to suit this module - which would make the Python driver and the
walkthrough disagree about what a guest is - this builds the one attribute it needs. A copy of
the click sequence would be a second set of measurements, and this repository has paid for that
kind of duplication more than once.
"""

from __future__ import annotations

import os
import sys

REPO = os.environ.get("REPO") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for path in (REPO, os.path.join(REPO, "lib"), os.path.join(REPO, "macos", "15")):
    if path not in sys.path:
        sys.path.insert(0, path)


class _Socket:
    """The one attribute enable_remote_login() reads: a drive QMP socket path."""

    def __init__(self, drive: str) -> None:
        self.drive = drive


class _Guest:
    def __init__(self, drive: str) -> None:
        self.paths = _Socket(drive)


def enable_remote_login(driver, step) -> bool:
    """Turn Remote Login on through System Settings, then read the preference back.

    The function below raises SystemExit when there is no QMP socket, which is right for a bake
    and wrong for a walkthrough - a step that returns False is reported by the caller with the
    screen, and an exception would take the whole run down without it. So it is caught.
    """
    import ssh as sshstage

    try:
        sshstage.enable_remote_login(_Guest(driver.qmp_path))
    except SystemExit as exc:
        driver.log(f"  enable_remote_login stopped: {exc}")
        return False

    # THE SWITCH AND THE DAEMON ARE TWO FACTS. A frame of the pane after Done says the pane
    # closed; only the port says sshd came up, and that is `require_ssh`'s job in the YAML.
    driver.look("remote-login-done")
    return True
