"""Read-only discovery contracts. Values never imply permission to write."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Union

Value = Union[str, int, bool, tuple[int, ...], tuple[str, ...], None]


class ReadStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    DENIED = "permission denied"
    MALFORMED = "malformed"
    ERROR = "read error"


class Kind(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    CHOICE = "choice"
    TABLE = "table"


class Risk(str, Enum):
    INFORMATION = "Information"
    STANDARD = "Standard tuning capability"
    ADVANCED = "Advanced capability"
    OVERCLOCKING = "Overclocking capability"


@dataclass(frozen=True)
class Source:
    path: str
    canonical_path: str
    provider: str
    raw: str | None
    timestamp: str
    encoding: str = "identity"


@dataclass(frozen=True)
class Relationship:
    relation: str
    target: str


@dataclass(frozen=True)
class Capability:
    id: str
    provider: str
    domain: str
    generation: int
    name: str
    category: str
    description: str
    kind: Kind
    current: Value
    status: ReadStatus
    sources: tuple[Source, ...]
    timestamp: str
    semantic: str
    requested: Value = None
    observed: Value = None
    units: str | None = None
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None
    potentially_writable: bool = False
    authorization: str | None = None
    blocked_reason: str | None = None
    lifetime: str | None = None
    documented_default: Value = None
    default_provenance: str | None = None
    risk: Risk = Risk.INFORMATION
    relationships: tuple[Relationship, ...] = ()
    # CPU membership, driver and labels are evidence, not vendor inference.
    metadata: dict[str, Value] = field(default_factory=dict)


@dataclass(frozen=True)
class Discovery:
    generation: int
    timestamp: str
    capabilities: tuple[Capability, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def providers(self):
        return sorted({s.provider for c in self.capabilities for s in c.sources})

    @property
    def adjustable_count(self):
        return sum(c.potentially_writable and c.status == ReadStatus.OK
                   for c in self.capabilities)

    def evidence(self):
        return {"providers": self.providers,
                "drivers": sorted({str(c.metadata["driver"]) for c in self.capabilities
                                   if c.metadata.get("driver")}),
                "modules": sorted({str(c.metadata["kernel_module"]) for c in self.capabilities
                                   if c.metadata.get("kernel_module")}),
                "capability_count": len(self.capabilities),
                "unavailable_count": sum(c.status != ReadStatus.OK for c in self.capabilities),
                "diagnostics": list(self.diagnostics)}
