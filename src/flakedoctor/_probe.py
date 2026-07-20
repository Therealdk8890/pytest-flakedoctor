"""Child-side pytest plugin, injected into every diagnostic subprocess with
``-p flakedoctor._probe``.

Responsibilities: enforce the exact nodeid selection/order from the probe
config, apply the perturbation around the *whole* runtest protocol (hermetic's
own plugin wraps only the call phase, so fixture-setup nondeterminism would
escape), and stream one JSON line per test phase to the result file so the
parent can classify outcomes without parsing pytest's terminal output.

This module must stay dependency-light and defensive: a probe bug must never
change the outcome of the test under diagnosis, and — because these are
pytest hookwrappers — must never fail to yield exactly once, which would
abort the whole child with an INTERNALERROR.
"""

from __future__ import annotations

import inspect
import json
import os
import sys

import pytest

_META = "__flakedoctor__"

# Which perturbation axis each sandbox subsystem corresponds to. The parent
# needs to know what was *actually* applied: an axis that stood itself down
# (a virtual clock on an async test) must not be treated as evidence.
_SUBSYSTEM_AXES = (("clock", "time"), ("rng", "rng"), ("network", "network"), ("fs", "fs"))


def _probe_config() -> dict:
    # A large nodeid list (order runs) arrives in a file to dodge argv/env
    # size limits; small configs stay inline in the environment.
    path = os.environ.get("FLAKEDOCTOR_PROBE_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            print(f"flakedoctor probe: could not read probe file: {exc}", file=sys.stderr)
            return {}
    else:
        raw = os.environ.get("FLAKEDOCTOR_PROBE", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print("flakedoctor probe: unreadable probe config, ignoring", file=sys.stderr)
        return {}


_CONFIG = _probe_config()

# Count real (unpatched) thread starts during a run so the parent can gate the
# interleave axis on actual thread use. Installed once at import, before any
# perturbation patches threading.
_thread_starts = 0


def _install_thread_counter() -> None:
    import threading

    original = threading.Thread.start

    def counting_start(self, *args, **kwargs):
        global _thread_starts
        _thread_starts += 1
        return original(self, *args, **kwargs)

    counting_start.__flakedoctor_wrapped__ = True  # type: ignore[attr-defined]
    if not getattr(threading.Thread.start, "__flakedoctor_wrapped__", False):
        threading.Thread.start = counting_start  # type: ignore[method-assign]


_install_thread_counter()


# Side-effect detection. The doctor re-runs a test dozens of times; if the test
# talks to a real service or spawns processes, repeating it could do real
# damage. The first baseline run reports what outbound network connections and
# subprocess spawns it observed, and the engine gates on that. Detection is
# scoped to the target test's own runtest protocol (setup + call + teardown) via
# _detecting, so pytest's own startup does not count.
_detecting = False
_side_effects: dict[str, list[str]] = {"network": [], "subprocess": []}
_SIDE_EFFECT_CAP = 20


def _note_side_effect(kind: str, detail: str) -> None:
    if not _detecting:
        return
    bucket = _side_effects[kind]
    if detail not in bucket and len(bucket) < _SIDE_EFFECT_CAP:
        bucket.append(detail)


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    return host in ("127.0.0.1", "::1", "localhost", "") or host.startswith("127.")


def _install_side_effect_detectors() -> None:
    import socket
    import subprocess

    def note_connect(sock, address):
        try:
            if getattr(sock, "family", None) in (socket.AF_INET, socket.AF_INET6) and isinstance(
                address, tuple
            ):
                host = address[0]
                if not _is_loopback(host):
                    port = address[1] if len(address) > 1 else "?"
                    _note_side_effect("network", f"{host}:{port}")
        except Exception:
            pass

    if not getattr(socket.socket.connect, "__flakedoctor_wrapped__", False):
        orig_connect = socket.socket.connect
        orig_connect_ex = socket.socket.connect_ex

        def watched_connect(self, address, *a, **k):
            note_connect(self, address)
            return orig_connect(self, address, *a, **k)

        def watched_connect_ex(self, address, *a, **k):
            note_connect(self, address)
            return orig_connect_ex(self, address, *a, **k)

        watched_connect.__flakedoctor_wrapped__ = True  # type: ignore[attr-defined]
        watched_connect_ex.__flakedoctor_wrapped__ = True  # type: ignore[attr-defined]
        socket.socket.connect = watched_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = watched_connect_ex  # type: ignore[method-assign]

    if not getattr(subprocess.Popen.__init__, "__flakedoctor_wrapped__", False):
        orig_popen_init = subprocess.Popen.__init__

        def watched_popen_init(self, args, *a, **k):
            try:
                if isinstance(args, (list, tuple)) and args:
                    prog = str(args[0])
                else:
                    text = str(args).strip()
                    prog = text.split()[0] if text else text
                # Show the program's basename, not a full interpreter path.
                _note_side_effect("subprocess", (os.path.basename(prog) or prog)[:80])
            except Exception:
                pass
            return orig_popen_init(self, args, *a, **k)

        watched_popen_init.__flakedoctor_wrapped__ = True  # type: ignore[attr-defined]
        subprocess.Popen.__init__ = watched_popen_init  # type: ignore[method-assign]


_install_side_effect_detectors()


def _record(payload: dict) -> None:
    path = os.environ.get("FLAKEDOCTOR_RESULT_FILE")
    if not path:
        return
    try:
        # Append-per-line with an immediate close: crash-tolerant, so a later
        # segfault cannot lose the phases that already completed.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except Exception as exc:  # never let probe bookkeeping alter the test run
        print(f"flakedoctor probe: failed to record: {exc!r}", file=sys.stderr)


def pytest_collection_modifyitems(config, items):
    wanted = _CONFIG.get("nodeids")
    if not wanted:
        return
    by_id: dict[str, object] = {}
    for item in items:
        by_id.setdefault(item.nodeid, item)
    ordered = [by_id[nodeid] for nodeid in wanted if nodeid in by_id]
    if not ordered:
        return  # let pytest report "not found" itself (exit 4/5)
    wanted_set = set(wanted)
    deselected = [item for item in items if item.nodeid not in wanted_set]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = ordered


def is_async_test(item) -> bool:
    """Whether a virtual clock would hang this test.

    Detection can never be complete — a sync test may drive an event loop
    through any number of wrappers — so the parent also treats a hang under an
    active clock axis as perturbation-induced. This catches the common shapes:
    async test functions, async-marked tests, async fixtures, and sync tests in
    a module that imports an async runtime.
    """
    function = getattr(item, "function", None)
    if function is not None and (
        inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function)
    ):
        return True
    for marker in ("asyncio", "anyio", "trio"):
        try:
            if item.get_closest_marker(marker) is not None:
                return True
        except Exception:
            pass
    # An async fixture pulls in an event loop even for a sync test body.
    try:
        for defs in getattr(item, "_fixtureinfo").name2fixturedefs.values():
            for fixturedef in defs:
                func = getattr(fixturedef, "func", None)
                if func is not None and (
                    inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)
                ):
                    return True
    except Exception:
        pass
    # Last resort: a sync test that calls asyncio.run() lives in a module that
    # had to import an async runtime to do so.
    try:
        module = getattr(item, "module", None)
        if module is not None:
            for name in ("asyncio", "anyio", "trio"):
                if getattr(module, name, None) is not None:
                    return True
    except Exception:
        pass
    return False


def _applied_axes(kwargs: dict) -> list[str]:
    return [axis for key, axis in _SUBSYSTEM_AXES if kwargs.get(key, "off") != "off"]


def _is_target(nodeid: str) -> bool:
    nodeids = _CONFIG.get("nodeids") or []
    return bool(nodeids) and nodeid == nodeids[-1]


def pytest_runtest_logstart(nodeid, location):
    # Detect side effects only for the test under diagnosis, across its whole
    # protocol (setup runs session/module fixtures, which may connect too).
    global _detecting
    if _is_target(nodeid):
        _side_effects["network"].clear()
        _side_effects["subprocess"].clear()
        _detecting = True


def pytest_runtest_logfinish(nodeid, location):
    global _detecting
    if _is_target(nodeid):
        _detecting = False
        _record(
            {
                "nodeid": nodeid,
                _META: True,
                "side_effects": {
                    "network": list(_side_effects["network"]),
                    "subprocess": list(_side_effects["subprocess"]),
                },
            }
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Wrap the entire protocol (setup + call + teardown) in the perturbation.

    Structured so the generator yields exactly once on every path: a hookwrapper
    that returns without yielding aborts pytest with an INTERNALERROR.
    """
    sandbox_kwargs = _CONFIG.get("sandbox")
    nodeids = _CONFIG.get("nodeids") or []
    if not sandbox_kwargs or not nodeids or item.nodeid != nodeids[-1]:
        yield
        return

    kwargs = dict(sandbox_kwargs)
    notes: list[str] = []
    if kwargs.get("clock") == "virtual" and is_async_test(item):
        kwargs["clock"] = "off"
        notes.append("clock axis skipped: async test (a virtual clock would hang it)")

    sandbox = None
    entered = False
    try:
        import hermetic

        seed = kwargs.pop("seed", None)
        sandbox = hermetic.Sandbox(seed, test_id=item.nodeid, **kwargs)
        sandbox.__enter__()
        entered = True
        _record(
            {
                "nodeid": item.nodeid,
                _META: True,
                "notes": notes,
                "applied_axes": _applied_axes(kwargs),
            }
        )
    except ImportError as exc:
        _record({"nodeid": item.nodeid, _META: True, "error": f"hermetic unavailable: {exc}"})
    except Exception as exc:
        # SandboxActiveError (the test drives its own sandbox), an invalid
        # config, or any __enter__ failure: the perturbation never applied.
        _record(
            {
                "nodeid": item.nodeid,
                _META: True,
                "notes": notes,
                "error": f"perturbation not applied: {type(exc).__name__}: {exc}",
            }
        )

    try:
        yield
    finally:
        if entered:
            try:
                sandbox.__exit__(None, None, None)
            except Exception as exc:
                # The test already ran and reported; a teardown problem must
                # not discard that evidence, only annotate it.
                _record(
                    {
                        "nodeid": item.nodeid,
                        _META: True,
                        "teardown_error": f"{type(exc).__name__}: {exc}",
                    }
                )


def _is_async_narrow(item) -> bool:
    """Async detection for the interleave gate: the test function itself, or an
    explicit async marker — no module-attribute heuristic. Matches the driver's
    own _is_drivable check so the gate and the driver agree."""
    func = getattr(item, "function", None)
    if func is not None and (
        inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)
    ):
        return True
    for marker in ("asyncio", "anyio", "trio"):
        try:
            if item.get_closest_marker(marker) is not None:
                return True
        except Exception:
            pass
    return False


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Interleave axis: drive the test through interleave-test's scheduler.

    Active only in interleave mode, only for the target test. Returning True
    tells pytest the call is handled; a found race re-raises the user's own
    exception so the normal report/fingerprint path classifies it.
    """
    cfg = _CONFIG.get("interleave")
    nodeids = _CONFIG.get("nodeids") or []
    if not cfg or not nodeids or pyfuncitem.nodeid != nodeids[-1]:
        return None
    from . import _interleave

    return _interleave.drive(pyfuncitem, cfg, _record)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    try:
        report = outcome.get_result()
        rec = {"nodeid": item.nodeid, "when": report.when, "outcome": report.outcome}
        if call.excinfo is not None:
            exc_type = call.excinfo.type
            if exc_type.__module__ in ("builtins", "exceptions"):
                rec["exc_type"] = exc_type.__qualname__
            else:
                rec["exc_type"] = f"{exc_type.__module__}.{exc_type.__qualname__}"
            rec["exc_message"] = str(call.excinfo.value)[:1000]
            rec["crash_site"] = _crash_site(call)
        _record(rec)
        # After the call phase of the target test, report how many threads it
        # started and whether it is async — the parent gates the interleave
        # axis on this.
        if report.when == "call" and item.nodeid == (_CONFIG.get("nodeids") or [None])[-1]:
            _record(
                {
                    "nodeid": item.nodeid,
                    _META: True,
                    "thread_starts": _thread_starts,
                    "is_async": is_async_test(item),
                    # A NARROW async signal for the interleave gate: the broad
                    # is_async_test also trips on a module that merely imports
                    # asyncio, which would wrongly skip real thread races.
                    "is_async_for_interleave": _is_async_narrow(item),
                    "is_unittest": type(item).__name__ == "TestCaseFunction",
                }
            )
    except Exception as exc:
        print(f"flakedoctor probe: failed to record phase: {exc!r}", file=sys.stderr)


# Frames from these packages are our machinery, not the user's test; the crash
# site must anchor on the user's own code (the interleave scheduler sits above
# the user frame in a race traceback).
_INTERNAL_FRAME_MARKERS = (f"{os.sep}interleave_test{os.sep}", f"{os.sep}flakedoctor{os.sep}")


def _crash_site(call) -> str:
    try:
        traceback = call.excinfo.traceback
        entry = traceback[-1]
        # Walk inward-to-outward skipping our own frames so identity anchors on
        # the deepest USER frame, not the scheduler that re-raised the error.
        for candidate in reversed(traceback):
            path = str(candidate.path)
            if not any(marker in path for marker in _INTERNAL_FRAME_MARKERS):
                entry = candidate
                break
        path = str(entry.path)
        cwd = os.getcwd()
        if path.startswith(cwd + os.sep):
            path = os.path.relpath(path, cwd)
        return f"{path}:{entry.lineno + 1}"
    except Exception:
        return ""
