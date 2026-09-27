# Performance & Tuning — Phase 1

This release discovers interfaces and displays readings only. It has no setter,
privileged helper, persistence, automatic tuning, stress test or overclocking
implementation. Opening the window does not request authentication. Missing
interfaces, including a completely empty discovery, are supported outcomes.

## Architecture

`tuning/model.py` defines typed capability, provenance, relationship and snapshot
records. `fs.py` provides bounded, read-only sysfs access. `providers.py` contains
eight composable providers: CPUFreq, Intel P-state, AMD P-state, platform profiles,
powercap, hwmon, thermal and CPU idle. `manager.py` orchestrates discovery and
reconciliation. `gui.py` owns the dedicated Tools window, not backend logic in the
main GUI module. All providers run; CPU vendor/model names do not select a backend.

Production `TuningManager()` reads `/sys`. `_fs` and `_providers` are internal
dependency injection points for tests and the existing scanner's fixture roots.
There is no command-line, GUI or privileged path-selection interface. Discovery
does not execute external commands. Files are opened in read mode only; bounded
reads reject oversized, invalid UTF-8 and malformed numeric attributes. Symlinks
must remain within the injected sysfs root. Bound driver module links are read
only to report Kconfig preservation evidence, never to load modules.

Providers yield results incrementally. A malformed or inaccessible attribute
retains its read status and unknown value; an absent optional attribute is omitted.
Enumerated sensor/constraint files disappearing during a read retain missing
status. An unexpected provider failure is reported without dropping prior yields
or preventing remaining providers from running. A refresh is a new snapshot,
not an atomic reading of all hardware. Settings can change externally during it.

## Data contract

Capabilities carry an opaque ID, provider, device/policy domain, generation,
timestamp, name, description, category, typed value, requested/observed values,
units, optional choices and bounds, optional step, status, potential write
semantics, authorization note, unavailability reason, lifetime, documented default
and provenance, risk, relationships, source paths/raw values/encoding, and driver,
CPU membership or sensor-label metadata. Source paths are informational only.

IDs are stable for an unchanged discovered domain/attribute, not a promise of
device identity across reboot, driver rebind or replacement. CPU policy domains
follow actual policies, not independently fabricated per-thread controls. Sensor
domains prefer resolved device paths and driver names over allocation-only hwmon
indices. Ambiguous same-named banks are disambiguated by canonical source.
Cross-boot identity must be separately designed before persistence.

`potentially_writable` means the documented interface supports adjustment; it
does not claim effective permissions, firmware acceptance or implemented write
support. No write probes occur. CPU idle and thermal policy remain informational
in this phase, even where a kernel attribute has write semantics. Mode information
is displayed without offering mode changes. Unknown lifetime/default/step stays
null. Idle `default_status` is the only currently imported explicit default;
current values are never renamed defaults. No values are captured for restoration.

Frequency requests and hardware observations remain distinct. `scaling_cur_freq`
is labelled a driver reading, not guaranteed actual clock telemetry. Raw EPP
numbers and abstract AMD performance numbers are not translated into profiles or
MHz. Intel `no_turbo` is inverted for “Turbo permitted”, retaining raw value and
encoding. Driver bounds are not certified safe limits. Critical/emergency thermal
thresholds are not recommended targets. Raw units are displayed explicitly.

## Reconciliation rules

1. Merge only identical canonical attributes with identical semantic and encoding
   keys. Keep all source/provider provenance and relationships; prefer a valid
   read over a failed one. Never merge merely because two values match.
2. CPU aliases are avoided by enumerating policy directories. Powercap class and
   nested-domain aliases are reconciled by canonical source; traversal is bounded
   by visited canonical domains.
3. Keep global/policy boost controls distinct but relate their scopes. Keep Intel
   percentage limits distinct from policy frequency limits and relate them.
4. Keep platform-profile aggregate and handler controls distinct. The aggregate
   records handler relationships; `custom` is not invented as a selectable choice.
5. Do not deduplicate hwmon and thermal measurements based on similar names or
   temperatures. Their physical equivalence is generally not established.
6. Contradictory lower/upper bounds produce no usable normalized range and an
   explanatory note. Raw source capabilities remain visible.

The generic CPUFreq provider owns governor/EPP/boost policy attributes. Vendor
providers add only their separate attributes. This avoids competing ownership.

## GUI and refresh

Tools → Open Performance & Tuning opens an 820×600 window (minimum 700×480),
avoiding another top-level tab in the existing 820×600-minimum main application.
Overview is always present. CPU, Power & Boost, Thermal and Advanced appear only
when populated. No GPU section or Apply action exists. Domain trees and scrollable
details prevent a wall of per-CPU controls. Risk labels use text, not colour alone.

Discovery runs on a worker thread. Only the Tk thread constructs widgets; a queue
is checked while a refresh is pending. Closing the window cancels its callback.
There is no repeating sensor poll. Refresh replaces the snapshot. The empty-control
message also permits monitoring-only results.

## Hardware Optimised integration

HardwareReport gains an optional `tuning` evidence dictionary. Old saved reports
remain valid. Hardware Scan reports providers; tuning never depends on a kernel
build, and the builder never applies settings.

Audit: the earlier optimizer preserved bus/module evidence but did not explicitly
preserve frequency governors, built-in CPU frequency/idle implementations or the
monitoring/profile interfaces as a capability family. `tuning_requirements` now
uses the existing target Kconfig closure and final verification pipeline:

- For detected families, retain baseline-enabled prompted symbols in the target
  cpufreq, cpuidle, thermal, hwmon and powercap Kconfig subtrees. This is deliberately
  conservative within the detected family; it may retain unused baseline drivers.
- Retain baseline-enabled core gates. Known active frequency-driver mappings are
  checked explicitly, so losing their target symbol is not silently ignored.
- Preserve working platform handlers that explicitly depend on/select the profile
  core, including built-ins, and resolve bound module evidence through target Kbuild.
- Resolve target types and dependencies; native Kconfig remains authoritative and
  the existing post-resolution verification checks requested capabilities.
- Do not enable absent baseline candidate options or every vendor/platform driver.

Unknown/out-of-tree driver mappings and renamed Kconfig symbols still need review.
The family baseline approach cannot establish every proprietary firmware dependency.
Observed interfaces with unavailable baseline configuration are not proof that a
particular option can safely be enabled; building with evidence requires baseline
and target source. There is no tuning operation in the preservation path.

## Tests and Phase 2 boundary

JSON manifests materialize fake sysfs trees under temporary directories. Fixtures
cover Intel, legacy/generic CPUFreq, AMD, heterogeneous ARM, VM/empty, multiple
platform handlers, hierarchical powercap, hwmon, thermal and idle. Tests cover
missing/malformed/denied/disappearing attributes, provider failure, alias handling,
generation stability, no-process/read-only access, GUI construction/refresh/close,
and target-aware Kconfig preservation. Real Tk tests run when a display is available.

Run `python3 -B -m unittest discover -s tests -v`. No test writes host sysfs/procfs.
Existing configuration-only native Kconfig tests require their optional local
sources/tools; they do not build or install a kernel.

Before Phase 2, separately review daemon ownership, permissions, supported write
allowlists, per-driver validation, interdependent changes, temporary leases,
rollback and crash recovery. No current metadata authorizes a future write.

## Implementation validation (27 September 2026)

- Complete suite: 230 tests, no failures, 3 skips. Real Tk tests passed with desktop
  access, including the new 700×480-minimum window and existing Tools controls.
- Skips: the three optional Linux 7.2.6/installed-config integration tests lacked
  their required local source/compiler/config inputs. Fixture Kconfig tests passed;
  no kernel was built or installed.
- Python in-memory syntax/compile checks passed for all 25 project/test Python
  files. Whitespace checks included new untracked files. No shell files changed.
- Read-only host discovery: 190 capabilities from CPUFreq, CPU idle, Intel EPB,
  powercap, hwmon and thermal providers; 23 potentially adjustable interfaces.
  Both policies exposed `acpi-cpufreq`/`schedutil`, with configured bounds of
  800000–2001000 kHz. No active P-state interface or platform profile was exposed.
- 180 readings succeeded, 6 were permission-denied and 4 returned read errors
  (RAPL maximum-power attributes). Those values remained unknown; no elevation
  was requested for discovery and no provider-wide failure occurred.

This machine is one observation, not a capability baseline or support definition.

## Primary interface references

- [CPUFreq](https://docs.kernel.org/admin-guide/pm/cpufreq.html)
- [Intel P-state](https://docs.kernel.org/admin-guide/pm/intel_pstate.html)
- [Intel EPB](https://docs.kernel.org/admin-guide/pm/intel_epb.html)
- [AMD P-state](https://docs.kernel.org/admin-guide/pm/amd-pstate.html)
- [Platform profiles](https://docs.kernel.org/userspace-api/sysfs-platform_profile.html)
- [Powercap](https://docs.kernel.org/power/powercap/powercap.html)
- [Hwmon](https://docs.kernel.org/hwmon/sysfs-interface.html)
- [Thermal](https://docs.kernel.org/driver-api/thermal/sysfs-api.html)
- [CPU idle](https://docs.kernel.org/admin-guide/pm/cpuidle.html)
