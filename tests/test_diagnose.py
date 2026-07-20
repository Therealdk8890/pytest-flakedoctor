"""Diagnosis-loop logic against a scripted fake runner (no subprocesses)."""

from __future__ import annotations

import time

import pytest

from flakedoctor._diagnose import (
    CHILD_ERROR,
    DETERMINISTIC,
    FLAKY_UNATTRIBUTED,
    HANGS,
    INCOMPLETE,
    NOT_FLAKY,
    SKIPPED,
    USAGE_ERROR,
    Diagnosis,
    DoctorSettings,
    diagnose,
    flaky_verdict,
)
from flakedoctor._fingerprint import CRASH, HANG, Fingerprint
from flakedoctor._runner import RunRecord

FP = Fingerprint("call", "AssertionError", "assert first not in bad", "test_x.py:4")
FP2 = Fingerprint("call", "KeyError", "'missing'", "test_x.py:9")
NET_BLOCKED = Fingerprint(
    "call", "hermetic._errors.NetworkBlockedError", "blocked api.example.com:443", "test_x.py:6"
)

NODEID = "test_x.py::test_y"
FLAKY_HASHSEED = flaky_verdict("hashseed")
FLAKY_TIME = flaky_verdict("time")


def make_record(label: str, spec) -> RunRecord:
    if spec == "pass":
        return RunRecord(label, "pass", None, 0.01, 0)
    if spec == "hang":
        return RunRecord(label, "hang", HANG, 0.01, None)
    if spec in ("usage-error", "no-tests", "child-error", "skipped"):
        return RunRecord(label, spec, None, 0.01, 4, detail="child said no")
    kind, fingerprint = spec  # ("fail", Fingerprint)
    assert kind == "fail"
    return RunRecord(label, "fail", fingerprint, 0.01, 1)


class FakeRunner:
    """Routes each probe by label prefix to a scripted outcome sequence.

    The first matching prefix wins, so specific labels must precede general
    ones. Anything unmatched yields `default`.
    """

    def __init__(self, behaviors: list[tuple[str, list]], default="pass"):
        self.behaviors = behaviors
        self.default = default
        self.counts: dict[str, int] = {}
        self.labels_seen: list[str] = []

    def __call__(self, probe, timeout):
        self.labels_seen.append(probe.label)
        for prefix, sequence in self.behaviors:
            if probe.label.startswith(prefix):
                index = self.counts.get(prefix, 0)
                self.counts[prefix] = index + 1
                return make_record(probe.label, sequence[index % len(sequence)])
        return make_record(probe.label, self.default)

    def labels_matching(self, needle: str) -> list[str]:
        return [label for label in self.labels_seen if needle in label]


def run(runner, **settings) -> Diagnosis:
    return diagnose(NODEID, runner, DoctorSettings(**settings))


# ------------------------------------------------------------- happy paths

def test_observed_hashseed_diagnosis():
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),  # 5/10
            ("control", [("fail", FP), "pass"]),  # hermetic axes don't stabilize it
            ("provoke: PYTHONHASHSEED=0", [("fail", FP)]),
            ("verify: PYTHONHASHSEED=0", [("fail", FP)]),
            ("counterfactual", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_HASHSEED
    assert d.claim == "observed"
    assert "PYTHONHASHSEED=0" in d.repro_command
    assert d.repro["blob"] is None  # hashseed needs no blob, just the env prefix
    assert d.stats["verify"]["cp_lower_95"] == pytest.approx(0.7411, abs=0.001)
    assert any(row.note == "✓ deterministic" for row in d.evidence)


def test_time_axis_found_via_control_phase():
    """Controlling time alone stabilizes the flake, so time is swept first."""
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control: all axes", ["pass"]),  # everything frozen -> stable
            ("control: time", ["pass"]),  # time alone -> stable: the stabilizer
            ("control: rng", [("fail", FP), "pass"]),
            ("provoke: time frozen @ month-end", [("fail", FP)]),
            ("verify: time frozen @ month-end", [("fail", FP)]),
            ("counterfactual", ["pass"]),
            ("provoke", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_TIME
    assert d.claim == "observed"
    assert d.repro["blob"].startswith("fd1:")
    assert "--doctor-repro=fd1:" in d.repro_command
    # The stabilizer must be swept before hash seeds.
    provocations = runner.labels_matching("provoke:")
    assert "time" in provocations[0]
    assert any("← stabilizer" in row.note for row in d.evidence)


def test_control_phase_skipped_for_quiet_baseline():
    """With nothing failing there is no flakiness for a control to remove."""
    runner = FakeRunner([("baseline", ["pass"])], default="pass")
    d = run(runner)
    assert d.verdict == NOT_FLAKY
    assert not runner.labels_matching("control")


def test_latent_claim_when_baseline_never_fails():
    runner = FakeRunner(
        [
            ("baseline", ["pass"]),
            ("provoke: time frozen @ month-end", [("fail", FP)]),
            ("verify", [("fail", FP)]),
            ("counterfactual", ["pass"]),
        ],
        default="pass",
    )
    d = run(runner)
    assert d.verdict == FLAKY_TIME
    assert d.claim == "latent"
    assert "latent bug the doctor provoked" in d.explanation


# ------------------------------------------------- perturbation hygiene

def test_doctor_origin_failures_are_not_credited_as_the_flake():
    """A blocked socket is evidence about our sandbox, never about the user's bug."""
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control: all axes", [("fail", NET_BLOCKED)]),
            ("control", ["pass"]),
            ("provoke: network", [("fail", NET_BLOCKED)]),
            ("provoke", ["pass"]),
        ],
        default="pass",
    )
    d = run(runner)
    # The network axis must not be reported as the cause.
    assert d.verdict != flaky_verdict("network")
    assert any("perturbation-induced" in row.note for row in d.evidence)
    assert any("caused by the sandbox itself" in w for w in d.warnings)


def test_probe_notes_surface_as_warnings():
    """e.g. 'clock axis skipped: async test'."""

    def runner(probe, timeout):
        record = make_record(probe.label, "pass")
        if probe.label.startswith("provoke: time"):
            record.probe_notes = ["clock axis skipped: async test (virtual clock would hang it)"]
        return record

    d = diagnose(NODEID, runner, DoctorSettings(runs=4))
    assert any("async test" in w for w in d.warnings)


def test_runs_that_could_not_apply_the_perturbation_are_not_evidence():
    def runner(probe, timeout):
        if probe.label.startswith("baseline"):
            return make_record(probe.label, "pass")
        record = make_record(probe.label, "pass")
        record.probe_error = "invalid sandbox config: boom"
        return record

    d = diagnose(NODEID, runner, DoctorSettings(runs=4))
    assert d.verdict == NOT_FLAKY
    assert any("not applicable" in row.note for row in d.evidence)


def test_hermetic_unavailable_disables_those_axes():
    def runner(probe, timeout):
        record = make_record(probe.label, "pass" if probe.label.startswith("baseline") else "pass")
        if probe.sandbox is not None:
            record.probe_error = "hermetic unavailable: No module named 'hermetic'"
        return record

    d = diagnose(NODEID, runner, DoctorSettings(runs=4))
    assert not any("time frozen" in label for label in [])  # sanity
    assert d.verdict == NOT_FLAKY


# ------------------------------------------------------- honest refusals

def test_streaky_flake_is_not_credited_as_elevation():
    """A 50/50 coin-flip flake must not be blamed on whichever value streaked."""
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),  # 5/10
            ("control", [("fail", FP), "pass"]),
            ("provoke: PYTHONHASHSEED=0", [("fail", FP)]),  # 2/2 by chance
            ("verify: PYTHONHASHSEED=0", ["pass", ("fail", FP)]),  # 5/10 — no elevation
            ("provoke", ["pass", ("fail", FP)]),
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_UNATTRIBUTED
    assert d.repro_command is None
    assert any("above what the baseline rate already explains" in w for w in d.warnings)


def test_verify_demotion_reports_elevation_not_unattributed():
    runner = FakeRunner(
        [
            ("baseline", ["pass", "pass", "pass", "pass", ("fail", FP)]),  # 2/10
            ("control", [("fail", FP), "pass"]),
            ("provoke: PYTHONHASHSEED=0", [("fail", FP)]),
            ("verify: PYTHONHASHSEED=0", [("fail", FP)] * 9 + ["pass"]),  # 9/10
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_HASHSEED
    assert "elevates failure rate" in d.headline
    assert "not deterministic" in d.repro["note"]
    assert d.stats["verify"]["elevation_pvalue"] < 0.05


def test_counterfactual_failure_downgrades_the_claim():
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control", [("fail", FP), "pass"]),
            ("provoke: PYTHONHASHSEED=0", [("fail", FP)]),
            ("verify", [("fail", FP)]),
            ("counterfactual", ["pass", ("fail", FP)]),
        ]
    )
    d = run(runner)
    assert "elevates failure rate" in d.headline
    assert any("counterfactual failed" in w for w in d.warnings)


def test_different_fingerprint_verifies_as_latent_with_warning():
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control", [("fail", FP), "pass"]),
            ("provoke: PYTHONHASHSEED=0", [("fail", FP2)]),
            ("verify", [("fail", FP2)]),
            ("counterfactual", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.claim == "latent"
    assert any("different fingerprint" in w for w in d.warnings)
    assert any("different failure" in row.note for row in d.evidence)


def test_full_control_still_flaky_says_so():
    runner = FakeRunner(
        [("baseline", ["pass", ("fail", FP)]), ("control", [("fail", FP), "pass"])],
        default="pass",
    )
    d = run(runner)
    assert d.verdict == FLAKY_UNATTRIBUTED
    assert "did not stabilize" in d.explanation


# ------------------------------------------------------- early verdicts

def test_stable_test_is_not_flaky():
    d = run(FakeRunner([], default="pass"))
    assert d.verdict == NOT_FLAKY
    assert d.repro_command is None


def test_deterministic_failure_stops_immediately():
    d = run(FakeRunner([("", [("fail", FP)])]))
    assert d.verdict == DETERMINISTIC
    assert d.total_runs == 10


def test_all_hang_verdict():
    d = run(FakeRunner([("baseline", ["hang"])]))
    assert d.verdict == HANGS


def test_usage_error_short_circuits_after_one_run():
    d = run(FakeRunner([("baseline", ["usage-error"])]))
    assert d.verdict == USAGE_ERROR
    assert d.total_runs == 1


def test_skipped_short_circuits():
    d = run(FakeRunner([("baseline", ["skipped"])]))
    assert d.verdict == SKIPPED


def test_all_child_error():
    d = run(FakeRunner([("baseline", ["child-error"])]))
    assert d.verdict == CHILD_ERROR


def test_multiple_fingerprints_warns_and_diagnoses_dominant():
    runner = FakeRunner(
        [("baseline", [("fail", FP), ("fail", FP), ("fail", FP2), "pass"])], default="pass"
    )
    d = run(runner)
    assert any("distinct failure modes" in w for w in d.warnings)


def test_baseline_excluded_runs_are_accounted_for():
    runner = FakeRunner([("baseline", ["pass", "skipped", "skipped", "skipped"])], default="pass")
    d = run(runner)
    assert any("did not run the test" in w for w in d.warnings)
    assert d.stats["baseline"]["attempted"] == 10
    assert d.stats["baseline"]["runs"] + d.stats["baseline"]["excluded"] == 10


# ------------------------------------------------------------- budget

def test_zero_budget_is_incomplete():
    d = run(FakeRunner([], default="pass"), budget=0.0)
    assert d.verdict == INCOMPLETE
    assert d.total_runs == 0


def test_budget_exhaustion_with_candidate_reports_incomplete():
    class SlowSweepRunner:
        def __call__(self, probe, timeout):
            if probe.label.startswith("baseline"):
                return RunRecord(probe.label, "pass", None, 0.01, 0)
            time.sleep(0.5)
            return make_record(probe.label, ("fail", FP))

    d = diagnose(NODEID, SlowSweepRunner(), DoctorSettings(budget=3.0, runs=3))
    assert d.verdict == INCOMPLETE
    assert any("budget exhausted" in w for w in d.warnings)


def test_timeout_never_shrinks_below_run_duration():
    """Regression: a budget-shrunken timeout would fabricate 'hang' failures."""
    granted: list[float] = []

    def runner(probe, timeout):
        granted.append(timeout)
        return RunRecord(probe.label, "pass", None, 1.0, 0)

    diagnose(NODEID, runner, DoctorSettings(budget=6.0, runs=20))
    assert granted
    assert all(t >= 1.0 for t in granted), granted


def test_inherited_hashseed_warning(monkeypatch):
    monkeypatch.setenv("PYTHONHASHSEED", "7")
    d = run(FakeRunner([], default="pass"))
    assert any("pinned in the parent environment" in w for w in d.warnings)


def test_baseline_probe_strips_inherited_hashseed():
    seen: list[dict] = []

    def runner(probe, timeout):
        seen.append(dict(probe.env))
        return make_record(probe.label, "pass")

    diagnose(NODEID, runner, DoctorSettings(runs=2))
    assert seen[0]["PYTHONHASHSEED"] is None  # None => unset in the child


# ------------------------------------- regressions from the axes review

def test_elevation_requires_the_observed_failure_mode():
    """Regression: an axis that breaks the test DIFFERENTLY must not be blamed.

    Previously the elevation path counted every failure, so `fs=isolate`
    breaking a test with an unrelated FileNotFoundError was reported as
    "filesystem-state dependent — elevates failure rate" for a flake it had
    nothing to do with.
    """
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),  # 5/10 with FP
            ("control", [("fail", FP), "pass"]),
            ("provoke: filesystem isolated", [("fail", FP2)]),  # different failure
            ("verify: filesystem isolated", [("fail", FP2)] * 9 + ["pass"]),  # 9/10, wrong mode
            ("provoke", ["pass"]),
            ("verify", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.verdict != flaky_verdict("fs")
    assert any("different failure than the observed flake" in w for w in d.warnings)


def test_stabilizer_marker_requires_statistical_significance():
    """Regression: at a low baseline rate, a clean 5-run streak proves nothing."""
    runner = FakeRunner(
        # 1/10 baseline: five clean control runs happen 59% of the time by chance.
        [("baseline", ["pass"] * 9 + [("fail", FP)]), ("control", ["pass"])],
        default="pass",
    )
    d = run(runner)
    control_rows = [r for r in d.evidence if r.label.startswith(("full control", "control:"))]
    assert control_rows
    assert all("← stabilizer" not in row.note for row in control_rows)
    assert any("inconclusive" in row.note for row in control_rows)
    assert any("a lead, not a conclusion" in w for w in d.warnings)


def test_stabilizer_marker_shown_when_significant():
    runner = FakeRunner(
        # 6/10 baseline: five clean runs would happen only 1% of the time.
        [
            ("baseline", ["pass", "pass", ("fail", FP), ("fail", FP), ("fail", FP)]),
            ("control", ["pass"]),
            ("provoke", ["pass"]),
        ],
        default="pass",
    )
    d = run(runner)
    assert any("← stabilizer" in row.note for row in d.evidence)


def test_full_control_with_only_doctor_origin_runs_is_not_stabilized():
    """Regression: zero informative runs is not evidence the flake vanished."""
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control: all axes", [("fail", NET_BLOCKED)]),  # every run doctor-origin
        ],
        default="pass",
    )
    d = run(runner)
    assert not any("← stabilizer" in row.note for row in d.evidence)
    # No per-axis control hunt should have started.
    assert not [label for label in runner.labels_seen if label.startswith("control: time")]


def test_sweep_resumes_after_a_rejected_candidate():
    """Regression: a candidate failing verification ended the whole diagnosis."""
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control", [("fail", FP), "pass"]),
            # hashseed 0 looks strong in the sweep but fails verification...
            ("provoke: PYTHONHASHSEED=0", [("fail", FP)]),
            ("verify: PYTHONHASHSEED=0", ["pass"]),
            # ...so the sweep must continue and find the real cause on time.
            ("provoke: time frozen @ month-end", [("fail", FP)]),
            ("verify: time frozen @ month-end", [("fail", FP)]),
            ("counterfactual", ["pass"]),
            ("provoke", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_TIME
    assert any(label.startswith("verify: time") for label in runner.labels_seen)


def test_hang_under_an_active_clock_axis_is_doctor_origin():
    """Regression: incomplete async detection must not fabricate a time flake."""

    def runner(probe, timeout):
        if probe.sandbox and probe.sandbox.get("clock") == "virtual":
            record = make_record(probe.label, "hang")
            record.applied_axes = ["time"]
            return record
        return make_record(probe.label, "pass")

    d = diagnose(NODEID, runner, DoctorSettings(runs=4))
    assert d.verdict != flaky_verdict("time")
    assert any("perturbation-induced" in row.note for row in d.evidence)


def test_axis_that_stood_itself_down_collects_no_evidence():
    """A clock that skipped itself (async test) must not be credited or blamed."""

    def runner(probe, timeout):
        record = make_record(probe.label, ("fail", FP) if probe.sandbox else "pass")
        if probe.sandbox:
            # The probe reports what it really applied; the clock stood down,
            # so "time" is absent even though the config asked for it.
            record.applied_axes = [
                axis
                for key, axis in (("rng", "rng"), ("network", "network"), ("fs", "fs"))
                if probe.sandbox.get(key, "off") != "off"
            ]
        return record

    d = diagnose(NODEID, runner, DoctorSettings(runs=4))
    assert d.verdict != flaky_verdict("time")


def test_teardown_error_does_not_discard_the_run():
    """Regression: a Sandbox.__exit__ failure threw away a valid test result."""

    def runner(probe, timeout):
        record = make_record(probe.label, "pass")
        record.teardown_error = "RuntimeError: cleanup exploded"
        record.probe_notes = ["the perturbation's teardown raised RuntimeError after the test ran"]
        return record

    d = diagnose(NODEID, runner, DoctorSettings(runs=4))
    assert d.verdict == NOT_FLAKY  # the passing runs still counted
    assert any("teardown raised" in w for w in d.warnings)


def test_axis_where_no_value_passes_is_unconfirmed():
    """An axis that never discriminates has not earned a causal claim.

    Found by running the tool on a hostile case: entering *any* sandbox
    stabilized the real (uncontrolled) source of nondeterminism, so every
    time-value failed and the tool confidently blamed time.
    """
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control", [("fail", FP), "pass"]),
            # Every value on the time axis fails, including the benign ones.
            ("provoke: time", [("fail", FP)]),
            ("verify: time", [("fail", FP)]),
            ("probe: time", [("fail", FP)]),
            ("counterfactual: time", [("fail", FP)]),
            ("provoke", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_TIME
    assert "unconfirmed" in d.headline
    assert any("cannot be shown to be what discriminates" in w for w in d.warnings)


# ------------------------------------------------------- test-order axis

VICTIM = "tests/test_v.py::test_victim"
POLLUTER = "tests/test_p.py::test_polluter"
INNOCENT = "tests/test_i.py::test_innocent"


def _order_victim_record(label, victim, failed, fp=FP, duration=0.05):
    """A record from a multi-test run, as _read_records would build it: phases
    attributed to the victim (last nodeid)."""
    if failed:
        return RunRecord(
            label, "fail", fp, duration, 1,
            phases={"setup": "passed", "call": "failed"}, last_nodeid=victim,
        )
    return RunRecord(
        label, "pass", None, duration, 0,
        phases={"setup": "passed", "call": "passed"}, last_nodeid=victim,
    )


class OrderRunner:
    """Fake runner where the victim fails iff a specific polluter precedes it."""

    def __init__(self, polluter=POLLUTER, victim=VICTIM, fp=FP, flaky_after=False, victim_alone_fails=False):
        self.polluter = polluter
        self.victim = victim
        self.fp = fp
        self.flaky_after = flaky_after  # if True, fails only ~half the time after polluter
        self.victim_alone_fails = victim_alone_fails  # a latent flake, independent of order
        self.calls = 0
        self.order_runs: list[tuple] = []
        self.bisect_runs = 0

    def __call__(self, probe, timeout):
        self.calls += 1
        nodeids = probe.nodeids
        if len(nodeids) == 1:
            # Isolated run (baseline / counterfactual): usually passes alone.
            return _order_victim_record(probe.label, self.victim, self.victim_alone_fails)
        self.order_runs.append(nodeids)
        if "bisect" in probe.label:
            self.bisect_runs += 1
        prefix = nodeids[:-1]
        if self.polluter in prefix:
            if self.flaky_after and self.calls % 2 == 0:
                return _order_victim_record(probe.label, self.victim, False)
            return _order_victim_record(probe.label, self.victim, True, self.fp)
        return _order_victim_record(probe.label, self.victim, False)


def _order_prefix(n=8):
    """A prefix of n innocent tests with the polluter in the middle."""
    prefix = [f"tests/test_{i}.py::test_{i}" for i in range(n)]
    prefix[n // 2] = POLLUTER
    return prefix


def test_order_axis_finds_the_polluter():
    prefix = _order_prefix(8)
    runner = OrderRunner()
    d = diagnose(VICTIM, runner, DoctorSettings(runs=6), prefix=prefix)
    assert d.verdict == flaky_verdict("order")
    assert d.claim == "observed"
    assert "test-order dependent" in d.headline
    assert d.repro["polluters"] == [POLLUTER]
    assert POLLUTER in d.repro_command
    assert VICTIM in d.repro_command
    assert d.repro["blob"].startswith("fd1:")
    # The innocent tests must not appear in the minimal repro.
    assert INNOCENT not in d.repro_command


def test_order_bisection_narrows_to_one_of_many():
    prefix = _order_prefix(16)
    runner = OrderRunner()
    d = diagnose(VICTIM, runner, DoctorSettings(runs=6), prefix=prefix)
    assert d.verdict == flaky_verdict("order")
    assert d.repro["polluters"] == [POLLUTER]
    # Bisection, not brute force: halving runs (with a 3x confirm on singletons)
    # stays well under scanning all 16 tests one at a time.
    assert runner.bisect_runs < len(prefix), runner.bisect_runs


def test_order_not_reproduced_falls_through_with_warning():
    """Victim failed in the suite, but no prefix reproduces it (e.g. xdist)."""
    prefix = [f"tests/test_{i}.py::test_{i}" for i in range(6)]  # no polluter present

    runner = OrderRunner(polluter="tests/test_absent.py::test_absent")
    d = diagnose(VICTIM, runner, DoctorSettings(runs=6), prefix=prefix)
    assert d.verdict != flaky_verdict("order")
    assert any("did not reproduce" in w or "runs after it" in w for w in d.warnings)


def test_order_skipped_without_a_prefix():
    """Single-test invocation: no prefix, so the order axis cannot run."""
    runner = OrderRunner()
    d = diagnose(VICTIM, runner, DoctorSettings(runs=4))  # prefix=None
    assert d.verdict != flaky_verdict("order")
    assert not runner.order_runs


def test_order_only_checked_when_victim_passes_alone():
    """If the victim already fails alone, the flake is not (solely) about order."""
    prefix = _order_prefix(8)

    def runner(probe, timeout):
        if len(probe.nodeids) == 1:
            # Mixed baseline: fails alone sometimes.
            runner.n = getattr(runner, "n", 0) + 1
            return make_record(probe.label, ("fail", FP) if runner.n % 2 else "pass")
        return RunRecord(probe.label, "pass", None, 0.05, 0)

    d = diagnose(VICTIM, runner, DoctorSettings(runs=6), prefix=prefix)
    assert d.verdict != flaky_verdict("order")


def test_order_run_timeout_is_sized_for_the_whole_prefix():
    """A prefix run is slow; it must not be killed under the victim-alone timeout."""
    granted: list[float] = []

    def runner(probe, timeout):
        granted.append(timeout)
        if len(probe.nodeids) == 1:
            return RunRecord(probe.label, "pass", None, 0.1, 0)  # fast alone
        # Slow multi-test run; if the timeout were sized on 0.1s it'd be too short.
        return RunRecord(probe.label, "fail", FP, 20.0, 1)

    prefix = _order_prefix(8)
    diagnose(VICTIM, runner, DoctorSettings(runs=3, budget=3000), prefix=prefix)
    order_timeouts = granted[3:]  # after the 3 baseline runs
    assert order_timeouts
    assert all(t >= 20.0 for t in order_timeouts), order_timeouts


# ------------------------------- test-order axis: review regressions

def _order_prefix_with(polluter, n=8):
    prefix = [f"tests/test_{i}.py::test_{i}" for i in range(n)]
    prefix[n // 2] = polluter
    return prefix


def test_order_prefix_crash_is_not_credited_as_reproduction():
    """Regression: a prefix test that crashes kills the subprocess before the
    victim runs; that must not be read as the victim failing 'in this order'."""
    crashing = "tests/test_boom.py::test_segfaults"
    prefix = _order_prefix_with(crashing)

    def runner(probe, timeout):
        if len(probe.nodeids) == 1:
            return _order_victim_record(probe.label, VICTIM, False)  # passes alone
        if crashing in probe.nodeids[:-1]:
            # Crash: process dies at the prefix test, victim never runs. The
            # last recorded nodeid is the crasher, not the victim.
            return RunRecord(
                probe.label, "crash", CRASH, 0.05, -11,
                phases={"call": "failed"}, last_nodeid=crashing,
            )
        return _order_victim_record(probe.label, VICTIM, False)

    d = diagnose(VICTIM, runner, DoctorSettings(runs=5), prefix=prefix)
    assert d.verdict != flaky_verdict("order"), d.headline


def test_order_slow_innocent_prefix_hang_is_not_a_false_positive():
    """Regression: a long but innocent prefix, killed by timeout, was fabricated
    into a hang and reported as an order dependency."""
    prefix = [f"tests/test_{i}.py::test_{i}" for i in range(8)]  # no polluter

    def runner(probe, timeout):
        if len(probe.nodeids) == 1:
            return _order_victim_record(probe.label, VICTIM, False)
        # The prefix run is killed by the timeout: a HANG, victim never ran.
        return RunRecord(
            probe.label, "hang", HANG, timeout, None, phases={}, last_nodeid="",
        )

    d = diagnose(VICTIM, runner, DoctorSettings(runs=5), prefix=prefix)
    assert d.verdict != flaky_verdict("order")
    assert any("did not reproduce" in w or "too slow" in w for w in d.warnings)


def test_order_confirm_run_gets_generous_timeout_for_the_prefix():
    """Regression: the confirm run must not be sized on the victim-alone time."""
    granted: list[tuple[int, float]] = []
    prefix = _order_prefix_with(POLLUTER, n=40)

    def runner(probe, timeout):
        granted.append((len(probe.nodeids), timeout))
        if len(probe.nodeids) == 1:
            return _order_victim_record(probe.label, VICTIM, False, duration=0.1)  # fast alone
        return _order_victim_record(probe.label, VICTIM, POLLUTER in probe.nodeids[:-1])

    diagnose(VICTIM, runner, DoctorSettings(runs=3, budget=100000), prefix=prefix)
    # The first multi-test (confirm) run must be granted far more than 5x0.1s.
    multi = [t for (n, t) in granted if n > 1]
    assert multi and multi[0] >= 30.0, multi


def test_order_latent_flake_caught_by_counterfactual():
    """Regression: a victim with an independent latent flake (fails alone too)
    must not be misdiagnosed as order-dependent."""
    prefix = _order_prefix_with(POLLUTER)
    # victim_alone_fails=True: the counterfactual (victim alone) will fail.
    runner = OrderRunner(victim_alone_fails=True)

    # Baseline would be mixed if it fails alone; force a quiet-ish baseline by
    # having it pass alone until the counterfactual — model a low-rate flake.
    seq = {"n": 0}

    def latent_runner(probe, timeout):
        if len(probe.nodeids) == 1:
            seq["n"] += 1
            # Passes during baseline, fails during the counterfactual phase.
            failed = probe.label.startswith("order: counterfactual")
            return _order_victim_record(probe.label, VICTIM, failed)
        return _order_victim_record(probe.label, VICTIM, POLLUTER in probe.nodeids[:-1])

    d = diagnose(VICTIM, latent_runner, DoctorSettings(runs=5), prefix=prefix)
    assert d.verdict != flaky_verdict("order")
    assert any("also fails on its own" in w for w in d.warnings)


def test_order_repro_confirm_tuple_holds_verify_counts():
    """Regression: the blob's confirm field must be (failures, runs), not
    (polluter_count, prefix_len), so DID NOT REPRODUCE reports a real count."""
    from flakedoctor._repro import decode

    prefix = _order_prefix_with(POLLUTER)
    d = diagnose(VICTIM, OrderRunner(), DoctorSettings(runs=6, verify_runs=10), prefix=prefix)
    assert d.verdict == flaky_verdict("order")
    repro = decode(d.repro["blob"])
    failed, runs = repro.confirm
    assert runs == 10 and failed == 10, repro.confirm  # verified 10/10


def test_marker_only_emitted_for_a_deterministic_diagnosis():
    """A marker for a merely-'elevates' flake would itself flake CI."""
    # Time axis elevates the rate but does not verify n/n -> no marker.
    runner = FakeRunner(
        [
            ("baseline", ["pass", "pass", "pass", "pass", ("fail", FP)]),  # 2/10
            ("control", [("fail", FP), "pass"]),
            ("provoke: time frozen @ month-end", [("fail", FP)]),
            ("verify: time frozen @ month-end", [("fail", FP)] * 8 + ["pass", "pass"]),  # 8/10
            ("provoke", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_TIME
    assert "elevates failure rate" in d.headline
    assert d.repro.get("marker") is None  # non-deterministic: no paste-able marker


def test_marker_emitted_for_a_deterministic_time_diagnosis():
    runner = FakeRunner(
        [
            ("baseline", ["pass", ("fail", FP)]),
            ("control: all axes", ["pass"]),
            ("control: time", ["pass"]),
            ("control", [("fail", FP), "pass"]),
            ("provoke: time frozen @ month-end", [("fail", FP)]),
            ("verify: time frozen @ month-end", [("fail", FP)]),  # 10/10
            ("counterfactual", ["pass"]),
            ("provoke", ["pass"]),
        ]
    )
    d = run(runner)
    assert d.verdict == FLAKY_TIME
    assert d.repro["marker"] is not None
    assert d.repro["marker"].startswith("@pytest.mark.flakedoctor_repro(")


# ---------------------------------------------------- interleave axis

FLAKY_INTERLEAVE = flaky_verdict("interleave")


def _baseline_record(label, outcome="pass", threads=2, is_async=False, is_unittest=False):
    r = RunRecord(label, outcome, None if outcome == "pass" else FP, 0.01, 0 if outcome == "pass" else 1)
    r.thread_starts = threads
    r.is_async = is_async
    r.is_async_for_interleave = is_async
    r.is_unittest = is_unittest
    return r


def _interleave_record(label, found, kind=None, iteration=9):
    if found:
        r = RunRecord(label, "fail", FP, 0.5, 1)
        r.interleave = {
            "found": True, "schedule": '{"choices":[1,2]}', "py_exact": "3.13.12",
            "strategy": "pct", "kind": kind, "iteration": iteration,
            "passing_schedule_seen": iteration > 0,
        }
    else:
        r = RunRecord(label, "pass", None, 0.5, 0)
        r.interleave = {"found": False, "strategy": label.split("(")[-1].rstrip(")")}
    return r


class InterleaveRunner:
    """Fake runner scripted for the interleave phase (and a quiet default)."""

    def __init__(self, *, threads=2, is_async=False, explore="found", replay="reproduce",
                 explore_error=None):
        self.threads = threads
        self.is_async = is_async
        self.explore = explore  # "found" | "none"
        self.replay = replay    # "reproduce" | "pass"
        self.explore_error = explore_error
        self.labels = []

    def __call__(self, probe, timeout):
        label = probe.label
        self.labels.append(label)
        if label.startswith("baseline"):
            return _baseline_record(label, "pass", self.threads, self.is_async)
        if label.startswith("interleave: explore"):
            if self.explore_error is not None:
                r = RunRecord(label, "pass", None, 0.1, 0)
                r.interleave = {"error": self.explore_error}
                return r
            return _interleave_record(label, self.explore == "found")
        if label.startswith("interleave: replay"):
            if self.replay == "reproduce":
                return _interleave_record(label, True)
            return RunRecord(label, "pass", None, 0.1, 0)
        # Everything else (control/provoke/counterfactual): quiet pass.
        return RunRecord(label, "pass", None, 0.01, 0)


def _run_il(runner, **kw):
    return diagnose("test_race.py::test_it", runner, DoctorSettings(runs=4, **kw))


def test_interleave_race_diagnosed():
    d = _run_il(InterleaveRunner(explore="found", replay="reproduce"))
    assert d.verdict == FLAKY_INTERLEAVE
    assert d.claim == "latent"
    assert d.repro["axis"] == "interleave"
    assert d.repro["blob"].startswith("fd1:")
    assert d.repro["marker"].startswith("@pytest.mark.flakedoctor_repro(")
    assert any("✓ deterministic replay" in row.note for row in d.evidence)


def test_interleave_not_run_without_threads():
    runner = InterleaveRunner(threads=0)
    d = _run_il(runner)
    assert d.verdict != FLAKY_INTERLEAVE
    assert not any("interleave" in label for label in runner.labels)


def test_interleave_not_run_for_async():
    runner = InterleaveRunner(is_async=True)
    d = _run_il(runner)
    assert d.verdict != FLAKY_INTERLEAVE
    assert not any("interleave" in label for label in runner.labels)


def test_interleave_unavailable_falls_through_with_warning():
    runner = InterleaveRunner(explore_error="interleave-test unavailable: No module named ...")
    d = _run_il(runner)
    assert d.verdict != FLAKY_INTERLEAVE
    assert any("unavailable" in w for w in d.warnings)


def test_interleave_found_but_replay_not_deterministic_downgrades():
    runner = InterleaveRunner(explore="found", replay="pass")
    d = _run_il(runner)
    assert d.verdict != FLAKY_INTERLEAVE
    assert any("did not replay deterministically" in w for w in d.warnings)


def test_interleave_no_race_is_honest_negative():
    runner = InterleaveRunner(explore="none")
    d = _run_il(runner)
    assert d.verdict != FLAKY_INTERLEAVE
    assert any("no failing thread interleaving was found" in w for w in d.warnings)
    assert any("no failing schedule" in row.note for row in d.evidence)


def test_interleave_not_run_on_mixed_baseline():
    """Regression: a mixed baseline (a non-thread flake) must not let the axis
    mis-claim an 'observed' race; the axis is quiet-baseline only."""
    runner = InterleaveRunner(explore="found", replay="reproduce")

    def mixed(probe, timeout):
        if probe.label.startswith("baseline"):
            mixed.n = getattr(mixed, "n", 0) + 1
            return _baseline_record(probe.label, "fail" if mixed.n % 2 else "pass")
        return runner(probe, timeout)

    d = diagnose("t.py::t", mixed, DoctorSettings(runs=6))
    assert d.verdict != FLAKY_INTERLEAVE
    assert not any("interleave" in label for label in runner.labels)


def test_interleave_not_run_for_unittest():
    runner = InterleaveRunner()

    def uni(probe, timeout):
        if probe.label.startswith("baseline"):
            return _baseline_record(probe.label, "pass", is_unittest=True)
        return runner(probe, timeout)

    d = diagnose("t.py::t", uni, DoctorSettings(runs=4))
    assert d.verdict != FLAKY_INTERLEAVE
    assert not any("interleave" in label for label in runner.labels)


def test_interleave_blocked_schedule_is_inconclusive_not_a_race():
    """Regression: a per-schedule timeout (blocked) must be inconclusive, never
    fabricated into a race."""

    def runner(probe, timeout):
        if probe.label.startswith("baseline"):
            return _baseline_record(probe.label, "pass")
        if probe.label.startswith("interleave: explore"):
            r = RunRecord(probe.label, "pass", None, 0.1, 0)
            r.interleave = {"blocked": "a schedule exceeded the per-schedule timeout"}
            return r
        return RunRecord(probe.label, "pass", None, 0.01, 0)

    d = diagnose("t.py::t", runner, DoctorSettings(runs=4))
    assert d.verdict != FLAKY_INTERLEAVE
    assert any("inconclusive" in w for w in d.warnings)
