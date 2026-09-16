"""Small, shared Ubuntu-style theme using only Tk/ttk and optional GSettings."""
import ast
import math
import os
import re
import shutil
import subprocess
import tkinter as tk
from tkinter import font, ttk


LIGHT = {
    "window": "#F6F5F4", "surface": "#FFFFFF", "text": "#2E2E2E",
    "muted": "#5E5C64", "border": "#8D8A86", "button": "#EBE9E7",
    "hover": "#E0DDDA", "pressed": "#D2CFCC", "disabled": "#73706D",
    # A deeper Ubuntu orange keeps white button/selection text readable.
    "accent": "#C34113", "accent_hover": "#AD390F", "accent_pressed": "#96310D",
    "accent_text": "#FFFFFF", "focus": "#C34113", "inactive_selection": "#ECD9D1",
}
DARK = {
    "window": "#242424", "surface": "#303030", "text": "#F6F5F4",
    "muted": "#C0BFBC", "border": "#85817D", "button": "#3D3D3D",
    "hover": "#494949", "pressed": "#555555", "disabled": "#ABA7A3",
    "accent": "#E95420", "accent_hover": "#F46B39", "accent_pressed": "#FF875A",
    "accent_text": "#171717", "focus": "#FF9366", "inactive_selection": "#604236",
}


def desktop_preferences():
    """Read GNOME's explicit preference once; never infer it from a theme name.

    Other desktops, missing schemas, and unavailable session buses use light
    Ubuntu defaults. No daemon, theme package, or Python GNOME binding is needed.
    """
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower().split(":")
    if not {"ubuntu", "gnome"}.intersection(desktop) or not shutil.which("gsettings"):
        return {}
    try:
        proc = subprocess.run(
            ["gsettings", "list-recursively", "org.gnome.desktop.interface"],
            capture_output=True, text=True, timeout=1,
        )
        if proc.returncode:
            return {}
        wanted = {"color-scheme", "font-name", "monospace-font-name", "text-scaling-factor"}
        values = {}
        for line in proc.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) == 3 and fields[1] in wanted:
                try:
                    values[fields[1]] = ast.literal_eval(fields[2])
                except (ValueError, SyntaxError):
                    continue
        return values
    except (OSError, subprocess.SubprocessError):
        return {}


def choose_font(description, available, fallbacks, default_size):
    """Resolve ordinary Pango family/point-size descriptions without Pango."""
    families = {name.casefold(): name for name in available}
    size = default_size
    if isinstance(description, str):
        match = re.fullmatch(r"(.+?)\s+(\d+(?:\.\d+)?)", description.strip())
        if match and 1 <= float(match[2]) <= 96:
            size = round(float(match[2]))
            if match[1].casefold() in families:
                return families[match[1].casefold()], size
    return next((families[name.casefold()] for name in fallbacks if name.casefold() in families), fallbacks[-1]), size


def apply_theme(root, preferences=None):
    preferences = desktop_preferences() if preferences is None else preferences
    colors = dict(DARK if preferences.get("color-scheme") == "prefer-dark" else LIGHT)
    root._ubuntu_colors = colors
    style = ttk.Style(root)
    style.theme_use("clam")  # Built into Tk; all colours are controllable on Linux.

    available = font.families(root)
    default = font.nametofont("TkDefaultFont", root=root)
    fixed = font.nametofont("TkFixedFont", root=root)
    family, size = choose_font(preferences.get("font-name"), available,
                               ("Ubuntu Sans", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans", default.actual("family")),
                               max(11, abs(default.actual("size"))))
    mono, mono_size = choose_font(preferences.get("monospace-font-name"), available,
                                  ("Ubuntu Sans Mono", "Ubuntu Mono", "DejaVu Sans Mono", fixed.actual("family")), size)
    scale = preferences.get("text-scaling-factor", 1.0)
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or not 0.5 <= scale <= 4:
        scale = 1.0
    size, mono_size = max(1, round(size * scale)), max(1, round(mono_size * scale))
    # Respect Tk's existing DPI scaling; use named fonts so dialogs match too.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                 "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
        named = font.nametofont(name, root=root)
        named.configure(family=family, size=size)
    fixed.configure(family=mono, size=mono_size)
    font.nametofont("TkHeadingFont", root=root).configure(weight="bold")
    line_height = default.metrics("linespace")

    root.configure(background=colors["window"])
    for option, value in {
        "*Font": "TkDefaultFont", "*background": colors["window"], "*foreground": colors["text"],
        "*activeBackground": colors["hover"], "*activeForeground": colors["text"],
        "*selectBackground": colors["accent"], "*selectForeground": colors["accent_text"],
        "*highlightBackground": colors["border"], "*highlightColor": colors["focus"],
        "*Dialog.msg.font": "TkDefaultFont", "*Dialog.msg.wrapLength": "480",
        "*TCombobox*Listbox.background": colors["surface"],
        "*TCombobox*Listbox.foreground": colors["text"],
    }.items():
        root.option_add(option, value)

    style.configure(".", font="TkDefaultFont", background=colors["window"], foreground=colors["text"],
                    bordercolor=colors["border"], lightcolor=colors["window"], darkcolor=colors["window"],
                    troughcolor=colors["window"], selectbackground=colors["accent"],
                    selectforeground=colors["accent_text"], focuscolor=colors["focus"])
    style.map(".", foreground=[("disabled", colors["disabled"])],
              background=[("disabled", colors["window"])])
    style.configure("TFrame", background=colors["window"])
    style.configure("TLabel", padding=(0, 2))
    style.configure("Status.TLabel", foreground=colors["muted"], padding=(8, 6))
    style.configure("TButton", padding=(12, 7), width=-8, relief="flat", borderwidth=1,
                    background=colors["button"], lightcolor=colors["button"], darkcolor=colors["button"])
    style.map("TButton", background=[("disabled", colors["button"]), ("pressed", colors["pressed"]), ("active", colors["hover"])],
              bordercolor=[("focus", colors["focus"]), ("!focus", colors["border"])],
              lightcolor=[("pressed", colors["pressed"]), ("active", colors["hover"])],
              darkcolor=[("pressed", colors["pressed"]), ("active", colors["hover"])])
    style.configure("Accent.TButton", background=colors["accent"], foreground=colors["accent_text"],
                    lightcolor=colors["accent"], darkcolor=colors["accent"], bordercolor=colors["accent"])
    accent_map = [("disabled", colors["button"]), ("pressed", colors["accent_pressed"]),
                  ("active", colors["accent_hover"]), ("!disabled", colors["accent"])]
    style.map("Accent.TButton", background=accent_map, lightcolor=accent_map, darkcolor=accent_map,
              foreground=[("disabled", colors["disabled"]), ("!disabled", colors["accent_text"])],
              bordercolor=[("focus", colors["focus"]), ("disabled", colors["border"]), ("!focus", colors["accent"])])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, padding=(2, 5), indicatormargin=(1, 1, 8, 1),
                        indicatorbackground=colors["surface"], indicatorforeground=colors["text"])
        style.map(name, background=[("active", colors["window"])],
                  indicatorbackground=[("disabled", colors["button"]), ("selected", colors["accent"]), ("!selected", colors["surface"])],
                  indicatorforeground=[("disabled", colors["disabled"]), ("selected", colors["accent_text"])])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, padding=(8, 6), fieldbackground=colors["surface"], foreground=colors["text"],
                        background=colors["button"], insertcolor=colors["text"], arrowsize=16,
                        arrowcolor=colors["text"], borderwidth=1, lightcolor=colors["surface"], darkcolor=colors["surface"])
        style.map(name, fieldbackground=[("disabled", colors["button"]), ("readonly", colors["surface"])],
                  foreground=[("disabled", colors["disabled"]), ("readonly", colors["text"])],
                  bordercolor=[("focus", colors["focus"]), ("!focus", colors["border"])],
                  lightcolor=[("focus", colors["focus"])], darkcolor=[("focus", colors["focus"])],
                  background=[("active", colors["hover"])])
    style.configure("TNotebook", borderwidth=0, tabmargins=(0, 0, 0, 8))
    style.configure("TNotebook.Tab", padding=(12, 9), background=colors["button"])
    style.map("TNotebook.Tab", padding=[("selected", (12, 9))],
              background=[("selected", colors["surface"]), ("active", colors["hover"])],
              foreground=[("selected", colors["text"])],
              lightcolor=[("selected", colors["accent"])], bordercolor=[("selected", colors["accent"])])
    style.configure("TLabelframe", relief="solid", borderwidth=1, padding=(8, 6))
    style.configure("TLabelframe.Label", font="TkHeadingFont", padding=(4, 0))
    style.configure("Treeview", background=colors["surface"], fieldbackground=colors["surface"],
                    foreground=colors["text"], rowheight=max(32, line_height + 12), borderwidth=1, relief="solid")
    style.map("Treeview", background=[("selected", "!focus", colors["inactive_selection"]), ("selected", colors["accent"])],
              foreground=[("selected", "!focus", colors["text"]), ("selected", colors["accent_text"])],
              bordercolor=[("focus", colors["focus"])])
    style.configure("Treeview.Heading", font="TkHeadingFont", background=colors["button"],
                    padding=(10, 8), relief="flat", borderwidth=1)
    style.map("Treeview.Heading", background=[("active", colors["hover"])])
    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=colors["button"], arrowcolor=colors["text"], borderwidth=0, arrowsize=16)
        style.map(name, background=[("pressed", colors["pressed"]), ("active", colors["hover"])])
    return colors


def text_options(parent):
    """Text has no ttk equivalent; give it the same surfaces and focus states."""
    root = parent._root()
    colors = getattr(root, "_ubuntu_colors", LIGHT)
    return dict(font="TkFixedFont", background=colors["surface"], foreground=colors["text"],
                insertbackground=colors["text"], selectbackground=colors["accent"],
                selectforeground=colors["accent_text"], inactiveselectbackground=colors["inactive_selection"],
                highlightbackground=colors["border"], highlightcolor=colors["focus"],
                highlightthickness=1, borderwidth=0, relief="flat", padx=12, pady=10, insertwidth=2)


def scrolled_tree(parent, **kwargs):
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, **kwargs)
    _scrollbars(frame, tree, horizontal=True)
    return frame, tree


def scrolled_text(parent, **kwargs):
    frame = ttk.Frame(parent)
    text = tk.Text(frame, **text_options(parent), **kwargs)
    _scrollbars(frame, text, horizontal=kwargs.get("wrap", "none") == "none")
    return frame, text


def _scrollbars(frame, widget, horizontal):
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    widget.grid(row=0, column=0, sticky="nsew")
    vertical = ttk.Scrollbar(frame, orient="vertical", command=widget.yview)
    vertical.grid(row=0, column=1, sticky="ns")
    widget.configure(yscrollcommand=vertical.set)
    if horizontal:
        scroll = ttk.Scrollbar(frame, orient="horizontal", command=widget.xview)
        scroll.grid(row=1, column=0, sticky="ew")
        widget.configure(xscrollcommand=scroll.set)
