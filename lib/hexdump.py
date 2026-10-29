"""Pick bytes out of a `qemu-io read -v` dump, by offset.

    hexdump.py <dump-file> <offset-hex> <count>

Prints the requested bytes as lowercase hex, or nothing if the offset is not in the dump.

WHY NOT sed

Because three runs of this check reported "mbr signature: unreadable" on a disk whose
boot code was present and correct, and each attempt to fix it with a sed expression
failed differently:

  's/.*1f0: \\(..\\) \\(..\\).*/'      the offset is padded to sixteen digits, so "1f0:"
                                     appears as "00000000000001f0:" and never matched
  '...\\(...\\)\\{1,8\\}...'               the repetition is greedy and swallowed the line

The dump format is fixed and trivial - a padded offset, a colon, sixteen byte pairs,
then ascii - so parsing it as tokens is both shorter and something that can be tested.
"""

import re
import sys

# 0000000000000010:  be 00 7c bf 00 06 b9 00 02 f3 a4 ea 21 06 00 be  ..|.........!...
LINE = re.compile(r"^([0-9a-f]+):((?:\s+[0-9a-f]{2})+)")


def main():
    path, offset_hex, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    want = int(offset_hex, 16)
    want_end = want + count

    data = {}
    with open(path, "r", errors="replace") as f:
        for line in f:
            m = LINE.match(line)
            if not m:
                continue
            base = int(m.group(1), 16)
            for i, byte in enumerate(m.group(2).split()):
                data[base + i] = byte

    if not data:
        print("", end="")
        return 1

    out = []
    for addr in range(want, want_end):
        if addr not in data:
            # The dump did not cover this range: say so rather than inventing a zero.
            return 1
        out.append(data[addr])
    print("".join(out).lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
