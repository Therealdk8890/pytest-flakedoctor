"""End-to-end: diagnosing thread-interleaving races through real subprocesses.

The interleave axis drives the test through interleave-test's scheduler to find
a failing thread schedule, verifies it by fresh-subprocess replay, and emits a
schedule repro. These tests need the optional `interleave` extra.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("interleave_test", reason="the interleave axis needs interleave-test")
pytest.importorskip("hermetic", reason="the doctor's other axes need hermetic-sandbox")

SRC = Path(__file__).resolve().parent.parent / "src"

from _support import pytest_argv  # noqa: E402  (tests dir is on sys.path)

# A lost-update race: two threads read-modify-write a shared cell without a lock.
RACE = '''\
import threading


def test_counter():
    box = {"n": 0}

    def worker():
        v = box["n"]
        box["n"] = v + 1

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert box["n"] == 2, f"lost update: {box['n']}"
'''

# A correctly-locked version: threads present, but no race.
LOCKED = '''\
import threading


def test_counter_locked():
    box = {"n": 0}
    lock = threading.Lock()

    def worker():
        with lock:
            v = box["n"]
            box["n"] = v + 1

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert box["n"] == 2
'''

DEADLOCK = '''\
import threading


def test_lock_ordering():
    a, b = threading.Lock(), threading.Lock()

    def t1():
        with a:
            with b:
                pass

    def t2():
        with b:
            with a:
                pass

    ts = [threading.Thread(target=t1), threading.Thread(target=t2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
'''

NO_THREADS = "def test_serial():\n    assert sum(range(100)) == 4950\n"


def _env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    for key in ("PYTHONHASHSEED", "FLAKEDOCTOR_CHILD"):
        env.pop(key, None)
    return env


def _pytest(cwd, args, env=None, timeout=300):
    return subprocess.run(
        pytest_argv(*args),
        cwd=str(cwd),
        env=env or _env(),
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=timeout,
    )


def _diagnose(tmp_path, body, nodeid, extra=()):
    (tmp_path / "test_corpus.py").write_text(body, encoding="utf-8")
    report = tmp_path / "report.json"
    proc = _pytest(
        tmp_path, [nodeid, "--doctor", "--doctor-json", str(report), "--doctor-budget", "180", *extra]
    )
    assert report.exists(), proc.stdout + proc.stderr
    return proc, json.loads(report.read_text())


def test_race_is_diagnosed_and_replays(tmp_path):
    proc, payload = _diagnose(tmp_path, RACE, "test_corpus.py::test_counter")
    assert payload["verdict"] == "flaky-interleave", payload["headline"]
    assert payload["claim"] == "latent"  # passes alone; the scheduler surfaced it
    assert payload["repro"]["axis"] == "interleave"
    assert payload["repro"]["blob"].startswith("fd1:")
    assert payload["stats"]["interleave"]["kind"] != "deadlock"
    assert payload["stats"]["interleave"]["reproduced"] == payload["stats"]["interleave"]["runs"]
    assert "race condition" in payload["headline"]

    # The printed blob must actually reproduce the race.
    blob = payload["repro"]["blob"]
    verify = _pytest(tmp_path, ["test_corpus.py::test_counter", f"--doctor-repro={blob}"])
    assert verify.returncode == 1, verify.stdout
    assert "lost update" in verify.stdout


def test_race_marker_reproduces_on_a_normal_run(tmp_path):
    _, payload = _diagnose(tmp_path, RACE, "test_corpus.py::test_counter")
    marker = payload["repro"]["marker"]
    assert marker and marker.startswith("@pytest.mark.flakedoctor_repro(")
    (tmp_path / "test_corpus.py").write_text(
        "import threading\nimport pytest\n\n\n"
        f"{marker}\n" + RACE.split("\n", 2)[2],  # reuse the body under the marker
        encoding="utf-8",
    )
    proc = _pytest(tmp_path, ["test_corpus.py", "-q"])
    assert proc.returncode == 1, proc.stdout
    assert "lost update" in proc.stdout


def test_deadlock_is_diagnosed(tmp_path):
    _, payload = _diagnose(tmp_path, DEADLOCK, "test_corpus.py::test_lock_ordering")
    assert payload["verdict"] == "flaky-interleave"
    assert payload["stats"]["interleave"]["kind"] == "deadlock"
    assert "deadlock" in payload["headline"]


def test_correctly_locked_test_finds_no_race(tmp_path):
    """Threads present but properly synchronized: an honest negative, no false
    'race' — and the wording never claims 'no race exists'."""
    proc, payload = _diagnose(tmp_path, LOCKED, "test_corpus.py::test_counter_locked")
    assert payload["verdict"] != "flaky-interleave"
    assert payload["verdict"] in ("not-flaky", "flaky-unattributed")
    # The axis ran (evidence) and reported an honest, qualified negative.
    assert any("interleave search" in row["label"] for row in payload["evidence"])
    assert any("no failing thread interleaving was found" in w for w in payload["warnings"])


def test_non_threaded_test_does_not_activate_the_axis(tmp_path):
    proc, payload = _diagnose(
        tmp_path, NO_THREADS, "test_corpus.py::test_serial", extra=("--doctor-runs", "4")
    )
    assert payload["verdict"] == "not-flaky"
    assert not any("interleave" in row["label"] for row in payload["evidence"])
    assert not any("interleaving" in w for w in payload["warnings"])


# --------------------------------------------- review regressions

TIME_ASSERT_LOCKED = '''\
import threading, time


def test_locked_with_time():
    t0 = time.monotonic()
    lock = threading.Lock()
    box = {"n": 0}

    def w():
        with lock:
            box["n"] += 1

    ts = [threading.Thread(target=w) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert box["n"] == 2
    assert time.monotonic() > t0, "clock did not advance"
'''

# A module that imports asyncio at top level but is a plain sync threaded test.
RACE_IN_ASYNC_MODULE = '''\
import asyncio  # noqa: F401  (a common import that must not disable the axis)
import threading


def test_counter():
    box = {"n": 0}

    def worker():
        v = box["n"]
        box["n"] = v + 1

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert box["n"] == 2, f"lost update: {box['n']}"
'''


def test_real_time_assertion_is_not_a_false_race(tmp_path):
    """Regression: interleave-test's virtual clock made real-time assertions
    fail under every schedule; a race-free test must not be diagnosed as a race."""
    _, payload = _diagnose(tmp_path, TIME_ASSERT_LOCKED, "test_corpus.py::test_locked_with_time")
    assert payload["verdict"] != "flaky-interleave", payload["headline"]


def test_axis_runs_when_module_imports_asyncio(tmp_path):
    """Regression: the broad async heuristic wrongly skipped the axis for any
    module that imports asyncio; a sync threaded test there must still run it."""
    _, payload = _diagnose(tmp_path, RACE_IN_ASYNC_MODULE, "test_corpus.py::test_counter")
    assert payload["verdict"] == "flaky-interleave", payload["headline"]


FIXTURE_ACCUMULATES = '''\
import threading
import pytest


@pytest.fixture
def seen():
    return []


def test_accumulates(seen):
    lock = threading.Lock()

    def w():
        with lock:
            seen.append(1)

    ts = [threading.Thread(target=w) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(seen) == 2, f"unexpected count: {len(seen)}"
'''


def test_mutable_fixture_accumulation_is_not_a_false_race(tmp_path):
    """Regression: explore re-runs the body N times sharing the resolved fixture,
    so a mutable fixture accumulates — but the fresh-subprocess replay (one run)
    won't reproduce it, so the verify gate must reject the false race."""
    _, payload = _diagnose(tmp_path, FIXTURE_ACCUMULATES, "test_corpus.py::test_accumulates")
    assert payload["verdict"] != "flaky-interleave", payload["headline"]
