"""Pull a published disk into the work directory.

    python3 windows/11/pull.py windows-base-20261102-140018 out/windows11-gold.qcow2

WHY THIS IS A FILE AND NOT AN INLINE `python -c`

It began as a heredoc inside the workflow's `run:` block, and a heredoc inside a
line-continued YAML block scalar ends the block: the workflow failed to parse, at a line
number that pointed at the heredoc's first statement rather than at the structure.

One script, one argument list, and the workflow stays a workflow.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "lib"))

import iso  # noqa: E402


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: pull.py <tag> <dest>")
    tag, dest = argv
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"{dest} already present ({os.path.getsize(dest) >> 20} MiB)")
        return 0
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    print(f"pulling {tag}")
    iso.fetch_tag(tag, dest)
    print(f"{dest}: {os.path.getsize(dest) >> 20} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
