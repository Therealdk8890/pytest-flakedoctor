"""Child-side driver for the thread-interleaving axis.

Runs *inside* a diagnostic subprocess, only when the probe config asks for it.
It turns the selected pytest test into a zero-argument model and hands it to
interleave-test's ``explore()`` (search for a failing thread schedule) or
``replay()`` (re-run a known schedule). interleave-test is imported lazily here
so the package has no hard dependency on it — the axis is an optional extra,
Python >=3.12 only.

Key facts this module is built around (validated against interleave-test 0.1.0):
- ``explore()`` runs the model many times internally under different schedules;
  its ``timeout=`` is a PER-SCHEDULE watchdog, not a total budget, so the parent
  process timeout bounds the search.
- A found ``Failure`` exposes ``.exception`` (the real user error, or None for a
  deadlock), ``.schedule`` (``.to_json()`` includes the Python version), and
  ``.iteration`` (>0 means passing schedules were seen before the failing one).
- Modules that grab a lock during the patch window capture a *modelled* lock
  that then errors at interpreter shutdown; pre-warming the usual offenders
  before ``explore()`` binds their locks to the real classes instead.
"""

from __future__ import annotations

import inspect

# Modules whose module-level locks would otherwise be captured as modelled
# primitives during the patch window and crash at interpreter shutdown.
_PREWARM = (
    "logging",
    "concurrent.futures.thread",
    "concurrent.futures.process",
    "queue",
    "asyncio",
    "subprocess",
    "warnings",
    "selectors",
)

_META = "__flakedoctor__"


class InterleaveDeadlock(Exception):
    """Raised in place of a deadlock, which interleave-test reports with no
    exception of its own. Its stable type makes deadlocks fingerprint cleanly."""


def _prewarm() -> None:
    import importlib

    for name in _PREWARM:
        try:
            importlib.import_module(name)
        except Exception:
            pass


def _build_model(pyfuncitem):
    """Replicate pytest's own default call: testfunction(**resolved_fixtures)."""
    argnames = pyfuncitem._fixtureinfo.argnames
    testargs = {name: pyfuncitem.funcargs[name] for name in argnames}
    func = pyfuncitem.obj  # already the bound method for class-based tests

    def model():
        func(**testargs)

    return model


def _is_drivable(pyfuncitem) -> tuple[bool, str]:
    """Whether this test can be driven as a zero-arg sync model."""
    func = getattr(pyfuncitem, "obj", None)
    if func is not None and (
        inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)
    ):
        return False, "async test (the scheduler drives only synchronous models)"
    for marker in ("asyncio", "anyio", "trio"):
        try:
            if pyfuncitem.get_closest_marker(marker) is not None:
                return False, "async test (the scheduler drives only synchronous models)"
        except Exception:
            pass
    if type(pyfuncitem).__name__ == "TestCaseFunction":
        return False, "unittest-style test (bypasses the pyfunc call hook)"
    return True, ""


def drive(pyfuncitem, cfg: dict, record) -> bool | None:
    """Run the test under interleave-test. Returns True if handled (a pass),
    or None to let pytest run the test normally (axis inapplicable). Raises the
    test's own exception when a failing schedule is found, so pytest's normal
    machinery records the failure with the user's traceback.
    """
    nodeid = pyfuncitem.nodeid
    drivable, why = _is_drivable(pyfuncitem)
    if not drivable:
        record({"nodeid": nodeid, _META: True, "interleave": {"skipped": why}})
        return None  # run the test normally; the axis stands down

    try:
        import interleave_test as it
    except ImportError as exc:
        record(
            {"nodeid": nodeid, _META: True, "interleave": {"error": f"interleave-test unavailable: {exc}"}}
        )
        return None

    _prewarm()
    model = _build_model(pyfuncitem)
    # Instrument the project tree so the user's code (and its helpers) get
    # scheduling points, even when the package is installed editable in
    # site-packages. Only real files under an included path are instrumented.
    include = cfg.get("include")
    if include is None:
        try:
            include = [str(pyfuncitem.config.rootpath)]
        except Exception:
            include = None

    try:
        if cfg.get("mode") == "replay":
            schedule = it.Schedule.from_json(cfg["schedule"])
            result = it.replay(
                model,
                schedule,
                timeout=cfg.get("per_schedule_timeout", 10.0),
                include=include,
                # patch_time replaces the real clock with a virtual one, which
                # makes real-time assertions (elapsed > 0, timestamp ordering)
                # fail for reasons unrelated to any race. Keep the real clock.
                patch_time=False,
                raise_on_failure=False,
            )
        else:
            result = it.explore(
                model,
                iterations=cfg.get("iterations", 200),
                seed=cfg.get("seed", 0),
                strategy=cfg.get("strategy", "pct"),
                timeout=cfg.get("per_schedule_timeout", 10.0),
                include=include,
                patch_time=False,
                raise_on_failure=False,
            )
    except Exception as exc:
        # A scheduler/replay fault is about our tooling, not the user's race.
        record(
            {
                "nodeid": nodeid,
                _META: True,
                "interleave": {"error": f"{type(exc).__module__}.{type(exc).__name__}: {exc}"},
            }
        )
        return None

    failure = getattr(result, "failure", None)
    if failure is None:
        record({"nodeid": nodeid, _META: True, "interleave": _result_meta(result, cfg)})
        return True  # no failing schedule under the modelled primitives → a pass

    exc = getattr(failure, "exception", None)
    kind = getattr(failure, "kind", None)
    detail = str(getattr(failure, "details", "") or "")

    # A per-schedule timeout is reported as a 'hang' with no exception: the model
    # blocked on something the scheduler cannot model (C-level I/O, a long
    # computation, an unpatched primitive) — NOT a race or a deadlock.
    if kind == "hang":
        record(
            {"nodeid": nodeid, _META: True, "interleave": {"blocked": detail[:500] or "per-schedule timeout"}}
        )
        return None

    # A tooling error surfaced as the failure's exception (e.g. a ReplayDivergence
    # when a recorded schedule no longer applies) is ours, not the user's race.
    if exc is not None and type(exc).__module__.split(".")[0] == "interleave_test":
        record(
            {"nodeid": nodeid, _META: True, "interleave": {"error": f"{type(exc).__name__}: {exc}"}}
        )
        return None

    record({"nodeid": nodeid, _META: True, "interleave": _result_meta(result, cfg)})

    # A genuine failing schedule. Re-raise the user's own exception (with its
    # traceback) so pytest records a natural failure and fingerprint.
    if exc is not None:
        raise exc.with_traceback(getattr(exc, "__traceback__", None))
    if kind == "deadlock":
        raise InterleaveDeadlock(detail or "threads deadlocked")
    # Found a failure with neither an exception nor a recognized kind: be safe
    # and do not fabricate a race.
    record({"nodeid": nodeid, _META: True, "interleave": {"blocked": f"unrecognized failure kind: {kind}"}})
    return None


def _result_meta(result, cfg: dict) -> dict:
    failure = getattr(result, "failure", None)
    meta: dict = {
        "found": failure is not None,
        "exhausted": bool(getattr(result, "exhausted", False)),
        "strategy": cfg.get("strategy", "pct"),
        "leaked_threads": getattr(result, "leaked_threads", 0),
    }
    if failure is None:
        return meta
    schedule = getattr(failure, "schedule", None)
    if schedule is not None:
        try:
            meta["schedule"] = schedule.to_json()
            meta["py_exact"] = getattr(schedule, "python", "")
        except Exception:
            pass
    kind = getattr(failure, "kind", None)
    meta["kind"] = kind
    meta["iteration"] = getattr(failure, "iteration", 0) or 0
    # A passing schedule seen before the failing one proves schedule-dependence.
    meta["passing_schedule_seen"] = meta["iteration"] > 0
    details = getattr(failure, "details", None)
    if details:
        meta["detail"] = str(details)[:500]
    return meta
