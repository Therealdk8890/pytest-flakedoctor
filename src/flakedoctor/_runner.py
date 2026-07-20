"""Subprocess orchestration.

The design invariant of flakedoctor: every diagnostic run is a fresh
``python -m pytest`` subprocess. This buys the PYTHONHASHSEED axis (hash
randomization is fixed at interpreter start), pristine module/fixture/import
state per run, and hang/crash immunity via OS kill — at the price of
interpreter+import time per run, which the budget planner accounts for.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ._fingerprint import CRASH, HANG, Fingerprint, normalize_message

# Child outcomes, coarsest to finest:
#   pass / fail       — the test ran; per-phase records tell the story
#   skipped           — the test was skipped; nothing to diagnose
#   hang              — the child exceeded its timeout and was killed
#   crash             — the child died on a signal / NTSTATUS exception
#   usage-error       — pytest exit 4: bad nodeid, bad args, collection error
#   no-tests          — pytest exit 5: selection matched nothing
#   child-error       — nonzero exit that fits none of the above

_NEUTRALIZED_PLUGINS = ("randomly", "rerunfailures", "flaky", "xdist", "hermetic")

# Windows reports fatal exceptions (access violation, stack overflow, ...) as
# NTSTATUS codes in the exit status rather than as negative signal numbers.
_NTSTATUS_FLOOR = 0xC0000000

_POST_KILL_DRAIN_TIMEOUT = 2.0

# Marks a result line as describing the perturbation rather than a test phase.
_PROBE_META = "__flakedoctor__"


@dataclass(frozen=True)
class ProbeConfig:
    """One diagnostic run: which nodeids to run, under which perturbation.

    An env value of ``None`` means "remove this variable in the child" —
    the baseline uses it to force fresh hash randomization per run even
    when the parent environment pins PYTHONHASHSEED.
    """

    nodeids: tuple[str, ...]
    env: dict[str, str | None] = field(default_factory=dict, hash=False)
    label: str = "baseline"
    # Merged hermetic.Sandbox kwargs, or None to run without a sandbox at all.
    sandbox: dict | None = field(default=None, hash=False)
    # When set, the probe drives the single test through interleave-test's
    # scheduler instead of running it normally (the thread-interleaving axis).
    interleave: dict | None = field(default=None, hash=False)

    def to_probe_json(self) -> str:
        payload: dict = {"nodeids": list(self.nodeids)}
        if self.sandbox is not None:
            payload["sandbox"] = self.sandbox
        if self.interleave is not None:
            payload["interleave"] = self.interleave
        return json.dumps(payload)


@dataclass
class RunRecord:
    label: str
    outcome: str  # see table above
    fingerprint: Fingerprint | None  # set iff outcome in ("fail", "hang", "crash")
    duration: float
    exit_code: int | None  # None for hang (killed)
    phases: dict[str, str] = field(default_factory=dict)  # when -> passed/failed/skipped
    detail: str = ""  # stderr/stdout tail for surfacing child problems
    # The nodeid the phases belong to — the LAST test that recorded anything.
    # For an order run [*prefix, victim] this is the victim iff it actually ran;
    # if a prefix test hung or crashed first, it is that prefix test, which is
    # how the order phase detects "the victim never executed".
    last_nodeid: str = ""
    # Probe-reported facts about the perturbation itself (axis skipped, sandbox
    # refused, ...) — distinct from anything the test did.
    probe_notes: list[str] = field(default_factory=list)
    probe_error: str = ""
    teardown_error: str = ""
    # Axes the probe reports it actually applied. An axis that stood itself
    # down (a virtual clock on an async test) is absent, so it cannot collect
    # evidence it did not earn. None means the probe reported nothing at all;
    # an empty list means it reported that nothing was applied — a distinction
    # that matters, since the latter is a stood-down axis.
    applied_axes: list[str] | None = None
    # Baseline meta: how many threads the test started, and whether it is async.
    # Used to gate the interleave axis (which needs real thread use).
    thread_starts: int | None = None
    is_async: bool = False
    # Narrow async / unittest signals for the interleave gate specifically.
    is_async_for_interleave: bool = False
    is_unittest: bool = False
    # Interleave-axis meta from an explore/replay run (see _probe): the found
    # schedule and search facts. None on non-interleave runs.
    interleave: dict | None = None

    @property
    def failed(self) -> bool:
        return self.outcome in ("fail", "hang", "crash")

    @property
    def ran(self) -> bool:
        """The test itself actually executed (so pass/fail is real evidence)."""
        return self.outcome in ("pass", "fail", "hang", "crash")

    @property
    def perturbation_applied(self) -> bool:
        """False when the probe could not apply the requested perturbation.

        A teardown problem does not count: the test already ran and reported,
        so its result stays valid evidence.
        """
        return not self.probe_error

    @property
    def doctor_origin(self) -> bool:
        """The failure came from the perturbation, not from the test's own bug."""
        if self.fingerprint is None:
            return False
        if self.fingerprint.exc_type.startswith("hermetic._errors."):
            return True
        # interleave-test's own errors (a replay that diverged, a scheduler
        # fault) are about our tooling, never the user's race.
        if self.fingerprint.exc_type.startswith("interleave_test."):
            return True
        # Backstop for incomplete async detection: a virtual clock never
        # advances an awaited sleep, so a hang under an active clock axis is
        # our doing. Charging it to the test would fabricate a time-axis flake.
        if self.outcome == "hang" and "time" in (self.applied_axes or ()):
            return True
        # A test that needs hermetic's own plugin cannot run in a child that
        # disables it; that is our limitation, not the test's bug.
        return _is_hermetic_fixture_error(self.fingerprint)


class SubprocessRunner:
    """Runs one ProbeConfig per fresh pytest subprocess and classifies the result."""

    def __init__(
        self,
        run_dir: str | Path,
        python: str = sys.executable,
        extra_args: tuple[str, ...] = (),
        forward_plugins: tuple[str, ...] = (),
    ) -> None:
        # Children run with cwd=run_dir and are given rootdir-relative nodeids,
        # so this must be the ROOTDIR — not the user's invocation directory,
        # which differs whenever pytest is run from a subdirectory.
        self.run_dir = str(run_dir)
        self.python = python
        self.extra_args = tuple(extra_args)
        self.forward_plugins = tuple(forward_plugins)

    def __call__(self, probe: ProbeConfig, timeout: float) -> RunRecord:
        return self.run(probe, timeout)

    def run(self, probe: ProbeConfig, timeout: float) -> RunRecord:
        fd, result_path = tempfile.mkstemp(prefix="flakedoctor-", suffix=".jsonl")
        os.close(fd)
        probe_file: str | None = None
        if len(probe.nodeids) > 1:
            pfd, probe_file = tempfile.mkstemp(prefix="flakedoctor-probe-", suffix=".json")
            with os.fdopen(pfd, "w", encoding="utf-8") as handle:
                handle.write(probe.to_probe_json())
        try:
            return self._run(probe, timeout, result_path, probe_file)
        finally:
            for path in (result_path, probe_file):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    # -- internals ---------------------------------------------------------

    def _child_env(self, probe: ProbeConfig, result_path: str, probe_file: str | None) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in probe.env.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        env["FLAKEDOCTOR_CHILD"] = "1"
        if probe_file is not None:
            # A large nodeid list (order runs) would blow the environment/argv
            # size limit, so it travels in a file instead of an env var.
            env["FLAKEDOCTOR_PROBE_FILE"] = probe_file
        else:
            env["FLAKEDOCTOR_PROBE"] = probe.to_probe_json()
        env["FLAKEDOCTOR_RESULT_FILE"] = result_path
        # The child's argv is fully constructed here, so inherited addopts can
        # only distort the measurement — or break the child outright when they
        # name options owned by the plugins we disable below (`-n auto`).
        env.pop("PYTEST_ADDOPTS", None)
        # Children must be able to import flakedoctor._probe whether the
        # package is pip-installed or on a dev PYTHONPATH. APPEND, never
        # prepend: prepending would put our install dir (site-packages when
        # installed) ahead of the user's own PYTHONPATH and silently flip
        # module shadowing, so children would diagnose different code.
        pkg_src = str(Path(__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (existing + os.pathsep + pkg_src) if existing else pkg_src
        return env

    def _child_argv(self, probe: ProbeConfig) -> list[str]:
        argv = [self.python, "-m", "pytest", "-q", "--tb=no"]
        # Forward the parent's -p plugin selections first: under
        # PYTEST_DISABLE_PLUGIN_AUTOLOAD (common CI hardening) they are the
        # only way the user's fixture plugins reach the child at all.
        for name in self.forward_plugins:
            argv += ["-p", name]
        argv += ["-p", "flakedoctor._probe"]
        for name in _NEUTRALIZED_PLUGINS:
            argv += ["-p", f"no:{name}"]
        argv += list(self.extra_args)
        if len(probe.nodeids) > 1:
            # Order runs can name hundreds of tests. Collect their files (far
            # fewer, deduplicated) and let the probe filter+order to the exact
            # nodeid list — keeps argv well under the OS command-line limit.
            files: list[str] = []
            seen: set[str] = set()
            for nodeid in probe.nodeids:
                path = nodeid.split("::", 1)[0]
                if path not in seen:
                    seen.add(path)
                    files.append(path)
            argv += files
        else:
            argv += list(probe.nodeids)
        return argv

    def _run(
        self, probe: ProbeConfig, timeout: float, result_path: str, probe_file: str | None = None
    ) -> RunRecord:
        env = self._child_env(probe, result_path, probe_file)
        argv = self._child_argv(probe)

        popen_kwargs: dict = {
            "cwd": self.run_dir,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            # Lossy decoding on purpose: child output feeds only the detail
            # tail, never the fingerprint, and strict decoding of stray bytes
            # would raise UnicodeDecodeError and abort the whole diagnosis.
            "encoding": "utf-8",
            "errors": "backslashreplace",
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        start = time.monotonic()
        try:
            proc = subprocess.Popen(argv, **popen_kwargs)
        except OSError as exc:
            # Most likely an argv/environment too large for the OS to exec.
            # Never let it abort the whole diagnosis.
            return RunRecord(
                label=probe.label,
                outcome="child-error",
                fingerprint=None,
                duration=time.monotonic() - start,
                exit_code=None,
                detail=f"could not launch the diagnostic subprocess: {exc}",
            )
        timed_out = False
        leaked_pipes = False
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                stdout, stderr, leaked_pipes = _drain(proc)
            duration = time.monotonic() - start
        finally:
            # Covers KeyboardInterrupt and any other unwind past a live child:
            # children run in their own session/process group, so the
            # terminal's Ctrl-C never reaches them.
            if proc.poll() is None:
                _kill_tree(proc)

        detail = _tail(stderr) or _tail(stdout)
        phases, meta = _read_records(result_path)

        if timed_out:
            # A timeout does not always mean the test hung: if the probe
            # recorded a teardown, pytest finished and something else (a
            # background process inheriting the pipes) held them open.
            if "teardown" in phases:
                note = (
                    "the test completed but the child's output pipes stayed open past the "
                    "timeout — a background process outlived the test and was killed"
                )
                record = self._from_phases(probe, phases, duration, None, detail)
                _attach_meta(record, meta)
                record.detail = (note + ("\n" + detail if detail else "")).strip()
                return record
            record = RunRecord(
                label=probe.label,
                outcome="hang",
                fingerprint=HANG,
                duration=duration,
                exit_code=None,
                phases=_phase_outcomes(phases),
                detail=(
                    ("output pipes stayed open after kill\n" if leaked_pipes else "") + detail
                ).strip(),
            )
            _attach_meta(record, meta)
            return record

        record = self._classify(probe, proc.returncode, phases, detail, duration)
        _attach_meta(record, meta)
        return record

    def _classify(
        self,
        probe: ProbeConfig,
        returncode: int,
        phases: dict[str, dict],
        detail: str,
        duration: float,
    ) -> RunRecord:
        phase_outcomes = _phase_outcomes(phases)

        if _is_crash(returncode):
            return RunRecord(probe.label, "crash", CRASH, duration, returncode, phase_outcomes, detail)
        if returncode == 4:
            return RunRecord(probe.label, "usage-error", None, duration, returncode, phase_outcomes, detail)
        if returncode == 5:
            return RunRecord(probe.label, "no-tests", None, duration, returncode, phase_outcomes, detail)

        record = self._from_phases(probe, phases, duration, returncode, detail)
        if record.outcome != "unknown":
            return record

        if returncode == 0 and not phases:
            # Ran green but our probe never wrote records — treat as a child
            # problem rather than silently calling it a pass.
            return RunRecord(
                probe.label,
                "child-error",
                None,
                duration,
                returncode,
                phase_outcomes,
                detail or "child exited 0 but the probe recorded no test phases",
            )
        if returncode == 0:
            return RunRecord(probe.label, "pass", None, duration, returncode, phase_outcomes, detail)
        if returncode == 1 and phases:
            # pytest says "tests failed" but no phase recorded a failure —
            # most likely a non-test error (e.g. unraisable warnings-as-error).
            return RunRecord(
                probe.label,
                "child-error",
                None,
                duration,
                returncode,
                phase_outcomes,
                detail or "pytest exited 1 but no test phase failed",
            )
        return RunRecord(probe.label, "child-error", None, duration, returncode, phase_outcomes, detail)

    def _from_phases(
        self,
        probe: ProbeConfig,
        phases: dict[str, dict],
        duration: float,
        returncode: int | None,
        detail: str,
    ) -> RunRecord:
        """Classify purely from recorded phases; outcome 'unknown' if they don't decide."""
        phase_outcomes = _phase_outcomes(phases)
        failing = [(when, rec) for when, rec in phases.items() if rec.get("outcome") == "failed"]
        if failing:
            # Setup errors come first: a broken fixture is the failure.
            order = {"setup": 0, "call": 1, "teardown": 2}
            when, rec = min(failing, key=lambda pair: order.get(pair[0], 9))
            fp = Fingerprint(
                phase=when,
                exc_type=rec.get("exc_type", "<unknown>"),
                message=normalize_message(rec.get("exc_message", "")),
                crash_site=rec.get("crash_site", ""),
            )
            return RunRecord(probe.label, "fail", fp, duration, returncode, phase_outcomes, detail)

        if phases and all(rec.get("outcome") in ("passed", "skipped") for rec in phases.values()):
            if any(rec.get("outcome") == "skipped" for rec in phases.values()):
                if phase_outcomes.get("call") != "passed":
                    return RunRecord(
                        probe.label, "skipped", None, duration, returncode, phase_outcomes, detail
                    )
            if returncode in (0, None):
                return RunRecord(probe.label, "pass", None, duration, returncode, phase_outcomes, detail)
        return RunRecord(probe.label, "unknown", None, duration, returncode, phase_outcomes, detail)


def _is_crash(returncode: int | None) -> bool:
    if returncode is None:
        return False
    if returncode < 0:
        return True  # POSIX: killed by signal
    if sys.platform == "win32":
        return (returncode & 0xFFFFFFFF) >= _NTSTATUS_FLOOR
    return False


def _phase_outcomes(phases: dict[str, dict]) -> dict[str, str]:
    return {when: rec.get("outcome", "?") for when, rec in phases.items()}


_HERMETIC_FIXTURE_MARKERS = ("hermetic_sandbox", "'hermetic'")


def _is_hermetic_fixture_error(fingerprint) -> bool:
    """A missing `hermetic_sandbox` fixture is caused by our own -p no:hermetic.

    Children disable hermetic's plugin so it cannot double-sandbox, which also
    removes its fixture. A test that needs it is not broken — we broke it.
    """
    if "FixtureLookupError" not in fingerprint.exc_type:
        return False
    return any(marker in fingerprint.message for marker in _HERMETIC_FIXTURE_MARKERS)


def _attach_meta(record: RunRecord, meta: dict) -> None:
    record.probe_notes = list(meta.get("notes") or [])
    record.probe_error = meta.get("error") or ""
    record.teardown_error = meta.get("teardown_error") or ""
    record.last_nodeid = meta.get("last_node", "") or ""
    reported = meta.get("applied_axes")
    record.applied_axes = list(reported) if reported is not None else None
    if "thread_starts" in meta:
        record.thread_starts = meta.get("thread_starts")
    record.is_async = bool(meta.get("is_async"))
    record.is_async_for_interleave = bool(meta.get("is_async_for_interleave"))
    record.is_unittest = bool(meta.get("is_unittest"))
    if meta.get("interleave") is not None:
        record.interleave = meta.get("interleave")
    if record.probe_error and not record.detail:
        record.detail = record.probe_error
    if record.teardown_error:
        record.probe_notes.append(
            f"the perturbation's teardown raised {record.teardown_error} after the test "
            "had already run"
        )


def _read_records(result_path: str) -> tuple[dict[str, dict], dict]:
    """Parse the probe's JSON-lines file into ({when: record}, probe metadata).

    The victim is the last nodeid in the child's run; with a single-nodeid
    probe every record belongs to it. Later (order axis) the prefix tests'
    records are present too, so key records by nodeid and keep the last
    nodeid's phases. Probe metadata lines describe the *perturbation*, not the
    test, and are collected separately.
    """
    per_node: dict[str, dict[str, dict]] = {}
    meta_by_node: dict[str, dict] = {}
    last_node: str | None = None
    try:
        with open(result_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                node = rec.get("nodeid", "")
                if rec.get(_PROBE_META):
                    existing = meta_by_node.setdefault(
                        node, {"notes": [], "error": "", "teardown_error": ""}
                    )
                    existing["notes"].extend(rec.get("notes") or [])
                    if rec.get("error"):
                        existing["error"] = rec["error"]
                    if rec.get("teardown_error"):
                        existing["teardown_error"] = rec["teardown_error"]
                    if "applied_axes" in rec:
                        existing["applied_axes"] = rec["applied_axes"]
                    if "thread_starts" in rec:
                        existing["thread_starts"] = rec["thread_starts"]
                    if "is_async" in rec:
                        existing["is_async"] = rec["is_async"]
                    if "is_async_for_interleave" in rec:
                        existing["is_async_for_interleave"] = rec["is_async_for_interleave"]
                    if "is_unittest" in rec:
                        existing["is_unittest"] = rec["is_unittest"]
                    if "interleave" in rec:
                        existing["interleave"] = rec["interleave"]
                    continue
                last_node = node
                per_node.setdefault(node, {})[rec.get("when", "?")] = rec
    except OSError:
        return {}, {}
    if last_node is None:
        # No phases at all; surface any metadata so a refused perturbation is
        # not mistaken for a silent child failure.
        single = dict(next(iter(meta_by_node.values()), {}))
        single["last_node"] = ""
        return {}, single
    meta = dict(meta_by_node.get(last_node, {}))
    meta["last_node"] = last_node
    return per_node[last_node], meta


def _drain(proc: subprocess.Popen) -> tuple[str, str, bool]:
    """Collect whatever output is available after a kill, without blocking forever.

    communicate() waits for EOF on the pipes, not for the child to exit: a
    surviving grandchild that inherited them would block us indefinitely.
    """
    try:
        stdout, stderr = proc.communicate(timeout=_POST_KILL_DRAIN_TIMEOUT)
        return stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=_POST_KILL_DRAIN_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass
        return "", "", True


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _tail(text: str, limit: int = 2000) -> str:
    text = (text or "").strip()
    return text[-limit:]
