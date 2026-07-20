"""End-to-end: a real `pytest --doctor` parent diagnosing a synthetic corpus.

These tests spawn a parent pytest which spawns diagnostic children, exactly
as a user would. They are the slowest tests in the suite (~1-2 minutes total).

The hashseed corpus is calibrated to be attributable on BOTH string-hash
algorithms CPython ships: siphash24 (<=3.10) and siphash13 (>=3.11). With the
fruit set and failing subset below, PYTHONHASHSEED=0 puts a *failing* element
first on both ("elderberry" on 3.10, "banana" on 3.11+), so the doctor's first
provocation seed reproduces the flake on every supported version — and random
seeds fail ~50% of the time, so a 10-run baseline is virtually guaranteed to be
MIXED. (Earlier this only held on 3.11+, which made the diagnosis flaky on 3.10:
its seeds 0/1/2 all landed on passing elements, leaving provocation to two
random seeds.)
"""

from __future__ import annotations

import pytest

HASHSEED_FLAKY = '''\
def test_first_fruit():
    fruits = {"apple", "banana", "cherry", "date", "elderberry", "fig"}
    first = next(iter(fruits))
    assert first not in {"apple", "banana", "elderberry"}
'''

STABLE = "def test_stable():\n    assert 1 + 1 == 2\n"

ALWAYS_FAILS = "def test_broken():\n    assert 1 == 2\n"

# A flake no covered axis can reach or determinize: process-id parity.
# hermetic does not virtualize getpid, and consecutively spawned children get
# consecutive pids, so this alternates pass/fail *deterministically*. That
# makes the baseline reliably MIXED, keeps every 2-rep sweep at exactly 1/2
# (so no value ever looks like a strong candidate), and holds verification at
# 5/10 (so nothing clears the elevation bar). The doctor must therefore refuse
# to name a cause — which is exactly what this test asserts. Using randomness
# here instead would be self-defeating: the rng axis now controls it.
UNCONTROLLED_FLAKY = (
    "import os\n"
    "def test_pid_parity():\n"
    "    assert os.getpid() % 2 == 0, 'odd pid'\n"
)

HANGS = "import time\ndef test_stuck():\n    time.sleep(300)\n"

SUITE = (
    "def test_fine():\n    assert True\n"
    "def test_broken():\n    assert 1 == 2\n"
)


def test_e2e_hashseed_diagnosis(run_doctor):
    proc, payload = run_doctor(HASHSEED_FLAKY, "test_corpus.py::test_first_fruit")
    assert payload is not None, proc.stdout + "\n" + proc.stderr
    assert payload["format"] == "flakedoctor-report"
    assert payload["version"] == 1
    assert payload["verdict"] == "flaky-hashseed"
    assert payload["claim"] == "observed"
    assert payload["repro"]["axis"] == "hashseed"
    # Which seed triggers the failure depends on the interpreter's hash
    # algorithm, so assert the mechanism, not a calibrated constant.
    seed = payload["repro"]["value"]
    assert f"PYTHONHASHSEED={seed}" in payload["repro"]["command"]
    assert payload["stats"]["verify"]["cp_lower_95"] == pytest.approx(0.7411, abs=0.001)
    cf = payload["stats"]["counterfactual"]
    assert cf is not None and cf["passed"] is True
    baseline = payload["stats"]["baseline"]
    assert 0 < baseline["failed"] < baseline["runs"]
    # The terminal block rendered and the run exited cleanly.
    assert "flakedoctor" in proc.stdout
    assert "DIAGNOSIS" in proc.stdout
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr


def test_e2e_stable_test(run_doctor):
    proc, payload = run_doctor(STABLE, "test_corpus.py::test_stable")
    assert payload is not None, proc.stdout + "\n" + proc.stderr
    assert payload["verdict"] == "not-flaky"
    assert payload["repro"] is None
    assert proc.returncode == 0


def test_e2e_deterministic_failure(run_doctor):
    proc, payload = run_doctor(ALWAYS_FAILS, "test_corpus.py::test_broken")
    assert payload is not None, proc.stdout + "\n" + proc.stderr
    assert payload["verdict"] == "deterministic-failure"
    assert "10/10" in payload["headline"]


def test_e2e_unattributed_flake(run_doctor):
    """The tool must refuse to name a cause it cannot demonstrate."""
    proc, payload = run_doctor(UNCONTROLLED_FLAKY, "test_corpus.py::test_pid_parity")
    assert payload is not None, proc.stdout + "\n" + proc.stderr
    assert payload["verdict"] == "flaky-unattributed", payload["headline"]
    assert payload["repro"] is None
    baseline = payload["stats"]["baseline"]
    assert 0 < baseline["failed"] < baseline["runs"]


def test_e2e_hang(run_doctor):
    proc, payload = run_doctor(
        HANGS,
        "test_corpus.py::test_stuck",
        extra_args=("--doctor-runs", "2", "--doctor-budget", "12"),
        timeout=120,
    )
    assert payload is not None, proc.stdout + "\n" + proc.stderr
    assert payload["verdict"] == "hang"
    assert "timed out" in payload["headline"]


def test_e2e_suite_mode_diagnoses_first_failure(run_doctor):
    proc, payload = run_doctor(SUITE, "test_corpus.py")
    assert payload is not None, proc.stdout + "\n" + proc.stderr
    assert payload["nodeid"] == "test_corpus.py::test_broken"
    # test_broken fails every run in isolation, so it's a plain deterministic
    # failure, not an order dependency — the order axis correctly stays out.
    assert payload["verdict"] == "deterministic-failure"
    assert "diagnosing first failure" in proc.stdout
    # The suite itself still fails normally (test_broken really failed).
    assert proc.returncode == 1
