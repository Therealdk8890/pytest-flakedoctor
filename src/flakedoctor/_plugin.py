"""Parent-side pytest plugin: options, orchestration, terminal summary.

Registered via the pytest11 entry point. In diagnostic child processes
(FLAKEDOCTOR_CHILD=1) everything here no-ops so children can never recurse
into spawning their own children.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import zlib
from pathlib import Path

import pytest

from . import __version__
from ._diagnose import DoctorSettings, diagnose as _engine_diagnose
from ._probe import is_async_test
from ._report import json_payload, render_block
from ._repro import ReproFormatError, decode
from ._runner import SubprocessRunner


def _is_child() -> bool:
    return os.environ.get("FLAKEDOCTOR_CHILD") == "1"


def _python_version() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def pytest_addoption(parser):
    group = parser.getgroup("flakedoctor", "flaky-test doctor")
    group.addoption(
        "--doctor",
        action="store_true",
        default=False,
        help="diagnose why the selected test is flaky (or, with a full suite, "
        "diagnose the first failure) and print a deterministic repro",
    )
    group.addoption(
        "--doctor-runs",
        type=int,
        default=None,
        help="baseline sample size (default: 10, or ini doctor_runs)",
    )
    group.addoption(
        "--doctor-budget",
        type=float,
        default=None,
        help="wall-clock budget in seconds for the whole diagnosis "
        "(default: 300, or ini doctor_budget)",
    )
    group.addoption(
        "--doctor-json",
        default=None,
        help="write the machine-readable flakedoctor-report v1 to this path",
    )
    group.addoption(
        "--doctor-thorough",
        action="store_true",
        default=False,
        help="sweep every axis instead of stopping at the first confirmed repro",
    )
    group.addoption(
        "--doctor-repro",
        default=None,
        metavar="BLOB",
        help="re-run the selected test under a diagnosed perturbation "
        "(the fd1:... blob printed by a previous --doctor run)",
    )
    parser.addini("doctor_runs", "flakedoctor: baseline sample size", default="10")
    parser.addini("doctor_budget", "flakedoctor: wall-clock budget (seconds)", default="300")
    parser.addini("doctor_json", "flakedoctor: default JSON report path", default="")


def pytest_configure(config):
    # Register the marker name unconditionally — including in diagnostic
    # children — so a marked test never trips an "unknown mark" warning.
    config.addinivalue_line(
        "markers",
        "flakedoctor_repro(blob): reproduce a diagnosed flake by applying its "
        "perturbation to this test on every run (from a fd1:... repro blob)",
    )
    if _is_child():
        return
    blob = config.getoption("--doctor-repro")
    if blob:
        if config.getoption("--doctor"):
            raise pytest.UsageError("--doctor and --doctor-repro are mutually exclusive")
        config.pluginmanager.register(_ReproPlugin(config, blob), "flakedoctor-repro")
        return
    if config.getoption("--doctor"):
        config.pluginmanager.register(_DoctorPlugin(config), "flakedoctor-orchestrator")
        return
    # A normal run: honor @pytest.mark.flakedoctor_repro markers so a diagnosed
    # flake reproduces deterministically in CI and code review until it's fixed.
    config.pluginmanager.register(_MarkerPlugin(), "flakedoctor-marker")


class _MarkerPlugin:
    """Applies `@pytest.mark.flakedoctor_repro("fd1:...")` on ordinary runs.

    The marker turns a diagnosed flake into a standing, version-controlled
    reproduction: the sandbox axes (time/rng/network/fs) are re-applied to the
    marked test so it fails deterministically wherever it runs — no --doctor
    needed. Hashseed and order repros cannot be re-applied purely in-process,
    so those markers report how to reproduce instead of silently doing nothing.
    """

    def __init__(self):
        self.applied: list[str] = []
        self.messages: list[str] = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(self, pyfuncitem):
        """Replay an interleave repro marker by re-running under its schedule."""
        blob = _marker_blob(pyfuncitem)
        if blob is None:
            return None
        try:
            repro = decode(blob, nodeid=pyfuncitem.nodeid)
        except Exception:
            return None  # the call-phase wrapper already reports a bad blob
        if not repro.interleave:
            return None
        try:
            import interleave_test  # noqa: F401
        except ImportError:
            self.messages.append(
                f"flakedoctor: {pyfuncitem.nodeid}: this is a thread-interleaving repro; "
                "install the `interleave` extra (Python >=3.12) to reproduce it"
            )
            return None
        self.applied.append(pyfuncitem.nodeid)
        return _replay_interleave(pyfuncitem, repro.interleave)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        # Wrap only the CALL phase, not the whole protocol: a session- or
        # module-scoped fixture built during a marked test's SETUP would
        # otherwise be constructed under the sandbox (frozen time, seeded rng)
        # and then leak that state into every other test that shares it.
        blob = _marker_blob(item)
        if blob is None:
            yield
            return
        try:
            repro = decode(blob, nodeid=item.nodeid)
        except Exception as exc:
            # A committed marker runs in CI; a malformed blob must skip one
            # reproduction, never abort the session with an INTERNALERROR.
            reason = exc if isinstance(exc, ReproFormatError) else f"{type(exc).__name__}: {exc}"
            self.messages.append(f"flakedoctor: {item.nodeid}: bad repro marker: {reason}")
            yield
            return
        if repro.order:
            self.messages.append(
                f"flakedoctor: {item.nodeid}: this is a test-order repro; it cannot be "
                "applied by a marker — reproduce it with the printed `pytest ... "
                "--doctor-repro=` command instead"
            )
            yield
            return
        if repro.hashseed is not None and os.environ.get("PYTHONHASHSEED") != repro.hashseed:
            self.messages.append(
                f"flakedoctor: {item.nodeid}: this repro needs PYTHONHASHSEED="
                f"{repro.hashseed} (set at interpreter start); the marker cannot set it — "
                f"run with `PYTHONHASHSEED={repro.hashseed} pytest ...`"
            )
        # A per-test reseeder (pytest-randomly) runs its own random.seed() inside
        # the call phase, overriding the pinned rng seed — the marker then can't
        # reproduce an rng flake. Warn instead of silently mis-reproducing.
        defeated_by_reseeder = False
        if any(v.axis == "rng" for v in repro.values):
            for name in ("randomly", "random_order"):
                if item.config.pluginmanager.hasplugin(name):
                    defeated_by_reseeder = True
                    self.messages.append(
                        f"flakedoctor: {item.nodeid}: pytest-{name} reseeds randomness per "
                        "test and overrides this repro's pinned seed — run with "
                        f"`-p no:{name}` for the marker to reproduce the flake"
                    )
        kwargs = repro.sandbox_kwargs()
        if not kwargs:
            yield  # hashseed-only: nothing for a sandbox to apply
            return
        if kwargs.get("clock") == "virtual" and is_async_test(item):
            kwargs = {**kwargs, "clock": "off"}
        sandbox = None
        entered = False
        try:
            import hermetic

            seed = kwargs.pop("seed", None)
            sandbox = hermetic.Sandbox(seed, test_id=item.nodeid, **kwargs)
            sandbox.__enter__()
            entered = True
            if not defeated_by_reseeder:
                self.applied.append(item.nodeid)
        except ImportError:
            self.messages.append(
                f"flakedoctor: {item.nodeid}: hermetic is not installed, so the repro "
                "marker could not be applied"
            )
        except Exception as exc:
            self.messages.append(
                f"flakedoctor: {item.nodeid}: could not apply the repro marker: "
                f"{type(exc).__name__}: {exc}"
            )
        try:
            yield
        finally:
            if entered:
                try:
                    sandbox.__exit__(None, None, None)
                except Exception:
                    pass

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        for message in self.messages:
            terminalreporter.write_line(message)
        if self.applied:
            terminalreporter.write_line(
                f"flakedoctor: applied repro markers to {len(self.applied)} test(s); "
                "any failures above are the diagnosed flakes reproducing"
            )


def _marker_blob(item) -> str | None:
    marker = item.get_closest_marker("flakedoctor_repro")
    if marker is None:
        return None
    if marker.args:
        return marker.args[0]
    return marker.kwargs.get("blob")


def _replay_interleave(pyfuncitem, interleave_data: dict):
    """Re-run a test under a recorded thread schedule (in-process).

    Returns True (handled → the test's outcome comes from the replay), or None
    to let pytest run it normally. A schedule that reproduces raises the user's
    exception; a stale one that no longer applies runs the test normally, which
    the DID-NOT-REPRODUCE check then reports.
    """
    from . import _interleave

    cfg = {
        "mode": "replay",
        "schedule": interleave_data.get("schedule"),
        "per_schedule_timeout": 10.0,
    }
    return _interleave.drive(pyfuncitem, cfg, lambda payload: None)


class _ReproPlugin:
    """Applies a diagnosed perturbation to the selected test, in-process.

    In-process on purpose: the point of a repro is that you can attach a
    debugger to it. Hypothesis semantics apply — a repro that passes exits
    loudly rather than pretending nothing is wrong.
    """

    def __init__(self, config, blob: str):
        self.config = config
        try:
            self.repro = decode(blob)
        except ReproFormatError as exc:
            raise pytest.UsageError(f"--doctor-repro: {exc}") from exc
        self.target_digest = _blob_node_digest(blob)
        self.targets: list[str] = []
        self.outcomes: dict[str, bool] = {}
        self.messages: list[str] = []
        self.did_not_reproduce = False
        self.interleave_unavailable = False

        if self.repro.interleave:
            try:
                import interleave_test  # noqa: F401
            except ImportError:
                raise pytest.UsageError(
                    "--doctor-repro: this is a thread-interleaving repro; install the "
                    "`interleave` extra (Python >=3.12) to replay it:\n"
                    "    pip install 'pytest-flakedoctor[interleave]'"
                )

        hashseed = self.repro.hashseed
        if hashseed is not None and os.environ.get("PYTHONHASHSEED") != hashseed:
            raise pytest.UsageError(
                f"--doctor-repro needs PYTHONHASHSEED={hashseed}, but the environment has "
                f"{os.environ.get('PYTHONHASHSEED') or '<unset>'}. Hash randomization is "
                f"fixed at interpreter start, so re-run with:\n"
                f"    PYTHONHASHSEED={hashseed} pytest ... --doctor-repro=<blob>"
            )
        if config.pluginmanager.hasplugin("xdist") and config.getoption("numprocesses", None):
            raise pytest.UsageError(
                "--doctor-repro applies the perturbation in-process for debuggability and "
                "cannot report a verdict under xdist; re-run without -n"
            )
        # pytest-randomly reseeds `random` per test, which would silently
        # override a pinned rng seed and make the repro look stale.
        if any(value.axis == "rng" for value in self.repro.values):
            for name in ("randomly", "random_order"):
                if config.pluginmanager.hasplugin(name):
                    self.messages.append(
                        f"flakedoctor: WARNING pytest-{name} is active and reseeds randomness "
                        "per test, which overrides this repro's pinned seed — re-run with "
                        f"-p no:{name}"
                    )

    def pytest_collection_modifyitems(self, config, items):
        """Select and order the tests the blob describes."""
        if self.repro.order:
            # Test-order repro: enforce the recorded sequence, since pytest's
            # own ordering (definition order within a file) can differ from the
            # order the tests were named on the command line.
            by_id: dict[str, object] = {}
            for item in items:
                by_id.setdefault(item.nodeid, item)
            ordered = [by_id[nodeid] for nodeid in self.repro.order if nodeid in by_id]
            missing = [nodeid for nodeid in self.repro.order if nodeid not in by_id]
            if missing:
                self.messages.append(
                    "flakedoctor: WARNING these tests from the repro were not collected, so "
                    f"the order is incomplete: {', '.join(missing)}"
                )
            if ordered:
                items[:] = ordered
            # Only the victim (last in the order) is judged.
            self.targets = [self.repro.order[-1]]
            return
        if not self.target_digest:
            self.targets = [item.nodeid for item in items]
            return
        matching = [
            item.nodeid
            for item in items
            if hashlib.sha256(item.nodeid.encode("utf-8")).hexdigest()[:12] == self.target_digest
        ]
        if matching:
            self.targets = matching
            return
        self.targets = [item.nodeid for item in items]
        self.messages.append(
            "flakedoctor: WARNING this repro blob was recorded for a different test than "
            "the one selected; applying it anyway"
        )

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(self, pyfuncitem):
        """Replay a recorded thread schedule for an interleave repro."""
        if self.repro.interleave and pyfuncitem.nodeid in self.targets:
            return _replay_interleave(pyfuncitem, self.repro.interleave)
        return None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        """Enter the perturbation around the protocol, always yielding once."""
        kwargs = self.repro.sandbox_kwargs()
        if not kwargs or item.nodeid not in self.targets:
            yield
            return
        if kwargs.get("clock") == "virtual" and is_async_test(item):
            kwargs = dict(kwargs)
            kwargs["clock"] = "off"
            self.messages.append(
                "flakedoctor: the clock axis was skipped for this async test "
                "(a virtual clock would hang it), so the repro is incomplete"
            )
        sandbox = None
        entered = False
        try:
            import hermetic

            seed = kwargs.pop("seed", None)
            sandbox = hermetic.Sandbox(seed, test_id=item.nodeid, **kwargs)
            sandbox.__enter__()
            entered = True
        except ImportError:
            self.messages.append(
                "flakedoctor: hermetic is not installed, so the perturbation was not applied"
            )
        except Exception as exc:
            self.messages.append(
                f"flakedoctor: could not apply the perturbation: {type(exc).__name__}: {exc}"
            )
        try:
            yield
        finally:
            if entered:
                try:
                    sandbox.__exit__(None, None, None)
                except Exception as exc:
                    self.messages.append(
                        f"flakedoctor: the perturbation's teardown raised "
                        f"{type(exc).__name__}: {exc}"
                    )

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        if item.nodeid not in self.targets:
            return
        try:
            report = outcome.get_result()
        except BaseException:
            return
        if report.when in ("setup", "call") and report.failed:
            self.outcomes[item.nodeid] = True
        self.outcomes.setdefault(item.nodeid, False)

    def pytest_sessionfinish(self, session, exitstatus):
        # Judge only the tests the repro was applied to: an unrelated failure
        # elsewhere in the selection must not read as a successful repro.
        if self.outcomes and not any(self.outcomes.values()):
            self.did_not_reproduce = True
            session.exitstatus = 1

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if self.repro.interleave:
            what = "a recorded thread schedule"
        elif self.repro.order:
            what = "the recorded test order"
        else:
            what = ", ".join(value.described() for value in self.repro.values) or "the repro"
        terminalreporter.write_line("")
        terminalreporter.write_line(f"flakedoctor: reproducing under {what}")
        for message in self.messages:
            terminalreporter.write_line(message)
        if self.did_not_reproduce:
            failed, runs = self.repro.confirm
            detail = f" (it failed {failed}/{runs} when diagnosed)" if runs else ""
            terminalreporter.write_line(
                f"flakedoctor: DID NOT REPRODUCE — the test passed under this "
                f"perturbation{detail}. The code may have changed since the diagnosis; "
                "re-run with --doctor."
            )


def _blob_node_digest(blob: str) -> str:
    """The nodeid digest a blob was recorded for, or '' if it carries none."""
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(blob.strip()[4:].encode("ascii")))
        return json.loads(raw).get("node", "") or ""
    except Exception:
        return ""


def pytest_report_teststatus(report, config):
    if getattr(report, "flakedoctor_diagnosed", False):
        return "diagnosed", "D", ("DIAGNOSED", {"purple": True})


def _forwarded_plugins(config) -> tuple[str, ...]:
    """The parent's ``-p NAME`` selections, to replay in children.

    Only when autoload is disabled: there, ``-p`` is the *only* way the user's
    fixture plugins get loaded, and a child without them would fail tests that
    pass for the user. With autoload on, entry points load in children anyway
    and forwarding risks double registration.
    """
    if not os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        return ()
    args = list(config.invocation_params.args)
    names: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        name = None
        if arg in ("-p", "--plugins"):
            if index + 1 < len(args):
                name = args[index + 1]
                index += 1
        elif arg.startswith("-p") and len(arg) > 2:
            name = arg[2:]
        elif arg.startswith("--plugins="):
            name = arg.split("=", 1)[1]
        if name and not name.startswith("no:") and not name.startswith("flakedoctor"):
            names.append(name)
        index += 1
    return tuple(names)


class _DoctorPlugin:
    def __init__(self, config):
        self.config = config
        runs = config.getoption("--doctor-runs")
        if runs is None:
            runs = int(config.getini("doctor_runs"))
        budget = config.getoption("--doctor-budget")
        if budget is None:
            budget = float(config.getini("doctor_budget"))
        self.json_path = config.getoption("--doctor-json") or (
            config.getini("doctor_json") or None
        )
        self.settings = DoctorSettings(
            runs=runs, budget=budget, thorough=bool(config.getoption("--doctor-thorough"))
        )
        self.items: list = []
        self.diagnosis = None
        self.first_failed_nodeid: str | None = None
        self.messages: list[str] = []
        self.json_written: str | None = None
        if self.json_path:
            # Fail fast: a bad report path must not surface only after a
            # multi-minute diagnosis has already run.
            problem = _probe_writable(self.json_path)
            if problem:
                raise pytest.UsageError(
                    f"--doctor-json path is not writable: {self.json_path} ({problem})"
                )

    # -- collection --------------------------------------------------------

    def pytest_collection_finish(self, session):
        self.items = list(session.items)

    # -- single-test mode: intercept the protocol and diagnose instead -----

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        if len(self.items) != 1 or item is not self.items[0]:
            return None
        item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        self.diagnosis = self._diagnose_nodeid(item.nodeid)
        try:
            report = pytest.TestReport(
                nodeid=item.nodeid,
                location=item.location,
                keywords=dict(item.keywords),
                outcome="passed",
                longrepr=None,
                when="call",
                duration=self.diagnosis.elapsed,
            )
            report.flakedoctor_diagnosed = True
        except Exception:
            report = None  # report cosmetics must never break the diagnosis
        if report is not None:
            item.ihook.pytest_runtest_logreport(report=report)
        item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        return True

    # -- suite mode: remember the first real failure -----------------------

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        try:
            report = outcome.get_result()
        except BaseException:
            return
        if (
            report.when in ("setup", "call")
            and report.failed
            and self.first_failed_nodeid is None
        ):
            self.first_failed_nodeid = item.nodeid

    # -- work + persistence: must not depend on the terminal plugin --------

    def pytest_sessionfinish(self, session, exitstatus):
        if self.diagnosis is None and self.first_failed_nodeid and len(self.items) > 1:
            self._emit(f"flakedoctor: diagnosing first failure: {self.first_failed_nodeid}")
            # Everything collected before the victim is the order-axis prefix.
            collected = [item.nodeid for item in self.items]
            try:
                cut = collected.index(self.first_failed_nodeid)
            except ValueError:
                cut = 0
            prefix = collected[:cut]
            self.diagnosis = self._diagnose_nodeid(
                self.first_failed_nodeid, suite_mode=True, prefix=prefix
            )
        if self.diagnosis is None or not self.json_path:
            return
        payload = json_payload(self.diagnosis, __version__, _python_version(), sys.platform)
        try:
            path = Path(self.json_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self.json_written = str(path)
        except OSError as exc:
            self.messages.append(f"flakedoctor: could not write JSON report: {exc}")

    # -- output ------------------------------------------------------------

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        if self.diagnosis is None:
            if len(self.items) > 1:
                terminalreporter.write_line(
                    "flakedoctor: no failures observed — nothing to diagnose "
                    "(select a single test to diagnose it directly)"
                )
            return
        block = render_block(self.diagnosis, _python_version(), sys.platform)
        terminalreporter.write_line("")
        for line in block.splitlines():
            terminalreporter.write_line(line)
        if self.json_written:
            terminalreporter.write_line(
                f"flakedoctor: JSON report written to {self.json_written}"
            )
        for message in self.messages:
            terminalreporter.write_line(message)

    # -- internals ---------------------------------------------------------

    def _terminalreporter(self):
        return self.config.pluginmanager.get_plugin("terminalreporter")

    def _shell_relative_nodeid(self, nodeid: str) -> str:
        """Respell a rootdir-relative nodeid relative to the user's cwd.

        The repro command is meant to be pasted into the shell the user is
        sitting in, which is not necessarily the rootdir.
        """
        path_part, sep, rest = nodeid.partition("::")
        try:
            target = (Path(self.config.rootpath) / path_part).resolve()
            relative = os.path.relpath(target, Path(self.config.invocation_params.dir).resolve())
        except (OSError, ValueError):  # e.g. different drives on Windows
            return nodeid
        return relative.replace(os.sep, "/") + sep + rest

    def _emit(self, message: str) -> None:
        reporter = self._terminalreporter()
        if reporter is not None:
            reporter.write_line(message)

    def _diagnose_nodeid(
        self, nodeid: str, suite_mode: bool = False, prefix: list[str] | None = None
    ):
        # Children get rootdir-relative nodeids as argv paths, so they must run
        # with cwd=rootdir — the invocation dir differs whenever pytest is run
        # from a subdirectory of the rootdir.
        runner = SubprocessRunner(
            str(self.config.rootpath),
            forward_plugins=_forwarded_plugins(self.config),
        )
        progress = _make_progress(self._terminalreporter())
        diagnosis = _engine_diagnose(
            nodeid,
            runner,
            self.settings,
            progress,
            display=self._shell_relative_nodeid,
            prefix=prefix,
        )
        if prefix is None and diagnosis.verdict in ("not-flaky", "flaky-unattributed"):
            diagnosis.warnings.append(
                "the test was diagnosed in isolation; if it only flakes inside the full "
                "suite, run the whole suite under --doctor to check test-order dependence"
            )
        return diagnosis


def _probe_writable(path_str: str) -> str | None:
    """Return a problem description if the report path cannot be written."""
    path = Path(path_str)
    try:
        if path.is_dir():
            return "path is a directory"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        return str(exc)
    return None


_PHASE_RE = re.compile(r"\s*\d+/\d+.*$")


def _make_progress(terminalreporter):
    """Per-run progress: live-rewrite on a tty, phase transitions otherwise."""
    is_tty = sys.stdout.isatty()
    last_phase: list[str] = [""]

    def progress(msg: str) -> None:
        if terminalreporter is None:
            return
        text = f"flakedoctor: {msg}"
        if is_tty and hasattr(terminalreporter, "rewrite"):
            terminalreporter.rewrite(text.ljust(76))
            return
        phase = _PHASE_RE.sub("", msg)
        if phase != last_phase[0]:
            last_phase[0] = phase
            terminalreporter.write_line(text)

    return progress
