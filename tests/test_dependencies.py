"""Dependency checks never install or remove real packages."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import dependency_checker as deps

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dependency_gui", PROJECT / "kernel-manager-gui.py")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


class DependencyCheckerTests(unittest.TestCase):
    required = deps.Dependency("required", "Required", "Application", "tests", True,
                               tools=("needed",), packages=(("apt", ("required-pkg",)),))
    optional = deps.Dependency("optional", "Optional", "Optional", "tests", False,
                               tools=("extra",), packages=(("apt", ("optional-pkg",)),))

    def check(self, tools=(), packages=()):
        return deps.check_dependencies(
            dependencies=(self.required, self.optional), manager="apt",
            which=lambda name: f"/mock/{name}" if name in tools else None,
            package_check=lambda manager, package: package in packages)

    def test_all_required_dependencies_present(self):
        _, results = self.check(("needed", "extra"), ("required-pkg", "optional-pkg"))
        self.assertFalse(any(item.missing for item in results))

    def test_required_dependencies_missing(self):
        _, results = self.check(("extra",), ("optional-pkg",))
        self.assertTrue(results[0].missing)
        self.assertTrue(results[0].dependency.required)
        self.assertEqual(deps.packages_to_install(results, "apt"), ["required-pkg"])

    def test_optional_dependencies_missing(self):
        _, results = self.check(("needed",), ("required-pkg",))
        self.assertFalse(results[0].missing)
        self.assertTrue(results[1].missing)
        self.assertEqual(deps.packages_to_install(results, "apt"), [])

    def test_user_declines_installation(self):
        _, results = self.check()
        app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        app._run_operation = mock.Mock()
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False):
            app._install_missing_dependencies("apt", results)
        app._run_operation.assert_not_called()

    def test_mocked_successful_installation(self):
        calls = []
        deps.install_packages("apt", ["one", "two"], {}, lambda text: None,
                              lambda command, env, log: calls.append(command))
        self.assertEqual(calls[0], ["sudo", "-A", "apt-get", "update"])
        self.assertEqual(calls[1][-3:], ["--", "one", "two"])

    def test_mocked_failed_partial_installation(self):
        calls = []
        def runner(command, env, log):
            calls.append(command)
            if "install" in command:
                raise RuntimeError("mock partial failure")
        with self.assertRaisesRegex(RuntimeError, "partial"):
            deps.install_packages("apt", ["one"], {}, lambda text: None, runner)
        self.assertEqual(len(calls), 2)

    def test_recheck_after_installation(self):
        installed = set()
        check = lambda: deps.check_dependencies(
            dependencies=(self.required,), manager="apt", which=lambda name: f"/mock/{name}" if installed else None,
            package_check=lambda manager, package: package in installed)[1]
        self.assertTrue(check()[0].missing)
        installed.add("required-pkg")
        # Package state is queried afresh; no stale result is cached.
        self.assertFalse(check()[0].missing)

    def test_first_run_and_subsequent_run_state(self):
        self.assertTrue(deps.initial_check_needed({}))
        self.assertFalse(deps.initial_check_needed({"dependency_check_completed": True}))
        self.assertTrue(deps.initial_check_needed({"dependency_check_completed": False}))


class DependencyDialogTests(unittest.TestCase):
    required = DependencyCheckerTests.required
    optional = DependencyCheckerTests.optional
    def setUp(self):
        self.app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        self.app.presets = {}
        self.app._run_operation = mock.Mock()

    def test_package_manager_detection(self):
        self.assertEqual(deps.detect_package_manager(lambda name: "/usr/bin/apt" if name == "apt" else None), "apt")
        self.assertIsNone(deps.detect_package_manager(lambda name: None))

    def test_mixed_and_unsupported_mappings(self):
        manual = deps.Dependency("manual", "Manual tool", "Test", "tests", True, tools=("manual",))
        results = [deps.DependencyResult(self.optional, missing_tools=("extra",)),
                   deps.DependencyResult(manual, missing_tools=("manual",))]
        self.assertEqual(deps.packages_to_install(results, "apt", include_optional=True), ["optional-pkg"])
        self.assertEqual(deps.packages_to_install(results, None, include_optional=True), [])
        self.assertEqual(deps.packages_to_install(results, "unsupported", include_optional=True), [])

    def test_manual_and_first_launch_paths(self):
        for first_launch in (False, True):
            for missing in (False, True):
                with self.subTest(first_launch=first_launch, missing=missing):
                    results = [deps.DependencyResult(self.optional, missing_tools=("extra",) if missing else ())]
                    self.app._read_async = lambda title, check, done: done(("apt", results))
                    self.app._show_dependency_dialog = mock.Mock()
                    with mock.patch.object(gui, "save_presets"), mock.patch.object(gui.messagebox, "showinfo") as info:
                        if first_launch:
                            self.app._run_initial_dependency_check()
                            self.assertTrue(self.app.presets["dependency_check_completed"])
                        else:
                            self.app.on_check_dependencies()
                        self.assertEqual(self.app._show_dependency_dialog.call_count, int(missing))
                        self.assertEqual(info.call_count, int(not missing and not first_launch))
                    self.app._run_operation.assert_not_called()

    def test_confirmation_and_gui_authentication(self):
        results = [deps.DependencyResult(self.optional, missing_tools=("extra",))]
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True) as confirm:
            self.app._install_missing_dependencies("apt", results)
        self.assertIn("optional-pkg", confirm.call_args.args[1])
        worker = self.app._run_operation.call_args.args[1]
        with mock.patch.object(gui, "gui_env", return_value={"SUDO_ASKPASS": "/mock/askpass"}), mock.patch.object(gui, "run_command") as runner:
            worker(mock.Mock())
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args.args[0], ["sudo", "-A", "apt-get", "install", "-y", "--", "optional-pkg"])
        self.assertEqual(runner.call_args.args[1], {"SUDO_ASKPASS": "/mock/askpass"})


class DependencyDialogLayoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = gui.tk.Tk()
        except gui.tk.TclError as error:
            self.skipTest(f"GUI display unavailable: {error}")
        self.addCleanup(self.root.destroy)
        gui.apply_theme(self.root, preferences={})
        self.app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        self.app.root = self.root
        self.app._run_operation = mock.Mock()

    def descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def test_tools_button_runs_manual_check(self):
        self.app.tools_tab = gui.ttk.Frame(self.root)
        self.app._read_async = mock.Mock()
        self.app._build_tools_tab()
        self.app.tools_dependency_btn.invoke()
        self.assertEqual(self.app._read_async.call_args.args[:2], ("Dependency check", gui.check_dependencies))
        with mock.patch.object(gui.messagebox, "showinfo") as info:
            self.app._read_async.call_args.args[2](("apt", []))
        info.assert_called_once()
        self.app._run_operation.assert_not_called()

    def test_button_visibility_and_action(self):
        required = DependencyCheckerTests.required
        optional = DependencyCheckerTests.optional
        manual = deps.Dependency("manual", "Manual", "Test", "tests", True, tools=("manual",))
        cases = [([], "apt", False),
                 ([deps.DependencyResult(required, missing_packages=("required-pkg",))], "apt", True),
                 ([deps.DependencyResult(optional, missing_tools=("extra",))], "apt", True),
                 ([deps.DependencyResult(required, missing_tools=("needed",)), deps.DependencyResult(manual, missing_tools=("manual",))], "apt", True),
                 ([deps.DependencyResult(required, missing_tools=("needed",))], None, False)]
        for results, manager, enabled in cases:
            with self.subTest(manager=manager, results=results):
                self.app._show_dependency_dialog(manager, results)
                win = next(w for w in self.root.winfo_children() if isinstance(w, gui.tk.Toplevel))
                buttons = [w for w in self.descendants(win) if isinstance(w, gui.ttk.Button)]
                install = next(w for w in buttons if w.cget("text") == "Install Missing Dependencies")
                for size in ("820x480", "680x360"):
                    win.geometry(size)
                    self.root.update()
                    for button in buttons:
                        self.assertTrue(button.winfo_ismapped())
                        self.assertGreaterEqual(button.winfo_height(), button.winfo_reqheight())
                        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), win.winfo_rooty() + win.winfo_height())
                self.assertEqual(not install.instate(["disabled"]), enabled)
                with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as confirm:
                    install.invoke()
                    self.assertEqual(confirm.call_count, int(enabled))
                self.app._run_operation.assert_not_called()
                tree = next(w for w in self.descendants(win) if isinstance(w, gui.ttk.Treeview))
                self.assertEqual(len(tree.get_children()), len(results))
                if any(item.dependency == manual for item in results):
                    self.assertEqual(tree.item(tree.get_children()[-1], "values")[-1], "Manual installation")
                win.destroy()


if __name__ == "__main__":
    unittest.main()
