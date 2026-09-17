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


if __name__ == "__main__":
    unittest.main()
