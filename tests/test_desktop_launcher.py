"""Launcher checks use temporary XDG directories, never real Applications data."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import desktop_launcher as launcher

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("launcher_gui", PROJECT / "kernel-manager-gui.py")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="launcher tests ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.appdir = self.root / 'Kernel space $dollar `tick` "quote" %percent \\slash'
        self.appdir.mkdir()
        for name in ("install-desktop-entry.sh", "kernel-manager-gui.py", "kernel-manager-icon.png", "askpass-gui.py"):
            shutil.copy2(PROJECT / name, self.appdir / name)
        bindir = self.root / "bin"
        bindir.mkdir()
        for name in ("bash", "dirname", "python3", "mkdir", "chmod"):
            (bindir / name).symlink_to(shutil.which(name))
        env = mock.patch.dict(os.environ, XDG_DATA_HOME=str(self.root / "user data"), PATH=str(bindir))
        env.start()
        self.addCleanup(env.stop)

    def test_first_run_has_no_launcher(self):
        self.assertFalse(launcher.launcher_is_current(self.appdir))
        self.assertFalse(launcher.launcher_path().exists())

    def test_install_and_existing_correct_launcher_with_special_paths(self):
        path = launcher.install_launcher(self.appdir)
        self.assertEqual(path, self.root / "user data/applications/kernel-manager-gui.desktop")
        self.assertTrue(launcher.launcher_is_current(self.appdir))
        self.assertIn(f"StartupWMClass={launcher.APP_CLASS}", path.read_text())
        self.assertEqual(path.stem, launcher.APP_CLASS.lower())

    def test_stale_launcher_and_update_after_move(self):
        launcher.install_launcher(self.appdir)
        moved = self.root / "moved Kernel Manager"
        self.appdir.rename(moved)
        self.assertFalse(launcher.launcher_is_current(moved))
        launcher.install_launcher(moved)
        self.assertTrue(launcher.launcher_is_current(moved))

    def test_wrong_identity_icon_hidden_and_malformed_launcher(self):
        path = launcher.install_launcher(self.appdir)
        original = path.read_text()
        for text in (original.replace(f"StartupWMClass={launcher.APP_CLASS}", "StartupWMClass=Tk"),
                     original.replace(f"StartupWMClass={launcher.APP_CLASS}", "StartupWMClass=KernelManager"),
                     original.replace("Icon=", "Icon=/incorrect"),
                     original + "Hidden=true\n", original + "NoDisplay=true\n",
                     "not a desktop entry", original.replace("Type=Application", "Type=Link")):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertFalse(launcher.launcher_is_current(self.appdir))

    def test_missing_installer(self):
        (self.appdir / "install-desktop-entry.sh").unlink()
        with self.assertRaisesRegex(RuntimeError, "installer is missing"):
            launcher.install_launcher(self.appdir)
        self.assertFalse(launcher.launcher_path().exists())

    def test_installer_failure_and_timeout(self):
        for outcome in (subprocess.CompletedProcess([], 1, "", "Permission denied"),
                        subprocess.TimeoutExpired("bash", 30)):
            with self.subTest(outcome=outcome):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with mock.patch.object(launcher.subprocess, "run", **kwargs) as run:
                    with self.assertRaisesRegex(RuntimeError, "Could not install"):
                        launcher.install_launcher(self.appdir)
                    self.assertEqual(run.call_args.args[0], ["bash", str(self.appdir / "install-desktop-entry.sh")])
                    self.assertNotIn("shell", run.call_args.kwargs)

    def test_success_status_without_valid_output_is_failure(self):
        with mock.patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            with self.assertRaisesRegex(RuntimeError, "launcher is invalid"):
                launcher.install_launcher(self.appdir)

    def test_empty_xdg_uses_home(self):
        with mock.patch.dict(os.environ, XDG_DATA_HOME=""), mock.patch.object(launcher.Path, "home", return_value=self.root):
            self.assertEqual(launcher.launcher_path(), self.root / ".local/share/applications/kernel-manager-gui.desktop")


class LauncherGuiTests(unittest.TestCase):
    def setUp(self):
        self.app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        self.app.root = mock.Mock()
        self.app.root.grab_current.return_value = None
        self.app.closed = False
        self.app.reads_pending = set()
        self.app._dispatch = lambda callback: callback()
        self.buttons = {}
        def button(parent, **kwargs):
            widget = mock.Mock()
            self.buttons[kwargs["text"]] = kwargs["command"]
            return widget
        for name in ("Frame", "Label", "LabelFrame", "Radiobutton"):
            patcher = mock.patch.object(gui.ttk, name)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(gui.ttk, "Button", side_effect=button)
        patcher.start()
        self.addCleanup(patcher.stop)

    def offer(self, current=False):
        with mock.patch.object(gui, "launcher_is_current", return_value=current), \
             mock.patch.object(gui.tk, "Toplevel") as window:
            self.app._offer_desktop_launcher()
        return window

    def test_first_run_offers_without_installing(self):
        with mock.patch.object(gui, "install_launcher") as install:
            window = self.offer()
            window.assert_called_once_with(self.app.root, class_=gui.APP_CLASS)
            self.assertEqual(set(self.buttons), {"Install", "Not Now"})
            install.assert_not_called()

    def test_correct_launcher_does_not_prompt(self):
        self.offer(current=True).assert_not_called()

    def test_stale_launcher_offers(self):
        self.offer(current=False).assert_called_once()

    def test_install_choice(self):
        with mock.patch.object(self.app, "on_install_desktop_launcher") as install:
            window = self.offer()
            self.buttons["Install"]()
            window.return_value.destroy.assert_called_once()
            install.assert_called_once_with()

    def test_not_now_can_offer_on_next_launch(self):
        with mock.patch.object(gui, "install_launcher") as install, mock.patch.object(gui, "save_presets") as save:
            window = self.offer()
            self.buttons["Not Now"]()
            window.return_value.destroy.assert_called_once()
            self.offer().assert_called_once()
            install.assert_not_called()
            save.assert_not_called()

    def test_waits_for_existing_startup_dialog(self):
        self.app.root.grab_current.return_value = mock.Mock()
        self.offer().assert_not_called()
        self.app.root.after.assert_called_once_with(700, self.app._offer_desktop_launcher)

    def test_tools_control_uses_same_install_action(self):
        self.app.tools_tab = mock.Mock()
        self.app.refresh_grub_menu = mock.Mock()
        with mock.patch.object(gui.tk, "StringVar"), \
             mock.patch.object(self.app, "on_install_desktop_launcher") as install:
            self.app._build_tools_tab()
            self.buttons["Install / Update Desktop Launcher"]()
            install.assert_called_once_with()

    def test_async_install_reports_success_and_failure(self):
        for error in (None, RuntimeError("Permission denied"), RuntimeError("installer is missing")):
            with self.subTest(error=error), \
                 mock.patch.object(gui, "install_launcher", side_effect=error, return_value=Path("/temporary/launcher")) as install, \
                 mock.patch.object(gui.threading, "Thread") as thread, \
                 mock.patch.object(gui.messagebox, "showinfo") as info, \
                 mock.patch.object(gui.messagebox, "showerror") as failure:
                thread.side_effect = lambda **kwargs: mock.Mock(start=kwargs["target"])
                self.app.on_install_desktop_launcher()
                install.assert_called_once_with(gui.SCRIPT_DIR)
                if error:
                    failure.assert_called_once_with("Desktop Launcher", str(error))
                    info.assert_not_called()
                else:
                    info.assert_called_once()
                    failure.assert_not_called()
                self.assertFalse(self.app.reads_pending)
                self.assertFalse(self.app.closed)
