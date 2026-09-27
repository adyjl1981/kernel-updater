"""Bounded read-only sysfs access. Alternate roots are internal test injection."""
from dataclasses import dataclass
from pathlib import Path
import re

from .model import ReadStatus


@dataclass(frozen=True)
class Reading:
    raw: str | None
    status: ReadStatus
    reason: str | None = None


class Sysfs:
    def __init__(self, root=Path("/sys")):
        self.root = Path(root).resolve()
        self.diagnostics = []
        self._modules = {}

    def path(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("sysfs path escapes discovery root")
        return path

    def canonical(self, relative):
        return "/sys/" + self.path(relative).resolve().relative_to(self.root).as_posix()

    def glob(self, pattern):
        try:
            found = []
            for path in self.root.glob(pattern):
                relative = path.relative_to(self.root).as_posix()
                try:
                    self.path(relative)
                    found.append(relative)
                except (OSError, ValueError, RuntimeError) as exc:
                    self.diagnostics.append(f"/sys/{relative}: {exc}")
            return sorted(found)
        except OSError as exc:
            self.diagnostics.append(f"/sys/{pattern}: {exc}")
            return []

    def bound_module(self, relative):
        """Follow kernel-owned device ancestry; no module loading or commands."""
        try:
            parent = self.path(relative).resolve().parent
            key = str(parent)
            if key in self._modules:
                return self._modules[key]
            for node in (parent, *parent.parents):
                if not node.is_relative_to(self.root):
                    break
                for link in (node / "driver/module", node / "device/driver/module"):
                    target = link.resolve()
                    if target.is_relative_to(self.root / "module") and target.exists():
                        self._modules[key] = target.name.replace("-", "_")
                        return self._modules[key]
            self._modules[key] = None
        except (OSError, ValueError, RuntimeError):
            return None
        return None

    def read(self, relative):
        try:
            with self.path(relative).open("r", encoding="utf-8", errors="strict") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                return Reading(None, ReadStatus.MALFORMED, "attribute exceeds read limit")
            return Reading(raw.strip(), ReadStatus.OK)
        except FileNotFoundError:
            return Reading(None, ReadStatus.MISSING, "attribute absent or device removed")
        except PermissionError:
            return Reading(None, ReadStatus.DENIED, "attribute is not readable without authorization")
        except (ValueError, UnicodeError, RuntimeError) as exc:
            return Reading(None, ReadStatus.MALFORMED, str(exc))
        except OSError as exc:
            return Reading(None, ReadStatus.ERROR, str(exc))


def integer(raw):
    if not re.fullmatch(r"-?\d+", raw) or len(raw) > 21:
        raise ValueError("expected bounded decimal integer")
    return int(raw)


def natural(raw):
    value = integer(raw)
    if value < 0:
        raise ValueError("expected non-negative integer")
    return value


def boolean(raw):
    if raw not in ("0", "1"):
        raise ValueError("expected 0 or 1")
    return raw == "1"


def words(raw):
    if not raw:
        raise ValueError("empty attribute")
    return tuple(raw.split())


def numbers(raw):
    return tuple(natural(x) for x in words(raw))


def text_value(raw):
    if not raw:
        raise ValueError("empty attribute")
    return raw
