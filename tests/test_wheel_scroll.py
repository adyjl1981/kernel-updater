"""Wheel routing with fixtures plus display-backed event regression."""
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk
import unittest
from unittest import mock
import wheel_scroll


def event(num=None, delta=0):
    return SimpleNamespace(num=num, delta=delta, x_root=10, y_root=20)


class RoutingTests(unittest.TestCase):
    def widget(self, kind, view=(0, 1), parent=None):
        widget = mock.Mock()
        widget.winfo_class.return_value = kind
        widget.yview.return_value = view
        widget.master = parent
        return widget

    def dispatch(self, widget, ev):
        router = wheel_scroll.WheelRouter.__new__(wheel_scroll.WheelRouter)
        router.root = mock.Mock()
        router.root.winfo_containing.return_value = widget
        return router.dispatch(ev)

    def test_hover_nested_edge_and_no_double_scroll(self):
        outer = self.widget('Canvas', (0, .5))
        inner = self.widget('Text', (0, .5), outer)
        self.assertEqual(self.dispatch(inner, event(5)), 'break')
        inner.yview_scroll.assert_called_once_with(1, 'units')
        outer.yview_scroll.assert_not_called()
        inner.yview.return_value = (.5, 1)
        self.assertEqual(self.dispatch(inner, event(5)), 'break')
        outer.yview_scroll.assert_called_once_with(1, 'units')

    def test_linux_and_mousewheel_directions(self):
        for ev, units in ((event(4), -1), (event(5), 1), (event(delta=120), -1),
                          (event(delta=-240), 2), (event(delta=1), -1)):
            widget = self.widget('Treeview', (.1, .9))
            self.assertEqual(self.dispatch(widget, ev), 'break')
            widget.yview_scroll.assert_called_once_with(units, 'units')

    def test_protected_controls_and_empty_events(self):
        outer = self.widget('Canvas', (0, .5))
        for kind in ('TCombobox', 'TScale', 'Scale', 'Spinbox', 'TSpinbox'):
            self.assertIsNone(self.dispatch(self.widget(kind, parent=outer), event(5)))
        outer.yview_scroll.assert_not_called()
        self.assertIsNone(self.dispatch(outer, event(delta=0)))
        self.assertEqual(self.dispatch(None, event(5)), 'break')

    def test_labels_bubble_and_top_edge_is_consumed(self):
        outer = self.widget('Canvas', (0, .5))
        label = self.widget('TLabel', parent=outer)
        self.assertEqual(self.dispatch(label, event(5)), 'break')
        outer.yview_scroll.assert_called_once_with(1, 'units')
        outer.yview_scroll.reset_mock()
        self.assertEqual(self.dispatch(label, event(4)), 'break')
        outer.yview_scroll.assert_not_called()


class RealGuiTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f'GUI display unavailable: {error}')
        self.addCleanup(self.root.destroy)
        self.root.geometry('400x260+0+0')
        self.root.wait_visibility()
        self.root.lift()
        self.root.update()
        self.router = wheel_scroll.install(self.root)

    def wheel(self, widget, sequence, **kwargs):
        y = 40 if isinstance(widget, ttk.Treeview) else 10
        widget.event_generate(sequence, x=10, y=y,
                              rootx=widget.winfo_rootx()+10,
                              rooty=widget.winfo_rooty()+y, **kwargs)
        self.root.update()

    def test_canvas_children_and_late_dialog_text(self):
        canvas = tk.Canvas(self.root, height=180)
        canvas.pack(fill='both', expand=True)
        frame = ttk.Frame(canvas)
        canvas.create_window(0, 0, window=frame, anchor='nw')
        label = ttk.Label(frame, text='Hover here')
        label.pack()
        canvas.configure(scrollregion=(0, 0, 400, 1500))
        self.root.update()
        before = canvas.yview()[0]
        self.wheel(label, '<Button-5>')
        self.assertGreater(canvas.yview()[0], before)
        canvas.yview_moveto(.3)
        self.root.update()
        before = canvas.yview()[0]
        self.wheel(canvas, '<Button-4>')
        self.assertLess(canvas.yview()[0], before)
        win = tk.Toplevel(self.root)
        win.geometry('300x180+450+0')
        text = tk.Text(win)
        text.pack(fill='both', expand=True)
        text.insert('1.0', '\n'.join(str(n) for n in range(100)))
        text.configure(state='disabled')
        self.root.update()
        self.assertIn(self.router.tag, text.bindtags())
        before = text.yview()[0]
        self.wheel(text, '<MouseWheel>', delta=-120)
        self.assertGreater(text.yview()[0], before)

    def test_tree_native_binding_does_not_double_scroll(self):
        tree = ttk.Treeview(self.root, height=4)
        tree.pack()
        for n in range(100):
            tree.insert('', 'end', text=str(n))
        self.root.update()
        tree.yview_scroll(1, 'units')
        expected = tree.yview()
        tree.yview_moveto(0)
        self.wheel(tree, '<Button-5>')
        self.assertEqual(tree.yview(), expected)

    def test_nested_edge_and_value_controls(self):
        canvas = tk.Canvas(self.root, highlightthickness=0)
        canvas.pack(fill='both', expand=True)
        frame = ttk.Frame(canvas)
        canvas.create_window(0, 0, window=frame, anchor='nw')
        text = tk.Text(frame, height=3, width=25)
        text.pack()
        text.insert('1.0', '\n'.join(str(n) for n in range(100)))
        combo = ttk.Combobox(frame, values=('one', 'two'), state='readonly')
        combo.pack()
        scale = ttk.Scale(frame, from_=0, to=100)
        scale.pack()
        canvas.configure(scrollregion=(0, 0, 400, 1500))
        self.root.update()
        self.wheel(text, '<Button-5>')
        self.assertGreater(text.yview()[0], 0)
        self.assertEqual(canvas.yview()[0], 0)
        text.yview_moveto(1)
        self.wheel(text, '<Button-5>')
        self.assertGreater(canvas.yview()[0], 0)
        canvas.yview_moveto(0)
        self.root.update()
        for control in (combo, scale):
            self.wheel(control, '<Button-5>')
            self.assertEqual(canvas.yview()[0], 0)
