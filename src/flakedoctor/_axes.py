"""Perturbation axes (AxisProvider-lite).

Private in v0.1 by design: the protocol exists so axes can be added without
rearchitecting — it is deliberately NOT a public extension point yet.

Each axis offers two symmetric moves:

* **control** — determinize the axis. If the flakiness disappears when only
  this axis is controlled, the flake lives here.
* **provoke** — pin an adversarial value. If failure becomes certain, that
  value is the reproduction.

The four hermetic axes (time, rng, network, fs) merge into a single
``hermetic.Sandbox(...)`` call, so their values contribute partial Sandbox
kwargs which the child probe merges over a base that switches everything off.
hashseed lives outside hermetic entirely: hash randomization is fixed at
interpreter start, which is why every diagnostic run is a fresh subprocess.
"""

from __future__ import annotations

import random
import shlex
import sys
from dataclasses import dataclass, field

# hermetic's own defaults turn every subsystem ON, so a config that means
# "perturb nothing" must switch each one off explicitly.
SANDBOX_OFF: dict[str, object] = {
    "clock": "off",
    "rng": "off",
    "network": "off",
    "fs": "off",
}

DEFAULT_NOW = "2020-01-01T00:00:00+00:00"

# Instants chosen to break the arithmetic that real code gets wrong: rollovers
# where two now() reads can straddle a boundary, and the leap day.
ADVERSARIAL_INSTANTS: tuple[tuple[str, str], ...] = (
    ("day-boundary", "2020-01-01T23:59:59.999000+00:00"),
    ("month-end", "2020-01-31T23:59:59.500000+00:00"),
    ("year-end", "2020-12-31T23:59:59.500000+00:00"),
    ("leap-day", "2020-02-29T12:00:00+00:00"),
    ("dst-spring-forward", "2020-03-08T06:59:59.500000+00:00"),
)


def _ps_quote(text: str) -> str:
    """Single-quote for PowerShell, where '' is the escaped single quote."""
    return "'" + text.replace("'", "''") + "'"


@dataclass(frozen=True)
class AxisValue:
    """One setting of one axis: how to configure it, and how to describe it."""

    axis: str
    value: str  # canonical, serializable; also what the repro blob stores
    sandbox: dict = field(default_factory=dict, hash=False)
    env: dict = field(default_factory=dict, hash=False)  # None value => unset in child
    label: str = ""

    def described(self) -> str:
        return self.label or f"{self.axis}={self.value}"


class _Axis:
    """Common behavior; subclasses define id/display and their values."""

    id = ""
    display = ""
    # Axes whose control move is meaningful get a control config; hashseed has
    # no "neutral" value, so it participates in provocation only.
    has_control = True

    def control(self) -> AxisValue | None:
        raise NotImplementedError

    def provocations(self) -> list[AxisValue]:
        raise NotImplementedError

    def explain(self, value: str) -> str:
        raise NotImplementedError


class TimeAxis(_Axis):
    """Wall-clock dependence: code whose result changes with the current time."""

    id = "time"
    display = "time-dependent (wall clock)"

    def control(self) -> AxisValue:
        return AxisValue(
            axis=self.id,
            value=f"frozen@{DEFAULT_NOW}",
            sandbox={"clock": "virtual", "now": DEFAULT_NOW, "tick": 1e-6},
            label="time frozen @ default",
        )

    def provocations(self) -> list[AxisValue]:
        values = [
            AxisValue(
                axis=self.id,
                value=f"frozen@{instant}",
                sandbox={"clock": "virtual", "now": instant, "tick": 1e-6},
                label=f"time frozen @ {name}",
            )
            for name, instant in ADVERSARIAL_INSTANTS
        ]
        # A huge tick makes every clock read jump forward: the coarse stand-in
        # for "this machine is very slow", which is what async-wait flakes need.
        values.append(
            AxisValue(
                axis=self.id,
                value=f"frozen@{DEFAULT_NOW}#tick=1.0",
                sandbox={"clock": "virtual", "now": DEFAULT_NOW, "tick": 1.0},
                label="time frozen @ default, tick=1s (slow machine)",
            )
        )
        return values

    def explain(self, value: str) -> str:
        instant, _, tick = value.partition("#")
        instant = instant.removeprefix("frozen@")
        if tick:
            return (
                "The test's outcome depends on how much time appears to pass between "
                "clock reads: with each read advancing a full second (a slow-machine "
                "simulation) it fails every time. Look for a timeout, a deadline, or a "
                "duration assertion that assumes the code runs fast."
            )
        return (
            f"The test's outcome depends on the wall clock: with time frozen at {instant} "
            "it fails every time. Look for an assertion that spans a date or time "
            "boundary between two now() reads, or logic that formats/compares the "
            "current date."
        )


class RngAxis(_Axis):
    """Randomness dependence: random, secrets, uuid4, os.urandom, numpy seeding."""

    id = "rng"
    display = "randomness-dependent (unseeded RNG)"

    def control(self) -> AxisValue:
        return AxisValue(
            axis=self.id,
            value="0",
            sandbox={"rng": "all", "seed": 0},
            label="rng seeded (seed=0)",
        )

    def provocations(self) -> list[AxisValue]:
        seeds = ["0", "1", "2", "12345"]
        return [
            AxisValue(
                axis=self.id,
                value=seed,
                sandbox={"rng": "all", "seed": int(seed)},
                label=f"rng seeded (seed={seed})",
            )
            for seed in seeds
        ]

    def explain(self, value: str) -> str:
        return (
            f"The test's outcome depends on random values: seeding all randomness with "
            f"seed={value} makes it fail every time. Look for unseeded random/secrets/"
            "uuid4 use, or a fixture generating random data that only sometimes "
            "collides or violates an assumption."
        )


class NetworkAxis(_Axis):
    """Hidden live-network dependence."""

    id = "network"
    display = "network-dependent (live network access)"

    def control(self) -> AxisValue:
        # Blocking is both the control and the provocation for this axis: it
        # removes the outside world's variance, at the risk of breaking tests
        # that genuinely need it — which fingerprinting catches and reports
        # separately as perturbation-induced breakage.
        return AxisValue(
            axis=self.id,
            value="block",
            sandbox={"network": "block", "allow_loopback": True},
            label="network blocked",
        )

    def provocations(self) -> list[AxisValue]:
        return [self.control()]

    def explain(self, value: str) -> str:
        return (
            "The test fails whenever outbound network access is blocked: it depends on "
            "a live network call. That makes it hostage to DNS, rate limits, and remote "
            "outages. Stub the call, or mark the test as requiring network."
        )


class FilesystemAxis(_Axis):
    """Dependence on ambient filesystem state (HOME, tmp, cwd leftovers)."""

    id = "fs"
    display = "filesystem-state dependent"

    def control(self) -> AxisValue:
        # chdir stays False: changing the working directory would break
        # repo-relative fixture paths and manufacture unrelated failures.
        return AxisValue(
            axis=self.id,
            value="isolate",
            sandbox={"fs": "isolate", "chdir": False},
            label="filesystem isolated",
        )

    def provocations(self) -> list[AxisValue]:
        return [self.control()]

    def explain(self, value: str) -> str:
        return (
            "The test fails when HOME and the temp directory are redirected to a clean "
            "isolated tree: it depends on ambient filesystem state — a cached file, a "
            "dotfile, or a leftover artifact from another test."
        )


class HashseedAxis(_Axis):
    """Hash randomization: dict/set iteration order leaking into behavior.

    PYTHONHASHSEED is fixed at interpreter start, which is exactly why every
    diagnostic run is a fresh subprocess — this axis cannot be varied in-process.
    """

    id = "hashseed"
    display = "hash-order dependent (PYTHONHASHSEED)"
    has_control = False

    def control(self) -> None:
        return None

    def provocations(self) -> list[AxisValue]:
        return [self._value(seed) for seed in ("0", "1", "2")] + self.extra_values(
            2, {"0", "1", "2"}
        )

    def extra_values(self, count: int, exclude: set[str]) -> list[AxisValue]:
        rng = random.Random()
        out: list[AxisValue] = []
        while len(out) < count:
            candidate = str(rng.randrange(3, 2**32 - 1))
            if candidate not in exclude:
                exclude.add(candidate)
                out.append(self._value(candidate))
        return out

    def _value(self, seed: str) -> AxisValue:
        return AxisValue(
            axis=self.id,
            value=seed,
            env={"PYTHONHASHSEED": seed},
            label=f"PYTHONHASHSEED={seed}",
        )

    def explain(self, value: str) -> str:
        return (
            "The test's outcome depends on Python's hash randomization: dict/set "
            "iteration order (or hash-based ordering) leaks into an assertion or into "
            f"order-sensitive logic. PYTHONHASHSEED={value} pins the order that "
            "triggers the failure."
        )


TIME = TimeAxis()
RNG = RngAxis()
NETWORK = NetworkAxis()
FS = FilesystemAxis()
HASHSEED = HashseedAxis()

# Sweep order is expected-yield first: hash order and time dominate the
# research taxonomy of Python flakes, and both are cheap to provoke.
ALL_AXES: tuple[_Axis, ...] = (HASHSEED, TIME, RNG, NETWORK, FS)
AXES_BY_ID = {axis.id: axis for axis in ALL_AXES}


def merge(values: list[AxisValue]) -> tuple[dict | None, dict]:
    """Merge axis values into (sandbox kwargs or None, child env deltas).

    Returns ``None`` for the sandbox when no hermetic axis participates, so
    unperturbed runs never enter a Sandbox at all.
    """
    sandbox: dict = {}
    env: dict = {}
    for value in values:
        sandbox.update(value.sandbox)
        env.update(value.env)
    if not sandbox:
        return None, env
    merged = dict(SANDBOX_OFF)
    merged.update(sandbox)
    return merged, env


def repro_command(
    nodeid: str,
    blob: str | None,
    hashseed: str | None,
    platform: str = sys.platform,
    neutralize_randomly: bool = False,
) -> str:
    """The copy-pasteable command that reproduces a diagnosed failure.

    hermetic's own CLI cannot express a clock/network/fs configuration, so any
    non-hashseed axis travels as a --doctor-repro blob. For an rng repro the
    command disables per-test reseeders (pytest-randomly), which would
    otherwise override the pinned seed; `-p no:<name>` is harmless when the
    plugin isn't installed.
    """
    tail = ["-p", "no:randomly", "-p", "no:random_order"] if neutralize_randomly else []
    if platform == "win32":
        parts = ["python", "-m", "pytest", _ps_quote(nodeid)]
        if blob:
            parts.append(f"--doctor-repro={blob}")
        command = " ".join(parts + tail)
        if hashseed is not None:
            command = f"$env:PYTHONHASHSEED='{hashseed}'; " + command
        return command
    # Parametrized nodeids routinely contain shell metacharacters.
    parts = ["pytest", shlex.quote(nodeid)]
    if blob:
        parts.append(f"--doctor-repro={blob}")
    command = " ".join(parts + tail)
    if hashseed is not None:
        command = f"PYTHONHASHSEED={hashseed} " + command
    return command


def order_repro_command(nodeids: list[str], blob: str, platform: str = sys.platform) -> str:
    """Reproduce a test-order failure: run these tests, in this order.

    The blob makes the repro plugin enforce the order regardless of how pytest
    would otherwise sort them (definition order within a file can override the
    order they are named on the command line).
    """
    quote = _ps_quote if platform == "win32" else shlex.quote
    listed = " ".join(quote(nodeid) for nodeid in nodeids)
    return f"pytest {listed} --doctor-repro={blob}"
