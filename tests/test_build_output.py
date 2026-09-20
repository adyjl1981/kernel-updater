"""Build output lifecycle tests; subprocesses and privileged work are doubles."""
import io
import queue
import threading
import unittest
from unittest import mock

from test_kernel_manager import gui
import test_kernel_manager


class BuildOutputTests(unittest.TestCase):
    drain_ui = test_kernel_manager.GuiStateTests.drain_ui
    # Reuse the safe GUI fixture without inheriting its test cases.
    def setUp(self):
        test_kernel_manager.GuiStateTests.setUp(self)
        self.app._append_log = gui.KernelManagerApp._append_log.__get__(self.app)
        self.window = mock.Mock()
        self.text = mock.Mock()
        self.text.yview.return_value = (0.9, 1.0)
        self.text.index.return_value = '1000.0'
        mock.patch.object(gui.tk, 'Toplevel', return_value=self.window).start()
        mock.patch.object(gui.ttk, 'Frame').start()
        mock.patch.object(gui.ttk, 'Button').start()
        mock.patch.object(gui, 'scrolled_text', return_value=(mock.Mock(), self.text)).start()
        mock.patch.object(gui, 'gui_env', return_value={}).start()
        mock.patch.object(gui, 'save_presets').start()

    def test_capture_open_live_close_reopen_copy(self):
        app = self.app
        large = 'x' * 70000 + '\n'
        app.log_queue.put(large)
        app._poll_log_queue()
        self.assertEqual(app.build_output, [large])
        app.log_queue.put('queued before open\n')
        app.view_build_output()
        gui.tk.Toplevel.assert_called_once_with(app.root, class_=gui.APP_CLASS)
        self.window.resizable.assert_called_once_with(True, True)
        self.text.insert.assert_called_with('1.0', large + 'queued before open\n')
        app.log_queue.put('live\n')
        app._poll_log_queue()
        self.text.insert.assert_called_with('end', 'live\n')
        proc = app.build_proc = mock.Mock()
        app.busy = 'build'
        app.build_cancellable = True
        with mock.patch.object(gui, 'terminate_build_group') as terminate:
            # Exercise the actual window-manager close callback.
            self.window.protocol.call_args.args[1]()
            terminate.assert_not_called()
        self.assertIs(app.build_proc, proc)
        self.assertEqual(app.busy, 'build')
        proc.terminate.assert_not_called()
        proc.wait.assert_not_called()
        app.log_queue.put('closed again\n')
        app.view_build_output()
        expected = large + 'queued before open\nlive\nclosed again\n'
        self.text.insert.assert_called_with('1.0', expected)
        app.log_queue.put('copy pending\n')
        app.copy_build_output()
        app.root.clipboard_clear.assert_called_once()
        app.root.clipboard_append.assert_called_once_with(expected + 'copy pending\n')
        app.view_build_output()
        self.assertEqual(gui.tk.Toplevel.call_count, 2)

    def test_scroll_position(self):
        self.app.view_build_output()
        for view, follows in (((0.9, 1.0), True), ((0.8995, 0.9995), True), ((0.2, 0.3), False)):
            with self.subTest(view=view):
                self.text.see.reset_mock()
                self.text.yview.return_value = view
                self.app._append_log('next\n')
                self.assertEqual(self.text.see.called, follows)
                self.text.configure.assert_called_with(state='disabled')

    def test_completed_failed_and_new_build_reset(self):
        for rc in (0, 1):
            with self.subTest(rc=rc):
                self.app.close_build_output()
                self.app._append_log('previous build sentinel\n')
                proc = mock.Mock(stdout=io.StringIO('x' * 70000 + '\n'))
                proc.wait.return_value = rc
                with mock.patch.object(gui.subprocess, 'Popen', return_value=proc):
                    self.app._start_stream(['bash', str(gui.BUILD_SCRIPT)], gui.SCRIPT_DIR, 'build')
                    self.app.build_thread.join(timeout=3)
                    self.assertFalse(self.app.build_thread.is_alive())
                    self.drain_ui()
                expected = ''.join(self.app.build_output)
                self.assertNotIn('previous build sentinel', expected)
                self.assertIn('x' * 70000, expected)
                self.assertIn(f'[build finished with exit code {rc}]', expected)
                self.assertIsNone(self.app.busy)
                self.app.view_build_output()
                self.text.insert.assert_called_with('1.0', expected)
                self.app.close_build_output()
                self.app.view_build_output()
                self.text.insert.assert_called_with('1.0', expected)
                self.app._reset_build_output()
                self.assertEqual(self.app.build_output, [])
                self.text.delete.assert_called_with('1.0', 'end')

    def test_open_and_close_while_worker_is_running(self):
        entered, release = threading.Event(), threading.Event()
        class Output(io.StringIO):
            def __iter__(self):
                yield 'before open\n'
                entered.set()
                release.wait(3)
                yield 'after close\n'
        proc = mock.Mock(stdout=Output())
        proc.wait.return_value = 0
        with mock.patch.object(gui.subprocess, 'Popen', return_value=proc):
            self.app._start_stream(['bash', str(gui.BUILD_SCRIPT)], gui.SCRIPT_DIR, 'build')
            try:
                self.assertTrue(entered.wait(3))
                self.app.view_build_output()
                self.assertIn('before open\n', self.text.insert.call_args.args[1])
                self.app.close_build_output()
                self.assertTrue(self.app.build_thread.is_alive())
                self.assertIs(self.app.build_proc, proc)
            finally:
                release.set()
                self.app.build_thread.join(3)
            self.drain_ui()
        self.app.view_build_output()
        self.assertIn('after close\n', self.text.insert.call_args.args[1])


class RealBuildOutputTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = gui.tk.Tk(className=gui.APP_CLASS)
        except gui.tk.TclError as exc:
            self.skipTest(f'Tk display unavailable: {exc}')
        self.addCleanup(self.root.destroy)
        gui.apply_theme(self.root, preferences={})
        gui.set_application_icon(self.root)
        self.app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        self.app.root = self.root
        self.app.presets = {}
        self.app.build_output = []
        self.app.output_window = self.app.output_text = None
        self.app.log_queue = queue.Queue()
        self.app.build_tab = gui.ttk.Frame(self.root, padding=12)
        self.app.build_tab.pack(fill='both', expand=True)
        self.app._build_build_tab()

    def test_small_layout_and_real_window_scrolling(self):
        app = self.app
        self.root.geometry('820x500')
        self.root.update()
        for widget in (app.start_btn, app.stop_btn, app.install_btn, app.build_status_label):
            self.assertTrue(widget.winfo_viewable())
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(),
                                 self.root.winfo_rooty() + self.root.winfo_height())
        app._append_log(''.join(f'line {i}\n' for i in range(20000)))
        app.view_build_output()
        self.root.update()
        win, text = app.output_window, app.output_text
        self.assertEqual(win.winfo_class(), gui.APP_CLASS)
        self.assertEqual(text.cget('background'), self.root._ubuntu_colors['surface'])
        self.assertEqual(text.get('1.0', 'end-1c'), ''.join(app.build_output))
        text.see('end')
        self.root.update()
        app._append_log('follow\n')
        self.root.update()
        self.assertAlmostEqual(text.yview()[1], 1.0)
        text.yview_moveto(0.2)
        self.root.update()
        top = text.index('@0,0')
        app._append_log('stay\n')
        self.root.update()
        self.assertEqual(text.index('@0,0'), top)
        app.copy_build_output()
        self.assertEqual(self.root.clipboard_get(), ''.join(app.build_output))
        win.geometry('500x250')
        self.root.update()
        self.assertLessEqual(text.winfo_height(), 250)
        app.close_build_output()
        app.view_build_output()
        self.assertEqual(app.output_text.get('1.0', 'end-1c'), ''.join(app.build_output))
