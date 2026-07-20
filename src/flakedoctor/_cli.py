"""Console-script sugar: ``flakedoctor NODEID`` == ``pytest NODEID --doctor``.

All arguments are forwarded to pytest, so ``--doctor-runs``, ``--doctor-json``
etc. work unchanged.
"""

from __future__ import annotations

import sys

_USAGE = """\
flakedoctor — the flaky-test doctor

usage:
  flakedoctor path/to/test_file.py::test_name [pytest options]

Diagnoses WHY the selected test is flaky (baseline reruns in fresh
subprocesses, perturbation sweep, verification, counterfactual) and prints
a deterministic reproduction command.

Common options (forwarded to pytest):
  --doctor-runs=N       baseline sample size (default 10)
  --doctor-budget=SECS  wall-clock budget for the diagnosis (default 300)
  --doctor-json=PATH    also write the machine-readable report
"""


def _entry_point_registered() -> bool:
    """True when pytest will auto-load our plugin via its pytest11 entry point.

    Passing ``-p flakedoctor._plugin`` on top of that is not harmless: ``-p``
    args are processed before entry points load, so pluggy would then see the
    same module registered under two names and raise ValueError.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib >=3.8
        return False
    try:
        points = entry_points(group="pytest11")
    except TypeError:  # pragma: no cover - very old importlib.metadata API
        points = entry_points().get("pytest11", [])  # type: ignore[union-attr]
    for point in points:
        if point.value.split(":")[0] == "flakedoctor._plugin" or point.name == "flakedoctor":
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    import pytest

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(_USAGE)
        return 0
    # Reaching main() means our own package is already imported, so pytest
    # cannot assert-rewrite it and says so. Harmless here (the plugin does not
    # rely on rewritten asserts) and unavoidable from a console script, so
    # silence it — placed before the user's args so their -W still wins.
    forwarded = ["-W", "ignore::pytest.PytestAssertRewriteWarning"]
    forwarded += args + ["--doctor"]
    if not _entry_point_registered():
        # Dev / PYTHONPATH-only setups: no entry point, so load explicitly.
        forwarded += ["-p", "flakedoctor._plugin"]
    return int(pytest.main(forwarded))


if __name__ == "__main__":
    raise SystemExit(main())
