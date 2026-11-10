"""QEMU key codes, and typing text with them.

    qcodes = keys.for_text("sudo systemsetup -setremotelogin on")
    for kind, code in qcodes:            # ("key", "s") or ("combo", ("shift","semicolon"))
        ...

WHY THIS IS IN lib/ AND NOT IN A GUEST DRIVER

windows/11/ssh.py had the only copy, and the macOS ssh stage needs the same table. A
second copy is a second place for ":" to mean the wrong thing, and the failure it
produces - a colon typed as a semicolon - reads as a shell syntax error rather than as a
keyboard bug. So the table lives here and both drivers import it.

THE SHIFTED FORM IS A COMBO, NOT A NAME. lib/qmp.key() sends one down/up pair, so a
character that needs shift is two keys held at once and has to go through key_combo.
`for_text` therefore returns the shape of each keystroke rather than a single string,
which is the only way a caller can send it correctly without deciding for itself.
"""

from __future__ import annotations

# Characters QEMU names differently from the letter on the key.
NAMED = {
    " ": "spc", "-": "minus", "=": "equal", "[": "bracket_left",
    "]": "bracket_right", "\\": "backslash", ";": "semicolon", "'": "apostrophe",
    ",": "comma", ".": "dot", "/": "slash", "`": "grave_accent",
}

# The same characters when shift is held: the qcode names the UNSHIFTED key.
SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "minus", "+": "equal", "{": "bracket_left",
    "}": "bracket_right", "|": "backslash", ":": "semicolon", '"': "apostrophe",
    "<": "comma", ">": "dot", "?": "slash", "~": "grave_accent",
}


def qcode_for(ch: str) -> str:
    """The qcode for one character that needs no shift.

    Raises rather than guessing: an unshifted character that has no qcode is a gap in
    the table, and typing something else silently is worse than stopping.
    """
    if ch.isdigit() or (ch.islower() and ch.isalpha()):
        return ch
    if ch.isupper() and ch.isalpha():
        raise ValueError(
            f"{ch!r} needs shift; for_text() returns it as a combo, qcode_for() does not"
        )
    if ch in NAMED:
        return NAMED[ch]
    raise ValueError(f"no qcode for {ch!r}")


def for_text(text: str) -> list[tuple[str, object]]:
    """Every keystroke for `text`, as ('key', qcode) or ('combo', (qcodes...)).

    NEWLINES AND TABS ARE REFUSED. lib/qmp.key() cannot produce either, and a caller
    that passes one has a command that needs splitting - a decision, not a retry. The
    Windows stage learned this as "cannot map newline" after typing nothing at all.
    """
    out: list[tuple[str, object]] = []
    for ch in text:
        if ch in "\r\n\t":
            raise ValueError(
                f"cannot type {ch!r}; split the command and send it as separate lines"
            )
        if ch.isupper() and ch.isalpha():
            out.append(("combo", ("shift", ch.lower())))
        elif ch in SHIFTED:
            out.append(("combo", ("shift", SHIFTED[ch])))
        else:
            out.append(("key", qcode_for(ch)))
    return out


def type_text(q, text: str, per_char: float = 0.15, hold: float = 0.02) -> list[str]:
    """Type what CAN be typed and return the characters that could not.

    THE OTHER TWO FUNCTIONS IN THIS MODULE REFUSE; THIS ONE REPORTS. `for_text()` raises on
    the first character it cannot map, which is right for a caller that knows its string is
    ASCII - the Windows stage validates before it sends for exactly that reason. A caller
    driving an installer wizard does not know: the text comes from a configuration file, and
    stopping the whole sequence because one character has no QMP name is worse than typing
    the rest and saying which ones were skipped.

    THAT IS THE CONTRACT linux/mx/drive-mx.py ALREADY EXPECTED. It imports `type_text` from
    this module and does

        unsupported = type_text(self.screen.q, text)
        if unsupported: log("COULD NOT TYPE %r - no qcode for those characters")

    and this module never had the function:

        ImportError: cannot import name 'type_text' from 'keys'

    The MX driver is not run by any workflow, which is why nothing ever reported it; it is
    the only module lib/attrcheck.py could not check, and it is fixed here rather than
    deleted because the MX support is kept deliberately (see linux/README.md).

    150 ms BETWEEN KEYS, matching windows/11/ssh.py: a wizard that validates as it is typed
    drops characters at 30, and the caller's own 0.4 s is for forms that repaint on every
    keystroke. This default is for a shell prompt.

    `hold` IS WHY A SHIFTED CHARACTER CAN BE FOLLOWED BY A LOWERCASE ONE.

    Every shifted character here is `key_combo(q, "shift", <key>)`, and that used to send
    shift down, the key down and up, and shift up as ONE `input-send-event` request. At the
    20 ms gap the ssh stage types its install commands with, the guest's input stack dropped
    the shift RELEASE often enough that the modifier stayed down - and from that character on
    every keystroke arrived shifted.

    Measured on run 42573056391's own recording (mx), the command this stage typed:

        export DEBIAN_FRONTEND=NONINTERACTIVE; apt-get update && apt-get install ...
        export DEBIAN_FRONTEND+NONINTERACTIVE; APT_GET UPDATE && apt-get install ...

    the second line being what arrived: `=` became `+` (shift+equal), `-` became `_`
    (shift+minus), and the lowercase words came out upper case. The shell answered

        sh: 1: export: DEBIAN_FRONTEND+NONINTERACTIVE: bad variable name

    and nothing was installed. THE SAME BUG BUILT A WRONG ACCOUNT on the same round: mint's
    installer derives the login name from the full name, and the full name had been typed as
    `DOCKER` - its lightdm title bar on run 42573050688 reads
    `DOCKERdocker-Standard-PC-Q35-ICH9-2009`.

    So a shifted character is sent as TWO requests with `hold` seconds between the press and
    the release. 20 ms is long enough for the guest to read the modifier down before the key
    and to see it come back up, and short enough that a 120-character line is still a few
    seconds.
    """
    import time as _time

    import qmp as qmpmod

    unsupported: list[str] = []
    for ch in text:
        if ch in "\r\n\t":
            unsupported.append(ch)
            continue
        try:
            if ch.isupper() and ch.isalpha():
                qmpmod.key_combo(q, "shift", ch.lower(), hold=hold)
            elif ch in SHIFTED:
                qmpmod.key_combo(q, "shift", SHIFTED[ch], hold=hold)
            else:
                qmpmod.key(q, qcode_for(ch))
        except (KeyError, ValueError):
            unsupported.append(ch)
            continue
        _time.sleep(per_char)
    return unsupported


def type_into(q, text: str, per_char: float = 0.03, sleep=None,
              hold: float = 0.02) -> None:
    """Send `text` to a QMP connection one keystroke at a time.

    `per_char` is the gap between keystrokes. 0.03s is what the Windows stage uses for a
    shell command; a form that validates as it is typed needs more - drive.py uses 0.4 -
    which is why this is a parameter and not a constant.
    """
    import time as _time

    import qmp as qmpmod

    wait = sleep or _time.sleep
    for kind, payload in for_text(text):
        if kind == "combo":
            # A CHORD IS HELD FOR A MOMENT, for the reason type_text documents: sending the
            # modifier's down and up in one request loses the release on a guest whose input
            # stack is slower than the request, and every later character arrives shifted.
            qmpmod.key_combo(q, *payload, hold=hold)   # type: ignore[arg-type]
        else:
            qmpmod.key(q, payload)                 # type: ignore[arg-type]
        wait(per_char)
