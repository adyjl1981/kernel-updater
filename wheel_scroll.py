"""Route wheel events before Tk class bindings, using the hovered ancestry."""
import tkinter as tk


class WheelRouter:
    def __init__(self, root):
        self.root = root
        self.tag = 'KernelWheel' + str(id(self))
        for sequence in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
            root.bind_class(self.tag, sequence, self.dispatch)
        self.map_binding = root.bind_all('<Map>', self.attach, add='+')
        self.destroy_binding = root.bind('<Destroy>', self.on_destroy, add='+')
        self.attach_tree(root)

    def on_destroy(self, event):
        if event.widget is self.root:
            for sequence in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
                self.root.unbind_class(self.tag, sequence)
            # Remove just our callback, preserving unrelated global bindings.
            self.root._unbind(('bind', 'all', '<Map>'), self.map_binding)

    def attach_tree(self, widget):
        self.attach_widget(widget)
        for child in widget.winfo_children():
            self.attach_tree(child)

    def attach(self, event):
        self.attach_widget(event.widget)

    def attach_widget(self, widget):
        tags = widget.bindtags()
        if self.tag not in tags:
            widget.bindtags((self.tag, *tags))

    def dispatch(self, event):
        number = getattr(event, 'num', None)
        delta = getattr(event, 'delta', 0)
        if number in (4, 5):
            units = -1 if number == 4 else 1
        elif delta:
            units = (-1 if delta > 0 else 1) * max(1, int(abs(delta) / 120))
        else:
            return None
        # Focus can differ from hover, particularly for MouseWheel events.
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            kind = widget.winfo_class()
            if kind in ('TCombobox', 'Combobox', 'TScale', 'Scale', 'Spinbox', 'TSpinbox', 'Entry', 'TEntry'):
                return None  # leave value controls to their native bindings
            try:
                if kind in ('Canvas', 'Text', 'Listbox', 'Treeview'):
                    first, last = widget.yview()
                    if (units < 0 and first > 0) or (units > 0 and last < 1):
                        widget.yview_scroll(units, 'units')
                        return 'break'  # prevent a second native scroll
            except tk.TclError:
                pass
            widget = getattr(widget, 'master', None)
        return 'break'  # no overflowing ancestor; avoid scrolling focused widgets


def install(root):
    if not hasattr(root, '_kernel_wheel_router'):
        root._kernel_wheel_router = WheelRouter(root)
    return root._kernel_wheel_router
