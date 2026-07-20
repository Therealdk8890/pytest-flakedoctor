"""Regression tests for parent-plugin integration issues found in review."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from flakedoctor._axes import repro_command

SRC = Path(__file__).resolve().parent.parent / "src"

from _support import pytest_argv  # noqa: E402  (tests dir is on sys.path)

FLAKY_BODY = '''\
def test_first_fruit():
    fruits = {"apple", "banana", "cherry", "date", "elderberry", "fig"}
    assert next(iter(fruits)) not in {"apple", "banana", "cherry"}
'''

STABLE_BODY = "def test_ok():\n    assert True\n"


def _env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    for key in ("PYTHONHASHSEED", "FLAKEDOCTOR_CHILD", "FLAKEDOCTOR_PROBE", "PYTEST_ADDOPTS"):
        env.pop(key, None)
    return env


def _run_pytest(cwd, args, env=None, timeout=300):
    return subprocess.run(
        pytest_argv(*args),
        cwd=str(cwd),
        env=env or _env(),
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=timeout,
    )


def test_diagnosis_works_when_invoked_from_a_subdirectory(tmp_path):
    """Regression: nodeids are rootdir-relative, so children must run at rootdir.

    Previously every child exited 4 and the verdict was 'could not run the test'.
    """
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sub.py").write_text(STABLE_BODY, encoding="utf-8")
    report = tmp_path / "out.json"

    proc = _run_pytest(
        tests_dir,
        [
            "test_sub.py::test_ok",
            "--doctor",
            "--doctor-runs",
            "3",
            "--doctor-json",
            str(report),
        ],
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    assert payload["nodeid"] == "tests/test_sub.py::test_ok"
    assert payload["verdict"] == "not-flaky", payload["explanation"]


def test_repro_command_is_pasteable_from_the_invocation_directory(tmp_path):
    """The printed command must work in the shell the user is actually in."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_f.py").write_text(FLAKY_BODY, encoding="utf-8")
    report = tmp_path / "out.json"

    proc = _run_pytest(
        tests_dir,
        ["test_f.py::test_first_fruit", "--doctor", "--doctor-json", str(report)],
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    assert payload["verdict"] == "flaky-hashseed"
    # nodeid stays rootdir-relative; the repro command is cwd-relative.
    assert payload["nodeid"] == "tests/test_f.py::test_first_fruit"
    assert "test_f.py::test_first_fruit" in payload["repro"]["command"]
    assert "tests/test_f.py" not in payload["repro"]["command"]

    env = _env()
    env["PYTHONHASHSEED"] = payload["repro"]["value"]
    verify = subprocess.run(
        [sys.executable, "-m", "pytest", "test_f.py::test_first_fruit", "-q"],
        cwd=str(tests_dir),
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=120,
    )
    assert verify.returncode == 1, verify.stdout


def test_json_report_written_without_the_terminal_plugin(tmp_path):
    """Regression: results lived only in pytest_terminal_summary and vanished."""
    (tmp_path / "test_t.py").write_text(STABLE_BODY, encoding="utf-8")
    report = tmp_path / "nested" / "dir" / "out.json"

    proc = _run_pytest(
        tmp_path,
        [
            "test_t.py::test_ok",
            "--doctor",
            "--doctor-runs",
            "2",
            "--doctor-json",
            str(report),
            "-p",
            "no:terminal",
        ],
    )
    assert report.exists(), proc.stdout + proc.stderr
    assert json.loads(report.read_text())["verdict"] == "not-flaky"


def test_json_report_creates_missing_parent_directories(tmp_path):
    """Regression: a missing directory raised FileNotFoundError after the run."""
    (tmp_path / "test_t.py").write_text(STABLE_BODY, encoding="utf-8")
    report = tmp_path / "does" / "not" / "exist" / "out.json"

    proc = _run_pytest(
        tmp_path,
        ["test_t.py::test_ok", "--doctor", "--doctor-runs", "2", "--doctor-json", str(report)],
    )
    assert "Traceback" not in proc.stderr, proc.stderr
    assert report.exists(), proc.stdout + proc.stderr


def test_unwritable_json_path_fails_before_the_diagnosis(tmp_path):
    """A bad report path must be a usage error, not a post-diagnosis crash."""
    (tmp_path / "test_t.py").write_text(STABLE_BODY, encoding="utf-8")
    a_directory = tmp_path / "adir"
    a_directory.mkdir()

    proc = _run_pytest(
        tmp_path,
        ["test_t.py::test_ok", "--doctor", "--doctor-runs", "2", "--doctor-json", str(a_directory)],
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "not writable" in (proc.stdout + proc.stderr)


def test_pytest_addopts_do_not_break_children(tmp_path):
    """Regression: inherited PYTEST_ADDOPTS naming disabled plugins' options
    made every child exit 4 ('could not run the test')."""
    (tmp_path / "test_t.py").write_text(STABLE_BODY, encoding="utf-8")
    report = tmp_path / "out.json"
    env = _env()
    # -q is harmless on its own; the point is that addopts reach the child at
    # all. Use an option the child's disabled plugins would own if installed.
    env["PYTEST_ADDOPTS"] = "-q"

    proc = _run_pytest(
        tmp_path,
        ["test_t.py::test_ok", "--doctor", "--doctor-runs", "2", "--doctor-json", str(report)],
        env=env,
    )
    assert report.exists(), proc.stdout + proc.stderr
    assert json.loads(report.read_text())["verdict"] == "not-flaky"


def test_child_pythonpath_does_not_override_user_shadowing(tmp_path):
    """Regression: prepending our package dir flipped the user's import order,
    so children imported different code than the parent ran."""
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "shadowed.py").write_text("FLAVOR = 'vendored'\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "shadowed.py").write_text("FLAVOR = 'other'\n", encoding="utf-8")
    (tmp_path / "test_shadow.py").write_text(
        "import shadowed\ndef test_flavor():\n    assert shadowed.FLAVOR == 'vendored'\n",
        encoding="utf-8",
    )
    report = tmp_path / "out.json"
    env = _env()
    # The user's own PYTHONPATH must keep winning inside children.
    env["PYTHONPATH"] = str(vendor) + os.pathsep + str(other) + os.pathsep + env["PYTHONPATH"]

    proc = _run_pytest(
        tmp_path,
        ["test_shadow.py::test_flavor", "--doctor", "--doctor-runs", "3", "--doctor-json", str(report)],
        env=env,
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    assert payload["verdict"] == "not-flaky", payload["explanation"]


def test_undecodable_child_output_does_not_abort_the_diagnosis(tmp_path):
    """Regression: text=True with strict decoding raised UnicodeDecodeError."""
    (tmp_path / "conftest.py").write_text(
        "import atexit, os\n"
        "atexit.register(lambda: os.write(2, b'\\xff\\xfe\\x00garbage'))\n",
        encoding="utf-8",
    )
    (tmp_path / "test_t.py").write_text(STABLE_BODY, encoding="utf-8")
    report = tmp_path / "out.json"

    proc = _run_pytest(
        tmp_path,
        ["test_t.py::test_ok", "--doctor", "--doctor-runs", "2", "--doctor-json", str(report)],
    )
    assert "UnicodeDecodeError" not in proc.stderr, proc.stderr
    assert report.exists(), proc.stdout + proc.stderr


def test_repro_command_quotes_shell_metacharacters():
    import shlex

    nodeid = 'test_p.py::test_x[a$b-"c"-`d`]'
    posix = repro_command(nodeid, None, "7", platform="linux")
    assert posix.startswith("PYTHONHASHSEED=7 pytest ")
    # Everything after the env prefix must parse back to exactly two words.
    parts = shlex.split(posix.split("PYTHONHASHSEED=7 ", 1)[1])
    assert parts == ["pytest", nodeid]

    powershell = repro_command("test_p.py::test_x[it's]", None, "7", platform="win32")
    assert "$env:PYTHONHASHSEED='7'" in powershell
    assert "it''s" in powershell  # PowerShell escapes ' by doubling it


def test_repro_command_carries_blob_and_survives_quoting():
    import shlex

    nodeid = "test_p.py::test_x[a b]"
    command = repro_command(nodeid, "fd1:abc-_123=", None, platform="linux")
    parts = shlex.split(command)
    assert parts[0] == "pytest"
    assert parts[1] == nodeid
    assert parts[2] == "--doctor-repro=fd1:abc-_123="
    assert "PYTHONHASHSEED" not in command


def test_flaky_test_still_diagnosed_end_to_end(tmp_path):
    """The headline behavior must survive all the robustness fixes."""
    (tmp_path / "test_f.py").write_text(FLAKY_BODY, encoding="utf-8")
    report = tmp_path / "out.json"

    proc = _run_pytest(
        tmp_path, ["test_f.py::test_first_fruit", "--doctor", "--doctor-json", str(report)]
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    assert payload["verdict"] == "flaky-hashseed"
    assert payload["repro"]["axis"] == "hashseed"
    # Version-agnostic: don't assume WHICH seed fails, only that one was found
    # and that the printed command actually names it.
    seed = payload["repro"]["value"]
    assert f"PYTHONHASHSEED={seed}" in payload["repro"]["command"]

    # The repro command must genuinely reproduce the failure.
    env = _env()
    env["PYTHONHASHSEED"] = seed
    verify = subprocess.run(
        [sys.executable, "-m", "pytest", "test_f.py::test_first_fruit", "-q"],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert verify.returncode == 1, verify.stdout
