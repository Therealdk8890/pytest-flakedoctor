"""The diagnosis loop.

Delta-debugging over axes of nondeterminism, then bisection over values, then
a statistical verification gate and a counterfactual check.

Two symmetric moves drive it. **Control** an axis (determinize it): if the
flakiness disappears when only that axis is controlled, the flake lives there.
**Provoke** an axis (pin an adversarial value): if failure becomes certain,
that value is the reproduction. Every run is classified by outcome *and*
failure fingerprint, so a perturbation that breaks the test differently than
the observed flake never earns causal credit.

The engine takes the runner as a plain callable, so the loop logic is
unit-testable without spawning subprocesses.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from ._axes import ALL_AXES, HASHSEED, AxisValue, merge, order_repro_command, repro_command
from ._fingerprint import Fingerprint
from ._repro import Repro, current_timezone, describe_timezone
from ._runner import ProbeConfig, RunRecord
from ._stats import clopper_pearson_lower, elevation_pvalue, runs_needed_to_stabilize

RunFn = Callable[[ProbeConfig, float], RunRecord]

# Verdict codes are part of the flakedoctor-report v1 contract (additive-only).
NOT_FLAKY = "not-flaky"
DETERMINISTIC = "deterministic-failure"
FLAKY_UNATTRIBUTED = "flaky-unattributed"
HANGS = "hang"
SKIPPED = "skipped"
USAGE_ERROR = "usage-error"
CHILD_ERROR = "child-error"
INCOMPLETE = "incomplete"


def flaky_verdict(axis_id: str) -> str:
    return f"flaky-{axis_id}"


FIRST_RUN_TIMEOUT_CAP = 120.0
MIN_RUN_TIMEOUT = 30.0
TIMEOUT_MULTIPLIER = 5.0
# A run must be allowed at least this multiple of the observed duration, or we
# stop instead: a run killed by a budget-shrunken timeout would be recorded as
# a hang, i.e. an entirely fabricated failure.
TIMEOUT_SAFETY_MARGIN = 1.5


@dataclass
class DoctorSettings:
    runs: int = 10
    budget: float = 300.0
    control_runs: int = 5
    verify_runs: int = 10
    counterfactual_runs: int = 5
    sweep_reps: int = 2
    max_verified_candidates: int = 2
    thorough: bool = False


@dataclass
class EvidenceRow:
    label: str
    runs: int
    failed: int
    note: str = ""


@dataclass
class Diagnosis:
    nodeid: str
    verdict: str
    claim: str  # "observed" | "latent" | "none"
    headline: str
    explanation: str
    evidence: list[EvidenceRow]
    repro_command: str | None
    repro: dict | None
    warnings: list[str]
    stats: dict
    elapsed: float
    total_runs: int


class _BudgetExhausted(Exception):
    pass


class _Session:
    """Owns the run budget, timeouts, counters, and the evidence table."""

    def __init__(self, run: RunFn, settings: DoctorSettings, progress: Callable[[str], None]):
        self.run_fn = run
        self.settings = settings
        self.progress = progress
        self.start = time.monotonic()
        self.total_runs = 0
        self.exhausted = False
        self.evidence: list[EvidenceRow] = []
        self.warnings: list[str] = []
        self.display_nodeid: str | None = None
        self.display: Callable[[str], str] = lambda nodeid: nodeid
        self.hermetic_available = True
        self._first_duration: float | None = None

    def remaining(self) -> float:
        return self.settings.budget - (time.monotonic() - self.start)

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def _timeout(self, floor: float | None = None) -> float:
        """Time to allow one run.

        `floor` is the minimum a run is expected to need — used by the order
        phase, where a run executes a whole prefix of tests and would be
        wrongly killed as a hang under the victim-alone timeout.
        """
        remaining = self.remaining()
        need = floor if floor is not None else self._first_duration
        if need is None:
            if remaining < 2.0:
                raise _BudgetExhausted
            return min(remaining, FIRST_RUN_TIMEOUT_CAP)
        if remaining < need * TIMEOUT_SAFETY_MARGIN:
            raise _BudgetExhausted
        generous = max(MIN_RUN_TIMEOUT, TIMEOUT_MULTIPLIER * need)
        return min(remaining, generous)

    def execute(self, probe: ProbeConfig, timeout_floor: float | None = None) -> RunRecord:
        record = self.run_fn(probe, self._timeout(timeout_floor))
        self.total_runs += 1
        if self._first_duration is None:
            self._first_duration = max(record.duration, 0.01)
        if record.probe_error and "hermetic unavailable" in record.probe_error:
            self.hermetic_available = False
        # Harvested centrally so every phase surfaces them — an axis that stood
        # itself down during the control sweep must say so too, not only during
        # provocation.
        for note in record.probe_notes:
            if note not in self.warnings:
                self.warnings.append(note)
        return record

    def series(
        self, probe: ProbeConfig, count: int, phase_name: str, timeout_floor: float | None = None
    ) -> list[RunRecord]:
        """Run up to `count` fresh subprocesses of one config; partial on budget end."""
        records: list[RunRecord] = []
        for index in range(count):
            try:
                records.append(self.execute(probe, timeout_floor))
            except _BudgetExhausted:
                self.note_exhausted()
                break
            failed = sum(1 for r in records if r.failed)
            self.progress(f"{phase_name} {index + 1}/{count} ({failed} failed)")
        return records

    def note_exhausted(self) -> None:
        if not self.exhausted:
            self.exhausted = True
            self.warnings.append(
                f"budget exhausted after {self.total_runs} runs — later phases were "
                "skipped or truncated; raise --doctor-budget for a full pass"
            )

    def avg_duration(self) -> float:
        return self._first_duration or 1.0

    def can_afford(self, runs: int) -> bool:
        return self.avg_duration() * runs <= self.remaining()


@dataclass
class _Baseline:
    completed: list[RunRecord]
    excluded: list[RunRecord]
    n: int
    f_count: int
    dominant_fp: Fingerprint | None
    stats: dict

    @property
    def quiet(self) -> bool:
        return self.f_count == 0


def _probe_for(nodeid: str, values: list[AxisValue], label: str) -> ProbeConfig:
    sandbox, env = merge(values)
    # Baseline and any config without a hashseed value must strip an inherited
    # PYTHONHASHSEED so each child randomizes independently.
    env.setdefault("PYTHONHASHSEED", None)
    return ProbeConfig(nodeids=(nodeid,), env=env, label=label, sandbox=sandbox)


def _partition(records: list[RunRecord], axis_id: str | None = None) -> tuple[list, list, list, list]:
    """Split runs into (real failures, doctor-origin failures, passes, non-evidence).

    A doctor-origin failure means the perturbation itself broke the test (a
    blocked socket, a spin-detector trip, a hang under a virtual clock) —
    evidence about our tooling, never about the user's flake.

    When `axis_id` names a hermetic axis, a run whose probe reports it did not
    actually apply that axis is non-evidence: an axis that stood itself down
    must not collect credit for what the test did without it.
    """
    real, doctor, passes, non_evidence = [], [], [], []
    hermetic_axes = {"time", "rng", "network", "fs"}
    for record in records:
        stood_down = (
            axis_id in hermetic_axes
            and record.applied_axes is not None
            and axis_id not in record.applied_axes
        )
        if not record.ran or not record.perturbation_applied or stood_down:
            non_evidence.append(record)
        elif record.failed and record.doctor_origin:
            doctor.append(record)
        elif record.failed:
            real.append(record)
        else:
            passes.append(record)
    return real, doctor, passes, non_evidence


def _evidence_note(doctor: list, non_evidence: list, extra: str = "") -> str:
    notes = []
    if extra:
        notes.append(extra)
    if doctor:
        notes.append(f"{len(doctor)} perturbation-induced")
    if non_evidence:
        notes.append(f"{len(non_evidence)} not applicable")
    return ", ".join(notes)


def diagnose(
    nodeid: str,
    run: RunFn,
    settings: DoctorSettings | None = None,
    progress: Callable[[str], None] = lambda msg: None,
    display_nodeid: str | None = None,
    prefix: list[str] | None = None,
    display: Callable[[str], str] | None = None,
) -> Diagnosis:
    """Diagnose `nodeid` (rootdir-relative, as children receive it).

    `display_nodeid` is the same test spelled relative to the user's shell,
    used only when printing the repro command so it can be pasted as-is.

    `prefix` is the ordered list of tests that ran before the victim in the
    suite (from suite-mode collection); it enables the test-order axis, which
    finds a *polluter* test that makes the victim fail. None (single-test
    invocation) means order dependence cannot be checked.
    """
    settings = settings or DoctorSettings()
    session = _Session(run, settings, progress)
    if display is not None:
        session.display = display
    session.display_nodeid = display_nodeid or session.display(nodeid)
    try:
        return _diagnose(nodeid, session, prefix or [])
    except _BudgetExhausted:  # belt-and-suspenders; series() normally absorbs it
        session.note_exhausted()
        return _incomplete(session, nodeid)


def _diagnose(nodeid: str, session: _Session, prefix: list[str]) -> Diagnosis:
    if os.environ.get("PYTHONHASHSEED") is not None:
        session.warnings.append(
            f"PYTHONHASHSEED={os.environ['PYTHONHASHSEED']} is pinned in the parent "
            "environment; baseline runs strip it so each child gets fresh randomization"
        )

    baseline = _phase_baseline(nodeid, session)
    if isinstance(baseline, Diagnosis):
        return baseline

    # Test-order axis: when the victim passes alone but a suite prefix precedes
    # it, a polluter is the likeliest cause — check before the per-test axes.
    if prefix and baseline.quiet and not session.exhausted:
        order = _phase_order(nodeid, session, baseline, prefix)
        if order is not None:
            return order

    # Thread-interleaving axis: if the test uses threads, search for a schedule
    # that makes it fail. Runs before the hermetic sweep (a race is a distinct,
    # highly actionable cause) and only when threads were actually observed.
    if not session.exhausted:
        interleave = _phase_interleave(nodeid, session, baseline)
        if interleave is not None:
            return interleave

    stabilizer = _phase_control(nodeid, session, baseline)
    passers: list[AxisValue] = []
    candidates = _peekable(_iter_candidates(nodeid, session, baseline, stabilizer, passers))

    if not candidates.has_next():
        if session.exhausted:
            return _incomplete(session, nodeid, stats={"baseline": baseline.stats})
        if baseline.quiet:
            return _quiet_not_flaky(session, nodeid, baseline)
        return _unattributed(session, nodeid, baseline, stabilizer)
    if session.exhausted:
        value = candidates.peek()[0]
        return _incomplete(
            session,
            nodeid,
            detail=f"{value.described()} failed in the sweep but could not be verified.",
            stats={"baseline": baseline.stats},
        )
    return _phase_verify(nodeid, session, baseline, candidates, passers)


class _peekable:
    """One-item lookahead over the lazy candidate stream."""

    def __init__(self, iterator):
        self._iterator = iter(iterator)
        self._buffer: list = []

    def has_next(self) -> bool:
        if self._buffer:
            return True
        try:
            self._buffer.append(next(self._iterator))
        except StopIteration:
            return False
        return True

    def peek(self):
        return self._buffer[0]

    def __iter__(self):
        while True:
            if self._buffer:
                yield self._buffer.pop(0)
                continue
            try:
                yield next(self._iterator)
            except StopIteration:
                return


# ---------------------------------------------------------------- phase 0

def _phase_baseline(nodeid: str, session: _Session) -> _Baseline | Diagnosis:
    settings = session.settings
    probe = _probe_for(nodeid, [], "baseline")
    records: list[RunRecord] = []
    for index in range(settings.runs):
        try:
            record = session.execute(probe)
        except _BudgetExhausted:
            session.note_exhausted()
            break
        records.append(record)
        session.progress(
            f"baseline {index + 1}/{settings.runs} "
            f"({sum(1 for r in records if r.failed)} failed)"
        )
        # A misconfigured run repeats identically — don't burn the whole budget.
        if index == 0 and record.outcome in ("usage-error", "no-tests", "skipped"):
            break
    if not records:
        return _incomplete(session, nodeid)

    first = records[0]
    if first.outcome in ("usage-error", "no-tests"):
        return _finish(
            session,
            nodeid,
            verdict=USAGE_ERROR,
            claim="none",
            headline="could not run the test",
            explanation=(
                f"pytest exited with code {first.exit_code} on the first run — the child "
                "could not run the selected test. Child output:\n" + (first.detail or "").strip()
            ),
            stats={},
        )
    if first.outcome == "skipped":
        return _finish(
            session,
            nodeid,
            verdict=SKIPPED,
            claim="none",
            headline="test is skipped — nothing to diagnose",
            explanation="The test was skipped in the diagnostic environment.",
            stats={},
        )

    completed = [r for r in records if r.ran]
    excluded = [r for r in records if not r.ran]
    if excluded and not completed:
        return _finish(
            session,
            nodeid,
            verdict=CHILD_ERROR,
            claim="none",
            headline="diagnostic subprocesses failed",
            explanation=(
                "Every baseline child run errored outside the test itself. Last child output:\n"
                + (excluded[-1].detail or "")
            ),
            stats={},
        )
    if excluded:
        summary = ", ".join(
            f"{count} {name}"
            for name, count in sorted(Counter(r.outcome for r in excluded).items())
        )
        session.warnings.append(
            f"{len(excluded)} of {len(records)} baseline run(s) did not run the test "
            f"({summary}) and were excluded from the statistics"
        )
    if not completed:
        return _incomplete(session, nodeid)

    failures = [r for r in completed if r.failed]
    hangs = [r for r in completed if r.outcome == "hang"]
    n, f_count = len(completed), len(failures)

    fp_counter: Counter = Counter(r.fingerprint.key() for r in failures if r.fingerprint)
    fingerprints = {r.fingerprint.key(): r.fingerprint for r in failures if r.fingerprint}
    dominant = fingerprints[fp_counter.most_common(1)[0][0]] if fp_counter else None
    if len(fp_counter) > 1:
        session.warnings.append(
            f"{len(fp_counter)} distinct failure modes observed in the baseline; "
            f"diagnosing the most frequent: {dominant.describe()}"
        )

    notes = []
    if hangs:
        notes.append(f"{len(hangs)} hang(s)")
    if excluded:
        notes.append(f"{len(excluded)} excluded")
    session.evidence.append(
        EvidenceRow("baseline (isolated, no perturbation)", n, f_count, ", ".join(notes))
    )

    stats = {
        "runs": n,
        "attempted": len(records),
        "excluded": len(excluded),
        "failed": f_count,
        "failure_rate": f_count / n,
        "distinct_fingerprints": len(fp_counter),
        "dominant_fingerprint": dominant.describe() if dominant else None,
    }

    if all(r.outcome == "hang" for r in completed):
        session.warnings.append(
            "every run timed out — if the test is merely slow, raise --doctor-budget"
        )
        return _finish(
            session,
            nodeid,
            verdict=HANGS,
            claim="none",
            headline=f"test hangs ({n}/{n} runs timed out)",
            explanation=(
                f"All {n} isolated runs exceeded the per-run timeout and were killed. "
                "This is a hang, not an ordinary flake; look for waits on external "
                "services, deadlocks, or unbounded polling loops."
            ),
            stats={"baseline": stats},
        )
    if f_count == n:
        modes = (
            "a single failure mode"
            if len(fp_counter) <= 1
            else f"{len(fp_counter)} distinct failure modes"
        )
        return _finish(
            session,
            nodeid,
            verdict=DETERMINISTIC,
            claim="none",
            headline=f"deterministic failure — fails {n}/{n} in isolation",
            explanation=(
                f"The test failed every isolated run with {modes}. That is a plain bug "
                "(or an environment problem), not a flake. "
                + (dominant.describe() if dominant else "")
            ),
            stats={"baseline": stats},
        )
    return _Baseline(completed, excluded, n, f_count, dominant, stats)


# ---------------------------------------------------------- order axis

# A run of a whole prefix legitimately takes far longer than the victim alone;
# these bound how many prefix-runs the phase will spend and how it confirms.
_ORDER_CONFIRM_RUNS = 3
_ORDER_MAX_BISECT_RUNS = 40


def _order_probe(nodeid: str, prefix: list[str], label: str) -> ProbeConfig:
    """Run `prefix` then the victim, in that exact order, unperturbed."""
    return ProbeConfig(
        nodeids=(*prefix, nodeid),
        env={"PYTHONHASHSEED": None},
        label=label,
        sandbox=None,
    )


def _victim_outcome(record: RunRecord, victim: str) -> str:
    """What the VICTIM did in a multi-test run: pass | fail | absent | other.

    Classification is driven by the victim's own recorded phases, never by the
    subprocess exit code — a failing prefix test makes pytest exit non-zero
    even when the victim passed. If a prefix test hung or crashed, the victim
    never ran and its nodeid is not the last one recorded, so this returns
    "absent": a broken prefix is never mistaken for the victim failing.
    """
    if record.last_nodeid != victim:
        return "absent"
    if record.doctor_origin:
        return "other"
    setup = record.phases.get("setup")
    call = record.phases.get("call")
    if setup == "failed" or call == "failed":
        return "fail"
    if call == "passed":
        return "pass"
    if setup == "skipped" or call == "skipped":
        return "skip"
    return "other"


def _order_failures(records: list[RunRecord], victim: str) -> list[RunRecord]:
    return [r for r in records if _victim_outcome(r, victim) == "fail"]


def _order_passes(records: list[RunRecord], victim: str) -> list[RunRecord]:
    return [r for r in records if _victim_outcome(r, victim) == "pass"]


def _order_reproduces(records: list[RunRecord], victim: str, dominant: Fingerprint | None) -> bool:
    """A prefix reproduces if the victim itself fails the observed way in a majority."""
    real = _order_failures(records, victim)
    if not real:
        return False
    if dominant is not None:
        real = [r for r in real if r.fingerprint and r.fingerprint.key() == dominant.key()]
    return len(real) * 2 > len(records)


def _phase_order(nodeid: str, session: _Session, baseline: _Baseline, prefix: list[str]):
    """Find a polluter: a test that, run before the victim, makes it fail.

    Precondition (checked by the caller): the victim passes alone and a suite
    prefix precedes it. Confirms [prefix + victim] reproduces, then bisects the
    prefix to a minimal polluter and verifies it.
    """
    # 1. Does running the whole prefix first reproduce the failure? This is the
    #    first multi-test run and its duration cannot be known in advance, so it
    #    gets headroom estimated from the prefix length — the victim-alone
    #    timeout would kill a long-but-innocent prefix and fabricate a hang.
    prefix_floor = max(MIN_RUN_TIMEOUT, session.avg_duration() * (len(prefix) + 1))
    confirm = session.series(
        _order_probe(nodeid, prefix, "order: after full suite prefix"),
        _ORDER_CONFIRM_RUNS,
        "order: after full prefix",
        timeout_floor=prefix_floor,
    )
    if not confirm:
        return None
    # Size later order runs from what the full prefix actually took.
    floor = max(prefix_floor, *(r.duration for r in confirm))
    victim_fails = _order_failures(confirm, nodeid)
    fingerprints = Counter(r.fingerprint.key() for r in victim_fails if r.fingerprint)
    dominant = None
    if fingerprints:
        by_key = {r.fingerprint.key(): r.fingerprint for r in victim_fails if r.fingerprint}
        dominant = by_key[fingerprints.most_common(1)[0][0]]
    session.evidence.append(
        EvidenceRow(
            f"after full suite prefix ({len(prefix)} tests)", len(confirm), len(victim_fails)
        )
    )
    if not _order_reproduces(confirm, nodeid, dominant):
        # The victim failed in the suite but not after this prefix alone. The
        # trigger runs after it, or needs parallel (xdist) peers, or the whole
        # prefix is merely too slow to finish — none of which serial order
        # bisection can isolate.
        session.warnings.append(
            "the victim failed in the suite but running the tests before it (in order) did "
            "not reproduce that — the trigger may be a test that runs after it, a parallel "
            "xdist worker, or a prefix too slow to run in the budget; order diagnosis "
            "cannot isolate those"
        )
        return None

    # 2. Bisect the prefix to a minimal reproducing polluter set.
    def reproduces(sublist: list[str]) -> bool:
        if session.exhausted or session.total_runs - start_runs > _ORDER_MAX_BISECT_RUNS:
            raise _BudgetExhausted
        records = session.series(
            _order_probe(nodeid, sublist, f"order: bisect ({len(sublist)} before victim)"),
            1 if len(sublist) > 1 else _ORDER_CONFIRM_RUNS,
            f"order: bisect {len(sublist)} tests",
            timeout_floor=floor,
        )
        return bool(records) and _order_reproduces(records, nodeid, dominant)

    start_runs = session.total_runs
    try:
        polluters = _bisect_polluter(prefix, reproduces)
    except _BudgetExhausted:
        session.note_exhausted()
        polluters = None
    if not polluters:
        session.evidence.append(
            EvidenceRow("polluter bisection", 0, 0, "could not narrow the prefix")
        )
        return _order_verdict(
            nodeid, session, baseline, prefix, prefix, dominant, verified=None, confirmed=(0, 0)
        )

    # 3. Verify the minimal polluter set makes the victim fail n/n.
    verify = session.series(
        _order_probe(nodeid, polluters, "order: verify polluter"),
        session.settings.verify_runs,
        "order: verify",
        timeout_floor=floor,
    )
    victim_fails_v = _order_failures(verify, nodeid)
    all_failed = bool(verify) and len(victim_fails_v) == len(verify)
    session.evidence.append(
        EvidenceRow(
            f"VERIFY: after {_names(polluters)}",
            len(verify),
            len(victim_fails_v),
            "✓ deterministic" if all_failed else "elevates failure rate",
        )
    )

    # 4. Counterfactual: the victim must pass WITHOUT the polluter. This rules
    #    out an independent latent flake (hashseed/rng) that a quiet baseline
    #    happened to miss — that would fail regardless of what precedes it.
    counterfactual = session.series(
        _probe_for(nodeid, [], "order: counterfactual (victim alone)"),
        session.settings.counterfactual_runs,
        "order: counterfactual",
    )
    cf_fails = [r for r in counterfactual if r.failed and not r.doctor_origin]
    if counterfactual and cf_fails:
        session.evidence.append(
            EvidenceRow(
                "counterfactual: victim alone", len(counterfactual), len(cf_fails), "still fails!"
            )
        )
        session.warnings.append(
            "the victim also fails on its own in the counterfactual, so this is not purely a "
            "test-order dependency — diagnosing it as an independent flake instead"
        )
        return None  # fall through to the per-test axes
    if counterfactual:
        session.evidence.append(
            EvidenceRow("counterfactual: victim alone", len(counterfactual), 0, "✓ passes")
        )
    return _order_verdict(
        nodeid, session, baseline, prefix, polluters, dominant,
        verified=all_failed, confirmed=(len(victim_fails_v), len(verify)),
    )


def _bisect_polluter(prefix: list[str], reproduces) -> list[str] | None:
    """Delta-debug `prefix` to a minimal sublist that still reproduces.

    Binary search finds a single polluter in ~log2(n) runs. When neither half
    reproduces alone the polluter needs elements from both (an interaction);
    this returns the smallest reproducing window it proved, never claiming a
    reduction it did not confirm.
    """
    current = list(prefix)
    while len(current) > 1:
        mid = len(current) // 2
        first, second = current[:mid], current[mid:]
        # Test the half nearer the victim first: state-mutating polluters tend
        # to be the most recent thing to have run.
        if reproduces(second):
            current = second
        elif reproduces(first):
            current = first
        else:
            break  # interaction across the split; stop at the proven window
    return current


def _names(nodeids: list[str], limit: int = 3) -> str:
    if len(nodeids) == 1:
        return nodeids[0]
    if len(nodeids) <= limit:
        return ", ".join(nodeids)
    return f"{nodeids[0]} and {len(nodeids) - 1} other tests"


def _order_verdict(
    nodeid: str,
    session: _Session,
    baseline: _Baseline,
    prefix: list[str],
    polluters: list[str],
    dominant: Fingerprint | None,
    verified: bool | None,
    confirmed: tuple[int, int] = (0, 0),
) -> Diagnosis:
    display_polluters = [session.display(p) for p in polluters]
    display_victim = session.display_nodeid or nodeid
    minimal = len(polluters) < len(prefix)

    if verified:
        headline = "test-order dependent"
        confidence = "fails every run in this order"
    elif verified is False:
        headline = "test-order dependent — elevates failure rate"
        confidence = "fails more often in this order, though not every run"
    else:
        headline = "test-order dependent — polluter not isolated"
        confidence = "reproduces after the suite prefix, but the polluter could not be narrowed"

    if len(polluters) == 1:
        who = display_polluters[0]
        culprit = f"{who} leaves behind state"
    elif minimal:
        shown = ", ".join(display_polluters[:5]) + ("" if len(display_polluters) <= 5 else ", …")
        who = f"these {len(polluters)} tests ({shown})"
        culprit = "one of them leaves behind state"
    else:
        who = f"the {len(polluters)} tests collected before it"
        culprit = "one of them leaves behind state"
    explanation = (
        f"The test passes {baseline.n}/{baseline.n} times on its own, but fails when run "
        f"after {who} — {confidence}. This is a test-order dependency: {culprit} "
        "(a global, a cached import, a file, a patched attribute, a database row) that the "
        "victim depends on being absent. Fix whichever test does not clean up after "
        "itself, or make the victim independent of that state."
    )

    repro = Repro(
        values=[],
        nodeid=nodeid,
        order=[*polluters, nodeid],
        tool=_tool_version(),
        fingerprint=(dominant.digest() if dominant else ""),
        confirm=confirmed,  # (victim failures, runs) at verification — the DID-NOT-REPRODUCE count
    )
    blob = repro.encode()
    command = order_repro_command([*display_polluters, display_victim], blob)
    note = "fails every run in this order" if verified else "not fully deterministic"
    repro_dict = {
        "axis": "order",
        "value": polluters[0] if len(polluters) == 1 else f"{len(polluters)} tests",
        "polluters": polluters,
        "note": note,
        "blob": blob,
        "hashseed": None,
        "tz": None,
    }
    stats = {
        "baseline": baseline.stats,
        "order": {
            "prefix_len": len(prefix),
            "polluters": polluters,
            "minimal": minimal,
            "verified": verified,
        },
    }
    return _finish(
        session,
        nodeid,
        verdict=flaky_verdict("order"),
        claim="observed",
        headline=headline,
        explanation=explanation,
        stats=stats,
        repro_command_override=command,
        repro_dict_override=repro_dict,
    )


# ------------------------------------------------------- interleave axis

# The strategies, cheapest first; DFS last because exhausted=True is the
# strongest bounded proof of "no failing schedule under the modelled primitives".
_INTERLEAVE_STRATEGIES = ("random", "pct", "dfs")
_INTERLEAVE_ITERATIONS = 200
_INTERLEAVE_PER_SCHEDULE_TIMEOUT = 10.0
# A generous floor so the search child is bounded by the parent kill-timeout,
# not by the victim-alone duration (explore internally re-runs the test N times).
_INTERLEAVE_SEARCH_FLOOR = 24.0
_INTERLEAVE_VERIFY_RUNS = 3


def _interleave_probe(nodeid: str, label: str, interleave: dict) -> ProbeConfig:
    return ProbeConfig(
        nodeids=(nodeid,),
        env={"PYTHONHASHSEED": None},
        label=label,
        interleave=interleave,
    )


def _phase_interleave(nodeid: str, session: _Session, baseline: _Baseline):
    """Search for a thread schedule that makes the test fail.

    Gated on: real thread use observed in the baseline, a synchronous test, and
    interleave-test importable in the child (Python >=3.12, installed). A found
    schedule is verified by fresh-subprocess replays before any claim is made.
    """
    completed = baseline.completed
    thread_starts = max(
        (r.thread_starts for r in completed if r.thread_starts is not None), default=0
    )
    is_async = any(r.is_async_for_interleave for r in completed)
    is_unittest = any(r.is_unittest for r in completed)
    # Gate on a QUIET baseline: a race passes when run alone, so this is the
    # axis's domain — and it avoids mistaking a mixed baseline caused by an rng
    # or hash-order flake for an "observed" race. Async and unittest tests
    # cannot be driven as a zero-arg model. Fewer than two threads: nothing to
    # interleave.
    if is_async or is_unittest or thread_starts < 2 or not baseline.quiet:
        return None

    # 1. Search: cheap strategies first, stop at the first found race.
    found_meta = None
    search_floor = _INTERLEAVE_SEARCH_FLOOR
    blocked_note = ""
    for strategy in _INTERLEAVE_STRATEGIES:
        if session.exhausted:
            break
        cfg = {
            "mode": "explore",
            "strategy": strategy,
            "iterations": _INTERLEAVE_ITERATIONS,
            "seed": 0,
            "per_schedule_timeout": _INTERLEAVE_PER_SCHEDULE_TIMEOUT,
        }
        try:
            record = session.execute(
                _interleave_probe(nodeid, f"interleave: explore ({strategy})", cfg),
                timeout_floor=search_floor,
            )
        except _BudgetExhausted:
            session.note_exhausted()
            break
        session.progress(f"interleave: explore {strategy}")
        # Only a completed (non-hang) search tells us how long a search takes;
        # a killed child's duration is the kill-timeout, which must not become
        # the floor and starve the rest of the diagnosis.
        if record.outcome != "hang":
            search_floor = max(search_floor, record.duration)
        meta = record.interleave or {}
        if meta.get("error"):
            # interleave-test not importable (Python <3.12 or not installed), or
            # a scheduler fault — the axis is unavailable, not "no race".
            session.warnings.append(
                "thread-interleaving axis unavailable: "
                + meta["error"]
                + " (install the `interleave` extra on Python >=3.12 to enable it)"
            )
            return None
        if meta.get("skipped"):
            return None  # async/unittest: the driver declined
        if meta.get("blocked"):
            # A schedule blocked on something the scheduler cannot model (I/O, a
            # long computation) — inconclusive, not a race.
            blocked_note = meta["blocked"]
            continue
        if record.outcome == "hang":
            # The explore child itself was killed by the parent timeout — do not
            # exhaust the whole diagnosis over it; fall through to the other axes.
            blocked_note = "the interleaving search exceeded its time budget"
            continue
        if meta.get("found"):
            found_meta = meta
            found_record = record
            break

    if found_meta is None:
        if blocked_note:
            session.evidence.append(
                EvidenceRow(f"interleave search ({thread_starts} threads)", 1, 0, "inconclusive")
            )
            session.warnings.append(
                "the thread-interleaving search was inconclusive: " + blocked_note + " — the "
                "test blocks on something the scheduler cannot model, so no race is claimed"
            )
            return None
        if session.exhausted:
            return None  # fall through; the budget note is already recorded
        # A genuine, bounded negative — but never phrased as "no race exists".
        session.evidence.append(
            EvidenceRow(
                f"interleave search ({thread_starts} threads)",
                len(_INTERLEAVE_STRATEGIES),
                0,
                "no failing schedule under the modelled primitives",
            )
        )
        session.warnings.append(
            "no failing thread interleaving was found under the primitives the scheduler "
            "can model — races in threads or locks created at import time (not in the test "
            "body), in thread-pool internals, in C code, or in state held by a fixture "
            "rather than created in the test are not covered"
        )
        return None

    session.evidence.append(EvidenceRow("interleave: schedule found", 1, 1, "✓ a race"))

    # 2. Verify: replay the found schedule in fresh subprocesses. Replay is
    # deterministic, so a real race reproduces every time.
    schedule = found_meta.get("schedule")
    replay_cfg = {
        "mode": "replay",
        "schedule": schedule,
        "per_schedule_timeout": _INTERLEAVE_PER_SCHEDULE_TIMEOUT,
    }
    verify = session.series(
        _interleave_probe(nodeid, "interleave: replay", replay_cfg),
        _INTERLEAVE_VERIFY_RUNS,
        "interleave: replay",
        timeout_floor=search_floor,
    )
    reproduced = [r for r in verify if r.failed and not r.doctor_origin]
    all_reproduced = bool(verify) and len(reproduced) == len(verify)
    session.evidence.append(
        EvidenceRow(
            "VERIFY: replay the schedule",
            len(verify),
            len(reproduced),
            "✓ deterministic replay" if all_reproduced else "replay did not reproduce",
        )
    )
    if not all_reproduced:
        session.warnings.append(
            "a failing schedule was found but did not replay deterministically — not "
            "claiming a race (this can happen when the failure also depends on real-time "
            "or external state the scheduler does not control)"
        )
        return None

    return _interleave_verdict(
        nodeid, session, baseline, found_record, found_meta, len(reproduced), len(verify)
    )


def _interleave_verdict(
    nodeid: str,
    session: _Session,
    baseline: _Baseline,
    record: RunRecord,
    meta: dict,
    reproduced: int,
    runs: int,
) -> Diagnosis:
    fingerprint = record.fingerprint
    is_deadlock = meta.get("kind") == "deadlock"
    # The axis runs only on a quiet baseline (the test passes when run alone),
    # so the claim is always latent: a real race the thread scheduler surfaced.
    claim = "latent"

    if is_deadlock:
        headline = "thread deadlock"
        what = "the threads deadlock under a specific ordering"
        cause = meta.get("detail") or "two threads each wait on a lock the other holds"
        remedy = (
            "Acquire the locks in a consistent order everywhere, or use a single lock, "
            "so the cyclic wait cannot form."
        )
    else:
        headline = "race condition (thread interleaving)"
        what = "a specific thread interleaving makes it fail"
        cause = (
            fingerprint.describe()
            if fingerprint
            else "shared state is read and written without synchronization"
        )
        remedy = (
            "Add synchronization (a lock around the shared read-modify-write, or an atomic "
            "operation) so the result no longer depends on thread ordering."
        )
    explanation = (
        f"The test passes {baseline.n}/{baseline.n} times on its own, but {what} — a real "
        f"concurrency bug the thread scheduler usually hides. The found schedule reproduces "
        f"it {reproduced}/{runs} times. Cause: {cause}. " + remedy
    )

    interleave_repro = {
        "schedule": meta.get("schedule"),
        "py_exact": meta.get("py_exact", ""),
        "strategy": meta.get("strategy", "pct"),
        "granularity": meta.get("granularity", "opcode"),
    }
    repro = Repro(
        values=[],
        nodeid=nodeid,
        interleave=interleave_repro,
        tool=_tool_version(),
        fingerprint=(fingerprint.digest() if fingerprint else ""),
        confirm=(reproduced, runs),
    )
    blob = repro.encode()
    display = session.display_nodeid or nodeid
    command = repro_command(display, blob, None)
    repro_dict = {
        "axis": "interleave",
        "value": meta.get("strategy", "pct"),
        "note": f"replays {reproduced}/{runs} deterministically",
        "blob": blob,
        "hashseed": None,
        "tz": None,
        "marker": f'@pytest.mark.flakedoctor_repro("{blob}")',
    }
    stats = {
        "baseline": baseline.stats,
        "interleave": {
            "strategy": meta.get("strategy"),
            "kind": meta.get("kind"),
            "passing_schedule_seen": meta.get("passing_schedule_seen"),
            "reproduced": reproduced,
            "runs": runs,
            "py_exact": meta.get("py_exact"),
        },
    }
    return _finish(
        session,
        nodeid,
        verdict=flaky_verdict("interleave"),
        claim=claim,
        headline=headline,
        explanation=explanation,
        stats=stats,
        repro_command_override=command,
        repro_dict_override=repro_dict,
    )


# ---------------------------------------------------------------- phase 1

def _phase_control(nodeid: str, session: _Session, baseline: _Baseline):
    """Determinize axes and see whether the flakiness disappears.

    Only meaningful for a live flake: with a clean baseline there is nothing
    for a stabilizer to remove, so the phase is skipped and provocation runs
    instead.
    """
    if baseline.quiet or session.exhausted:
        return None
    controllable = [axis for axis in ALL_AXES if axis.has_control]
    runs = session.settings.control_runs
    if not session.can_afford(runs * (1 + len(controllable)) + session.settings.verify_runs):
        session.warnings.append(
            "budget-limited: control sweep skipped, going straight to provocation"
        )
        return None

    # A clean streak only means something if the baseline rate makes it
    # unlikely: at 20% failure, five passes happen a third of the time.
    needed = runs_needed_to_stabilize(baseline.f_count, baseline.n)
    conclusive = needed and runs >= needed

    full = [axis.control() for axis in controllable]
    records = session.series(
        _probe_for(nodeid, full, "control: all axes"), runs, "control all axes"
    )
    if not records:
        return None
    if not session.hermetic_available:
        session.warnings.append(
            "hermetic is not importable in the diagnostic subprocess — the time, rng, "
            "network and filesystem axes are unavailable; install hermetic-sandbox"
        )
        return None
    real, doctor, passes, non_evidence = _partition(records)
    # "Stabilized" needs positive evidence: runs that actually executed the
    # test, under a perturbation that actually applied, and all passed.
    stabilized = bool(passes) and not real and not doctor and not non_evidence
    session.evidence.append(
        EvidenceRow(
            "full control (time+rng+net+fs)",
            len(records),
            len(real),
            _evidence_note(
                doctor,
                non_evidence,
                ("← stabilizer" if conclusive else "inconclusive") if stabilized else "",
            ),
        )
    )
    if doctor:
        session.warnings.append(
            "under full control the test failed for reasons caused by the sandbox itself "
            f"({doctor[0].fingerprint.exc_type}); that is reported as not applicable "
            "rather than as your flake"
        )
    if not stabilized:
        # Still failing (or nothing informative happened) with everything
        # determinized: the cause is likely outside the covered axes, but
        # provocation may still find something, so continue.
        return None
    if not conclusive:
        session.warnings.append(
            f"the control sweep saw {len(passes)} clean runs, but at the observed baseline "
            f"rate ({baseline.f_count}/{baseline.n}) that needs {needed} to be significant "
            "— the axis attribution below is a lead, not a conclusion"
        )

    # Flakiness vanished — find which single axis was responsible.
    for axis in controllable:
        if session.exhausted or not session.can_afford(runs + session.settings.verify_runs):
            break
        control = axis.control()
        single = session.series(
            _probe_for(nodeid, [control], f"control: {control.described()}"),
            runs,
            f"control {control.described()}",
        )
        if not single:
            break
        s_real, s_doctor, s_pass, s_non = _partition(single, axis.id)
        axis_stabilized = bool(s_pass) and not s_real and not s_doctor and not s_non
        session.evidence.append(
            EvidenceRow(
                f"control: {control.described()}",
                len(single),
                len(s_real),
                _evidence_note(
                    s_doctor,
                    s_non,
                    ("← stabilizer" if conclusive else "inconclusive")
                    if axis_stabilized
                    else "",
                ),
            )
        )
        if axis_stabilized:
            return axis
    return None


# ---------------------------------------------------------------- phase 2

def _iter_candidates(nodeid: str, session: _Session, baseline: _Baseline, stabilizer, passers: list):
    """Sweep adversarial values, strongest-suspect axis first, yielding lazily.

    Lazy on purpose: the caller verifies each candidate as it arrives, so a
    candidate that fails verification resumes the sweep where it left off
    instead of ending the diagnosis.
    """
    axes = list(ALL_AXES)
    if stabilizer is not None:
        axes.remove(stabilizer)
        axes.insert(0, stabilizer)
    if not session.hermetic_available:
        axes = [axis for axis in axes if axis is HASHSEED]

    deferred: list[tuple[AxisValue, bool]] = []
    reps = session.settings.sweep_reps

    for axis in axes:
        for value in axis.provocations():
            if session.exhausted:
                return
            records = session.series(
                _probe_for(nodeid, [value], f"provoke: {value.described()}"),
                reps,
                f"provoke {value.described()}",
            )
            if not records:
                return
            real, doctor, passes, non_evidence = _partition(records, axis.id)
            matched = _all_match(real, baseline.dominant_fp)
            session.evidence.append(
                EvidenceRow(
                    f"provoke: {value.described()}",
                    len(records),
                    len(real),
                    _evidence_note(
                        doctor, non_evidence, "different failure" if real and not matched else ""
                    ),
                )
            )
            if real and len(real) == len(records):
                yield value, matched
                break  # this axis has spoken; move on
            if real:
                deferred.append((value, matched))
            elif len(passes) == len(records):
                passers.append(value)
    # Values that failed some but not all reps are weaker evidence, but a
    # partial failure still contradicts "nothing provoked it".
    for pair in sorted(deferred, key=lambda item: not item[1]):
        yield pair


# ---------------------------------------------------------------- phases 4-5

def _phase_verify(
    nodeid: str,
    session: _Session,
    baseline: _Baseline,
    candidates,
    passers: list[AxisValue],
) -> Diagnosis:
    settings = session.settings
    verify_runs = settings.verify_runs
    if not session.can_afford(verify_runs + settings.counterfactual_runs):
        verify_runs = max(5, verify_runs // 2)
        session.warnings.append("budget-limited: confidence reduced (fewer verification runs)")

    confirmed = None
    best_elevation = None
    attempts = 0
    for value, _swept_match in candidates:
        if session.exhausted or attempts >= settings.max_verified_candidates:
            break
        attempts += 1
        records = session.series(
            _probe_for(nodeid, [value], f"verify: {value.described()}"),
            verify_runs,
            f"verify {value.described()}",
        )
        if not records:
            break
        real, doctor, _passes, non_evidence = _partition(records, value.axis)
        all_failed = len(real) == len(records) == verify_runs
        matched_dominant = (
            _all_match(real, baseline.dominant_fp) if not baseline.quiet else False
        )
        if all_failed and (matched_dominant or _consistent(real)):
            session.evidence.append(
                EvidenceRow(
                    f"VERIFY: {value.described()}", len(records), len(real), "✓ deterministic"
                )
            )
            confirmed = (value, matched_dominant, records)
            break
        session.evidence.append(
            EvidenceRow(
                f"VERIFY: {value.described()}",
                len(records),
                len(real),
                _evidence_note(doctor, non_evidence, "elevates failure rate"),
            )
        )
        # Only failures that look like the observed flake can raise the
        # attributed rate. Counting a different exception here would let an
        # unrelated breakage masquerade as "this axis elevates your flake".
        if baseline.quiet:
            attributable = real if _consistent(real) else []
        else:
            attributable = [
                r
                for r in real
                if r.fingerprint is not None
                and baseline.dominant_fp is not None
                and r.fingerprint.key() == baseline.dominant_fp.key()
            ]
            if real and not attributable:
                session.warnings.append(
                    f"{value.described()} makes the test fail, but with a different failure "
                    "than the observed flake — not counted as a cause of it"
                )
        pvalue = elevation_pvalue(
            len(attributable), len(records), baseline.f_count, baseline.n
        )
        if (
            attributable
            and pvalue < 0.05
            and (best_elevation is None or len(attributable) > best_elevation[1])
        ):
            best_elevation = (value, len(attributable), len(records), pvalue)

    if confirmed is None:
        return _not_verified(session, nodeid, baseline, best_elevation)

    value, fp_matched, verify_records = confirmed
    counterfactual_ok, cf_value, cf_runs = _phase_counterfactual(
        nodeid, session, value, passers
    )
    return _verdict(
        session, nodeid, baseline, value, fp_matched, verify_records,
        counterfactual_ok, cf_value, cf_runs,
    )


def _phase_counterfactual(nodeid: str, session: _Session, winner: AxisValue, passers):
    """A benign value on the same axis must actually pass, or the claim weakens.

    The sweep exits as soon as it finds a failing value, so it usually has not
    established that *any* value on the axis passes. Without that, "this axis
    determines the outcome" is unearned — so hunt for a benign value here.
    """
    same_axis = [value for value in passers if value.axis == winner.axis]
    axis = next((a for a in ALL_AXES if a.id == winner.axis), None)
    if not same_axis and axis is not None and not session.exhausted:
        candidates = [v for v in axis.provocations() if v.value != winner.value]
        if axis.has_control:
            control = axis.control()
            if control.value != winner.value:
                candidates.insert(0, control)
        if axis is HASHSEED:
            seen = {v.value for v in candidates} | {winner.value}
            candidates.extend(axis.extra_values(2, seen))
        for candidate in candidates[:4]:
            if session.exhausted:
                break
            records = session.series(
                _probe_for(nodeid, [candidate], f"probe: {candidate.described()}"),
                session.settings.sweep_reps,
                f"probe {candidate.described()}",
            )
            if not records:
                break
            real, doctor, passes, non_evidence = _partition(records)
            session.evidence.append(
                EvidenceRow(
                    f"probe: {candidate.described()}",
                    len(records),
                    len(real),
                    _evidence_note(doctor, non_evidence),
                )
            )
            if len(passes) == len(records):
                same_axis = [candidate]
                break
    if not same_axis:
        # Every value on this axis fails. "X causes the failure" requires that
        # not-X does not, so an axis that never discriminates has not earned a
        # causal claim — something common to all its values (often merely
        # being inside a sandbox) is the more likely explanation.
        session.warnings.append(
            f"no value on the {winner.axis} axis lets the test pass, so the axis cannot be "
            "shown to be what discriminates — the cause may be something shared by every "
            "run under it rather than the axis itself"
        )
        return "no-benign-value", None, 0
    if session.exhausted:
        session.warnings.append(
            f"counterfactual on the {winner.axis} axis skipped (budget exhausted)"
        )
        return None, None, 0

    cf_value = same_axis[0]
    runs = session.settings.counterfactual_runs
    records = session.series(
        _probe_for(nodeid, [cf_value], f"counterfactual: {cf_value.described()}"),
        runs,
        f"counterfactual {cf_value.described()}",
    )
    if not records:
        return None, None, 0
    real, doctor, passes, non_evidence = _partition(records, cf_value.axis)
    ok = not real and not doctor and len(passes) == len(records)
    session.evidence.append(
        EvidenceRow(
            f"counterfactual: {cf_value.described()}",
            len(records),
            len(real),
            "✓ passes" if ok else _evidence_note(doctor, non_evidence, "still fails!" if real else ""),
        )
    )
    if real:
        session.warnings.append(
            f"counterfactual failed: {cf_value.described()} was expected to pass but failed "
            f"{len(real)}/{len(records)} — the axis elevates the failure rate rather than "
            "fully determining it"
        )
    elif not ok:
        session.warnings.append(
            "the counterfactual runs did not cleanly execute the test — treat the "
            "determinism claim as unconfirmed"
        )
    return ok, cf_value, len(records)


# ---------------------------------------------------------------- verdicts

def _verdict(
    session, nodeid, baseline, value, fp_matched, verify_records,
    counterfactual_ok, cf_value, cf_runs,
) -> Diagnosis:
    axis = next(a for a in ALL_AXES if a.id == value.axis)
    cp_lower = clopper_pearson_lower(len(verify_records), len(verify_records))
    claim = "observed" if (not baseline.quiet and fp_matched) else "latent"
    if not baseline.quiet and not fp_matched:
        session.warnings.append(
            "the verified repro fails with a different fingerprint than the observed "
            "baseline flake — treat this as a latent bug; the observed flake may have "
            "another cause"
        )
    if counterfactual_ok is True:
        headline = axis.display
    elif counterfactual_ok == "no-benign-value":
        # The axis never discriminates: cannot claim it as the cause.
        headline = axis.display + " — unconfirmed (no value on this axis passes)"
    elif counterfactual_ok is False:
        headline = axis.display + " — elevates failure rate"
    else:
        headline = axis.display + " — unconfirmed"

    if claim == "observed":
        lead = (
            f"Failed {baseline.f_count}/{baseline.n} isolated baseline runs; fails "
            f"{len(verify_records)}/{len(verify_records)} with {value.described()}"
            + (
                f", and passes with {cf_value.described()}. "
                if counterfactual_ok is True and cf_value is not None
                else ". "
            )
        )
    else:
        lead = (
            f"The baseline never failed in {baseline.n} isolated runs, but "
            f"{value.described()} makes it fail "
            f"{len(verify_records)}/{len(verify_records)} — a latent bug the doctor "
            "provoked. Failures you have seen elsewhere may have another cause. "
        )
    explanation = lead + axis.explain(value.value)

    repro = Repro(
        values=[value],
        nodeid=nodeid,
        tz=current_timezone(),
        tool=_tool_version(),
        fingerprint=(baseline.dominant_fp.digest() if baseline.dominant_fp else ""),
        confirm=(len(verify_records), len(verify_records)),
    )
    if value.axis == "time" and repro.tz is None:
        session.warnings.append(
            "frozen wall time is rendered through this machine's local timezone "
            f"({describe_timezone()}) and TZ is not set in the environment — this repro "
            "may not reproduce on a machine in another timezone; set TZ to pin it"
        )

    stats = {
        "baseline": baseline.stats,
        "verify": {
            "axis": axis.id,
            "value": value.value,
            "runs": len(verify_records),
            "failed": len(verify_records),
            "cp_lower_95": round(cp_lower, 4),
        },
        "counterfactual": (
            {"value": cf_value.value, "runs": cf_runs, "passed": counterfactual_ok}
            if cf_value is not None
            else ({"status": counterfactual_ok} if counterfactual_ok else None)
        ),
    }
    return _finish(
        session,
        nodeid,
        verdict=flaky_verdict(axis.id),
        claim=claim,
        headline=headline,
        explanation=explanation,
        stats=stats,
        repro=repro,
        repro_note=(
            f"fails {len(verify_records)}/{len(verify_records)}; "
            f"≥{cp_lower:.0%} repro rate at 95% confidence"
        ),
    )


def _not_verified(session, nodeid, baseline, best_elevation) -> Diagnosis:
    if session.exhausted:
        return _incomplete(session, nodeid, stats={"baseline": baseline.stats})
    if best_elevation is None:
        session.warnings.append(
            "no provocation value failed consistently, and none raised the failure rate "
            "above what the baseline rate already explains — no cause claimed"
        )
        if baseline.quiet:
            return _quiet_not_flaky(session, nodeid, baseline)
        return _unattributed(session, nodeid, baseline, None)

    value, failed, runs, pvalue = best_elevation
    axis = next(a for a in ALL_AXES if a.id == value.axis)
    session.warnings.append(
        "the implicated value raised the failure rate but did not verify n/n — "
        "no deterministic repro claimed"
    )
    if baseline.quiet:
        explanation = (
            f"The test passed all {baseline.n} isolated baseline runs, but with "
            f"{value.described()} it failed {failed}/{runs}. This axis clearly "
            "influences the test without fully determining it — expect a second, "
            "interacting source of nondeterminism. The command below reproduces the "
            "elevated failure rate, not a certain failure. "
        )
    else:
        explanation = (
            f"Failed {baseline.f_count}/{baseline.n} isolated baseline runs; with "
            f"{value.described()} the rate rose to {failed}/{runs}. This axis influences "
            "the test without fully determining it — expect a second, interacting source "
            "of nondeterminism. "
        )
    stats = {
        "baseline": baseline.stats,
        "verify": {
            "axis": axis.id,
            "value": value.value,
            "runs": runs,
            "failed": failed,
            "cp_lower_95": round(clopper_pearson_lower(failed, runs), 4),
            "elevation_pvalue": round(pvalue, 6),
        },
        "counterfactual": None,
    }
    return _finish(
        session,
        nodeid,
        verdict=flaky_verdict(axis.id),
        claim="latent",
        headline=axis.display + " — elevates failure rate",
        explanation=explanation + axis.explain(value.value),
        stats=stats,
        repro=Repro(
            values=[value],
            nodeid=nodeid,
            tz=current_timezone(),
            tool=_tool_version(),
            confirm=(failed, runs),
        ),
        repro_note=f"fails {failed}/{runs} — not deterministic",
    )


def _quiet_not_flaky(session: _Session, nodeid: str, baseline: _Baseline) -> Diagnosis:
    return _finish(
        session,
        nodeid,
        verdict=NOT_FLAKY,
        claim="none",
        headline=f"no flake reproduced ({session.total_runs} runs)",
        explanation=(
            f"The test passed every isolated baseline run ({baseline.n}/{baseline.n}) and "
            "no perturbation reproduced a failure. If it flakes in CI, the cause may lie "
            "in axes this build does not cover yet (test ordering, thread scheduling) or "
            "in the CI environment itself. Try --doctor-runs=50, or run the doctor in CI."
        ),
        stats={"baseline": baseline.stats},
    )


def _unattributed(session: _Session, nodeid: str, baseline: _Baseline, stabilizer) -> Diagnosis:
    extra = ""
    if stabilizer is None:
        extra = (
            " Full control over time, randomness, network and filesystem did not stabilize "
            "it, so the cause is most likely test ordering, thread scheduling, or external "
            "state — the axes still to come."
        )
    return _finish(
        session,
        nodeid,
        verdict=FLAKY_UNATTRIBUTED,
        claim="none",
        headline=(
            f"flaky ({baseline.f_count}/{baseline.n} isolated runs failed) — "
            "cause not in covered axes"
        ),
        explanation=(
            "The flake is real, but none of the axes this build covers (time, randomness, "
            "network, filesystem, hash order) determines it." + extra
        ),
        stats={"baseline": baseline.stats},
    )


def _incomplete(session, nodeid, detail: str = "", stats: dict | None = None) -> Diagnosis:
    return _finish(
        session,
        nodeid,
        verdict=INCOMPLETE,
        claim="none",
        headline="diagnosis incomplete (budget exhausted)",
        explanation=(
            "The run budget ran out before the diagnosis could finish. "
            + (detail + " " if detail else "")
            + "What was collected is shown below; raise --doctor-budget to complete it."
        ),
        stats=stats or {},
    )


def _finish(
    session: _Session,
    nodeid: str,
    *,
    verdict: str,
    claim: str,
    headline: str,
    explanation: str,
    stats: dict,
    repro: Repro | None = None,
    repro_note: str = "",
    repro_command_override: str | None = None,
    repro_dict_override: dict | None = None,
) -> Diagnosis:
    command = repro_command_override
    repro_dict = repro_dict_override
    if repro is not None:
        display = session.display_nodeid or nodeid
        blob = repro.encode() if any(v.axis != "hashseed" for v in repro.values) else None
        rng_axis = any(v.axis == "rng" for v in repro.values)
        command = repro_command(display, blob, repro.hashseed, neutralize_randomly=rng_axis)
        axis = repro.values[0].axis
        # The marker re-applies a sandbox axis in-process; hashseed (needs the
        # interpreter env) and order (needs other tests) can't travel that way.
        # Only offer it for a DETERMINISTIC diagnosis: a marker for a flake that
        # merely "elevates the failure rate" would itself flake CI.
        deterministic = repro.confirm[1] > 0 and repro.confirm[0] == repro.confirm[1]
        marker = None
        if blob is not None and axis in ("time", "rng", "network", "fs") and deterministic:
            marker = f'@pytest.mark.flakedoctor_repro("{blob}")'
        repro_dict = {
            "axis": axis,
            "value": repro.values[0].value,
            "note": repro_note,
            "blob": blob,
            "hashseed": repro.hashseed,
            "tz": repro.tz,
            "marker": marker,
        }
    return Diagnosis(
        nodeid=nodeid,
        verdict=verdict,
        claim=claim,
        headline=headline,
        explanation=explanation,
        evidence=session.evidence,
        repro_command=command,
        repro=repro_dict,
        warnings=session.warnings,
        stats=stats,
        elapsed=session.elapsed(),
        total_runs=session.total_runs,
    )


def _tool_version() -> str:
    from . import __version__

    return __version__


def _all_match(failures: list[RunRecord], dominant: Fingerprint | None) -> bool:
    if not failures:
        return False
    if dominant is None:
        return _consistent(failures)
    return all(
        r.fingerprint is not None and r.fingerprint.key() == dominant.key() for r in failures
    )


def _consistent(failures: list[RunRecord]) -> bool:
    keys = {r.fingerprint.key() for r in failures if r.fingerprint}
    return len(keys) == 1
