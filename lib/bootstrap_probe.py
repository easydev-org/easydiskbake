"""A bootstrap.py that proves the whole chain, and writes nothing to the disk.

    put this in ~/shared/bootstrap.py before a launch

IT IS THE TEST, NOT A FIXTURE. The chain it has to prove is longer than "python runs":

    the login launcher fired
    the share was mounted by then
    python was on the disk and was found
    the script ran with the user's HOME

So it writes `~/hello.txt` - INSIDE THE GUEST, NOT INTO THE SHARE - and that is deliberate. A
file written to the share would appear on the host whether or not the guest ever ran anything
(a mount is not proof of execution), and a file written into the share is also the one thing
that could be mistaken for part of the published disk. `~/hello.txt` can only exist if the guest
itself ran the script.

WHY THE OUTPUT IS THIS SMALL

It is asserted on by `lib/launch_selftest.py`, which reads it back over the launch's own ssh
channel. Every line here is one assertion there:

    hello.txt            the script ran at all
    whoami=<user>        it ran as the account, not as root through a helper
    python=<version>     the interpreter was the one the dev stage installed
    share=readable       the mount was there when it fired, not later
    stamp=<time>         it ran during this launch and not in a previous one
"""

from __future__ import annotations

import os
import platform
import sys
import time

# THE LINES lib/launch_selftest.py LOOKS FOR. Named here so the two cannot drift.
MARKER = "hello.txt"


def main() -> int:
    home = os.path.expanduser("~")
    out = os.path.join(home, MARKER)

    # THE SHARE IS READ, NOT WRITTEN. If it is missing the launch still proves python and the
    # launcher; it only fails to prove the mount, and it says which.
    share = os.path.join(home, "shared")
    readable = os.path.isdir(share) and os.path.isfile(os.path.join(share, "bootstrap.py"))

    # GETPWUID, NOT THE ENVIRONMENT. A launchd agent and a Task Scheduler task both run with a
    # smaller environment than a shell, and `USER` is one of the things that can be absent - so
    # reading it would report `user=?` on the very runs this test exists to check.
    try:
        import getpass
        user = getpass.getuser()
    except Exception:                                       # noqa: BLE001
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "?"

    lines = [
        "hello from the bootstrap",
        f"user={user}",
        f"python={platform.python_version()}",
        f"share={'readable' if readable else 'MISSING'}",
        f"stamp={int(time.time())}",
        f"platform={sys.platform}",
    ]
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # AND ON STDOUT, so the launcher's log has it even if the file write was refused - which is
    # the difference between "the script did not run" and "it ran and could not write".
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
