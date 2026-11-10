"""The one extension macos/15/steps/primitives.yaml needs: write a small file on the host.

WHY IT IS A `python:` STEP AND NOT `script:`

`script:` copies a body into the GUEST and runs it there. This writes a file the next `scp:` step
copies in, which is how a real stage ships anything that is not typeable - and the point of the
example is to show that a host-side write plus a copy are two steps, not one.

THE PATH IS RELATIVE TO THE DRIVER'S WORK DIRECTORY, because that is where `scp:` looks first
(see lib/steps.py). A file written anywhere else would be a file the next step cannot find, which
is a bug this repository has already had once.
"""

from __future__ import annotations

import os


def log(message: str) -> None:
    print(f"[primitives] {message}", flush=True)


def write_script(driver, step, **_kwargs) -> bool:
    """Write demo.sh into the work directory and say where it went."""
    work = getattr(driver, "work", "") or "."
    os.makedirs(work, exist_ok=True)
    path = os.path.join(work, "demo.sh")
    with open(path, "w") as fh:
        fh.write(
            "#!/bin/sh\n"
            "# Written on the host by primitives_demo:write_script, copied in by the next scp:,\n"
            "# and run by an `ssh:` step. Three steps, three facts.\n"
            "echo \"demo.sh ran as $(id -un) on $(hostname)\"\n"
        )
    os.chmod(path, 0o755)
    log(f"  wrote {path} ({os.path.getsize(path)} bytes)")
    return True
