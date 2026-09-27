"""Preservation is based on working config and target Kconfig, not CPU names."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import hardware_optimizer as hw


class TuningPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.baseline = self.root / "baseline"
        self.baseline.write_text("CONFIG_CPU_FREQ=y\nCONFIG_CPU_IDLE=y\nCONFIG_CPU_FREQ_GOV_SCHEDUTIL=y\n"
                                 "CONFIG_ARM_SCMI_CPUFREQ=m\nCONFIG_ARM_SCMI_PROTOCOL=y\n"
                                 "CONFIG_X86_INTEL_PSTATE=n\nCONFIG_HWMON=y\nCONFIG_SENSORS_FIXTURE=m\n"
                                 "CONFIG_ACPI_PLATFORM_PROFILE=y\nCONFIG_FIXTURE_PROFILE=y\nCONFIG_UNRELATED=m\n")
        self.write("drivers/cpufreq/Kconfig", 'config CPU_FREQ\n bool "frequency"\n'
                   'config CPU_FREQ_GOV_SCHEDUTIL\n bool "schedutil"\n depends on CPU_FREQ\n'
                   'config ARM_SCMI_CPUFREQ\n tristate "SCMI"\n depends on CPU_FREQ && ARM_SCMI_PROTOCOL\n'
                   'config X86_INTEL_PSTATE\n bool "Intel"\n')
        self.write("drivers/hwmon/Kconfig", 'config HWMON\n bool "monitor"\n'
                   'config SENSORS_FIXTURE\n tristate "sensor"\n depends on HWMON\n')
        self.write("drivers/platform/Kconfig", 'config ACPI_PLATFORM_PROFILE\n tristate\n'
                   'config FIXTURE_PROFILE\n bool "profile"\n select ACPI_PLATFORM_PROFILE\n')
        self.write("Kconfig", 'config ARM_SCMI_PROTOCOL\n bool "protocol"\nconfig CPU_IDLE\n bool "idle"\n'
                   'config UNRELATED\n tristate "unrelated"\n')
        self.report = hw.HardwareReport("arm64", "Fixture", root_filesystem="ext4",
                                        tuning={"providers": ["cpufreq"], "modules": []})

    def write(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)

    def requirements(self):
        return hw.tuning_requirements(self.report, self.baseline, self.root)

    def test_preserves_only_working_detected_family_and_dependencies(self):
        group = self.requirements()["CPU frequency"]
        self.assertEqual(group["CPU_FREQ"], "y")
        self.assertEqual(group["ARM_SCMI_CPUFREQ"], "m")
        self.assertEqual(group["ARM_SCMI_PROTOCOL"], "y")
        self.assertNotIn("X86_INTEL_PSTATE", group)
        self.assertNotIn("SENSORS_FIXTURE", group)
        self.assertNotIn("UNRELATED", group)

    def test_target_types_are_authoritative(self):
        path = self.root / "drivers/cpufreq/Kconfig"
        path.write_text(path.read_text().replace('tristate "SCMI"', 'bool "SCMI"'))
        self.assertEqual(self.requirements()["CPU frequency"]["ARM_SCMI_CPUFREQ"], "y")

    def test_no_interfaces_no_requirements(self):
        self.report.tuning = {}
        self.assertEqual(hw.tuning_requirements(self.report, None, None), {})

    def test_missing_baseline_or_source_refused(self):
        with self.assertRaisesRegex(RuntimeError, "baseline and target"):
            hw.tuning_requirements(self.report, None, self.root)

    def test_missing_working_core_is_not_silently_dropped(self):
        path = self.root / "drivers/cpufreq/Kconfig"
        path.write_text(path.read_text().replace('config CPU_FREQ\n bool "frequency"\n', ''))
        with self.assertRaisesRegex(RuntimeError, "CONFIG_CPU_FREQ"):
            self.requirements()

    def test_missing_active_driver_is_not_silently_dropped(self):
        self.report.tuning["drivers"] = ["scmi-cpufreq"]
        path = self.root / "drivers/cpufreq/Kconfig"
        path.write_text(path.read_text().replace("config ARM_SCMI_CPUFREQ", "config RENAMED_DRIVER"))
        with self.assertRaisesRegex(RuntimeError, "CONFIG_ARM_SCMI_CPUFREQ"):
            self.requirements()

    def test_idle_driver_outside_cpuidle_subtree_preserved(self):
        with self.baseline.open("a") as stream:
            stream.write("CONFIG_INTEL_IDLE=y\n")
        self.write("drivers/idle/Kconfig", 'config INTEL_IDLE\n bool "Intel idle"\n depends on CPU_IDLE\n')
        self.report.tuning = {"providers": ["cpuidle"], "drivers": ["intel_idle"]}
        group = self.requirements()["Interface driver intel_idle"]
        self.assertEqual(group, {"INTEL_IDLE": "y", "CPU_IDLE": "y"})

    def test_profile_builtin_handler_preserved_without_module(self):
        self.report.tuning = {"providers": ["platform-profile"]}
        group = self.requirements()["Platform profiles"]
        self.assertEqual(group["FIXTURE_PROFILE"], "y")
        self.assertNotIn("UNRELATED", group)

    def test_post_kconfig_verification_rejects_lost_interfaces(self):
        config = self.root / ".config"
        config.write_text("CONFIG_CPU_FREQ=y\n")
        with self.assertRaisesRegex(RuntimeError, "Performance interfaces.*ARM_SCMI_CPUFREQ"):
            hw.verify_capability_groups(config, "Performance interfaces", self.requirements())

    def test_update_config_integrates_requirements(self):
        config = self.root / ".config"
        config.write_text("")
        with mock.patch.object(hw, "container_requirements", return_value={}), \
             mock.patch.object(hw, "network_requirements", return_value={}), \
             mock.patch.object(hw, "hardware_requirements", return_value={}), \
             mock.patch.object(hw, "compatibility_requirements", return_value={}), \
             mock.patch.object(hw, "software_requirements", return_value={}):
            hw.update_config(config, self.report, self.baseline, self.root)
        self.assertIn("CONFIG_ARM_SCMI_CPUFREQ=m", config.read_text())
        hw.verify_capability_groups(config, "Performance interfaces", self.requirements())

    def test_hardware_scan_shares_fixture_root_and_reports_evidence(self):
        self.write("sys/devices/system/cpu/cpufreq/policy0/scaling_driver", "fixture\n")
        scanner = hw.Scanner(self.root / "sys", self.root / "proc", self.root / "boot",
                             runner=lambda args: "", etc_root=self.root / "etc")
        with mock.patch.object(hw.shutil, "which", return_value=None):
            report = scanner.scan()
        self.assertEqual(report.tuning["providers"], ["cpufreq"])
        self.assertIn("Performance & Tuning interfaces (read-only): cpufreq", hw.render_report(report))

    def test_old_saved_reports_remain_compatible(self):
        report = hw.HardwareReport(**{"architecture": "x86_64", "cpu": "old saved report"})
        self.assertEqual(report.tuning, {})
        self.assertIn("none detected", hw.render_report(report))
