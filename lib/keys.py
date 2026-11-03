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


def type_into(q, text: str, per_char: float = 0.03, sleep=None) -> None:
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
            qmpmod.key_combo(q, *payload)          # type: ignore[arg-type]
        else:
            qmpmod.key(q, payload)                 # type: ignore[arg-type]
        wait(per_char)
