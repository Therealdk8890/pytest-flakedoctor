"""Subprocess runner classification against real pytest children."""

from __future__ import annotations

import sys

import pytest

from flakedoctor._runner import ProbeConfig, SubprocessRunner


def _run(tmp_path, nodeids, env=None, timeout=90.0):
    runner = SubprocessRunner(tmp_path)
    probe = ProbeConfig(nodeids=tuple(nodeids), env=env or {}, label="test")
    return runner.run(probe, timeout=timeout)


def test_pass(write_test_file, tmp_path):
    write_test_file("def test_ok():\n    assert True\n")
    rec = _run(tmp_path, ["test_target.py::test_ok"])
    assert rec.outcome == "pass"
    assert not rec.failed
    assert rec.fingerprint is None
    assert rec.phases.get("call") == "passed"
    assert rec.exit_code == 0


def test_fail_fingerprint(write_test_file, tmp_path):
    write_test_file("def test_bad():\n    assert 1 == 2\n")
    rec = _run(tmp_path, ["test_target.py::test_bad"])
    assert rec.outcome == "fail"
    fp = rec.fingerprint
    assert fp is not None
    assert fp.phase == "call"
    assert fp.exc_type == "AssertionError"
    assert fp.crash_site.endswith("test_target.py:2")


def test_setup_error_is_setup_phase(write_test_file, tmp_path):
    write_test_file(
        "import pytest\n"
        "@pytest.fixture\n"
        "def boom():\n"
        "    raise ValueError('bad fixture')\n"
        "def test_needs(boom):\n"
        "    assert True\n"
    )
    rec = _run(tmp_path, ["test_target.py::test_needs"])
    assert rec.outcome == "fail"
    assert rec.fingerprint.phase == "setup"
    assert rec.fingerprint.exc_type == "ValueError"
    assert "bad fixture" in rec.fingerprint.message


def test_skipped(write_test_file, tmp_path):
    write_test_file(
        "import pytest\n"
        "@pytest.mark.skip(reason='nope')\n"
        "def test_skipped():\n"
        "    assert True\n"
    )
    rec = _run(tmp_path, ["test_target.py::test_skipped"])
    assert rec.outcome == "skipped"
    assert not rec.failed


def test_hang_is_killed_and_classified(write_test_file, tmp_path):
    write_test_file("import time\ndef test_stuck():\n    time.sleep(120)\n")
    rec = _run(tmp_path, ["test_target.py::test_stuck"], timeout=8.0)
    assert rec.outcome == "hang"
    assert rec.failed
    assert rec.fingerprint.exc_type == "<hang>"
    assert rec.exit_code is None
    assert rec.duration >= 7.5


@pytest.mark.skipif(sys.platform == "win32", reason="signal-based crash is POSIX-only")
def test_crash_on_signal(write_test_file, tmp_path):
    write_test_file(
        "import os, signal\n"
        "def test_dies():\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    rec = _run(tmp_path, ["test_target.py::test_dies"])
    assert rec.outcome == "crash"
    assert rec.failed
    assert rec.exit_code is not None and rec.exit_code < 0


def test_usage_error_on_missing_file(tmp_path):
    rec = _run(tmp_path, ["test_missing_file.py::test_nope"])
    assert rec.outcome == "usage-error"
    assert rec.exit_code == 4


def test_no_tests_collected(write_test_file, tmp_path):
    write_test_file("x = 1\n")
    rec = _run(tmp_path, ["test_target.py"])
    assert rec.outcome == "no-tests"
    assert rec.exit_code == 5


def test_env_set_and_remove_semantics(write_test_file, tmp_path, monkeypatch):
    monkeypatch.setenv("FLAKEDOCTOR_TEST_REMOVE_ME", "present")
    write_test_file(
        "import os\n"
        "def test_env():\n"
        "    assert os.environ.get('FLAKEDOCTOR_TEST_SET') == 'yes'\n"
        "    assert 'FLAKEDOCTOR_TEST_REMOVE_ME' not in os.environ\n"
    )
    rec = _run(
        tmp_path,
        ["test_target.py::test_env"],
        env={"FLAKEDOCTOR_TEST_SET": "yes", "FLAKEDOCTOR_TEST_REMOVE_ME": None},
    )
    assert rec.outcome == "pass", rec.detail


def test_probe_enforces_selection_order(write_test_file, tmp_path):
    write_test_file(
        "import os\n"
        "def test_one():\n    open('order.txt', 'a').write('one;')\n"
        "def test_two():\n    open('order.txt', 'a').write('two;')\n"
    )
    rec = _run(tmp_path, ["test_target.py::test_two", "test_target.py::test_one"])
    assert rec.outcome == "pass"
    assert (tmp_path / "order.txt").read_text() == "two;one;"
