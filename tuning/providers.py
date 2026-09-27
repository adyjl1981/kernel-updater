"""Composable readers of documented Linux ABIs; intentionally no setters.

Documentation links and reconciliation rules are in docs/performance-tuning.md.
Attribute presence establishes discovery, never successful write support.
"""
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Protocol
import re

from .fs import boolean, integer, natural, numbers, text_value, words
from .model import Capability, Kind, ReadStatus, Relationship, Risk, Source

CPU = "devices/system/cpu"
POLICIES = CPU + "/cpufreq/policy*"


class Provider(Protocol):
    name: str

    def discover(self, fs, generation, timestamp):
        """Yield capabilities; failure must not invalidate earlier results."""
        ...


def parsed(fs, path, parser=text_value):
    reading = fs.read(path)
    if reading.status == ReadStatus.OK:
        try:
            return parser(reading.raw)
        except ValueError:
            pass
    return None


class Reader:
    def __init__(self, fs, provider, generation, timestamp):
        self.fs, self.provider = fs, provider
        self.generation, self.timestamp = generation, timestamp

    def cap(self, path, domain, name, category="CPU", *, parser=text_value,
            kind=Kind.TEXT, semantic=None, writable=False, risk=None,
            description="", units=None, choices=(), minimum=None, maximum=None,
            metadata=None, relationships=(), encoding="identity", required=False,
            value_role=None, documented_default=None, default_provenance=None):
        read = self.fs.read(path)
        if read.status == ReadStatus.MISSING and not required:
            return
        status, reason, value = read.status, read.reason, None
        if status == ReadStatus.OK:
            try:
                value = parser(read.raw)
            except ValueError as exc:
                status, reason = ReadStatus.MALFORMED, str(exc)
        try:
            canonical = self.fs.canonical(path)
        except (OSError, ValueError):
            canonical = "/sys/" + path
        semantic = semantic or PurePosixPath(path).name
        identity = sha256((domain + "\0" + semantic).encode()).hexdigest()[:24]
        source = Source("/sys/" + path, canonical, self.provider, read.raw,
                        self.timestamp, encoding)
        metadata = dict(metadata or {})
        module = self.fs.bound_module(path)
        if module:
            metadata["kernel_module"] = module
        yield Capability(
            id="tuning:" + identity, provider=self.provider, domain=domain,
            generation=self.generation, name=name, category=category,
            description=description, kind=kind, current=value, status=status,
            sources=(source,), timestamp=self.timestamp, semantic=semantic,
            requested=value if value_role == "requested" else None,
            observed=value if value_role == "observed" else None,
            units=units, choices=choices or (), minimum=minimum, maximum=maximum,
            potentially_writable=writable,
            authorization="not assessed; privileged access normally required" if writable else None,
            blocked_reason=reason or ("Phase 1 is read-only; write support is not probed" if writable else None),
            risk=risk or (Risk.STANDARD if writable else Risk.INFORMATION),
            relationships=tuple(relationships), metadata=metadata,
            documented_default=documented_default, default_provenance=default_provenance)


class CpuFreqProvider:
    name = "cpufreq"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        for policy in fs.glob(POLICIES):
            domain = fs.canonical(policy)
            driver = parsed(fs, policy + "/scaling_driver")
            metadata = {"driver": driver,
                        "affected_cpus": parsed(fs, policy + "/affected_cpus", numbers),
                        "related_cpus": parsed(fs, policy + "/related_cpus", numbers)}
            for attr, parser, kind in (("scaling_driver", text_value, Kind.TEXT),
                                       ("affected_cpus", numbers, Kind.TABLE),
                                       ("related_cpus", numbers, Kind.TABLE)):
                yield from r.cap(policy + "/" + attr, domain, attr.replace("_", " ").title(),
                                 parser=parser, kind=kind, metadata=metadata)
            choices = parsed(fs, policy + "/scaling_available_governors", words)
            yield from r.cap(policy + "/scaling_available_governors", domain, "Available governors",
                             parser=words, kind=Kind.TABLE, metadata=metadata)
            yield from r.cap(policy + "/scaling_governor", domain, "Governor", kind=Kind.CHOICE,
                             choices=choices, writable=True, metadata=metadata,
                             description="Driver-specific policy algorithm; names alone do not imply fixed clocks.")
            lower = parsed(fs, policy + "/cpuinfo_min_freq", natural)
            upper = parsed(fs, policy + "/cpuinfo_max_freq", natural)
            for attr, label in (("cpuinfo_min_freq", "Reported hardware minimum"),
                                ("cpuinfo_max_freq", "Reported hardware maximum"),
                                ("scaling_min_freq", "Requested minimum frequency"),
                                ("scaling_max_freq", "Requested maximum frequency"),
                                ("scaling_cur_freq", "Driver-reported current frequency"),
                                ("cpuinfo_cur_freq", "Hardware-reported current frequency"),
                                ("cpuinfo_avg_freq", "Hardware-reported average frequency"),
                                ("bios_limit", "Firmware frequency limit")):
                adjustable = attr in ("scaling_min_freq", "scaling_max_freq")
                yield from r.cap(policy + "/" + attr, domain, label, parser=natural,
                                 kind=Kind.NUMBER, units="kHz", writable=adjustable,
                                 minimum=lower if adjustable else None,
                                 maximum=upper if adjustable else None, metadata=metadata,
                                 value_role="requested" if adjustable else (
                                     "observed" if attr in ("cpuinfo_cur_freq", "cpuinfo_avg_freq") else None),
                                 description="A driver reading may be a request or estimate, not an instantaneous measurement."
                                 if attr == "scaling_cur_freq" else "",
                                 relationships=(Relationship("shared policy", domain),))
            yield from r.cap(policy + "/scaling_available_frequencies", domain, "Available frequencies",
                             parser=numbers, kind=Kind.TABLE, units="kHz", metadata=metadata)
            for attr in ("boost", "cpb"):
                yield from r.cap(policy + "/" + attr, domain, "Boost permitted", "Power & Boost",
                                 parser=boolean, kind=Kind.BOOLEAN, writable=True, metadata=metadata,
                                 description="Permission to boost does not mean boost is currently active.")
            choices = parsed(fs, policy + "/energy_performance_available_preferences", words)
            yield from r.cap(policy + "/energy_performance_available_preferences", domain,
                             "Available energy preferences", parser=words, kind=Kind.TABLE, metadata=metadata)
            yield from r.cap(policy + "/energy_performance_preference", domain, "Energy performance preference",
                             kind=Kind.CHOICE, choices=choices, writable=True, metadata=metadata,
                             description="Raw driver preference; numeric encodings are not translated into invented profiles.")
        yield from r.cap(CPU + "/cpufreq/boost", "cpu:global", "Global boost permitted", "Power & Boost",
                         parser=boolean, kind=Kind.BOOLEAN, writable=True,
                         description="Global permission; may overlap policy-specific boost controls.")


class IntelPstateProvider:
    name = "intel-pstate"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        base = CPU + "/intel_pstate"
        domain = "/sys/" + base
        mode = parsed(fs, base + "/status")
        description = ("Active mode supplies driver-specific algorithms; powersave is not a fixed minimum clock."
                       if mode == "active" else "Passive mode uses generic CPUFreq governors."
                       if mode == "passive" else "Driver mode is unknown or inactive.")
        yield from r.cap(base + "/status", domain, "Intel P-state mode", description=description,
                         writable=True, risk=Risk.ADVANCED)
        yield from r.cap(base + "/no_turbo", domain, "Turbo permitted", "Power & Boost",
                         parser=lambda raw: not boolean(raw), kind=Kind.BOOLEAN, writable=True,
                         semantic="boost-permitted", encoding="inverted boolean (no_turbo)",
                         description="Normalized from no_turbo: 0 permits turbo; 1 prohibits it.")
        for attr in ("min_perf_pct", "max_perf_pct", "turbo_pct", "num_pstates", "hwp_dynamic_boost", "energy_efficiency"):
            toggle = attr in ("hwp_dynamic_boost", "energy_efficiency")
            writable = toggle or attr in ("min_perf_pct", "max_perf_pct")
            yield from r.cap(base + "/" + attr, domain, attr.replace("_", " ").title(), "Advanced",
                             parser=boolean if toggle else natural, kind=Kind.BOOLEAN if toggle else Kind.NUMBER,
                             units="%" if attr.endswith("pct") else None, writable=writable,
                             risk=Risk.ADVANCED if writable else Risk.INFORMATION,
                             description="Global driver attribute; frequency policies may impose additional limits.")
        for path in fs.glob(CPU + "/cpu[0-9]*/power/energy_perf_bias"):
            yield from r.cap(path, "/sys/" + path.rsplit("/", 2)[0], "Energy performance bias", "Advanced",
                             parser=natural, kind=Kind.NUMBER, writable=True, minimum=0, maximum=15,
                             risk=Risk.ADVANCED,
                             description="EPB is distinct from EPP. The hardware register may be shared by CPUs.")


class AmdPstateProvider:
    name = "amd-pstate"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        base = CPU + "/amd_pstate"
        for attr in ("status", "prefcore"):
            yield from r.cap(base + "/" + attr, "/sys/" + base, "AMD P-state " + attr,
                             writable=attr == "status", risk=Risk.ADVANCED if attr == "status" else Risk.INFORMATION,
                             description="Driver information only; this does not establish PBO or Curve Optimizer support.")
        for policy in fs.glob(POLICIES):
            for attr, units in (("amd_pstate_highest_perf", None), ("amd_pstate_max_freq", "kHz"),
                                ("amd_pstate_lowest_nonlinear_freq", "kHz"),
                                ("amd_pstate_prefcore_ranking", None), ("amd_pstate_hw_prefcore", None),
                                ("amd_pstate_floor_freq", "kHz"), ("amd_pstate_floor_count", None)):
                yield from r.cap(policy + "/" + attr, fs.canonical(policy), attr.replace("_", " "),
                                 parser=natural, kind=Kind.NUMBER, units=units,
                                 writable=attr == "amd_pstate_floor_freq",
                                 risk=Risk.ADVANCED if attr == "amd_pstate_floor_freq" else Risk.INFORMATION,
                                 minimum=parsed(fs, policy + "/cpuinfo_min_freq", natural) if attr == "amd_pstate_floor_freq" else None,
                                 maximum=parsed(fs, policy + "/scaling_max_freq", natural) if attr == "amd_pstate_floor_freq" else None,
                                 description="Driver-reported information; abstract performance values are not MHz.")


class PlatformProfileProvider:
    name = "platform-profile"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        handlers = fs.glob("class/platform-profile/*")
        for base, attr, choices_attr in [("firmware/acpi", "platform_profile", "platform_profile_choices")] + [
                (path, "profile", "choices") for path in handlers]:
            domain = fs.canonical(base)
            label = parsed(fs, base + "/name")
            metadata = {"driver": label} if label else {}
            choices = parsed(fs, base + "/" + choices_attr, words)
            relationships = tuple(Relationship("aggregate of", fs.canonical(p)) for p in handlers) if attr == "platform_profile" else ()
            yield from r.cap(base + "/" + choices_attr, domain, "Available platform profiles", "Power & Boost",
                             parser=words, kind=Kind.TABLE, metadata=metadata)
            yield from r.cap(base + "/" + attr, domain, label or "Platform profile", "Power & Boost",
                             kind=Kind.CHOICE, choices=choices, writable=True, metadata=metadata,
                             relationships=relationships,
                             description="Firmware/platform policy; aggregate and individual handlers are distinct scopes.")


class PowercapProvider:
    name = "powercap"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        # Class entries often alias nested devices. Traverse backing children too.
        pending = fs.glob("class/powercap/*")
        seen = set()
        while pending:
            base = pending.pop(0)
            canonical = fs.canonical(base)
            if canonical not in seen:
                seen.add(canonical)
                pending.extend(p for p in fs.glob(base + "/*") if ":" in PurePosixPath(p).name)
            label = parsed(fs, base + "/name")
            metadata = {"label": label}
            parent = str(PurePosixPath(canonical).parent)
            relationships = (Relationship("parent domain", parent),)
            for attr, parser, units in (("name", text_value, None), ("enabled", boolean, None),
                                        ("energy_uj", natural, "µJ"), ("max_energy_range_uj", natural, "µJ"),
                                        ("power_uw", natural, "µW"), ("max_power_range_uw", natural, "µW")):
                yield from r.cap(base + "/" + attr, canonical, (label or "Power domain") + ": " + attr,
                                 "Power & Boost", parser=parser,
                                 kind=Kind.TEXT if attr == "name" else Kind.BOOLEAN if attr == "enabled" else Kind.NUMBER,
                                 units=units, metadata=metadata, relationships=relationships)
            for path in fs.glob(base + "/constraint_*_*"):
                match = re.fullmatch(r"constraint_(\d+)_(name|power_limit_uw|time_window_us|min_power_uw|max_power_uw|min_time_window_us|max_time_window_us)", PurePosixPath(path).name)
                if not match:
                    continue
                index, attr = match.groups()
                prefix = base + "/constraint_" + index + "_"
                name = parsed(fs, prefix + "name") or "Constraint " + index
                adjustable = attr in ("power_limit_uw", "time_window_us")
                bound = "power_uw" if attr == "power_limit_uw" else "time_window_us"
                yield from r.cap(path, canonical, name + ": " + attr, "Power & Boost",
                                 parser=text_value if attr == "name" else natural,
                                 kind=Kind.TEXT if attr == "name" else Kind.NUMBER,
                                 units="µW" if attr.endswith("_uw") else "µs" if attr.endswith("_us") else None,
                                 writable=adjustable, risk=Risk.ADVANCED if adjustable else Risk.INFORMATION,
                                 minimum=parsed(fs, prefix + "min_" + bound, natural) if adjustable else None,
                                 maximum=parsed(fs, prefix + "max_" + bound, natural) if adjustable else None,
                                 metadata=metadata, relationships=relationships, required=True,
                                 description="Constraint number has no universal meaning. Accepted ranges are not safety guarantees.")


class HwmonProvider:
    name = "hwmon"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        for base in fs.glob("class/hwmon/hwmon*"):
            driver = parsed(fs, base + "/name")
            # Remove allocation-only hwmon indices from device-backed identities.
            canonical = fs.canonical(base)
            domain = re.sub(r"/(?:hwmon/)?hwmon\d+$", "", canonical) + "/sensor:" + (driver or "unknown")
            for path in fs.glob(base + "/*"):
                attr = PurePosixPath(path).name
                match = re.fullmatch(r"(temp|power|in|curr|fan|freq)(\d+)_(input|average|min|max|crit|lcrit|emergency|cap|cap_min|cap_max|alarm|min_alarm|max_alarm|crit_alarm|fault)", attr)
                if not match:
                    continue
                family, channel, field = match.groups()
                allowed = {
                    "temp": {"input", "min", "max", "crit", "lcrit", "emergency", "alarm", "min_alarm", "max_alarm", "crit_alarm", "fault"},
                    "power": {"input", "average", "cap", "cap_min", "cap_max", "min", "max", "crit", "lcrit", "alarm", "min_alarm", "max_alarm", "crit_alarm"},
                    "in": {"input", "min", "max", "crit", "lcrit", "alarm", "min_alarm", "max_alarm", "crit_alarm"},
                    "curr": {"input", "min", "max", "crit", "lcrit", "alarm", "min_alarm", "max_alarm", "crit_alarm"},
                    "fan": {"input", "min", "max", "alarm", "min_alarm", "max_alarm", "fault"},
                    "freq": {"input"},
                }
                if field not in allowed[family]:
                    continue
                label = parsed(fs, base + "/" + family + channel + "_label")
                alarm = field.endswith("alarm") or field == "fault"
                units = {"temp": "m°C", "power": "µW", "in": "mV", "curr": "mA", "fan": "RPM", "freq": "Hz"}[family]
                writable = family == "power" and field == "cap"
                yield from r.cap(path, domain, f"{label or family + channel}: {field}",
                                 "Thermal" if family in ("temp", "fan") or alarm else "Power & Boost",
                                 parser=boolean if alarm else integer,
                                 kind=Kind.BOOLEAN if alarm else Kind.NUMBER, units=None if alarm else units,
                                 writable=writable, risk=Risk.ADVANCED if writable else Risk.INFORMATION,
                                 minimum=parsed(fs, base + f"/power{channel}_cap_min", natural) if writable else None,
                                 maximum=parsed(fs, base + f"/power{channel}_cap_max", natural) if writable else None,
                                 metadata={"driver": driver, "label": label, "channel": family + channel},
                                 value_role="observed" if field in ("input", "average") else None,
                                 required=True, description="Driver sensor/limit. Critical and emergency thresholds are not recommended targets.")


class ThermalProvider:
    name = "thermal"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        for base in fs.glob("class/thermal/thermal_zone*"):
            domain = fs.canonical(base)
            label = parsed(fs, base + "/type")
            yield from r.cap(base + "/type", domain, "Thermal zone type", "Thermal")
            yield from r.cap(base + "/temp", domain, label or "Zone temperature", "Thermal",
                             parser=integer, kind=Kind.NUMBER, units="m°C", value_role="observed")
            for path in fs.glob(base + "/trip_point_*_*"):
                match = re.fullmatch(r"trip_point_(\d+)_(type|temp|hyst)", PurePosixPath(path).name)
                if not match:
                    continue
                index, attr = match.groups()
                trip_type = parsed(fs, base + "/trip_point_" + index + "_type")
                yield from r.cap(path, domain, f"Trip {index} ({trip_type or 'unknown'}): {attr}", "Thermal",
                                 parser=text_value if attr == "type" else integer,
                                 kind=Kind.TEXT if attr == "type" else Kind.NUMBER,
                                 units=None if attr == "type" else "m°C", required=True,
                                 metadata={"zone_type": label, "trip_type": trip_type},
                                 description="Typed thermal trip, not an operating target. Phase 1 never changes thermal policy.")


class CpuIdleProvider:
    name = "cpuidle"

    def discover(self, fs, generation, timestamp):
        r = Reader(fs, self.name, generation, timestamp)
        for attr in ("current_driver", "current_governor", "current_governor_ro", "available_governors"):
            yield from r.cap(CPU + "/cpuidle/" + attr, "cpu:idle", "Idle " + attr, "Advanced",
                             metadata={"driver": parsed(fs, CPU + "/cpuidle/current_driver")})
        for base in fs.glob(CPU + "/cpu[0-9]*/cpuidle/state*"):
            domain = fs.canonical(base)
            default = parsed(fs, base + "/default_status")
            for attr in ("name", "desc", "disable", "default_status", "latency", "residency",
                         "time", "usage", "above", "below", "rejected", "power"):
                textual = attr in ("name", "desc", "default_status")
                yield from r.cap(base + "/" + attr, domain, "Idle " + attr, "Advanced",
                                 parser=text_value if textual else boolean if attr == "disable" else natural,
                                 kind=Kind.TEXT if textual else Kind.BOOLEAN if attr == "disable" else Kind.NUMBER,
                                 units="µs" if attr in ("latency", "residency", "time") else "mW" if attr == "power" else None,
                                 documented_default=(default == "disabled") if attr == "disable" and default in ("enabled", "disabled") else None,
                                 default_provenance="/sys/" + base + "/default_status" if attr == "disable" and default in ("enabled", "disabled") else None,
                                 description="Informational only. Idle power 0 means unspecified; counters need not measure actual hardware residency.")


DEFAULT_PROVIDERS = (CpuFreqProvider, IntelPstateProvider, AmdPstateProvider,
                     PlatformProfileProvider, PowercapProvider, HwmonProvider,
                     ThermalProvider, CpuIdleProvider)
