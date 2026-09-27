"""All sysfs writes in these tests create temporary fixtures, never host files."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tuning import TuningManager, ReadStatus, Risk
from tuning.fs import Sysfs, Reading
from tuning.providers import CPU, CpuFreqProvider, HwmonProvider
from tuning import gui


class Fixtures(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fs = Sysfs(self.root)

    def put(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n")
        return path

    def fixture(self, name):
        data = json.loads((Path(__file__).parent / "fixtures/tuning" / (name + ".json")).read_text())
        for path, value in data.items():
            self.put(path, value)

    def discover(self, providers=None):
        return TuningManager(_fs=self.fs, _providers=providers).discover()

    def cap(self, result, semantic):
        return next(c for c in result.capabilities if c.semantic == semantic)


class DiscoveryTests(Fixtures):
    def test_modern_intel_composes_and_preserves_inversion(self):
        self.fixture("intel")
        result = self.discover()
        self.assertEqual(result.providers, ["cpufreq", "intel-pstate"])
        turbo = self.cap(result, "boost-permitted")
        self.assertIs(turbo.current, True)
        self.assertEqual(turbo.sources[0].raw, "0")
        self.assertIn("inverted", turbo.sources[0].encoding)
        maximum = self.cap(result, "scaling_max_freq")
        self.assertEqual(maximum.requested, 4200000)
        self.assertIsNone(maximum.observed)
        self.assertEqual((maximum.minimum, maximum.maximum), (400000, 4800000))
        self.assertIsNone(maximum.step)
        self.assertIsNone(maximum.documented_default)
        self.assertTrue(self.cap(result, "max_perf_pct").relationships)
        self.assertIn("not a fixed", self.cap(result, "status").description)

    def test_intel_passive_semantics(self):
        self.put(CPU + "/intel_pstate/status", "passive")
        self.assertIn("generic", self.cap(self.discover(), "status").description)

    def test_older_intel_has_discrete_table_and_distinct_epb(self):
        self.fixture("older_intel")
        result = self.discover()
        self.assertEqual(self.cap(result, "scaling_available_frequencies").current, (800000, 1600000, 2400000))
        self.assertEqual(self.cap(result, "energy_perf_bias").maximum, 15)
        self.assertNotIn("energy_performance_preference", [c.semantic for c in result.capabilities])

    def test_amd_modes_no_overclocking_inference(self):
        self.fixture("amd")
        for mode in ("active", "passive", "guided"):
            self.put(CPU + "/amd_pstate/status", mode)
            result = self.discover()
            self.assertIn("cpufreq", result.providers)
            self.assertIn("amd-pstate", result.providers)
            self.assertEqual(self.cap(result, "status").current, mode)
            self.assertIsNone(self.cap(result, "amd_pstate_highest_perf").units)
            self.assertFalse(any(c.risk == Risk.OVERCLOCKING for c in result.capabilities))

    def test_generic_observation_and_absent_optional_files(self):
        self.fixture("generic")
        result = self.discover()
        self.assertEqual(self.cap(result, "cpuinfo_cur_freq").observed, 1500000)
        self.assertNotIn("scaling_available_frequencies", [c.semantic for c in result.capabilities])
        self.assertFalse(result.diagnostics)

    def test_arm_heterogeneous_and_shared_policy(self):
        self.fixture("arm")
        result = self.discover()
        policies = [c for c in result.capabilities if c.semantic == "affected_cpus"]
        self.assertEqual(len(policies), 2)
        self.assertEqual(policies[0].current, (0, 1, 2, 3))
        self.assertEqual({c.current for c in result.capabilities if c.semantic == "cpuinfo_max_freq"}, {1800000, 2800000})
        self.assertEqual(result.providers, ["cpufreq"])

    def test_vm_empty_success(self):
        self.fixture("vm")
        result = self.discover()
        self.assertEqual(result.capabilities, ())
        self.assertEqual(result.diagnostics, ())
        self.assertEqual(result.adjustable_count, 0)

    def test_platform_handlers_not_collapsed_into_aggregate(self):
        self.fixture("platform")
        result = self.discover()
        profiles = [c for c in result.capabilities if c.potentially_writable]
        self.assertEqual(len(profiles), 3)
        aggregate = self.cap(result, "platform_profile")
        self.assertEqual(aggregate.current, "custom")
        self.assertNotIn("custom", aggregate.choices)
        self.assertEqual(len(aggregate.relationships), 2)

    def test_powercap_names_hierarchy_units_and_unknown_bounds(self):
        self.fixture("monitoring")
        result = self.discover()
        limit = self.cap(result, "constraint_7_power_limit_uw")
        self.assertIn("long_term", limit.name)
        self.assertEqual(limit.units, "µW")
        self.assertIsNone(limit.minimum)
        self.assertEqual(limit.maximum, 28000000)
        child = next(c for c in result.capabilities if c.semantic == "energy_uj" and c.current == 30000000)
        self.assertTrue(child.relationships[0].target.endswith("fixture:0"))

    def test_hwmon_thermal_identity_limits_and_alarm(self):
        self.fixture("monitoring")
        result = self.discover()
        sensor = self.cap(result, "temp1_input")
        self.assertEqual(sensor.metadata["driver"], "fixture-sensor")
        self.assertEqual(sensor.metadata["label"], "Package")
        self.assertEqual(sensor.observed, 47000)
        self.assertIsNone(sensor.maximum)
        self.assertIs(self.cap(result, "temp1_crit_alarm").current, False)
        self.assertEqual(self.cap(result, "trip_point_0_temp").metadata["trip_type"], "critical")
        self.assertFalse(self.cap(result, "temp1_crit").potentially_writable)

    def test_idle_is_informational_and_default_has_provenance(self):
        self.fixture("monitoring")
        cap = self.cap(self.discover(), "disable")
        self.assertFalse(cap.potentially_writable)
        self.assertIs(cap.documented_default, False)
        self.assertTrue(cap.default_provenance.endswith("default_status"))

    def test_malformed_sensor_does_not_remove_others(self):
        self.fixture("monitoring")
        self.put("class/hwmon/hwmon0/temp1_input", "broken")
        result = self.discover()
        self.assertEqual(self.cap(result, "temp1_input").status, ReadStatus.MALFORMED)
        self.assertIsNone(self.cap(result, "temp1_input").current)
        self.assertEqual(self.cap(result, "power1_input").current, 9000000)

    def test_permission_failure_and_disappearing_sensor(self):
        self.fixture("monitoring")
        original = self.fs.read
        def read(path):
            if path.endswith("temp1_input"):
                return Reading(None, ReadStatus.DENIED, "test denied")
            if path.endswith("temp1_crit"):
                return Reading(None, ReadStatus.MISSING, "device disappeared")
            return original(path)
        with mock.patch.object(self.fs, "read", side_effect=read):
            result = self.discover()
        self.assertEqual(self.cap(result, "temp1_input").status, ReadStatus.DENIED)
        self.assertEqual(self.cap(result, "temp1_crit").status, ReadStatus.MISSING)
        self.assertIn("thermal", result.providers)

    def test_reader_real_exceptions_and_large_invalid_data(self):
        self.put("value", "1")
        for error, status in ((PermissionError(), ReadStatus.DENIED), (FileNotFoundError(), ReadStatus.MISSING),
                              (OSError("device lost"), ReadStatus.ERROR)):
            with mock.patch.object(Path, "open", side_effect=error):
                self.assertEqual(self.fs.read("value").status, status)
        self.put("value", "x" * 65537)
        self.assertEqual(self.fs.read("value").status, ReadStatus.MALFORMED)
        (self.root / "value").write_bytes(b"\xff")
        self.assertEqual(self.fs.read("value").status, ReadStatus.MALFORMED)

    def test_partial_provider_failure_retains_yielded_and_other_providers(self):
        self.fixture("generic")
        self.fixture("monitoring")
        class Broken(CpuFreqProvider):
            def discover(self, *args):
                yield next(super().discover(*args))
                raise OSError("device removed")
        result = self.discover([Broken(), HwmonProvider()])
        self.assertIn("cpufreq", result.providers)
        self.assertIn("hwmon", result.providers)
        self.assertIn("partial discovery", result.diagnostics[0])

    def test_reconciliation_aliases_preserve_provenance_not_distinct_controls(self):
        self.fixture("generic")
        cap = self.cap(self.discover(), "scaling_governor")
        alias = replace(cap, provider="alias", sources=(replace(cap.sources[0], provider="alias", path="/sys/alias"),))
        separate = replace(cap, id="distinct", domain="another-domain", sources=(replace(cap.sources[0], canonical_path="/sys/another"),))
        result = TuningManager.reconcile([cap, alias, separate])
        self.assertEqual(len(result), 2)
        self.assertEqual(len(next(c for c in result if c.id == cap.id).sources), 2)

    def test_powercap_class_alias_not_duplicated(self):
        self.fixture("monitoring")
        (self.root / "class/powercap/alias:0").symlink_to(self.root / "class/powercap/fixture:0")
        result = self.discover()
        self.assertEqual(len([c for c in result.capabilities if c.semantic == "constraint_7_power_limit_uw"]), 1)
        self.assertEqual(len(self.cap(result, "constraint_7_power_limit_uw").sources), 2)

    def test_ambiguous_sensor_identity_does_not_duplicate_ids(self):
        self.fixture("generic")
        cap = self.cap(self.discover(), "scaling_governor")
        distinct = replace(cap, sources=(replace(cap.sources[0], canonical_path="/sys/another-bank"),))
        result = TuningManager.reconcile([cap, distinct])
        self.assertEqual(len({c.id for c in result}), 2)

    def test_bound_module_is_read_only_evidence(self):
        self.put("module/example/.fixture", "exists")
        self.put("devices/example/hwmon/hwmon7/name", "example")
        self.put("devices/example/hwmon/hwmon7/temp1_input", "42000")
        (self.root / "devices/example/driver").mkdir()
        (self.root / "devices/example/driver/module").symlink_to(self.root / "module/example")
        (self.root / "class/hwmon").mkdir(parents=True)
        (self.root / "class/hwmon/hwmon7").symlink_to(self.root / "devices/example/hwmon/hwmon7")
        result = self.discover()
        self.assertEqual(result.evidence()["modules"], ["example"])
        self.assertNotIn("hwmon7", result.capabilities[0].domain)

    def test_unknown_hwmon_attribute_is_not_given_invented_semantics(self):
        self.fixture("monitoring")
        self.put("class/hwmon/hwmon0/freq1_cap", "1000")
        self.assertNotIn("freq1_cap", [c.semantic for c in self.discover().capabilities])

    def test_bad_numeric_bounds_and_boolean_remain_unknown(self):
        self.fixture("intel")
        self.put(CPU + "/intel_pstate/no_turbo", "2")
        self.put(CPU + "/cpufreq/policy0/cpuinfo_max_freq", "-1")
        result = self.discover()
        self.assertIsNone(self.cap(result, "boost-permitted").current)
        self.assertEqual(self.cap(result, "boost-permitted").status, ReadStatus.MALFORMED)
        self.assertIsNone(self.cap(result, "scaling_max_freq").maximum)

    def test_ids_stable_across_refresh_and_generation_changes(self):
        self.fixture("intel")
        manager = TuningManager(_fs=self.fs)
        first, second = manager.discover(), manager.discover()
        self.assertEqual([c.id for c in first.capabilities], [c.id for c in second.capabilities])
        self.assertEqual(second.generation, first.generation + 1)
        self.assertEqual(len({c.id for c in first.capabilities}), len(first.capabilities))

    def test_contradictory_bounds_are_not_usable_range(self):
        self.fixture("intel")
        self.put(CPU + "/cpufreq/policy0/cpuinfo_min_freq", "9999999")
        cap = self.cap(self.discover(), "scaling_max_freq")
        self.assertIsNone(cap.minimum)
        self.assertIsNone(cap.maximum)
        self.assertIn("Contradictory", cap.blocked_reason)

    def test_escape_symlink_cannot_read_host(self):
        (self.root / "escape").symlink_to("/sys")
        self.assertEqual(self.fs.read("escape/kernel/uevent_seqnum").status, ReadStatus.MALFORMED)

    def test_discovery_opens_only_fixture_files_read_only_and_no_processes(self):
        self.fixture("intel")
        self.fixture("monitoring")
        original = Path.open
        def checked(path, mode="r", *args, **kwargs):
            self.assertEqual(mode, "r")
            self.assertTrue(path.is_relative_to(self.root))
            return original(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", checked), mock.patch("subprocess.run", side_effect=AssertionError("no commands")):
            result = self.discover()
        self.assertFalse(result.diagnostics)
        self.assertTrue(result.capabilities)


class GuiTests(Fixtures):
    def test_dynamic_sections_and_details(self):
        self.fixture("intel")
        result = self.discover()
        self.assertEqual(set(gui.sections(result)), {"CPU", "Power & Boost", "Advanced"})
        self.assertIn("read-only", gui.details(result.capabilities[0]))
        self.assertNotIn("GPU", gui.sections(result))

    def test_construction_empty_and_populated_without_display(self):
        for populated in (False, True):
            if populated:
                self.fixture("intel")
            result = self.discover()
            with mock.patch.object(gui.tk, "Toplevel"), mock.patch.object(gui.tk, "Text"), \
                 mock.patch.object(gui, "ttk") as ttk, mock.patch.object(gui.TuningWindow, "refresh"):
                view = gui.TuningWindow(mock.Mock(), _manager=mock.Mock())
                view.notebook.winfo_children.return_value = []
                view.render(result)
                tabs = [call.kwargs["text"] for call in view.notebook.add.call_args_list]
                self.assertEqual(tabs, ["Overview", *gui.sections(result)])
                labels = [call.kwargs.get("text") for call in ttk.Button.call_args_list]
                self.assertEqual(labels, ["Refresh"])
                texts = " ".join(str(call) for call in gui.tk.Text.return_value.insert.call_args_list)
                self.assertEqual(gui.EMPTY in texts, not populated)

    def test_async_refresh_and_close_do_not_poll_sensors(self):
        with mock.patch.object(gui.tk, "Toplevel"), mock.patch.object(gui, "ttk"), \
             mock.patch.object(gui.threading, "Thread") as thread:
            manager = mock.Mock()
            manager.discover.return_value = self.discover()
            view = gui.TuningWindow(mock.Mock(), _manager=manager)
            manager.discover.assert_not_called()
            view.refresh()
            self.assertEqual(thread.call_count, 1)
            thread.call_args.kwargs["target"]()
            with mock.patch.object(view, "render") as render:
                view._collect()
                render.assert_called_once()
            manager.discover.assert_called_once()
            self.assertIsNone(view.after_id)
            view._destroyed(mock.Mock(widget=view.window))
            view.refresh()
            self.assertEqual(thread.call_count, 1)

    def test_real_tk_layout_if_display_available(self):
        try:
            root = gui.tk.Tk()
        except gui.tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.geometry("820x600")
        for populated in (False, True):
            if populated:
                self.fixture("monitoring")
            with mock.patch.object(gui.TuningWindow, "refresh"):
                view = gui.TuningWindow(root, _manager=mock.Mock())
            view.render(self.discover())
            view.window.geometry("700x480")
            root.update_idletasks()
            self.assertLessEqual(view.refresh_button.winfo_rootx() + view.refresh_button.winfo_width(),
                                 view.window.winfo_rootx() + view.window.winfo_width())
            self.assertEqual(len(view.notebook.tabs()), 1 + len(gui.sections(self.discover())))
            view.window.destroy()
