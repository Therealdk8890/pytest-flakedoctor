"""Repro artifacts.

hermetic's own CLI can express a seed but not a clock, network, or filesystem
configuration, so a diagnosed perturbation travels as a compact blob that
``--doctor-repro`` applies to the selected test.

Format: ``fd1:`` + urlsafe-base64( zlib( canonical JSON ) ). The payload is
versioned per axis so new axes can be added without invalidating old blobs,
and decoding fails loudly on anything it does not fully understand — a repro
that silently does less than it claims is worse than no repro.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import zlib
from dataclasses import dataclass, field

from ._axes import AXES_BY_ID, AxisValue, merge

PREFIX = "fd1:"
PAYLOAD_VERSION = 1


class ReproFormatError(Exception):
    """A blob could not be decoded, or asks for something this build cannot do."""


@dataclass
class Repro:
    """Everything needed to re-run a test the way the doctor did."""

    values: list[AxisValue]
    nodeid: str
    tz: str | None = None
    python: str = field(default_factory=lambda: ".".join(str(p) for p in sys.version_info[:2]))
    tool: str = ""
    fingerprint: str = ""
    confirm: tuple[int, int] = (0, 0)  # (failed, runs) observed at verification
    # For test-order repros: the exact nodeid sequence to run (victim last).
    order: list[str] = field(default_factory=list)
    # For thread-interleave repros: {schedule, py_exact, strategy, granularity}.
    # Opaque data consumed only by interleave_test.replay(), never a Sandbox.
    interleave: dict | None = None

    @property
    def hashseed(self) -> str | None:
        for value in self.values:
            if value.axis == "hashseed":
                return value.value
        return None

    def sandbox_kwargs(self) -> dict | None:
        sandbox, _env = merge(self.values)
        if sandbox is not None and self.tz:
            # Frozen wall time is rendered through the machine's local zone, so
            # a date-boundary repro only transfers if the zone travels with it.
            sandbox = dict(sandbox)
            sandbox["env"] = {**dict(sandbox.get("env") or {}), "TZ": self.tz}
        return sandbox

    def encode(self) -> str:
        payload = {
            "v": PAYLOAD_VERSION,
            "tool": self.tool,
            "py": self.python,
            "node": hashlib.sha256(self.nodeid.encode("utf-8")).hexdigest()[:12],
            "axes": {value.axis: {"v": 1, "value": value.value} for value in self.values},
        }
        if self.order:
            payload["order"] = list(self.order)
        if self.interleave:
            payload["interleave"] = self.interleave
        if self.tz:
            payload["tz"] = self.tz
        if self.fingerprint:
            payload["sig"] = self.fingerprint
        if self.confirm != (0, 0):
            payload["confirm"] = list(self.confirm)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode(blob: str, nodeid: str | None = None) -> Repro:
    """Decode a blob, or raise ReproFormatError explaining exactly what failed.

    Every malformed input must surface as ReproFormatError, never a bare
    ValueError/AttributeError/TypeError — a committed repro marker runs in CI,
    so a single corrupt blob must degrade to one skipped reproduction, never
    crash the whole session.
    """
    try:
        return _decode(blob, nodeid)
    except ReproFormatError:
        raise
    except Exception as exc:  # defensive: any unforeseen malformation
        raise ReproFormatError(f"corrupt repro blob: {type(exc).__name__}: {exc}") from exc


def _decode(blob: object, nodeid: str | None) -> Repro:
    if not isinstance(blob, str):
        raise ReproFormatError(f"repro blob must be a string, got {type(blob).__name__}")
    text = blob.strip()
    if not text.startswith(PREFIX):
        raise ReproFormatError(
            f"not a flakedoctor repro blob (expected it to start with {PREFIX!r})"
        )
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(text[len(PREFIX) :].encode("ascii")))
        payload = json.loads(raw)
    except Exception as exc:
        raise ReproFormatError(f"corrupt repro blob: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReproFormatError("corrupt repro blob: payload is not an object")

    version = payload.get("v")
    if version != PAYLOAD_VERSION:
        raise ReproFormatError(
            f"repro blob has payload version {version}, but this flakedoctor "
            f"understands version {PAYLOAD_VERSION} — upgrade pytest-flakedoctor"
        )

    axes = payload.get("axes") or {}
    if not isinstance(axes, dict):
        raise ReproFormatError("corrupt repro blob: 'axes' must be an object")
    values: list[AxisValue] = []
    for axis_id, spec in axes.items():
        axis = AXES_BY_ID.get(axis_id)
        if axis is None:
            raise ReproFormatError(
                f"repro blob uses axis {axis_id!r}, which this flakedoctor does not "
                "know — upgrade pytest-flakedoctor"
            )
        if not isinstance(spec, dict) or spec.get("v") != 1:
            raise ReproFormatError(
                f"axis {axis_id!r} has payload version {spec.get('v') if isinstance(spec, dict) else '?'}, "
                "which this flakedoctor cannot apply — upgrade pytest-flakedoctor"
            )
        wanted = spec.get("value")
        value = _rebuild(axis, wanted)
        if value is None:
            raise ReproFormatError(
                f"axis {axis_id!r} value {wanted!r} is not one this flakedoctor can rebuild"
            )
        values.append(value)

    if nodeid is not None:
        digest = hashlib.sha256(nodeid.encode("utf-8")).hexdigest()[:12]
        if payload.get("node") and payload["node"] != digest:
            raise ReproFormatError(
                "this repro blob was recorded for a different test than the one selected"
            )

    order = payload.get("order") or []
    if order and not all(isinstance(entry, str) for entry in order):
        raise ReproFormatError("corrupt repro blob: order must be a list of node ids")

    interleave = payload.get("interleave")
    if interleave is not None:
        if not isinstance(interleave, dict) or not isinstance(interleave.get("schedule"), str):
            raise ReproFormatError("corrupt repro blob: interleave must carry a schedule")

    confirm = payload.get("confirm") or [0, 0]
    try:
        pair = (int(confirm[0]), int(confirm[1])) if len(confirm) == 2 else (0, 0)
    except (TypeError, ValueError):
        pair = (0, 0)
    return Repro(
        values=values,
        nodeid=nodeid or "",
        order=list(order),
        interleave=interleave,
        tz=payload.get("tz"),
        python=payload.get("py", ""),
        tool=payload.get("tool", ""),
        fingerprint=payload.get("sig", ""),
        confirm=pair,
    )


def _rebuild(axis, wanted: object) -> AxisValue | None:
    """Find the axis value matching a serialized value, reconstructing configs.

    Values are rebuilt from the axis definition rather than stored verbatim, so
    a blob can never smuggle arbitrary kwargs into a Sandbox call.
    """
    if not isinstance(wanted, str):
        return None
    if axis.id == "hashseed":
        if not wanted.isdigit():
            return None
        return axis._value(wanted)
    candidates = list(axis.provocations())
    control = axis.control()
    if control is not None:
        candidates.append(control)
    for candidate in candidates:
        if candidate.value == wanted:
            return candidate
    if axis.id == "time":
        return _rebuild_time(wanted)
    if axis.id == "rng":
        try:
            seed = int(wanted)
        except ValueError:
            return None
        return AxisValue(
            axis="rng", value=wanted, sandbox={"rng": "all", "seed": seed},
            label=f"rng seeded (seed={wanted})",
        )
    return None


def _rebuild_time(wanted: str) -> AxisValue | None:
    """Rebuild an arbitrary frozen instant, e.g. after boundary bisection."""
    instant, _, tick_part = wanted.partition("#")
    if not instant.startswith("frozen@"):
        return None
    instant = instant.removeprefix("frozen@")
    tick = 1e-6
    if tick_part:
        if not tick_part.startswith("tick="):
            return None
        try:
            tick = float(tick_part.removeprefix("tick="))
        except ValueError:
            return None
    return AxisValue(
        axis="time",
        value=wanted,
        sandbox={"clock": "virtual", "now": instant, "tick": tick},
        label=f"time frozen @ {instant}" + (f", tick={tick}s" if tick_part else ""),
    )


def current_timezone() -> str | None:
    """The timezone to pin into a blob, if we can name one portably.

    Only an explicit TZ is recorded: `time.tzname` yields abbreviations like
    'EST' that do not round-trip. When None is returned the report warns that a
    time-based repro may not transfer across machines.
    """
    tz = os.environ.get("TZ")
    return tz or None


def describe_timezone() -> str:
    """Human-readable local zone, for the report's cross-machine caveat."""
    try:
        return f"{time.tzname[0]} (UTC{time.strftime('%z')})"
    except Exception:  # pragma: no cover - platform quirks
        return "unknown"
