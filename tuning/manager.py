"""Discovery orchestration and conservative reconciliation; no mutation API."""
from dataclasses import replace
from datetime import datetime, timezone
from collections import Counter
from hashlib import sha256
from threading import Lock

from .fs import Sysfs
from .model import Discovery, ReadStatus, Relationship
from .providers import DEFAULT_PROVIDERS


class TuningManager:
    def __init__(self, *, _fs=None, _providers=None):
        self._fs = _fs if _fs is not None else Sysfs()
        self._providers = tuple(p() for p in DEFAULT_PROVIDERS) if _providers is None else tuple(_providers)
        self._generation = 0
        self._lock = Lock()

    def discover(self):
        with self._lock:
            self._generation += 1
            timestamp = datetime.now(timezone.utc).isoformat()
            self._fs.diagnostics.clear()
            self._fs._modules.clear()
            found, diagnostics = [], []
            for provider in self._providers:
                try:
                    for capability in provider.discover(self._fs, self._generation, timestamp):
                        found.append(capability)
                except Exception as exc:
                    diagnostics.append(f"{provider.name}: partial discovery failed ({type(exc).__name__}: {exc})")
            caps = self.reconcile(found)
            diagnostics.extend(self._fs.diagnostics)
            return Discovery(self._generation, timestamp, tuple(caps), tuple(diagnostics))

    @staticmethod
    def reconcile(capabilities):
        # Merge only the same physical attribute with the same interpretation.
        # Shared values, labels or CPU vendors never establish identity.
        merged = {}
        for cap in capabilities:
            key = (cap.sources[0].canonical_path, cap.semantic, cap.sources[0].encoding)
            previous = merged.get(key)
            if previous:
                preferred = cap if previous.status != ReadStatus.OK and cap.status == ReadStatus.OK else previous
                merged[key] = replace(preferred,
                                      sources=tuple(dict.fromkeys(previous.sources + cap.sources)),
                                      relationships=tuple(dict.fromkeys(previous.relationships + cap.relationships)))
            else:
                merged[key] = cap
        caps = list(merged.values())
        counts = Counter(c.id for c in caps)
        # A driver can expose two same-named sensor banks on one device.
        # Keep both rather than letting presentation identity merge their data.
        caps = [replace(c, id=c.id + ":" + sha256(c.sources[0].canonical_path.encode()).hexdigest()[:12])
                if counts[c.id] > 1 else c for c in caps]
        global_boost = [c for c in caps if c.domain == "cpu:global" and c.semantic == "boost"]
        result = []
        for cap in caps:
            related = list(cap.relationships)
            if cap.semantic in ("boost", "cpb", "boost-permitted"):
                related += [Relationship("overlapping global permission", c.id) for c in global_boost if c.id != cap.id]
            if cap.provider == "intel-pstate" and cap.semantic in ("min_perf_pct", "max_perf_pct"):
                related += [Relationship("overlapping frequency limit", c.id) for c in caps
                            if c.semantic in ("scaling_min_freq", "scaling_max_freq")]
            if cap.minimum is not None and cap.maximum is not None and cap.minimum > cap.maximum:
                cap = replace(cap, minimum=None, maximum=None,
                              blocked_reason="Contradictory reported bounds; no usable range")
            result.append(replace(cap, relationships=tuple(dict.fromkeys(related))))
        return sorted(result, key=lambda c: (c.category, c.domain, c.name, c.id))
