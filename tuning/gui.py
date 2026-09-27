"""Dedicated, dynamically populated read-only Tk view."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .manager import TuningManager
from .model import ReadStatus

EMPTY = "No supported performance tuning controls were detected on this system."
SECTIONS = ("CPU", "Power & Boost", "Thermal", "Advanced")


def display_value(cap):
    if cap.status != ReadStatus.OK:
        return cap.status.value
    value = cap.current
    if value is None:
        return "Unknown"
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    elif isinstance(value, tuple):
        value = ", ".join(map(str, value))
    return str(value) + (" " + cap.units if cap.units else "")


def sections(discovery):
    return {name: tuple(c for c in discovery.capabilities if c.category == name)
            for name in SECTIONS if any(c.category == name for c in discovery.capabilities)}


def details(cap):
    lines = [cap.name, cap.description, "", f"Value: {display_value(cap)}",
             f"Classification: {cap.risk.value}", f"Domain: {cap.domain}",
             f"Provider: {', '.join(dict.fromkeys(s.provider for s in cap.sources))}",
             f"Status: {cap.status.value}", f"Capability ID: {cap.id}",
             f"Discovery generation: {cap.generation}", f"Timestamp: {cap.timestamp}",
             "Access: read-only in this release" + (
                 "; interface may be adjustable in a future phase" if cap.potentially_writable else "")]
    for label, value in (("Requested", cap.requested), ("Observed", cap.observed),
                         ("Minimum", cap.minimum), ("Maximum", cap.maximum), ("Step", cap.step),
                         ("Choices", ", ".join(cap.choices) or None),
                         ("Documented default", cap.documented_default),
                         ("Default source", cap.default_provenance), ("Lifetime", cap.lifetime),
                         ("Authorization", cap.authorization), ("Note", cap.blocked_reason)):
        lines.append(f"{label}: {value if value is not None else 'Unknown / not supplied'}")
    lines.extend(f"{key}: {value if value is not None else 'Unknown'}" for key, value in cap.metadata.items())
    lines.extend(f"Relationship — {item.relation}: {item.target}" for item in cap.relationships)
    for source in cap.sources:
        lines += [f"Source: {source.path}", f"Canonical source: {source.canonical_path}",
                  f"Encoding: {source.encoding}", f"Raw reading: {source.raw}"]
    return "\n".join(lines)


class TuningWindow:
    def __init__(self, parent, *, _manager=None):
        self.manager = _manager if _manager is not None else TuningManager()
        self.window = tk.Toplevel(parent)
        self.window.title("Performance & Tuning")
        self.window.geometry("820x600")
        self.window.minsize(700, 480)
        self.closed = self.pending = False
        self.after_id = None
        self.results = queue.Queue()
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.bind("<Destroy>", self._destroyed, add="+")
        header = ttk.Frame(self.window, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="Performance & Tuning", font="TkHeadingFont").pack(anchor="w")
        ttk.Label(header, text="Discovery only — this page never changes system settings.",
                  wraplength=650).pack(anchor="w", pady=4)
        self.refresh_button = ttk.Button(header, text="Refresh", command=self.refresh)
        self.refresh_button.pack(anchor="e")
        self.status = ttk.Label(header, text="", wraplength=650)
        self.status.pack(anchor="w")
        self.notebook = ttk.Notebook(self.window)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.refresh()

    def _destroyed(self, event):
        if event.widget is self.window:
            self.closed = True
            if self.after_id is not None:
                self.window.after_cancel(self.after_id)
                self.after_id = None

    def refresh(self):
        if self.pending or self.closed:
            return
        self.pending = True
        self.refresh_button.configure(state="disabled")
        self.status.configure(text="Discovering available interfaces…")

        def work():
            try:
                self.results.put((self.manager.discover(), None))
            except Exception as exc:
                self.results.put((None, str(exc)))
        threading.Thread(target=work, daemon=True).start()
        self.after_id = self.window.after(100, self._collect)

    def _collect(self):
        self.after_id = None
        if self.closed:
            return
        try:
            discovery, error = self.results.get_nowait()
        except queue.Empty:
            self.after_id = self.window.after(100, self._collect)
            return
        self.pending = False
        self.refresh_button.configure(state="normal")
        if error:
            self.status.configure(text="Discovery failed: " + error)
        else:
            self.render(discovery)

    def _text(self, frame):
        text = tk.Text(frame, wrap="word", height=9, font="TkDefaultFont")
        colors = getattr(self.window.master, "_ubuntu_colors", {})
        if colors:
            text.configure(background=colors["surface"], foreground=colors["text"])
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        return text

    @staticmethod
    def _set_text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def render(self, discovery):
        for child in self.notebook.winfo_children():
            child.destroy()
        self.status.configure(text=f"{len(discovery.capabilities)} readings • {discovery.timestamp}")
        overview = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(overview, text="Overview")
        summary = ["Read-only discovery. No authentication or configuration changes.", "",
                   "Detected providers: " + (", ".join(discovery.providers) or "none"),
                   f"Potentially adjustable interfaces: {discovery.adjustable_count}"]
        unavailable = sum(c.status != ReadStatus.OK for c in discovery.capabilities)
        if unavailable:
            summary.append(f"Unavailable/malformed readings: {unavailable}; select readings for explanations.")
        if not discovery.adjustable_count:
            summary += ["", EMPTY, "Monitoring/informational data is shown where available."]
        summary += ["", "Select a section, then a reading for its source, limits and explanation.",
                    "Accepted driver limits are not guarantees of safe operation."]
        if discovery.diagnostics:
            summary += ["", "Discovery notes:", *discovery.diagnostics]
        self._set_text(self._text(overview), "\n".join(summary))
        for section, capabilities in sections(discovery).items():
            frame = ttk.Frame(self.notebook, padding=6)
            self.notebook.add(frame, text=section)
            panes = ttk.Panedwindow(frame, orient="vertical")
            panes.pack(fill="both", expand=True)
            listing, information = ttk.Frame(panes), ttk.Frame(panes)
            panes.add(listing, weight=3)
            panes.add(information, weight=2)
            tree = ttk.Treeview(listing, columns=("value", "class"), height=8)
            for column, title, width in (("#0", "Domain / capability", 260),
                                         ("value", "Current value", 130),
                                         ("class", "Classification", 210)):
                tree.heading(column, text=title)
                tree.column(column, width=width, minwidth=80)
            scroll = ttk.Scrollbar(listing, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            tree.pack(fill="both", expand=True)
            info = self._text(information)
            self._set_text(info, "Select a reading to inspect its provenance and semantics.")
            domains, entries = {}, {}
            for cap in capabilities:
                if cap.domain not in domains:
                    domains[cap.domain] = tree.insert("", "end", text=cap.domain, open=False)
                item = tree.insert(domains[cap.domain], "end", text=cap.name,
                                   values=(display_value(cap), cap.risk.value))
                entries[item] = cap

            def selected(event, tree=tree, info=info, entries=entries):
                selection = tree.selection()
                if selection and selection[0] in entries:
                    self._set_text(info, details(entries[selection[0]]))
            tree.bind("<<TreeviewSelect>>", selected)
