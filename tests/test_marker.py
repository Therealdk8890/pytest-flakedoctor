"""The @pytest.mark.flakedoctor_repro marker: a diagnosed flake, pasted into
the test file, reproduces deterministically on ordinary runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

hermetic = pytest.importorskip("hermetic", reason="the sandbox axes need hermetic-sandbox")

SRC = Path(__file__).resolve().parent.parent / "src"

from _support import pytest_argv  # noqa: E402  (tests dir is on sys.path)

TIME_FLAKY = '''\
import datetime


def test_year_is_current():
    assert datetime.date.today().year >= 2026, "clock rolled back"
'''


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


def _diagnose_marker(tmp_path, body, nodeid):
    (tmp_path / "test_flaky.py").write_text(body, encoding="utf-8")
    report = tmp_path / "report.json"
    _pytest(tmp_path, [nodeid, "--doctor", "--doctor-json", str(report)])
    payload = json.loads(report.read_text())
    return payload["repro"]["marker"]


def test_time_marker_reproduces_on_a_normal_run(tmp_path):
    marker = _diagnose_marker(tmp_path, TIME_FLAKY, "test_flaky.py::test_year_is_current")
    assert marker and marker.startswith("@pytest.mark.flakedoctor_repro(")

    # Paste the marker above the test and run pytest normally (no --doctor).
    (tmp_path / "test_flaky.py").write_text(
        "import datetime\nimport pytest\n\n\n"
        f"{marker}\n"
        "def test_year_is_current():\n"
        '    assert datetime.date.today().year >= 2026, "clock rolled back"\n',
        encoding="utf-8",
    )
    proc = _pytest(tmp_path, ["test_flaky.py", "-q"])
    assert proc.returncode == 1, proc.stdout  # the flake now reproduces
    assert "clock rolled back" in proc.stdout
    assert "applied repro markers to 1 test" in proc.stdout


def test_marker_is_inert_without_the_blob_matching_the_test(tmp_path):
    """A marker whose blob was recorded for a different test is not applied."""
    marker = _diagnose_marker(tmp_path, TIME_FLAKY, "test_flaky.py::test_year_is_current")
    # Same marker, but on a differently-named test.
    (tmp_path / "test_other.py").write_text(
        "import pytest\n\n\n"
        f"{marker}\n"
        "def test_something_else():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    proc = _pytest(tmp_path, ["test_other.py", "-q"])
    assert proc.returncode == 0, proc.stdout  # not applied → still passes
    assert "different test" in proc.stdout


def test_hashseed_marker_reports_the_env_requirement(tmp_path):
    """Hashseed can't be applied in-process; the marker says how instead."""
    from flakedoctor._axes import HASHSEED
    from flakedoctor._repro import Repro

    blob = Repro(
        values=[HASHSEED.provocations()[0]], nodeid="test_hs.py::test_hs"
    ).encode()
    (tmp_path / "test_hs.py").write_text(
        "import pytest\n\n\n"
        f'@pytest.mark.flakedoctor_repro("{blob}")\n'
        "def test_hs():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    env = _env()
    env["PYTHONHASHSEED"] = "999"  # not the blob's seed
    proc = _pytest(tmp_path, ["test_hs.py", "-q"], env=env)
    assert proc.returncode == 0  # can't apply; test runs unperturbed
    assert "PYTHONHASHSEED=0" in proc.stdout


def test_unknown_marker_is_recognized_not_warned(tmp_path):
    """The marker must be registered so pytest doesn't warn it's unknown."""
    (tmp_path / "test_reg.py").write_text(
        "import pytest\n\n\n"
        '@pytest.mark.flakedoctor_repro("fd1:whatever")\n'
        "def test_reg():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    proc = _pytest(tmp_path, ["test_reg.py", "-q", "-W", "error::pytest.PytestUnknownMarkWarning"])
    # A bad blob is reported, but the marker itself is a known mark (no
    # PytestUnknownMarkWarning turning into an error).
    assert "PytestUnknownMarkWarning" not in proc.stdout + proc.stderr


def test_marker_inert_in_diagnostic_children(tmp_path):
    """Running --doctor on a marked test must not double-apply the marker in
    the diagnostic subprocesses."""
    marker = _diagnose_marker(tmp_path, TIME_FLAKY, "test_flaky.py::test_year_is_current")
    (tmp_path / "test_flaky.py").write_text(
        "import datetime\nimport pytest\n\n\n"
        f"{marker}\n"
        "def test_year_is_current():\n"
        '    assert datetime.date.today().year >= 2026, "clock rolled back"\n',
        encoding="utf-8",
    )
    report = tmp_path / "report2.json"
    # Diagnosing the marked test still works — the children (FLAKEDOCTOR_CHILD=1)
    # ignore the marker, so the baseline still sees it pass alone.
    proc = _pytest(
        tmp_path,
        ["test_flaky.py::test_year_is_current", "--doctor", "--doctor-runs", "3",
         "--doctor-json", str(report)],
    )
    assert report.exists(), proc.stdout + proc.stderr
    payload = json.loads(report.read_text())
    assert payload["verdict"] == "flaky-time", payload["headline"]


# ------------------------------------------- marker review regressions

def test_malformed_marker_does_not_crash_the_session(tmp_path):
    """A committed bad blob must skip one reproduction, never abort CI."""
    (tmp_path / "test_bad.py").write_text(
        "import pytest\n\n\n"
        '@pytest.mark.flakedoctor_repro(b"fd1:not-even-a-string")\n'  # bytes -> would TypeError
        "def test_marked():\n    assert True\n\n\n"
        "def test_innocent():\n    assert True\n",
        encoding="utf-8",
    )
    proc = _pytest(tmp_path, ["test_bad.py", "-q"])
    assert "INTERNALERROR" not in (proc.stdout + proc.stderr)
    assert "2 passed" in proc.stdout  # both tests ran
    assert "bad repro marker" in proc.stdout


def test_rng_marker_warns_under_pytest_randomly(tmp_path):
    """pytest-randomly reseeds per test, defeating a pinned rng seed — the
    marker must say so instead of silently not reproducing."""
    pytest.importorskip("pytest_randomly", reason="needs pytest-randomly installed")
    from flakedoctor._axes import RNG
    from flakedoctor._repro import Repro

    nodeid = "test_rng.py::test_rng"
    blob = Repro(values=[RNG.provocations()[0]], nodeid=nodeid, confirm=(10, 10)).encode()
    (tmp_path / "test_rng.py").write_text(
        "import pytest\n\n\n"
        f'@pytest.mark.flakedoctor_repro("{blob}")\n'
        "def test_rng():\n    assert True\n",
        encoding="utf-8",
    )
    # pytest-randomly is autoloaded; the marker must warn (not claim success).
    proc = _pytest(tmp_path, ["test_rng.py", "-q"])
    assert "reseeds randomness" in proc.stdout
    assert "-p no:randomly" in proc.stdout


def test_marker_does_not_contaminate_a_session_fixture(tmp_path):
    """A session fixture built during a marked test must not be frozen and then
    leak that frozen state into other tests (marker wraps only the call phase)."""
    marker = _diagnose_marker(tmp_path, TIME_FLAKY, "test_flaky.py::test_year_is_current")
    (tmp_path / "conftest.py").write_text(
        "import datetime, pytest\n\n"
        "@pytest.fixture(scope='session')\n"
        "def first_seen_year():\n"
        "    return datetime.date.today().year\n",
        encoding="utf-8",
    )
    # A marked test that USES the session fixture, and a later test that checks
    # the fixture saw the REAL year (not 2020 from a leaked frozen clock).
    (tmp_path / "test_flaky.py").write_text(
        "import datetime, pytest\n\n\n"
        f"{marker}\n"
        "def test_year_is_current(first_seen_year):\n"
        '    assert datetime.date.today().year >= 2026, "clock rolled back"\n\n\n'
        "def test_fixture_saw_real_year(first_seen_year):\n"
        "    assert first_seen_year >= 2026, f'session fixture was frozen to {first_seen_year}'\n",
        encoding="utf-8",
    )
    proc = _pytest(tmp_path, ["test_flaky.py", "-q", "-p", "no:randomly"])
    # The marked test reproduces (fails); the fixture-check test must PASS
    # (fixture saw the real year, i.e. was not built under the frozen clock).
    assert "test_fixture_saw_real_year" not in proc.stdout or "1 failed, 1 passed" in proc.stdout
    assert "was frozen" not in proc.stdout
