# macOS 15 Setup Assistant: the panes, in order, with the coordinate that worked

Every coordinate is a GUEST pixel on the 1920x1080 screen, measured from a screendump of that
very pane. screendump writes the guest's own framebuffer at its own size, so a pixel read in
that image IS the coordinate to send - do not scale it.

Walked by hand on 2026-11-06 against `macos15-setup.qcow2` from ghcr, in a pod on the k3s
node with a bridged tap. The machine reached a Finder menu bar and a Dock.

## The sequence

| # | Pane | Action | Settle |
|---|------|--------|--------|
| 1 | Select Your Country or Region | click the list (900,500), type `united states`, click Continue (1306,810) | 12s |
| 2 | Transfer Your Data to This Mac | **click the LABEL at (880,666)**, then Continue (1306,810) | 12s |
| 3 | Written and Spoken Languages | Continue (1306,810) | 12s |
| 4 | Accessibility | **Not Now** (1306,810) | 12s |
| 5 | Data & Privacy | Continue (1306,810) | 12s |
| 6 | Create a Mac Account | Full name is already focused; type `docker`. Password (850,531) `admin`, verify (1086,531) `admin`, Continue (1306,810) | **90s** |
| 7 | Sign In to Your Apple Account | `Set Up Later` (620,811), then the sheet's **Skip** (1026,634) | 12s |
| 8 | Terms and Conditions | footer Agree (1302,805), then the sheet's **Agree** (1019,613) | 15s |
| 9 | Enable Location Services | Continue (1306,810), then the sheet's **Don't Use** (960,632) | 12s |
| 10 | Select Your Time Zone | Continue (1306,806) - the default is Pacific/Cupertino | 12s |
| 11 | Analytics | untick Share Mac Analytics (808,513), Continue (1306,806) | 12s |
| 12 | Screen Time | `Set Up Later` (619,806) | **wait it out** |
| 13 | loginwindow | click the password box (960,985), type `admin`, Return | 25s |
| 14 | Keyboard Setup Assistant | **Quit** (1222,821) | 6s |

## The four things that do not work the way they look

**The radio button's LABEL is the target, not its circle.** On the transfer pane, clicks at
(824,666) and (823,666) - the circle itself, twice - moved the pointer onto the circle and
changed nothing. Two frames an hour apart show the pointer sitting on it with the old row
still selected. The click at (880,666), on the words "Set up as new", took immediately.

**A sheet's default button sometimes needs Return.** On the Location Services confirmation,
a click at (960,632) darkened "Don't Use" and left the sheet up; `key ret` dismissed it. A
click that visibly depresses the button is not the same as the button firing.

**The footer Agree and the sheet Agree are different clicks.** (1302,805) opens the
"I have read and agree" sheet; (1019,613) is the sheet's own Agree. Doing only the first
leaves the licence pane up.

**Screen Time's `Set Up Later` is not instant.** It spins in the lower-left corner with both
footer buttons greyed for a long time. That is the pane that was mistaken for a hang for
eighteen minutes in run 30626563534's predecessor - see the MTU note below.

## Why #12 looked like a hang, and was not

The pane sat with a spinner and three ESTABLISHED connections to an Apple CDN whose byte
counter did not move. The guest's MTU was 1500 while the container's uplink is 1450, so
every full-size packet was dropped silently and the TLS handshake could never receive a
certificate. `lib/net.py` now reads the MTU from the uplink and clamps MSS. With that fixed
the same pane completes in seconds.

If a pane spins for more than a minute or two, the thing to check first is
`MASQUERADE` packet growth on the pod - not the pane.

## Where the account is created

The form's Continue starts a real account creation: home directory, keychain, login items.
Measured at 72 seconds on one run and still running at 80 on another, so `drive.py` waits
180. Clicking earlier does not fail - it clicks Continue on a form that is still working, and
the wizard returns to the account pane, which reads like "the wizard went backwards".
