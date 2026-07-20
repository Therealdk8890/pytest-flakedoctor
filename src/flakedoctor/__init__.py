"""pytest-flakedoctor: diagnoses WHY a test is flaky and hands you a
deterministic reproduction.

Every other tool retries your flaky test. flakedoctor tells you why it's
flaky — and gives you a command that makes it fail every time.
"""

from __future__ import annotations

import sys as _sys
from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
from importlib.metadata import version as _pkg_version

from ._diagnose import Diagnosis, DoctorSettings
from ._diagnose import diagnose as _engine_diagnose
from ._runner import SubprocessRunner

try:
    # Single source of truth: the version declared in pyproject.toml, read from
    # the installed distribution metadata so the two can never drift.
    __version__ = _pkg_version("pytest-flakedoctor")
except _PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["Diagnosis", "DoctorSettings", "__version__", "diagnose"]


def diagnose(
    nodeid: str,
    *,
    runs: int = 10,
    budget: float = 300.0,
    invocation_dir: str = ".",
    python: str = _sys.executable,
) -> Diagnosis:
    """Scripting entry point: diagnose one test and return the Diagnosis.

    Equivalent to ``pytest <nodeid> --doctor`` but usable from Python.
    """
    runner = SubprocessRunner(invocation_dir, python=python)
    settings = DoctorSettings(runs=runs, budget=budget)
    return _engine_diagnose(nodeid, runner, settings)
