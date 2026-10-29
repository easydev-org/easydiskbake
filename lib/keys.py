"""Type text into a guest through QMP, one key at a time.

    from keys import type_text
    type_text(q, "docker")          # as if typed at the keyboard

WHY A MAPPING IS NEEDED

QMP's input-send-event takes a QKeyCode - "a", "shift-a", "spc", "ret" - not a
character. There is no "send the string" command. So each character has to be turned
into the key that produces it plus, where necessary, shift.

WHY THE SHIFTED FORMS ARE SPELLED OUT

QEMU accepts "shift-a" and also "A"; both work, but the explicit form is what the
documentation shows and what the existing key_combo() in qmp.py composes. Being
consistent about that matters more than the extra table entry, because the one wrong
entry here shows up as a typo in a username written to disk.
"""

# Plain characters. QEMU's qcodes are the PC scancode names.
PLAIN = {
    " ": "spc", "-": "minus", "_": "shift-minus", ".": "dot", ",": "comma",
    "/": "slash", "\\": "backslash", "=": "equal", "+": "shift-equal",
    ":": "shift-semicolon", ";": "semicolon", "'": "apostrophe",
    '"': "shift-apostrophe", "@": "shift-2", "!": "shift-1", "#": "shift-3",
    "$": "shift-4", "%": "shift-5", "^": "shift-6", "&": "shift-7",
    "*": "shift-8", "(": "shift-9", ")": "shift-0",
}

for _c in "abcdefghijklmnopqrstuvwxyz":
    PLAIN[_c] = _c
for _d in "0123456789":
    PLAIN[_d] = _d
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    PLAIN[_c] = "shift-" + _c.lower()


def qcodes_for(ch):
    """The qcode for one character, or None if it cannot be typed.

    Returning None rather than guessing is deliberate: a username with a character that
    silently produces nothing is a bake that creates the wrong account, and the failure
    surfaces much later as 'the password does not work'.
    """
    return PLAIN.get(ch)


def type_text(q, text, delay=0.06):
    """Type `text` into the focused widget."""
    import time
    unsupported = []
    for ch in text:
        code = qcodes_for(ch)
        if code is None:
            unsupported.append(ch)
            continue
        q.execute("input-send-event", events=[
            {"type": "key", "data": {"down": True,
                                     "key": {"type": "qcode", "data": code}}}])
        time.sleep(0.02)
        q.execute("input-send-event", events=[
            {"type": "key", "data": {"down": False,
                                     "key": {"type": "qcode", "data": code}}}])
        time.sleep(delay)
    return unsupported


def press(q, code, delay=0.08):
    import time
    q.execute("input-send-event", events=[
        {"type": "key", "data": {"down": True,
                                 "key": {"type": "qcode", "data": code}}}])
    time.sleep(0.02)
    q.execute("input-send-event", events=[
        {"type": "key", "data": {"down": False,
                                 "key": {"type": "qcode", "data": code}}}])
    time.sleep(delay)
