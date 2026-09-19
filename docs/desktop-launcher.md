# Desktop launcher identity

Kernel Manager uses the same PNG for Tk's `iconphoto(True, ...)` and the
per-user Applications launcher. The PNG is resolved relative to the script,
not the working directory; image-loading errors remain nonfatal.

The running application's identity is separate from its window icon. On the
Ubuntu/GNOME XWayland session used for verification, `Tk(className="KernelManager")`
actually produced:

```text
WM_CLASS(STRING) = "kernelManager", "Kernelmanager"
```

Neither value matched the old `StartupWMClass=KernelManager`. Tk title-cases the
root class, losing the internal capital M. An explicit Toplevel `class_` does
not undergo that same transformation.

The corrected identity is:

| Setting | Value |
| --- | --- |
| Desktop filename | `kernel-manager-gui.desktop` |
| Tk `className` and child `class_` | `Kernel-manager-gui` |
| Root WM_CLASS instance | `kernel-manager-gui` |
| Root and child WM_CLASS class | `Kernel-manager-gui` |
| Desktop `StartupWMClass` | `Kernel-manager-gui` |

This supplies an exact class match and a filename match through the root's
instance name. GNOME's window tracker tries both WM_CLASS fields against
StartupWMClass and then desktop filenames:
https://github.com/GNOME/gnome-shell/blob/main/src/shell-window-tracker.c

Tk's Linux X11 backend uses the same properties under an X11 session or under
XWayland in a Wayland session. The live probe reported `tk windowingsystem` as
`x11` in this Wayland session. This is not a native Wayland application, so no
native Wayland app_id setting is needed for this backend.

## Refresh and verify

Use **Tools → Install / Update Desktop Launcher**, or accept **Install** on the
first-run offer. The old StartupWMClass is considered stale, so the offer also
appears for an existing launcher with the old identity. Installation still
requires the user's explicit choice and uses the existing shell installer.
The desktop filename is unchanged, preserving the pinned launcher's identity.
Close Kernel Manager and launch the updated application again.

Run this in a terminal, then click inside the main Kernel Manager window:

```sh
xprop WM_CLASS
```

Expected:

```text
WM_CLASS(STRING) = "kernel-manager-gui", "Kernel-manager-gui"
```

Check the installed launcher with:

```sh
grep -E '^(Exec|Icon|StartupWMClass)=' "${XDG_DATA_HOME:-$HOME/.local/share}/applications/kernel-manager-gui.desktop"
```

When inspecting Tk programmatically, use the X parent of `winfo_id()` (Tk's
client wrapper). `wm frame` can instead identify GNOME's decoration window,
whose WM_CLASS is `mutter-x11-frames`. The regression test inspects the client
wrapper's WM_CLASS and _NET_WM_ICON for both the root and a child window.
