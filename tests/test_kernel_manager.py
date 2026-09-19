"""Regression checks: python3 -B -m unittest discover -s tests -v.

No real package, bootloader, signing, or kernel installation commands are run.
Shell integration checks use temporary trees, rebound filesystem paths, and an
isolated PATH containing safe utilities and command doubles.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kernel_gui", PROJECT / "kernel-manager-gui.py")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


def result(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout, "")


class HelperTests(unittest.TestCase):
    def test_release_rejects_paths_and_options(self):
        for value in ("../etc", "-rf", "6.1/../../boot", "6.1;reboot", "6.1\n"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                gui.validate_release(value)
        self.assertEqual(gui.validate_release("6.1.2-custom+test"), "6.1.2-custom+test")

    def test_release_lookup_uses_json(self):
        data = io.BytesIO(b'{"latest_stable":{"version":"6.12.4"}}')
        with mock.patch.object(gui.urllib.request, "urlopen", return_value=data), mock.patch.object(gui.subprocess, "check_output") as fallback:
            self.assertEqual(gui.latest_stable_version(), "6.12.4")
            fallback.assert_not_called()

    def test_release_fallback_ignores_rc_and_sorts_numerically(self):
        with mock.patch.object(gui.urllib.request, "urlopen", side_effect=OSError), mock.patch.object(gui.subprocess, "check_output", return_value="a refs/tags/v6.9.9\nb refs/tags/v6.10.1\nc refs/tags/v7.0-rc1\n"):
            self.assertEqual(gui.latest_stable_version(), "6.10.1")

    def test_grub_submenu_paths_and_saved_default(self):
        config = '''load_env
set default="${saved_entry}"
submenu 'Advanced options for Debian' {
 menuentry 'Debian, with Linux 6.12.4-custom' {
  linux /boot/vmlinuz-6.12.4-custom root=/dev/test ro
 }
 menuentry 'Debian, with Linux 6.12.4-custom (recovery mode)' {
 }
}
'''
        grub_cfg = Path("/boot/grub/grub.cfg")
        outputs = [result(), result(), result("saved_entry=Advanced options for Debian>Debian, with Linux 6.12.4-custom\n")]
        with mock.patch.object(gui, "grub_config_path", return_value=grub_cfg), \
             mock.patch.object(gui, "read_grub_config", return_value=config), \
             mock.patch.object(gui.Path, "is_file", return_value=True), \
             mock.patch.object(gui.Path, "is_dir", return_value=True), \
             mock.patch.object(gui.shutil, "which", side_effect=lambda x: x), \
             mock.patch.object(gui, "run_command", side_effect=outputs) as run:
            gui.set_default_boot("6.12.4-custom", {}, lambda text: None, False)
            self.assertEqual(run.call_count, 3)
            write = run.call_args_list[1].args[0]
            self.assertEqual(write, ["sudo", "-A", "grub-set-default", "--boot-directory=/boot", "Advanced options for Debian>Debian, with Linux 6.12.4-custom"])
            self.assertFalse(any("update-grub" in call.args[0] for call in run.call_args_list))

    def test_grub_default_refuses_wrong_image_before_write(self):
        config = '''load_env
set default="${saved_entry}"
menuentry 'Linux 6.1-custom' {
 linux /boot/vmlinuz-6.2-other root=/dev/test ro
}
'''
        with mock.patch.object(gui, "grub_config_path", return_value=Path("/boot/grub/grub.cfg")), \
             mock.patch.object(gui, "read_grub_config", return_value=config), \
             mock.patch.object(gui.Path, "is_file", return_value=True), \
             mock.patch.object(gui.Path, "is_dir", return_value=True), \
             mock.patch.object(gui, "run_command") as run:
            with self.assertRaisesRegex(RuntimeError, "does not unambiguously load"):
                gui.set_default_boot("6.1-custom", {}, lambda text: None, False)
            run.assert_not_called()

    def test_grub_default_refuses_pending_once_override_before_write(self):
        config = '''load_env
set default="${saved_entry}"
menuentry 'Linux 6.1-custom' {
 linux /boot/vmlinuz-6.1-custom root=/dev/test ro
}
'''
        with mock.patch.object(gui, "grub_config_path", return_value=Path("/boot/grub/grub.cfg")), \
             mock.patch.object(gui, "read_grub_config", return_value=config), \
             mock.patch.object(gui.Path, "is_file", return_value=True), \
             mock.patch.object(gui.Path, "is_dir", return_value=True), \
             mock.patch.object(gui.shutil, "which", side_effect=lambda x: x), \
             mock.patch.object(gui, "run_command", return_value=result("next_entry=old choice\n")) as run:
            with self.assertRaisesRegex(RuntimeError, "already pending"):
                gui.set_default_boot("6.1-custom", {}, lambda text: None, False)
            self.assertEqual(run.call_count, 1)

    def test_grub_default_zero_is_safely_enabled_and_selected(self):
        initial = '''load_env
set default=0
submenu 'Advanced options for Ubuntu' {
 menuentry 'Ubuntu, with Linux 7.2.0-custom' {
  linux /boot/vmlinuz-7.2.0-custom root=/dev/test ro
 }
}
'''
        generated = initial.replace("set default=0", 'set default="${saved_entry}"')
        defaults_before = "GRUB_DEFAULT=0\nGRUB_TIMEOUT_STYLE=hidden\nGRUB_TIMEOUT=0\n"
        defaults_after = defaults_before.replace("GRUB_DEFAULT=0", "GRUB_DEFAULT=saved")
        entry = "Advanced options for Ubuntu>Ubuntu, with Linux 7.2.0-custom"

        def command_result(command, *args, **kwargs):
            if command[-1] == "list":
                command_result.reads += 1
                value = "Advanced options for Ubuntu>Ubuntu, with Linux 7.0.0-30-generic" if command_result.reads == 1 else entry
                return result(f"saved_entry={value}\n")
            return result()
        command_result.reads = 0

        with tempfile.TemporaryDirectory() as tmp:
            defaults = Path(tmp) / "grub"
            defaults.write_text(defaults_before)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(gui, "GRUB_DEFAULTS_FILE", defaults))
                stack.enter_context(mock.patch.object(gui, "grub_config_path", return_value=Path("/boot/grub/grub.cfg")))
                stack.enter_context(mock.patch.object(gui, "grub_update_command", return_value=["update-grub"]))
                stack.enter_context(mock.patch.object(gui, "read_grub_config", side_effect=[initial, generated, generated]))
                stack.enter_context(mock.patch.object(gui, "read_system_file", side_effect=[defaults_before, defaults_after]))
                stack.enter_context(mock.patch.object(gui.Path, "is_file", return_value=True))
                stack.enter_context(mock.patch.object(gui.Path, "is_dir", return_value=True))
                stack.enter_context(mock.patch.object(gui.Path, "is_symlink", return_value=False))
                stack.enter_context(mock.patch.object(gui.Path, "exists", return_value=False))
                stack.enter_context(mock.patch.object(gui.shutil, "which", side_effect=lambda name: name))
                run = stack.enter_context(mock.patch.object(gui, "run_command", side_effect=command_result))
                gui.set_default_boot("7.2.0-custom", {}, lambda text: None, False)

            commands = [call.args[0] for call in run.call_args_list]
            self.assertTrue(any(command[2:4] == ["cp", "--preserve=all"] for command in commands))
            self.assertTrue(any(command[2] == "install" and command[-1] == defaults for command in commands))
            self.assertIn(["sudo", "-A", "update-grub"], commands)
            self.assertIn(["sudo", "-A", "grub-set-default", "--boot-directory=/boot", entry], commands)
            self.assertLess(commands.index(["sudo", "-A", "update-grub"]),
                            commands.index(["sudo", "-A", "grub-set-default", "--boot-directory=/boot", entry]))

    def test_saved_default_edit_preserves_unrelated_settings(self):
        before = "# keep this\nGRUB_DEFAULT=0\nGRUB_TIMEOUT_STYLE=hidden\nGRUB_TIMEOUT=0\n"
        self.assertEqual(gui.saved_default_config(before),
                         "# keep this\nGRUB_DEFAULT=saved\nGRUB_TIMEOUT_STYLE=hidden\nGRUB_TIMEOUT=0\n")
        with self.assertRaisesRegex(RuntimeError, "Multiple active"):
            gui.saved_default_config("GRUB_DEFAULT=0\nexport GRUB_DEFAULT=saved\n")

    def test_saved_default_regeneration_failure_restores_backup(self):
        before = "GRUB_DEFAULT=0\nGRUB_TIMEOUT=0\n"
        with tempfile.TemporaryDirectory() as tmp:
            defaults = Path(tmp) / "grub"
            defaults.write_text(before)
            update_calls = 0

            def command_result(command, *args, **kwargs):
                nonlocal update_calls
                if command[-1] == "update-grub":
                    update_calls += 1
                    if update_calls == 1:
                        raise RuntimeError("mock regeneration failure")
                return result()

            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(gui, "GRUB_DEFAULTS_FILE", defaults))
                stack.enter_context(mock.patch.object(gui, "grub_update_command", return_value=["update-grub"]))
                stack.enter_context(mock.patch.object(gui, "read_system_file", return_value=before))
                stack.enter_context(mock.patch.object(gui.Path, "exists", return_value=False))
                run = stack.enter_context(mock.patch.object(gui, "run_command", side_effect=command_result))
                with self.assertRaisesRegex(RuntimeError, "original configuration was restored"):
                    gui.enable_saved_grub_default({}, lambda text: None, Path("/boot/grub/grub.cfg"))

            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(sum(command[-1] == "update-grub" for command in commands), 2)
            self.assertEqual(sum(command[2] == "cp" for command in commands), 2)

    def test_grub_refuses_ambiguous_or_missing_entries(self):
        for config in ("blscfg\n", "menuentry 'Linux 6.1-custom' {\n}\nmenuentry 'Other 6.1-custom' {\n}\n"):
            with mock.patch.object(gui, "read_grub_config", return_value=config), self.assertRaises(RuntimeError):
                gui.grub_entry_title("6.1-custom")

    def test_one_time_boot_requires_supported_config(self):
        config = "menuentry 'Linux 6.1-custom' {\n}\n"
        with mock.patch.object(gui, "read_grub_config", return_value=config), mock.patch.object(gui, "run_command") as run:
            with self.assertRaisesRegex(RuntimeError, "one-time"):
                gui.set_default_boot("6.1-custom", {}, lambda text: None, True)
            run.assert_not_called()

    def test_unknown_and_rpm_ownership_do_not_delete(self):
        for owner in ("rpm", "pacman", "unknown"):
            with mock.patch.object(gui, "running_kernel", return_value="6.2"), mock.patch.object(gui, "package_manager_for_kernel", return_value=owner), mock.patch.object(gui, "run_command") as run:
                with self.assertRaises(RuntimeError):
                    gui.delete_kernel(gui.KernelInfo("6.1", False, False, "?"), {}, lambda text: None)
                run.assert_not_called()

    def test_running_kernel_is_rechecked(self):
        with mock.patch.object(gui, "running_kernel", return_value="6.1"), mock.patch.object(gui, "run_command") as run:
            with self.assertRaises(RuntimeError):
                gui.delete_kernel(gui.KernelInfo("6.1", False, False, "?"), {}, lambda text: None)
            run.assert_not_called()

    def test_apt_only_purges_installed_matching_packages(self):
        data = "linux-image-6.1\tinstalled\nlinux-modules-6.1\tinstalled\nlinux-headers-6.1\tconfig-files\nlinux-image-6.2\tinstalled\n"
        with mock.patch.object(gui, "run_command", return_value=result(data)):
            self.assertEqual(gui.installed_kernel_packages("6.1"), ["linux-image-6.1", "linux-modules-6.1"])

    def test_apt_refuses_unrelated_dependency_removals(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(gui, "running_kernel", return_value="6.2"))
            stack.enter_context(mock.patch.object(gui, "package_manager_for_kernel", return_value="apt"))
            stack.enter_context(mock.patch.object(gui.Path, "is_file", return_value=True))
            stack.enter_context(mock.patch.object(gui, "grub_update_command", return_value=["update-grub"]))
            stack.enter_context(mock.patch.object(gui, "read_grub_config", return_value=""))
            stack.enter_context(mock.patch.object(gui, "installed_kernel_packages", return_value=["linux-image-6.1"]))
            run = stack.enter_context(mock.patch.object(gui, "run_command", return_value=result("Purg linux-image-6.1\nRemv unrelated-app\n")))
            with self.assertRaisesRegex(RuntimeError, "other packages"):
                gui.delete_kernel(gui.KernelInfo("6.1", True, False, "?"), {}, lambda text: None)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "apt-get")

    def test_custom_delete_refreshes_grub_and_never_autoremoves(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(gui, "running_kernel", return_value="6.2"))
            stack.enter_context(mock.patch.object(gui, "package_manager_for_kernel", return_value="custom"))
            stack.enter_context(mock.patch.object(gui.Path, "is_file", return_value=True))
            stack.enter_context(mock.patch.object(gui.Path, "exists", return_value=True))
            stack.enter_context(mock.patch.object(gui.Path, "is_symlink", return_value=False))
            stack.enter_context(mock.patch.object(gui, "grub_update_command", return_value=["update-grub"]))
            stack.enter_context(mock.patch.object(gui, "read_grub_config", return_value=""))
            run = stack.enter_context(mock.patch.object(gui, "run_command"))
            gui.delete_kernel(gui.KernelInfo("6.1", False, False, "?"), {}, lambda text: None)
            self.assertEqual(run.call_args.args[0], ["sudo", "-A", "update-grub"])
            self.assertFalse(any("autoremove" in call.args[0] for call in run.call_args_list))

    def test_package_owner_is_detected_outside_debian(self):
        with mock.patch.object(gui.shutil, "which", side_effect=lambda x: x if x == "rpm" else None), mock.patch.object(gui.subprocess, "run", return_value=result("kernel-core")):
            self.assertEqual(gui.package_manager_for_kernel("6.1"), "rpm")

    def test_presets_invalid_shape_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gui, "CONFIG_FILE", Path(tmp) / "config.json"):
            gui.CONFIG_FILE.write_text("[]")
            self.assertEqual(gui.load_presets(), {})

    def test_missing_askpass_is_reported(self):
        with mock.patch.object(gui, "ASKPASS_SCRIPT", Path("/nonexistent/kernel-test-askpass")), self.assertRaisesRegex(RuntimeError, "password helper"):
            gui.gui_env()

    def test_enrollment_passes_path_as_data(self):
        weird = Path("/tmp/a'b $HOME `echo no`/mok")
        with mock.patch.object(gui, "MOK_DIR", weird), mock.patch.object(gui, "find_terminal_emulator", return_value="/usr/bin/xterm"), mock.patch.object(gui.shutil, "which", return_value="/usr/bin/mokutil"), mock.patch.object(gui.subprocess, "run", return_value=result()) as run:
            gui.enroll_mok_key_in_terminal(lambda text: None)
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[1:4], ["-e", "bash", "-c"])
            self.assertEqual(cmd[-1], str(weird / "MOK.der"))
            self.assertNotIn(str(weird), cmd[4])

    def test_stop_signals_process_group(self):
        proc = mock.Mock(pid=123456)
        with mock.patch.object(gui.os, "killpg") as kill, mock.patch.object(gui.threading, "Event"):
            gui.terminate_build_group(proc)
        self.assertEqual(kill.call_args_list, [mock.call(123456, signal.SIGTERM), mock.call(123456, signal.SIGKILL)])
        proc.wait.assert_called_once()


class GuiStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(gui, "KERNEL_BUILD_DIR", Path(self.tmp.name)).start()
        mock.patch.object(gui.messagebox, "showinfo").start()
        mock.patch.object(gui.messagebox, "showerror").start()
        mock.patch.object(gui, "send_notification").start()
        self.app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
        app = self.app
        app.root = mock.Mock()
        app.busy = app.operation_lock = app.build_proc = app.built_kernel_dir = app.cancel_thread = None
        app.closed = app.build_cancellable = False
        app.log_queue = queue.Queue()
        app.ui_queue = queue.Queue()
        app.reads_pending = set()
        app.start_btn, app.stop_btn, app.install_btn = mock.Mock(), mock.Mock(), mock.Mock()
        app.log_text = mock.Mock()
        app._append_log = mock.Mock()
        app._dependencies_available = mock.Mock(return_value=True)
        app.refresh_installed = mock.Mock()
        app.refresh_maintenance_tab = mock.Mock()
        app.refresh_logs_tab = mock.Mock()
        for name, value in (("jobs_var", "2"), ("toolchain_var", "gcc"), ("lto_var", False), ("debug_var", False), ("force_var", False), ("build_mode_var", "standard")):
            var = mock.Mock()
            var.get.return_value = value
            setattr(app, name, var)

    def drain_ui(self):
        while not self.app.ui_queue.empty():
            self.app.ui_queue.get_nowait()()

    def test_operations_are_exclusive(self):
        self.assertTrue(self.app._begin_operation("install"))
        self.assertFalse(self.app._begin_operation("build"))
        other = (gui.KERNEL_BUILD_DIR / ".operation.lock").open("a")
        with other, self.assertRaises(BlockingIOError):
            gui.fcntl.flock(other, gui.fcntl.LOCK_EX | gui.fcntl.LOCK_NB)
        self.app._finish_operation()
        self.assertIsNone(self.app.busy)

    def test_process_start_failure_restores_install_retry(self):
        tree = gui.KERNEL_BUILD_DIR / "linux-6.1"
        tree.mkdir()
        (tree / ".kernel-manager-complete").write_text("done")
        self.app.built_kernel_dir = str(tree)
        with mock.patch.object(gui.subprocess, "Popen", side_effect=OSError("cannot start")):
            self.app.start_install()
            self.app.build_thread.join(5)
            self.drain_ui()
        self.assertIsNone(self.app.busy)
        self.app.install_btn.configure.assert_called_with(state="normal")

    def test_successful_install_clears_pending_build(self):
        tree = gui.KERNEL_BUILD_DIR / "linux-6.1"
        tree.mkdir()
        (tree / ".kernel-manager-complete").write_text("done")
        self.app.built_kernel_dir = str(tree)
        proc = mock.Mock(stdout=io.StringIO("installed\n"))
        proc.wait.return_value = 0
        with mock.patch.object(gui.subprocess, "Popen", return_value=proc) as popen:
            self.app.start_install()
            self.app.build_thread.join(5)
            self.drain_ui()
        self.assertIsNone(self.app.built_kernel_dir)
        self.assertEqual(popen.call_args.args[0], ["bash", str(gui.INSTALL_SCRIPT)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertTrue(popen.call_args.kwargs["pass_fds"])

    def test_close_does_not_abandon_active_install(self):
        self.app.busy = "install"
        self.app._on_close()
        self.app.root.destroy.assert_not_called()

    def test_invalid_jobs_never_start_process(self):
        self.app.jobs_var.get.return_value = "0"
        with mock.patch.object(gui.subprocess, "Popen") as popen:
            self.app.start_build()
            popen.assert_not_called()

    def test_build_modes_select_only_the_optimised_flag(self):
        self.app._start_stream = mock.Mock()
        self.app.start_build()
        standard = self.app._start_stream.call_args.args[0]
        self.assertNotIn("--hardware-optimised", standard)
        self.app.build_mode_var.get.return_value = "hardware"
        self.app.start_build()
        optimised = self.app._start_stream.call_args.args[0]
        self.assertIn("--hardware-optimised", optimised)

    def test_newer_running_kernel_is_not_an_update(self):
        with mock.patch.object(gui, "latest_stable_version", return_value="6.9.1"), mock.patch.object(gui, "running_kernel", return_value="6.10.0-custom"):
            self.app.update_label = mock.Mock()
            self.app._read_async = lambda key, work, done: done(work())
            self.app.check_for_updates()
        self.assertIn("No newer", self.app.update_label.config.call_args.kwargs["text"])

    def test_unprivileged_phase_is_required_for_stop(self):
        self.app.busy = "build"
        self.app.build_proc = mock.Mock()
        with mock.patch.object(gui, "terminate_build_group") as stop:
            self.app.stop_build()
            stop.assert_not_called()


# One executable command double logs arguments; all filesystem writes stay in
# this test's temporary root, and sudo never invokes a privileged process.
COMMAND_DOUBLE = r'''#!/usr/bin/python3
import json, os, pathlib, shutil, sys
root = pathlib.Path(os.environ["KERNEL_TEST_HOME"])
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
with (root / "commands.jsonl").open("a") as log:
    log.write(json.dumps([name, args]) + "\n")
localversion = next((arg.split("=", 1)[1] for arg in args if arg.startswith("LOCALVERSION=")), "-custom")
version = "9.9.9" + localversion
if name == "sudo":
    while args and args[0].startswith("-"):
        args.pop(0)
    if not args or args[0] in ("apt", "dnf", "pacman"):
        sys.exit(0)
    os.execv(str(root / "bin" / args[0]), args)
elif name in ("apt", "ccache", "installkernel", "depmod"):
    pass
elif name == "uname":
    print(os.environ.get("KERNEL_TEST_ARCH", "x86_64") if "-m" in args else "8.8.8-running")
elif name == "curl":
    if "-o" not in args:
        print('{"latest_stable":{"version":"9.9.9"}}')
    else:
        output = pathlib.Path(args[args.index("-o")+1])
        if ".sign" in str(output): output.write_text("signature")
        elif os.environ.get("KERNEL_TEST_DOWNLOAD_FAIL"):
            output.write_text("partial")
            sys.exit(22)
        else: shutil.copyfile(root / "fixture.tar.xz", output)
elif name == "gpg":
    homedir = pathlib.Path(args[args.index("--homedir") + 1])
    marker = homedir / "trusted.key"
    if "--locate-keys" in args:
        if os.environ.get("KERNEL_TEST_KEY_ACQUIRE_FAIL"): sys.exit(1)
        homedir.mkdir(parents=True, exist_ok=True)
        marker.write_text("key")
    elif "--list-keys" in args:
        if not marker.exists(): sys.exit(2)
        fingerprint = ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                       if os.environ.get("KERNEL_TEST_WRONG_FINGERPRINT")
                       else "647F28654894E3BD457199BE38DBBDC86092693E")
        print("pub:-:4096:1:38DBBDC86092693E:0:0:::::::")
        print("fpr:::::::::" + fingerprint + ":")
    elif "--export" in args:
        if not marker.exists(): sys.exit(2)
        print("trusted public key")
    elif "--import" in args:
        sys.stdin.read()
        homedir.mkdir(parents=True, exist_ok=True)
        marker.write_text("key")
    elif "--verify" in args:
        sys.stdin.buffer.read()
        if os.environ.get("KERNEL_TEST_BAD_SIGNATURE"):
            print("[GNUPG:] BADSIG 38DBBDC86092693E Greg Kroah-Hartman")
            sys.exit(1)
        if not marker.exists():
            print("[GNUPG:] NO_PUBKEY 38DBBDC86092693E")
            sys.exit(2)
        print("[GNUPG:] VALIDSIG 647F28654894E3BD457199BE38DBBDC86092693E 0 0 0 0 0 0 0 0")
elif name == "make":
    if "kernelrelease" in args:
        print(os.environ.get("KERNEL_TEST_RELEASE", version)); sys.exit(0)
    if "image_name" in args:
        print("arch/x86/boot/bzImage"); sys.exit(0)
    if "defconfig" in args:
        pathlib.Path(".config").write_text("CONFIG_MODULES=y\nCONFIG_MODULE_SIG=y\n")
    elif "olddefconfig" in args:
        if os.environ.get("KERNEL_TEST_CONFIG_FAIL"): sys.exit(2)
    elif "modules_install" in args:
        (root / "system/lib/modules" / version).mkdir(parents=True)
    elif "install" in args:
        for prefix, source in (("vmlinuz-", "arch/x86/boot/bzImage"), ("System.map-", "System.map"), ("config-", ".config")):
            shutil.copyfile(source, root / "system/boot" / (prefix + version))
    else:
        if os.environ.get("KERNEL_TEST_BUILD_FAIL"): sys.exit(2)
        for path, data in (("vmlinux", "kernel"), ("System.map", "map"), ("arch/x86/boot/bzImage", "image"), ("include/config/kernel.release", version), ("drivers/test.ko", "module")):
            path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(data)
elif name == "update-initramfs":
    release = args[args.index("-k") + 1]
    (root / "system/boot" / ("initrd.img-" + release)).write_text("initramfs")
elif name == "update-grub":
    images = sorted((root / "system/boot").glob("vmlinuz-9.9.9*"))
    release = images[-1].name.removeprefix("vmlinuz-") if images else version
    (root / "system/boot/grub/grub.cfg").write_text("menuentry 'Linux " + release + "' {\n}\n")
else:
    raise RuntimeError("Unexpected test command: " + name)
'''


class ShellIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kernel-regression-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        bindir = self.root / "bin"
        bindir.mkdir()
        for name in ("bash", "python3", "awk", "cat", "grep", "flock", "mkdir", "chmod", "sha256sum", "cp", "cut", "mv", "mktemp", "rm", "df", "du", "find", "tar", "xz", "timeout", "tee", "nproc", "sort", "tail", "dirname"):
            binary = shutil.which(name)
            if not binary: self.skipTest(f"Required test utility missing: {name}")
            (bindir / name).symlink_to(binary)
        for name in ("sudo", "apt", "ccache", "uname", "curl", "gpg", "make", "installkernel", "update-initramfs", "update-grub", "depmod", "gcc", "g++", "perl", "bison", "flex", "bc", "git", "fakeroot", "rsync", "cpio", "pahole", "zstd", "gawk", "clang", "ld.lld", "llvm-ar"):
            (bindir / name).write_text(COMMAND_DOUBLE)
            (bindir / name).chmod(0o755)
        self.env = dict(os.environ, PATH=str(bindir), KERNEL_TEST_HOME=str(self.root))
        # Rebind only literal paths; the actual script logic is executed.
        for name in ("build-custom-kernel.sh", "install-custom-kernel.sh"):
            code = (PROJECT / name).read_text().replace("$HOME", "$KERNEL_TEST_HOME")
            code = code.replace("/boot/", str(self.root / "system/boot") + "/").replace("/boot ", str(self.root / "system/boot") + " ")
            # Restore source-relative architecture paths after the /boot substitution.
            code = code.replace("arch/x86" + str(self.root / "system/boot"), "arch/x86/boot")
            code = code.replace("/lib/modules", str(self.root / "system/lib/modules"))
            (self.root / name).write_text(code)
        (self.root / "hardware_optimizer.py").write_text(
            "import pathlib,sys\n"
            "action=sys.argv[1]\n"
            "if action == 'scan': print('fixture hardware: ready')\n"
            "elif action == 'lsmod': print('Module Size Used by\\nnvme 0 0')\n"
        )
        (self.root / "system/boot/grub").mkdir(parents=True)
        (self.root / "system/boot/grub/grub.cfg").write_text("menuentry 'old' {\n}\n")
        (self.root / "system/boot/config-8.8.8-running").write_text(
            "CONFIG_MODULES=y\nCONFIG_MODULE_SIG=y\n"
        )
        (self.root / "system/lib/modules").mkdir(parents=True)
        source = self.root / "fixture/linux-9.9.9"
        (source / "scripts").mkdir(parents=True)
        (source / "kernel").mkdir()
        (source / "Makefile").write_text("# fake kernel tree\n")
        config = source / "scripts/config"
        config.write_text('#!/usr/bin/python3\nimport pathlib,sys\np=pathlib.Path(".config")\nif "--enable" in sys.argv: p.write_text(p.read_text()+sys.argv[-1]+"=y\\n")\n')
        config.chmod(0o755)
        with tarfile.open(self.root / "fixture.tar.xz", "w:xz") as archive:
            archive.add(source, arcname="linux-9.9.9")
        self.tree = self.root / "kernel-build/linux-9.9.9"

    def run_script(self, name, *args, cwd=None, **environment):
        return subprocess.run([str(self.root / "bin/bash"), str(self.root / name), *args],
                              cwd=cwd or self.root, env=dict(self.env, **environment),
                              capture_output=True, text=True, timeout=20)

    def commands(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def build(self, *args, **environment):
        proc = self.run_script("build-custom-kernel.sh", "--jobs", "2", *args, **environment)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_bad_arguments_fail_before_any_commands(self):
        for args in (("--jobs",), ("--jobs", "0"), ("--localversion", "../bad"), ("--lto",)):
            proc = self.run_script("build-custom-kernel.sh", *args)
            self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.commands(), [])

    def test_gcc_build_resume_and_metadata(self):
        self.build()
        self.assertTrue((self.tree / ".kernel-manager-complete").is_file())
        args = (self.tree / ".kernel-manager-make-args").read_bytes().split(b"\0")
        self.assertIn(b"LOCALVERSION=-custom", args)
        self.assertIn(b"CC=ccache gcc", args)
        proc = self.build()
        self.assertIn("already complete", proc.stdout)
        self.assertIn("KERNEL_MANAGER_BUILD_DIR=", proc.stdout)
        self.assertFalse(list((self.root / "kernel-build").glob("previous-*")))

    def test_hardware_optimised_default_release_is_used_through_install(self):
        proc = self.build("--hardware-optimised")
        self.assertIn("Kernel 9.9.9-optimized is built", proc.stdout)
        args = (self.tree / ".kernel-manager-make-args").read_bytes().split(b"\0")
        self.assertIn(b"LOCALVERSION=-optimized", args)
        self.assertEqual((self.tree / "include/config/kernel.release").read_text(),
                         "9.9.9-optimized")
        installed = self.run_script("install-custom-kernel.sh", cwd=self.tree)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertTrue((self.root / "system/boot/vmlinuz-9.9.9-optimized").is_file())
        self.assertTrue((self.root / "system/boot/initrd.img-9.9.9-optimized").is_file())
        self.assertIn("9.9.9-optimized", (self.root / "system/boot/grub/grub.cfg").read_text())

    def test_final_network_validation_failure_stops_before_compilation(self):
        optimiser = self.root / "hardware_optimizer.py"
        with optimiser.open("a") as output:
            output.write(
                "if action == 'verify':\n"
                "    count = pathlib.Path('verify-count')\n"
                "    n = int(count.read_text()) + 1 if count.exists() else 1\n"
                "    count.write_text(str(n))\n"
                "    if n == 2: raise SystemExit('detected network adapter lost')\n")
        proc = self.run_script("build-custom-kernel.sh", "--jobs", "2", "--hardware-optimised")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Final boot/network validation failed", proc.stdout + proc.stderr)
        self.assertFalse((self.tree / ".kernel-manager-complete").exists())
        self.assertFalse(any(command[0] == "make" and any(arg.startswith("-j") for arg in command[1])
                             for command in self.commands()))

    def test_explicit_localversion_overrides_optimised_default(self):
        self.build("--hardware-optimised", "--localversion", "-lab")
        args = (self.tree / ".kernel-manager-make-args").read_bytes().split(b"\0")
        self.assertIn(b"LOCALVERSION=-lab", args)
        self.assertEqual((self.tree / "include/config/kernel.release").read_text(), "9.9.9-lab")

    def test_clang_lto_is_used_for_configuration_and_install(self):
        self.build("--clang", "--lto")
        proc = self.run_script("install-custom-kernel.sh", cwd=self.tree)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        make_calls = [args for name, args in self.commands() if name == "make"]
        for args in make_calls:
            self.assertIn("LLVM=1", args)
            self.assertIn("LOCALVERSION=-custom", args)
            self.assertIn("CC=ccache clang", args)
        targets = [args[-1] for args in make_calls]
        self.assertIn("modules_install", targets)
        self.assertIn("install", targets)
        self.assertTrue((self.root / "system/boot/initrd.img-9.9.9-custom").is_file())

    def test_toolchain_change_preserves_previous_source(self):
        self.build()
        (self.tree / "my-local-edit").write_text("keep me")
        self.build("--clang")
        self.assertEqual((self.tree / "my-local-edit").read_text(), "keep me")
        rebuilt = list((self.root / "kernel-build").glob("linux-9.9.9.rebuild.*/.kernel-manager-complete"))
        self.assertEqual(len(rebuilt), 1)
        proc = self.build("--clang")
        self.assertIn("already complete", proc.stdout)
        self.assertIn(str(rebuilt[0].parent), proc.stdout)

    def test_bad_signature_never_extracts_or_moves_existing_tree(self):
        self.tree.mkdir(parents=True)
        (self.tree / "keep").write_text("original")
        keyring = self.root / "kernel-build/.gnupg"
        keyring.mkdir()
        (keyring / "trusted.key").write_text("key")
        proc = self.run_script("build-custom-kernel.sh", KERNEL_TEST_BAD_SIGNATURE="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("bad or invalid signature", proc.stdout + proc.stderr)
        self.assertEqual((self.tree / "keep").read_text(), "original")
        self.assertFalse((self.tree / "Makefile").exists())
        self.assertTrue((self.root / "kernel-build/linux-9.9.9.tar.xz").is_file())

    def test_valid_signature_with_already_trusted_key(self):
        keyring = self.root / "kernel-build/.gnupg"
        keyring.mkdir(parents=True)
        (keyring / "trusted.key").write_text("key")
        proc = self.build()
        self.assertIn("signature is valid", proc.stdout)
        self.assertFalse(any("--locate-keys" in args for name, args in self.commands() if name == "gpg"))

    def test_missing_key_is_reported_and_acquired_then_verification_retried(self):
        proc = self.build()
        self.assertIn("signing key is missing", proc.stdout)
        self.assertIn("Trusted kernel.org release signing key acquired", proc.stdout)
        gpg_calls = [args for name, args in self.commands() if name == "gpg"]
        self.assertTrue(any("--locate-keys" in args and "gregkh@kernel.org" in args for args in gpg_calls))
        self.assertEqual(sum("--verify" in args for args in gpg_calls), 2)

    def test_wrong_wkd_fingerprint_is_rejected_before_verification(self):
        proc = self.run_script("build-custom-kernel.sh", KERNEL_TEST_WRONG_FINGERPRINT="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unexpected fingerprint", proc.stdout)
        self.assertIn("Refusing an untrusted", proc.stdout + proc.stderr)
        gpg_calls = [args for name, args in self.commands() if name == "gpg"]
        self.assertEqual(sum("--verify" in args for args in gpg_calls), 1)
        self.assertFalse(any("--import" in args for args in gpg_calls))
        self.assertTrue((self.root / "kernel-build/linux-9.9.9.tar.xz").is_file())

    def test_key_acquisition_failure_stops_and_retains_archive(self):
        proc = self.run_script("build-custom-kernel.sh", KERNEL_TEST_KEY_ACQUIRE_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("could not be obtained and validated safely", proc.stdout + proc.stderr)
        self.assertTrue((self.root / "kernel-build/linux-9.9.9.tar.xz").is_file())
        self.assertFalse(self.tree.exists())

    def test_cached_archive_is_verified_on_retry_without_redownload(self):
        first = self.run_script("build-custom-kernel.sh", KERNEL_TEST_KEY_ACQUIRE_FAIL="1")
        self.assertNotEqual(first.returncode, 0)
        before = len([1 for name, args in self.commands() if name == "curl" and "linux-9.9.9.tar.xz" in args])
        self.build()
        after = len([1 for name, args in self.commands() if name == "curl" and "linux-9.9.9.tar.xz" in args])
        self.assertEqual(before, after)
        self.assertTrue(self.tree.is_dir())

    def test_partial_download_is_not_cached_as_finished(self):
        proc = self.run_script("build-custom-kernel.sh", KERNEL_TEST_DOWNLOAD_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.root / "kernel-build/linux-9.9.9.tar.xz").exists())
        self.assertTrue((self.root / "kernel-build/linux-9.9.9.tar.xz.part").exists())
        self.build()

    def test_config_and_build_failures_have_no_completion_marker(self):
        for flag in ("KERNEL_TEST_CONFIG_FAIL", "KERNEL_TEST_BUILD_FAIL"):
            proc = self.run_script("build-custom-kernel.sh", **{flag: "1"})
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse((self.tree / ".kernel-manager-complete").exists())

    def test_changed_artifact_blocks_install_before_sudo(self):
        self.build()
        before = len(self.commands())
        (self.tree / "vmlinux").write_text("changed")
        proc = self.run_script("install-custom-kernel.sh", cwd=self.tree)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("changed", proc.stderr)
        self.assertNotIn("sudo", [name for name, args in self.commands()[before:]])

    def test_release_mismatch_and_collision_block_before_sudo(self):
        self.build()
        before = len(self.commands())
        proc = self.run_script("install-custom-kernel.sh", cwd=self.tree, KERNEL_TEST_RELEASE="9.9.9-wrong")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("mismatch", proc.stderr)
        collision = self.root / "system/boot/vmlinuz-9.9.9-custom"
        collision.write_text("existing")
        proc = self.run_script("install-custom-kernel.sh", cwd=self.tree)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(collision.read_text(), "existing")
        self.assertNotIn("sudo", [name for name, args in self.commands()[before:]])

    def test_bls_layout_is_rejected_before_install(self):
        self.build()
        (self.root / "system/boot/grub/grub.cfg").write_text("blscfg\n")
        before = len(self.commands())
        proc = self.run_script("install-custom-kernel.sh", cwd=self.tree)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("BLS", proc.stderr)
        self.assertNotIn("sudo", [name for name, args in self.commands()[before:]])

    def test_inherited_operation_lock_allows_child_but_blocks_other_runs(self):
        directory = self.root / "kernel-build"
        directory.mkdir()
        with (directory / ".operation.lock").open("a") as handle:
            gui.fcntl.flock(handle, gui.fcntl.LOCK_EX | gui.fcntl.LOCK_NB)
            proc = self.run_script("build-custom-kernel.sh")
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(self.commands(), [])
            proc = subprocess.run([str(self.root / "bin/bash"), str(self.root / "build-custom-kernel.sh")],
                                  env=dict(self.env, KERNEL_MANAGER_LOCK_FD=str(handle.fileno())),
                                  pass_fds=(handle.fileno(),), capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_dnf_dependencies_do_not_require_obsolete_groupinstall(self):
        (self.root / "bin/apt").unlink()
        (self.root / "bin/dnf").write_text(COMMAND_DOUBLE)
        (self.root / "bin/dnf").chmod(0o755)
        self.build()
        commands = [args for name, args in self.commands() if name == "sudo"]
        self.assertEqual(commands, [])
        self.assertNotIn("Installing build dependencies", self.run_script("build-custom-kernel.sh").stdout)

    def test_pacman_does_not_refresh_database_without_full_upgrade(self):
        (self.root / "bin/apt").unlink()
        (self.root / "bin/pacman").write_text(COMMAND_DOUBLE)
        (self.root / "bin/pacman").chmod(0o755)
        self.build()
        commands = [args for name, args in self.commands() if name == "sudo"]
        self.assertEqual(commands, [])

    def test_arm_hardware_without_x86_fields_builds_with_portable_flags(self):
        cpu = self.root / "cpuinfo"
        cpu.write_text("processor: 0\nCPU architecture: 8\nHardware: ARM fixture\n")
        script = self.root / "build-custom-kernel.sh"
        script.write_text(script.read_text().replace("/proc/cpuinfo", str(cpu)))
        self.build(KERNEL_TEST_ARCH="aarch64")
        args = (self.tree / ".kernel-manager-make-args").read_bytes().split(b"\0")
        self.assertIn(b"KCFLAGS=-O2", args)
        self.assertFalse(any(b"march=native" in arg for arg in args))

    def test_missing_initramfs_tool_stops_before_install(self):
        self.build()
        (self.root / "bin/update-initramfs").unlink()
        before = len(self.commands())
        proc = self.run_script("install-custom-kernel.sh", cwd=self.tree)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("No supported initramfs", proc.stderr)
        self.assertNotIn("sudo", [name for name, args in self.commands()[before:]])

    def test_low_boot_space_stops_before_install(self):
        self.build()
        df = self.root / "bin/df"
        df.unlink()
        df.write_text("#!/bin/bash\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 100 99 1 99%% /test\\n'\n")
        df.chmod(0o755)
        before = len(self.commands())
        proc = self.run_script("install-custom-kernel.sh", cwd=self.tree)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Insufficient", proc.stderr)
        self.assertNotIn("sudo", [name for name, args in self.commands()[before:]])



class SigningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kernel-sign-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.version = "6.1.2-custom"
        self.source = self.root / "build/linux-6.1.2"
        (self.source / "include/config").mkdir(parents=True)
        (self.source / "include/config/kernel.release").write_text(self.version)
        (self.source / ".config").write_text("CONFIG_MODULE_SIG=y\n")
        self.modules = self.root / "modules" / self.version
        self.modules.mkdir(parents=True)
        self.image = self.root / "boot" / ("vmlinuz-" + self.version)
        self.image.parent.mkdir()
        self.image.write_bytes(b"EFI-IMAGE")
        self.mok = self.root / "mok"
        self.mok.mkdir()
        (self.mok / "MOK.der").write_bytes(b"certificate")
        (self.mok / "MOK.priv").write_bytes(b"key")
        self.calls, self.replacements = [], []
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in (("KERNEL_BUILD_DIR", self.root / "build"), ("MOK_DIR", self.mok)):
            self.stack.enter_context(mock.patch.object(gui, target, value))
        self.stack.enter_context(mock.patch.object(gui, "package_manager_for_kernel", return_value="custom"))
        self.stack.enter_context(mock.patch.object(gui.shutil, "which", side_effect=lambda name: name))
        self.stack.enter_context(mock.patch.object(gui, "build_sign_file_tool", return_value=self.source / "scripts/sign-file"))
        self.stack.enter_context(mock.patch.object(gui, "initramfs_command", return_value=["update-initramfs", "-u", "-k", self.version]))
        self.stack.enter_context(mock.patch.object(gui, "Path", side_effect=self.rebound_path))
        self.stack.enter_context(mock.patch.object(gui, "run_command", side_effect=self.command))
        self.stack.enter_context(mock.patch.object(gui, "replace_signed_file", side_effect=self.replace))
        self.stack.enter_context(mock.patch.object(gui.subprocess, "run", side_effect=self.compress))

    def rebound_path(self, value):
        value = str(value)
        if value.startswith("/boot/"):
            return self.root / "boot" / value.removeprefix("/boot/")
        if value.startswith("/lib/modules/"):
            return self.root / "modules" / value.removeprefix("/lib/modules/")
        return Path(value)

    def command(self, args, *unused, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        if args[0] == "sbsign":
            Path(args[args.index("--output") + 1]).write_bytes(b"SIGNED-EFI")
        elif args[0].endswith("scripts/sign-file"):
            raw = Path(args[-1])
            self.assertEqual(raw.suffix, ".ko")
            raw.write_bytes(raw.read_bytes() + b"-MODULE-SIGNATURE")
        return result()

    def compress(self, args, stdout, check):
        import gzip
        import lzma
        tool, *flags, source = args
        data = Path(source).read_bytes()
        if tool == "gzip":
            data = gzip.decompress(data) if "-dc" in flags else gzip.compress(data)
        elif tool == "xz":
            data = lzma.decompress(data) if "-dc" in flags else lzma.compress(data, check=lzma.CHECK_CRC32)
        elif tool == "zstd":
            data = data.removeprefix(b"ZSTD:") if "-dc" in flags else b"ZSTD:" + data
        else:
            self.fail(f"Unexpected subprocess: {args}")
        stdout.write(data)
        return result()

    def replace(self, source, destination, *unused):
        self.replacements.append(destination)
        destination.write_bytes(source.read_bytes())

    def test_image_uses_sbsign_and_all_module_formats_are_signed(self):
        import gzip
        import lzma
        fixtures = {"plain.ko": b"PLAIN", "gzip.ko.gz": gzip.compress(b"GZIP"),
                    "xz.ko.xz": lzma.compress(b"XZ"), "zstd.ko.zst": b"ZSTD:ZSTD"}
        for name, data in fixtures.items():
            (self.modules / name).write_bytes(data)
        gui.sign_kernel_and_modules(self.version, {}, lambda text: None)
        self.assertEqual(self.image.read_bytes(), b"SIGNED-EFI")
        self.assertTrue((self.modules / "plain.ko").read_bytes().endswith(b"-MODULE-SIGNATURE"))
        self.assertTrue(gzip.decompress((self.modules / "gzip.ko.gz").read_bytes()).endswith(b"-MODULE-SIGNATURE"))
        self.assertTrue(lzma.decompress((self.modules / "xz.ko.xz").read_bytes()).endswith(b"-MODULE-SIGNATURE"))
        self.assertTrue((self.modules / "zstd.ko.zst").read_bytes().endswith(b"-MODULE-SIGNATURE"))
        self.assertEqual(self.replacements[-1], self.image)
        self.assertTrue(any("sbverify" in cmd for cmd in self.calls))
        self.assertTrue(any("update-initramfs" in cmd for cmd in self.calls))
        self.assertEqual(sum(cmd[0].endswith("sign-file") for cmd in self.calls), 4)

    def test_missing_modules_does_not_report_success(self):
        with self.assertRaisesRegex(RuntimeError, "No modules"):
            gui.sign_kernel_and_modules(self.version, {}, lambda text: None)
        self.assertEqual(self.replacements, [])

    def test_bad_efi_signature_leaves_installed_files_untouched(self):
        (self.modules / "test.ko").write_bytes(b"ORIGINAL")
        old_command = self.command
        def command(args, *other, **kwargs):
            if args[0] == "sbverify":
                raise RuntimeError("invalid EFI signature")
            return old_command(args, *other, **kwargs)
        with mock.patch.object(gui, "run_command", side_effect=command), self.assertRaisesRegex(RuntimeError, "EFI signature"):
            gui.sign_kernel_and_modules(self.version, {}, lambda text: None)
        self.assertEqual(self.replacements, [])
        self.assertEqual(self.image.read_bytes(), b"EFI-IMAGE")

    def test_source_release_must_match_installed_release(self):
        (self.source / "include/config/kernel.release").write_text("6.1.2-different")
        with self.assertRaisesRegex(RuntimeError, "matching built source"):
            gui.sign_kernel_and_modules(self.version, {}, lambda text: None)
        self.assertEqual(self.replacements, [])


class DesktopTests(unittest.TestCase):
    def test_launcher_escaping_and_xdg_directory(self):
        validator = shutil.which("desktop-file-validate")
        if not validator:
            self.skipTest("desktop-file-validate unavailable")
        with tempfile.TemporaryDirectory(prefix="kernel-desktop-test-") as tmp:
            root = Path(tmp)
            appdir = root / 'app with space $dollar `tick` "quote" %percent \\slash'
            appdir.mkdir()
            for name in ("install-desktop-entry.sh", "kernel-manager-gui.py", "askpass-gui.py", "kernel-manager-icon.png"):
                shutil.copy2(PROJECT / name, appdir / name)
            bindir = root / "bin"
            bindir.mkdir()
            for name in ("bash", "dirname", "python3", "mkdir", "chmod"):
                (bindir / name).symlink_to(shutil.which(name))
            proc = subprocess.run([str(bindir / "bash"), str(appdir / "install-desktop-entry.sh")],
                                  env=dict(os.environ, PATH=str(bindir), XDG_DATA_HOME=str(root / "data")),
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            desktop = root / "data/applications/kernel-manager-gui.desktop"
            checked = subprocess.run([validator, str(desktop)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("%%percent", desktop.read_text())
            self.assertIn("Exec=python3 ", desktop.read_text())


class AdditionalRegressionTests(unittest.TestCase):
    def test_optimized_release_is_discovered_and_matched_in_grub(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            modules_root = root / "modules"
            (modules_root / "7.2.6-optimized").mkdir(parents=True)
            stack.enter_context(mock.patch.object(gui, "running_kernel", return_value="7.2.0-custom"))
            stack.enter_context(mock.patch.object(gui, "package_manager_for_kernel", return_value="custom"))
            stack.enter_context(mock.patch.object(
                gui, "Path", side_effect=lambda value: modules_root
                if str(value) == "/lib/modules" else Path(value)))
            stack.enter_context(mock.patch.object(gui.subprocess, "check_output", return_value="1M\n"))
            with mock.patch.object(gui, "KERNEL_BUILD_DIR", root):
                kernels = gui.list_installed_kernels()
                self.assertEqual([(k.version, k.manager) for k in kernels],
                                 [("7.2.6-optimized", "custom")])
                config = "menuentry 'Ubuntu, with Linux 7.2.6-optimized' {\n linux /boot/vmlinuz-7.2.6-optimized root=/dev/test ro\n}\n"
                self.assertEqual(gui.grub_entry_title("7.2.6-optimized", config=config),
                                 "Ubuntu, with Linux 7.2.6-optimized")

    def test_gui_completed_status_uses_exact_optimized_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "include/config").mkdir(parents=True)
            (tree / "include/config/kernel.release").write_text("7.2.6-optimized")
            app = gui.KernelManagerApp.__new__(gui.KernelManagerApp)
            app.cancel_thread = None
            app.build_status_label = mock.Mock()
            app._finish_operation = mock.Mock()
            app._stream_finished("build", 0, str(tree))
            app.build_status_label.configure.assert_called_once_with(
                text="Build completed: 7.2.6-optimized")

    def test_source_lookup_finds_rebuild_directories(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gui, "KERNEL_BUILD_DIR", Path(tmp)):
            tree = Path(tmp) / "linux-6.1.rebuild.ABC123"
            (tree / "include/config").mkdir(parents=True)
            (tree / "include/config/kernel.release").write_text("6.1-custom")
            self.assertEqual(gui.find_source_tree("6.1-custom"), tree)

    def test_completed_build_discovery_rejects_changed_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            stack.enter_context(mock.patch.object(gui, "KERNEL_BUILD_DIR", root))
            tree = root / "linux-99.1"
            (tree / "include/config").mkdir(parents=True)
            (tree / ".kernel-manager-complete").write_text("99.1 gcc")
            (tree / ".kernel-manager-make-args").write_bytes(b"CC=gcc\0")
            (tree / "include/config/kernel.release").write_text("99.1-regression-fixture")
            run = stack.enter_context(mock.patch.object(gui.subprocess, "run", return_value=result(code=1)))
            self.assertEqual(gui.completed_build_dirs(), [])
            run.return_value = result()
            self.assertEqual(gui.completed_build_dirs(), [tree])

    def test_corrupt_package_database_is_not_treated_as_custom(self):
        for tool in ("dpkg-query", "rpm", "pacman"):
            with mock.patch.object(gui.shutil, "which", side_effect=lambda name: name if name == tool else None), mock.patch.object(gui.subprocess, "run", return_value=result("database error", 1)):
                self.assertEqual(gui.package_manager_for_kernel("6.1"), "unknown")

    def test_sudo_keeps_only_required_noninteractive_environment(self):
        with mock.patch.object(gui.subprocess, "run", return_value=result()) as run:
            gui.run_command(["sudo", "-A", "apt-get", "purge", "linux-image-6.1"], {"DEBIAN_FRONTEND": "noninteractive"})
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[1], "--preserve-env=DEBIAN_FRONTEND,NEEDRESTART_MODE")
            self.assertNotIn("-E", cmd)

    def test_cancelled_log_window_does_not_block_operations(self):
        window = gui.LogWindow.__new__(gui.LogWindow)
        window.closed = True
        window.queue = queue.Queue(maxsize=1)
        window.queue.put("old output")
        window.append("operation can continue")
        self.assertEqual(window.queue.qsize(), 1)

    def test_gui_constructs_all_tabs_and_callbacks_without_system_operations(self):
        class Widget:
            def __init__(self, *args, **kwargs):
                self.rows = {}
                self.kwargs = kwargs
            def __getattr__(self, name):
                return lambda *args, **kwargs: None
            def get_children(self):
                return list(self.rows)
            def insert(self, *args, **kwargs):
                if "iid" in kwargs:
                    self.rows[kwargs["iid"]] = kwargs
            def delete(self, key, *args):
                self.rows.pop(key, None)
            def count(self, *args):
                return (1,)
        class Variable:
            def __init__(self, value=""):
                self.value = value
            def get(self):
                return self.value
            def set(self, value):
                self.value = value
        with contextlib.ExitStack() as stack:
            for name in ("Notebook", "Frame", "Button", "Label", "Treeview", "LabelFrame", "Radiobutton", "Checkbutton", "Spinbox", "Scrollbar", "Combobox"):
                stack.enter_context(mock.patch.object(gui.ttk, name, Widget))
            stack.enter_context(mock.patch.object(gui.tk, "Text", Widget))
            stack.enter_context(mock.patch.object(gui.tk, "Menu", Widget))
            stack.enter_context(mock.patch.object(gui.tk, "StringVar", Variable))
            stack.enter_context(mock.patch.object(gui.tk, "BooleanVar", Variable))
            stack.enter_context(mock.patch.object(gui, "load_presets", return_value={}))
            stack.enter_context(mock.patch.object(gui, "apply_theme"))
            stack.enter_context(mock.patch.object(gui.KernelManagerApp, "_read_async"))
            app = gui.KernelManagerApp(Widget())
            for name in ("tree", "install_btn", "maint_tree", "logs_tree", "mok_version_combo", "sysinfo_text", "tools_tab", "tools_dependency_btn"):
                self.assertTrue(hasattr(app, name))
            self.assertEqual(app.tools_dependency_btn.kwargs["text"], "Check Dependencies…")
            self.assertIs(app.tools_dependency_btn.kwargs["command"].__self__, app)
            self.assertIs(app.tools_dependency_btn.kwargs["command"].__func__, gui.KernelManagerApp.on_check_dependencies)
            self.assertIsNone(app.busy)
            self.assertFalse(app.build_cancellable)


if __name__ == "__main__":
    unittest.main()
