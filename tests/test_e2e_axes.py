"""End-to-end: real `pytest --doctor` runs diagnosing hermetic-axis flakes.

These spawn a parent pytest which spawns diagnostic children, exactly as a
user would, and then check that the printed repro command genuinely
reproduces the failure.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

hermetic = pytest.importorskip("hermetic", reason="hermetic-sandbox drives these axes")

SRC = Path(__file__).resolve().parent.parent / "src"

# Fails only when the frozen clock lands in 2020 — the doctor's adversarial
# instants are all in 2020, while the real clock never is. Anchoring on the
# real year (rather than on "is today month-end?") keeps the unperturbed
# baseline green on every calendar day, so this test cannot flake.
TIME_FLAKY = '''\
import datetime

REAL_YEAR = 2026


def test_ledger_year_is_current():
    today = datetime.date.today()
    assert today.year >= REAL_YEAR, f"ledger year {today.year} is stale"
'''

def rng_flaky_corpus() -> str:
    """A test that fails only under the exact seed the doctor sweeps first.

    Self-calibrating on purpose: the forbidden value is computed from hermetic
    here rather than hardcoded, so the corpus cannot drift out of sync with
    hermetic's seeding. The unseeded failure rate is 1e-6, which keeps the
    baseline reliably green — a corpus that flakes on its own would let an
    unrelated axis look guilty by chance.
    """
    with hermetic.Sandbox(0, clock="off", rng="all", network="off", fs="off"):
        forbidden = random.randrange(10**6)
    return (
        "import random\n\n"
        f"FORBIDDEN = {forbidden}\n\n\n"
        "def test_token_is_not_reserved():\n"
        "    token = random.randrange(10**6)\n"
        '    assert token != FORBIDDEN, f"drew the reserved token {token}"\n'
    )

NETWORK_FLAKY = '''\
import socket


def test_resolves_upstream():
    socket.create_connection(("example.com", 80), timeout=2).close()
'''

STABLE = "def test_ok():\n    assert True\n"


def _env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    for key in ("PYTHONHASHSEED", "FLAKEDOCTOR_CHILD", "FLAKEDOCTOR_PROBE", "PYTEST_ADDOPTS"):
        env.pop(key, None)
    return env


def _pytest(cwd, args, env=None, timeout=600):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "flakedoctor._plugin", *args],
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
        tmp_path,
        [nodeid, "--doctor", "--doctor-json", str(report), *extra],
    )
    assert report.exists(), proc.stdout + proc.stderr
    return proc, json.loads(report.read_text())


def test_time_axis_diagnosis_and_repro(tmp_path):
    proc, payload = _diagnose(tmp_path, TIME_FLAKY, "test_corpus.py::test_ledger_year_is_current")
    assert payload["verdict"] == "flaky-time", payload["headline"]
    assert payload["repro"] is not None, payload["headline"]
    assert payload["repro"]["axis"] == "time"
    assert payload["repro"]["blob"].startswith("fd1:")
    assert payload["repro"]["value"].startswith("frozen@")
    assert payload["stats"]["verify"]["failed"] == payload["stats"]["verify"]["runs"]

    # The printed blob must actually reproduce the failure.
    blob = payload["repro"]["blob"]
    verify = _pytest(
        tmp_path, ["test_corpus.py::test_ledger_year_is_current", f"--doctor-repro={blob}"]
    )
    assert verify.returncode == 1, verify.stdout
    assert "is stale" in verify.stdout


def test_rng_axis_diagnosis(tmp_path):
    proc, payload = _diagnose(
        tmp_path, rng_flaky_corpus(), "test_corpus.py::test_token_is_not_reserved"
    )
    assert payload["verdict"] == "flaky-rng", payload["headline"]
    assert payload["repro"]["axis"] == "rng"
    # Latent, not observed: the baseline never failed, and the report must say so.
    assert payload["claim"] == "latent"
    blob = payload["repro"]["blob"]
    verify = _pytest(
        tmp_path, ["test_corpus.py::test_token_is_not_reserved", f"--doctor-repro={blob}"]
    )
    assert verify.returncode == 1, verify.stdout


def test_did_not_reproduce_exits_nonzero(tmp_path):
    """A repro that goes green must fail loudly, never silently pass."""
    _, payload = _diagnose(tmp_path, TIME_FLAKY, "test_corpus.py::test_ledger_year_is_current")
    assert payload["repro"] is not None, payload["verdict"]
    blob = payload["repro"]["blob"]
    (tmp_path / "test_corpus.py").write_text(
        TIME_FLAKY + "\n\ndef test_unaffected():\n    assert True\n", encoding="utf-8"
    )
    verify = _pytest(tmp_path, ["test_corpus.py::test_unaffected", f"--doctor-repro={blob}"])
    assert verify.returncode != 0
    assert "DID NOT REPRODUCE" in verify.stdout


def test_repro_blob_requires_matching_hashseed(tmp_path):
    """Hash randomization is fixed at startup, so the blob cannot apply it itself."""
    from flakedoctor._axes import HASHSEED
    from flakedoctor._repro import Repro

    blob = Repro(values=[HASHSEED.provocations()[0]], nodeid="test_corpus.py::test_ok").encode()
    (tmp_path / "test_corpus.py").write_text(STABLE, encoding="utf-8")
    env = _env()
    env["PYTHONHASHSEED"] = "999"
    proc = _pytest(tmp_path, ["test_corpus.py::test_ok", f"--doctor-repro={blob}"], env=env)
    assert proc.returncode == 4
    assert "needs PYTHONHASHSEED=0" in (proc.stdout + proc.stderr)


def test_doctor_and_repro_are_mutually_exclusive(tmp_path):
    (tmp_path / "test_corpus.py").write_text(STABLE, encoding="utf-8")
    proc = _pytest(tmp_path, ["test_corpus.py::test_ok", "--doctor", "--doctor-repro=fd1:x"])
    assert proc.returncode == 4
    assert "mutually exclusive" in (proc.stdout + proc.stderr)


def test_corrupt_blob_is_a_usage_error(tmp_path):
    (tmp_path / "test_corpus.py").write_text(STABLE, encoding="utf-8")
    proc = _pytest(tmp_path, ["test_corpus.py::test_ok", "--doctor-repro=not-a-blob"])
    assert proc.returncode == 4
    assert "not a flakedoctor repro blob" in (proc.stdout + proc.stderr)


def test_async_test_skips_the_clock_axis(tmp_path):
    """Virtual time hangs awaited sleeps, so the clock axis must stand down."""
    pytest.importorskip("pytest_asyncio", reason="async marker support")
    body = (
        "import asyncio, pytest\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_async_ok():\n"
        "    await asyncio.sleep(0)\n"
        "    assert True\n"
    )
    (tmp_path / "test_corpus.py").write_text(body, encoding="utf-8")
    report = tmp_path / "report.json"
    proc = _pytest(
        tmp_path,
        [
            "test_corpus.py::test_async_ok",
            "--doctor",
            "--doctor-runs",
            "3",
            "--doctor-json",
            str(report),
        ],
        timeout=300,
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    # It must finish (not hang) and say the clock axis stood down.
    assert payload["verdict"] in ("not-flaky", "flaky-unattributed")
    assert any("async" in w for w in payload["warnings"]), payload["warnings"]


def test_stable_test_survives_every_axis(tmp_path):
    """No axis may manufacture a failure in a genuinely stable test."""
    _, payload = _diagnose(tmp_path, STABLE, "test_corpus.py::test_ok", extra=("--doctor-runs", "4"))
    assert payload["verdict"] == "not-flaky", payload["headline"]
    assert payload["repro"] is None
