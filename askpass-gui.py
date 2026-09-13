#!/usr/bin/env python3
"""
askpass-gui.py

A minimal SUDO_ASKPASS helper: sudo calls this program whenever it needs a
password and can't (or shouldn't) prompt on a terminal — which is exactly
the situation a GUI app is in. It shows a small password dialog and prints
whatever was entered to stdout, which is the contract sudo expects from an
askpass program.

This is used by Kernel Manager GUI; it also works standalone:
    SUDO_ASKPASS=/path/to/askpass-gui.py sudo -A whoami
"""
import sys
import tkinter as tk


def ask_password() -> str:
    root = tk.Tk()
    root.title("Authentication required")
    root.resizable(False, False)
    # Keep it on top and roughly centered
    root.attributes("-topmost", True)

    result = {"value": None}

    frame = tk.Frame(root, padx=20, pady=16)
    frame.pack()

    label_text = "sudo needs your password to continue"
    if len(sys.argv) > 1:
        # sudo passes a descriptive prompt as argv[1], e.g. "[sudo] password for adrian: "
        label_text = sys.argv[1].strip()

    tk.Label(frame, text=label_text, wraplength=320, justify="left").pack(pady=(0, 10))

    entry_var = tk.StringVar()
    entry = tk.Entry(frame, textvariable=entry_var, show="*", width=30)
    entry.pack()
    entry.focus_set()

    def submit(event=None):
        result["value"] = entry_var.get()
        root.destroy()

    def cancel(event=None):
        result["value"] = None
        root.destroy()

    entry.bind("<Return>", submit)
    entry.bind("<Escape>", cancel)

    btn_frame = tk.Frame(frame)
    btn_frame.pack(pady=(12, 0))
    tk.Button(btn_frame, text="OK", width=10, command=submit).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Cancel", width=10, command=cancel).pack(side="left", padx=4)

    root.update_idletasks()
    # Center on screen
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    root.mainloop()
    return result["value"]


if __name__ == "__main__":
    pw = ask_password()
    if pw is None:
        sys.exit(1)
    # sudo reads the password from stdout, nothing else should go there.
    print(pw)
