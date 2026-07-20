from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"


@pytest.fixture
def run_doctor(tmp_path):
    """Write a corpus test file and run a real `pytest --doctor` parent on it.

    Returns (CompletedProcess, report_payload_or_None).
    """

    def _run(test_body: str, select: str, extra_args: tuple[str, ...] = (), timeout: float = 600):
        (tmp_path / "test_corpus.py").write_text(test_body, encoding="utf-8")
        json_path = tmp_path / "report.json"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("PYTHONHASHSEED", None)
        env.pop("FLAKEDOCTOR_CHILD", None)
        env.pop("FLAKEDOCTOR_PROBE", None)
        env.pop("FLAKEDOCTOR_RESULT_FILE", None)
        argv = [
            sys.executable,
            "-m",
            "pytest",
            select,
            "-p",
            "flakedoctor._plugin",
            "--doctor",
            "--doctor-json",
            str(json_path),
            *extra_args,
        ]
        proc = subprocess.run(
            argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=timeout
        )
        payload = json.loads(json_path.read_text()) if json_path.exists() else None
        return proc, payload

    return _run


@pytest.fixture
def write_test_file(tmp_path):
    def _write(body: str, name: str = "test_target.py") -> Path:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    return _write
