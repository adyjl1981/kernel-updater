"""Small, shared Ubuntu-style theme using only Tk/ttk and optional GSettings."""
import ast
import math
import os
import re
import shutil
import subprocess
import queue
import threading
import tkinter as tk
from tkinter import font, ttk


LIGHT = {
    "window": "#F6F5F4", "surface": "#FFFFFF", "text": "#2E2E2E",
    "muted": "#5E5C64", "border": "#8D8A86", "button": "#EBE9E7",
    "hover": "#E0DDDA", "pressed": "#D2CFCC", "disabled": "#73706D",
}
DARK = {
    "window": "#242424", "surface": "#303030", "text": "#F6F5F4",
    "muted": "#C0BFBC", "border": "#85817D", "button": "#3D3D3D",
    "hover": "#494949", "pressed": "#555555", "disabled": "#ABA7A3",
}


# Ubuntu Yaru's common/accent-colors.scss.in; GNOME names are aliases below.
ACCENTS = {
    "default": "#E95420", "orange": "#E95420", "bark": "#787859",
    "sage": "#657B69", "olive": "#4B8501", "viridian": "#03875B",
    "prussiangreen": "#308280", "blue": "#0073E5", "purple": "#7764D8",
    "magenta": "#B34CB3", "red": "#DA3450", "yellow": "#C88800",
    "wartybrown": "#B39169", "mate": "#87A556",
    "teal": "#308280", "green": "#4B8501", "pink": "#B34CB3",
    "slate": "#657B69", "brown": "#B39169",
}


def detect_accent(preferences):
    value = preferences.get("accent-color")
    if isinstance(value, str) and value in ACCENTS:
        return ACCENTS[value]
    # Before GNOME 47 Ubuntu's appearance panel selected a Yaru GTK variant.
    # Only recognize exact stock names; never infer appearance from theme names.
    theme = preferences.get("gtk-theme", "")
    if isinstance(theme, str):
        match = re.fullmatch(r"Yaru(?:-([a-z]+))?(?:-dark)?", theme)
        if match:
            return ACCENTS.get(match[1] or "default", ACCENTS["default"])
    return ACCENTS["default"]


def luminance(color):
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))


def contrast(first, second):
    low, high = sorted((luminance(first), luminance(second)))
    return (high + .05) / (low + .05)


def mix(first, second, amount):
    return "#" + "".join(f"{round(int(first[i:i+2], 16) * (1-amount) + int(second[i:i+2], 16) * amount):02X}"
                         for i in (1, 3, 5))


def palette(preferences):
    colors = dict(DARK if preferences.get("color-scheme") == "prefer-dark" else LIGHT)
    accent = detect_accent(preferences)
    ink = max(("#000000", "#FFFFFF"), key=lambda c: contrast(c, accent))
    # Move toward the opposite of the text so hover/pressed retain text contrast.
    target = "#FFFFFF" if ink == "#000000" else "#000000"
    focus = accent
    for step in range(1, 101):
        if min(contrast(focus, colors[k]) for k in ("window", "surface")) >= 3:
            break
        focus = mix(accent, "#FFFFFF" if preferences.get("color-scheme") == "prefer-dark" else "#000000", step / 100)
    colors.update(accent=accent, accent_text=ink, accent_hover=mix(accent, target, .10),
                  accent_pressed=mix(accent, target, .20), focus=focus,
                  inactive_selection=mix(colors["surface"], accent, .18))
    return colors


def desktop_preferences():
    """Read supported GNOME settings without changing desktop preferences.

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
        wanted = {"color-scheme", "font-name", "monospace-font-name", "text-scaling-factor", "accent-color", "gtk-theme"}
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
    automatic = preferences is None
    preferences = desktop_preferences() if automatic else preferences
    colors = palette(preferences)
    root._ubuntu_colors = colors
    style = ttk.Style(root)
    style.theme_use("clam")  # Built into Tk; all colours are controllable on Linux.

    available = font.families(root)
    default = font.nametofont("TkDefaultFont", root=root)
    fixed = font.nametofont("TkFixedFont", root=root)
    if not hasattr(root, "_ubuntu_font_sizes"):
        root._ubuntu_font_sizes = max(11, abs(default.actual("size")))
    family, size = choose_font(preferences.get("font-name"), available,
                               ("Ubuntu Sans", "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans", default.actual("family")),
                               root._ubuntu_font_sizes)
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
    # Use Clam's supported indicator colours without replacing native elements.
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
              background=[("selected", colors["accent"]), ("active", colors["hover"])],
              foreground=[("selected", colors["accent_text"])],
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
    for name in ("Horizontal.TProgressbar", "Vertical.TProgressbar"):
        style.configure(name, background=colors["accent"], lightcolor=colors["accent"],
                        darkcolor=colors["accent"], troughcolor=colors["button"])
    _refresh_classic(root)
    root._ubuntu_preferences = dict(preferences)
    if automatic and not getattr(root, "_ubuntu_watch_started", False):
        _watch_preferences(root)
    return colors


def _refresh_classic(widget):
    if isinstance(widget, tk.Toplevel):
        widget.configure(background=widget._root()._ubuntu_colors["window"])
    elif isinstance(widget, tk.Text):
        options = text_options(widget)
        # Preserve intentional UI font, geometry and padding.
        for key in ("font", "padx", "pady", "borderwidth", "relief", "highlightthickness", "insertwidth"):
            options.pop(key)
        widget.configure(**options)
    for child in widget.winfo_children():
        _refresh_classic(child)


def _watch_preferences(root):
    """Bounded reads off the Tk thread; all widget changes stay on that thread."""
    root._ubuntu_watch_started = True
    results = queue.Queue()
    after_id = None

    def schedule(delay, callback):
        nonlocal after_id
        after_id = root.after(delay, callback)

    def destroyed(event):
        if event.widget is root and after_id is not None:
            root.after_cancel(after_id)

    root.bind("<Destroy>", destroyed, add="+")

    def read():
        results.put(desktop_preferences())

    def start():
        threading.Thread(target=read, daemon=True).start()
        schedule(100, collect)

    def collect():
        try:
            preferences = results.get_nowait()
        except queue.Empty:
            schedule(100, collect)
            return
        # A transient bus failure must not erase a valid desktop preference.
        if preferences and preferences != root._ubuntu_preferences:
            apply_theme(root, preferences)
        schedule(3000, start)

    schedule(3000, start)


def text_options(parent):
    """Text has no ttk equivalent; give it the same surfaces and focus states."""
    root = parent._root()
    colors = getattr(root, "_ubuntu_colors", palette({}))
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
