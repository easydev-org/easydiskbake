"""Print the Android system image pin, so a job can show what it is about to verify.

    python3 lib/android_pin.py

WHY THIS IS A FILE AND NOT `python3 -c`

The workflow used to inline it:

    python3 -c "import sys; sys.path.insert(0, 'lib'); import images; \\
pin = images.ANDROID['sysimg:android-15-aosp']; ..."

A backslash continuation inside a YAML block scalar is not a continuation - the block
ends at the first line that is not indented, and the second line of that snippet
started at column 1. The workflow stopped parsing. It is the same failure that put
windows/11/verify.py in the tree.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import images  # noqa: E402

KEY = "sysimg:android-15-aosp"


def main() -> int:
    pin = images.ANDROID.get(KEY)
    if pin is None:
        print(f"{KEY} is not in lib/images.py", file=sys.stderr)
        return 1

    print(f"api     {pin['api']} ({pin['abi']}, flavour {pin['flavour']})")
    print(f"sha1    {pin['sha1']}")
    print(f"size    {pin['size'] >> 20} MiB")
    print(f"url     {pin['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
