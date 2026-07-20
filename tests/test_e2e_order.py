"""End-to-end: diagnosing a real test-order dependency through subprocesses.

A parent `pytest --doctor` collects a suite, observes the victim fail, and the
order axis bisects the collection to the polluter — then the printed repro
command is run to confirm it reproduces.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# A classic order dependency via leaked module-global state. The victim passes
# alone (the global starts empty) but fails after the polluter mutates it.
# Interposed innocent tests give the bisection something to narrow away.
SUITE = '''\
import state_mod


def test_innocent_a():
    assert True


def test_polluter():
    state_mod.LEAKED.append("dirty")


def test_innocent_b():
    assert True


def test_innocent_c():
    assert True


def test_victim():
    assert state_mod.LEAKED == [], f"global was polluted: {state_mod.LEAKED}"
'''

STATE_MOD = "LEAKED = []\n"


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


def _write_suite(tmp_path):
    (tmp_path / "state_mod.py").write_text(STATE_MOD, encoding="utf-8")
    (tmp_path / "test_suite.py").write_text(SUITE, encoding="utf-8")


def test_order_axis_diagnoses_polluter_end_to_end(tmp_path):
    _write_suite(tmp_path)
    report = tmp_path / "report.json"
    # Suite mode: run everything; the victim fails, and the doctor diagnoses it.
    proc = _pytest(
        tmp_path, ["test_suite.py", "--doctor", "--doctor-json", str(report), "-p", "no:randomly"]
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    assert payload["verdict"] == "flaky-order", payload["headline"]
    assert payload["nodeid"] == "test_suite.py::test_victim"
    assert payload["repro"]["polluters"] == ["test_suite.py::test_polluter"]
    assert "test-order dependent" in payload["headline"]
    # The named polluter must be in the diagnosis text and command.
    assert "test_polluter" in payload["repro"]["command"]
    assert "DIAGNOSIS" in proc.stdout


def test_order_repro_reproduces_the_failure(tmp_path):
    _write_suite(tmp_path)
    report = tmp_path / "report.json"
    proc = _pytest(
        tmp_path, ["test_suite.py", "--doctor", "--doctor-json", str(report), "-p", "no:randomly"]
    )
    payload = json.loads(report.read_text())
    blob = payload["repro"]["blob"]

    # Running just [polluter, victim] under the blob must reproduce the failure.
    verify = _pytest(
        tmp_path,
        [
            "test_suite.py::test_polluter",
            "test_suite.py::test_victim",
            f"--doctor-repro={blob}",
        ],
    )
    assert verify.returncode == 1, verify.stdout
    assert "was polluted" in verify.stdout


def test_order_repro_enforces_order_regardless_of_arg_order(tmp_path):
    """The blob must make it reproduce even when the victim is named first."""
    _write_suite(tmp_path)
    report = tmp_path / "report.json"
    _pytest(tmp_path, ["test_suite.py", "--doctor", "--doctor-json", str(report), "-p", "no:randomly"])
    payload = json.loads(report.read_text())
    blob = payload["repro"]["blob"]

    # Victim named FIRST on the command line; the blob reorders it to run last.
    verify = _pytest(
        tmp_path,
        [
            "test_suite.py::test_victim",
            "test_suite.py::test_polluter",
            f"--doctor-repro={blob}",
        ],
    )
    assert verify.returncode == 1, verify.stdout


def test_order_across_a_large_prefix(tmp_path):
    """A big prefix must not overflow argv/env: nodeids travel via a file, and
    argv lists deduplicated file paths, not every nodeid."""
    (tmp_path / "app_state.py").write_text("STATE = {}\n", encoding="utf-8")
    lines = ["import app_state\n\n"]
    for i in range(25):
        lines.append(f"def test_pre_{i:03d}():\n    assert True\n\n")
    lines.append("def test_the_polluter():\n    app_state.STATE['leak'] = 1\n\n")
    for i in range(25):
        lines.append(f"def test_post_{i:03d}():\n    assert True\n\n")
    lines.append(
        "def test_victim():\n    assert app_state.STATE == {}, f'polluted: {app_state.STATE}'\n"
    )
    (tmp_path / "test_big.py").write_text("".join(lines), encoding="utf-8")
    report = tmp_path / "report.json"
    proc = _pytest(
        tmp_path,
        ["test_big.py", "--doctor", "--doctor-json", str(report), "-p", "no:randomly",
         "--doctor-budget", "200"],
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    assert payload["verdict"] == "flaky-order", payload["headline"]
    assert payload["repro"]["polluters"] == ["test_big.py::test_the_polluter"]


def test_victim_selected_alone_skips_order(tmp_path):
    """Single-test invocation has no prefix; order cannot be checked."""
    _write_suite(tmp_path)
    report = tmp_path / "report.json"
    proc = _pytest(
        tmp_path,
        [
            "test_suite.py::test_victim",
            "--doctor",
            "--doctor-runs",
            "3",
            "--doctor-json",
            str(report),
        ],
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    # It passes alone, so not flaky, and the report says how to check order.
    assert payload["verdict"] == "not-flaky"
    assert any("whole suite" in w for w in payload["warnings"])
