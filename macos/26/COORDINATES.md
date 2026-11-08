# macOS 26 Setup Assistant — measured coordinates

> **macOS 26 is not a supported release here.** The coordinates below are correct and the
> wizard can be driven with them, but the wizard never finishes, and a 26 disk does not boot
> back to a desktop on its own. `.github/workflows/macos26.yml` therefore stops at `desktop`.
> See `macos/README.md` for the measurements and `README.md` for the summary.

Every number here was measured from a 1920x1080 screendump of a running guest, which is the
same coordinate system a click uses. Nothing here is scaled from a 1280x800 recording.

Status: **the walkthrough is complete through the Apple Account pane**. The remaining screens
(age range onward) are recorded as they were measured; see the bottom section. The panes
after Apple Account are not reachable in a bake: the pane before them holds. 

    All coordinates are GUEST pixels at 1920x1080.
    Left = the y where the control sits; x is the horizontal centre unless noted.

---

## 1. Select Your Country or Region

| Action | Coordinate | Note |
|---|---|---|
| Focus the list | `(960, 540)` | inside the list box, empty row area |
| Type the country | — | keyboard: `united states`; the list filters and selects |
| Continue | `(1286, 799)` | |

A single click gives the list keyboard focus and the filter starts working immediately.
Do NOT click twice: the second click lands after the pane has changed.

## 2. Transfer Your Data to This Mac

The four radio circles are all at **x = 742**. The label text starts at x = 759, so a click at
x ≥ 860 is PAST the text and selects nothing.

| Row | Radio centre |
|---|---|
| From a Mac, Time Machine, or startup disk | `(742, 647)` |
| From a Windows PC | `(742, 668)` |
| Set up with iPhone or iPad | `(742, 690)` |
| **Set up as new** | **`(742, 712)`** ← this one |

| Action | Coordinate |
|---|---|
| Continue | `(1286, 799)` |

**`(742, 712)` is the number that matters.** An earlier attempt used `(900, 710)` — which is
to the right of the label's end at x=843 — and the selection never changed. The frames showed
"From a Mac" staying selected while the pointer sat on empty space.

The keyboard route also works and is what `drive.py` does: click the FIRST row to focus, then
three Downs. It avoids y entirely, so it survives a layout shift.

## 3. Written and Spoken Languages

| Action | Coordinate |
|---|---|
| Continue | `(1286, 799)` |

## 4. Accessibility

macOS 26 shows **`Not Now`** here, not `Continue` — the left of the pair.

| Action | Coordinate |
|---|---|
| Not Now | `(1283, 799)` |

## 5. Data & Privacy

| Action | Coordinate |
|---|---|
| Continue | `(1286, 799)` |

With "Set up as new" chosen, this does NOT open the pairing handshake that macOS 15 does.

## 6. Create a Mac Account

| Field | Coordinate |
|---|---|
| Full Name | `(960, 493)` |
| Account Name | `(960, 528)` — auto-filled from Full Name, leave it alone |
| Password | `(843, 602)` |
| Verify Password | `(1068, 602)` |
| Hint (Optional) | leave empty |
| Continue | `(1286, 799)` |

After Continue the footer shows `Creating account…` and Continue greys out. It took **60
seconds** on this machine. 180 seconds is the budget `drive.py` uses.

## 7. Sign In to Your Apple Account

| Action | Coordinate |
|---|---|
| `Other Sign-In Options` button | `(670, 802)` |
| menu item `Use Multiple Accounts` | `(683, 829)` |
| **menu item `Sign In Later in Settings`** | **`(683, 856)`** ← this one |
| `Skip` in the confirmation alert | `(1021, 645)` |

The menu animates out of the button; wait ~4s after the click before choosing an item.

## 8. Terms and Conditions

| Action | Coordinate |
|---|---|
| Agree (footer) | `(1284, 799)` |
| Agree (the "I have read and agree" alert) | `(1022, 609)` |

Two different Agrees: the footer one opens the alert, the alert one confirms.

## 9. Age Range (macOS 26 only — not in macOS 15)

| Row | Coordinate |
|---|---|
| Child (12 or younger) | `(960, 583)` |
| Teen (13 to 17) | `(960, 648)` |
| **Adult (18 or older)** | **`(960, 713)`** ← this one |

These are full-width rows, so x = 960 is the middle of the row and safe.

## 10. Enable Location Services

| Action | Coordinate |
|---|---|
| Continue (leave the checkbox OFF) | `(1280, 799)` |
| `Don't Use` in the confirmation alert | `(960, 576)` |

## 11. Select Your Time Zone

Default is `Cupertino, CA - United States`; leave it.

| Action | Coordinate |
|---|---|
| Continue | `(1280, 799)` |

## 12. Analytics

| Action | Coordinate |
|---|---|
| Uncheck "Share Mac Analytics with Apple" | `(742, 522)` |
| Continue | `(1280, 799)` |

## 13. Screen Time

| Action | Coordinate |
|---|---|
| **Set Up Later** | `(1134, 799)` |
| Continue | `(1280, 799)` |

## 14. FileVault

| Action | Coordinate |
|---|---|
| **Not Now** | `(1131, 799)` |
| `Continue` in the "Mac Data Will Not Be Securely Encrypted" alert | `(1022, 606)` |

## 15. Choose Your Look

Light is selected by default.

| Action | Coordinate |
|---|---|
| Continue | `(1280, 799)` |

## 16. Update Mac Automatically

| Action | Coordinate |
|---|---|
| Continue | `(1280, 799)` |

**This is the screen that hung for six minutes** under qemu's user-mode NAT, because macOS was
waiting for `gdmf.apple.com` and the slirp path could not reach it. Untested on a bridged tap so
far; that is the next thing to confirm.

---

## What is still missing from `macos/15/drive.py`

The script's fixed sequence was written against macOS 15. Comparing it to the list above:

| Screen | In drive.py? |
|---|---|
| Country | yes |
| Transfer | yes (but at the old x=825) |
| Languages | yes |
| Accessibility | yes — but it clicks `CONTINUE`, while 26 shows `Not Now` |
| Data & Privacy | yes |
| Apple Account | yes |
| **Create a Mac Account** | yes |
| **Terms and Conditions** | yes |
| **Age Range** | **NO — 26 only** |
| **Location Services** | **NO — 26 only** |
| **Time Zone** | **NO — 26 only** |
| **Analytics** | **NO — 26 only** |
| **Screen Time** | **NO — 26 only** |
| **FileVault** | **NO — 26 only** |
| **Choose Your Look** | **NO — 26 only** |
| **Update Mac Automatically** | **NO — 26 only** |

That is why the macOS 26 desktop stage could not finish: after the account pane, `drive.py`'s
`tail()` sends ten blind "Continue" clicks at a layout it has never seen. The four new screens
need their own steps, in this order, between the Apple Account pane and the end.
