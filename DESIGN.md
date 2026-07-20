# flakedoctor — design

Distilled from a 9-agent design workflow (2026-07-18): three source-level
capability studies of the building-block packages, a prior-art sweep, three
competing architectures (adoption-first / diagnosis-rigor-first /
platform-first), an adversarial feasibility review verified line-by-line
against the sources, and a judge synthesis. The adoption-first design won;
the grafts and corrections below are folded in.

## Positioning

Every existing tool in this space *detects or retries* flaky tests
(pytest-rerunfailures, pytest-flakefinder, quarantine SaaS). Nobody
*diagnoses*. The research taxonomy (Luo et al. ICSE 2014 and successors) says
flaky root causes cluster into a small set of nondeterminism axes — async
wait, concurrency, test-order dependency, time, randomness, unordered
collections, network, io. Those axes are exactly what Danny's packages
control. flakedoctor = delta-debugging over those axes with a statistical
honesty layer.

## The design invariant

**Every diagnostic run is a fresh `python -m pytest` subprocess.** This one
decision buys:

- the PYTHONHASHSEED axis (fixed at interpreter start; impossible in-process);
- clean module/fixture/import state per run (shared fixture caches are
  themselves a flakiness confounder);
- segfault/hang immunity — OS timeout+kill supersedes any cooperative
  cancellation;
- Windows support (no `os.fork`).

Cost: interpreter+import time per run. The budget planner measures the first
run and re-plans; the in-process fast path is deliberately deferred to v0.3.

## Module layout

```
src/flakedoctor/
  _plugin.py      # parent pytest plugin; no-ops under FLAKEDOCTOR_CHILD=1
  _probe.py       # child plugin (-p flakedoctor._probe): selection order + JSONL phase records
  _runner.py      # subprocess orchestration, timeout+kill, outcome classification
  _axes.py        # AxisProvider-lite (private): hashseed today; hermetic axes next
  _diagnose.py    # the loop: baseline -> sweep -> verify -> counterfactual
  _fingerprint.py # failure identity + message normalization
  _stats.py       # Clopper-Pearson bounds, categories
  _report.py      # terminal block + flakedoctor-report v1 JSON
  _cli.py         # `flakedoctor NODEID` passthrough
```

Planned additions: `_order.py` (polluter prefix delta-debugging), `_sniff.py`
(phase-0 capability sniff), `_repro.py` (blob encode/decode, cache, marker).

## The diagnosis loop

Two symmetric moves per axis: **control** (determinize it — if flakiness
disappears, the flake lives on that axis) and **provoke** (pin an adversarial
value — if failure becomes certain, that value is the repro).

- **Phase 0 — baseline**: `--doctor-runs` (default 10) isolated, unperturbed
  runs, PYTHONHASHSEED stripped so each child randomizes. 10 runs detect any
  flake with true rate ≥ 0.26 at ≥95% probability. Branches: all-fail →
  `deterministic-failure`; all-hang → `hang`; all-pass → quiet flake,
  provocation-first; mixed → live flake.
- **Phase 1 — control sweep** *(lands with the hermetic axes)*: full-control
  config (time+rng+network frozen), then leave-one-out attribution.
- **Phase 2 — provocation sweep**: adversarial values, fixed values before
  random ones, ~2 runs per value. Failure fingerprints gate causal credit.
- **Phase 3 — order axis** *(planned)*: prefix delta-debugging over the
  collected item list down to a minimal named polluter.
- **Phase 4 — verification**: winning config must fail n/n (default 10) fresh
  runs with a consistent fingerprint. Report prints the count plus the exact
  one-sided 95% Clopper-Pearson lower bound (10/10 → ≥74%). Partial results
  demote to "elevates failure rate" and the loop tries the next candidate.
- **Phase 5 — counterfactual**: a value observed to pass must pass again
  (default 5/5) or the verdict downgrades. This closes the
  correlation-vs-causation hole a verification-only gate leaves open.

### Claim-type honesty

Verdict wording is chosen mechanically:

- **observed** — baseline failed AND the verified repro carries the baseline's
  dominant fingerprint: "your observed flake is caused by X".
- **latent** — baseline never failed, or the repro's fingerprint differs:
  "a latent flake provoked by X; your observed failures may differ".

### Never claim a cause the numbers don't support

Four gates, each added after a review or a live run produced a confident wrong
answer:

- **The failure must be the right failure.** Only failures carrying the
  baseline's dominant fingerprint can raise an axis's attributed rate.
  Otherwise `fs=isolate` breaking a test with an unrelated `FileNotFoundError`
  gets reported as "filesystem-state dependent" for a flake it has nothing to
  do with.
- **The axis must discriminate.** "X causes the failure" requires that not-X
  does not. If no value on an axis lets the test pass, the verdict is
  downgraded to *unconfirmed* — something shared by every run under that axis
  (often merely being inside a sandbox) is the likelier explanation. Found by
  pointing the tool at a test whose real nondeterminism was address parity,
  which any sandbox stabilizes.
- **The perturbation must have actually applied.** An axis that stood itself
  down — a virtual clock skipped for an async test — is excluded from that
  axis's evidence rather than credited with whatever happened without it.
- **A clean streak is not significance.** See below.

Two gates, both learned the hard way from review findings:

- **Significance.** A candidate only counts as "elevating the failure rate"
  when the elevation is statistically distinguishable from the baseline rate
  (`elevation_pvalue < 0.05`, exact binomial; a clean 0/n baseline is tested
  against its own 95% upper bound rather than against 0). Without this, an
  ordinary 50/50 coin-flip flake produces streaks that look like elevation
  and the tool confidently blames whichever seed streaked. A confident wrong
  diagnosis is this project's worst failure mode.
  The same rule governs the control phase: "controlling this axis stabilized
  the test" needs `stabilization_pvalue < 0.05`, because at a 20% failure rate
  five clean runs happen a third of the time. Below that bar the row reads
  *inconclusive* rather than *← stabilizer*, and the report says the
  attribution is a lead rather than a conclusion.
- **Evidence must have actually run.** Pass-evidence requires
  `outcome == "pass"`, never merely "did not fail" — a child that errored or
  skipped proves nothing, and previously could satisfy the counterfactual
  gate with zero real runs.

Similarly, any provocation failure that goes unattributed is surfaced rather
than dropped: "not-flaky" is only ever printed when nothing failed anywhere.

### Fingerprints

Identity = `(phase, exception type, crash site)`. The normalized message is
recorded for display but **deliberately excluded from the identity**: pytest's
assertion rewriting embeds run-varying values (`assert 'banana' not in ...`),
so matching on messages misclassifies nearly every real flake as a "different
failure" (found empirically by this project's own e2e suite). Hangs and
crashes get synthetic fingerprints (`<hang>`, `<crash>`) so flaky-hangs are
first-class. Doctor-origin exceptions (hermetic's SandboxError,
NetworkBlockedError, …) will be classified as perturbation-induced, never as
the user's flake.

## How the three packages plug in

### hermetic — the load-bearing dependency (IMPLEMENTED)

Published as distribution **`hermetic-sandbox`** with import name **`hermetic`**
— the two differ, which the dependency declaration has to get right.

Drive `hermetic.Sandbox(...)` (the class, not the `sandbox()` factory —
`test_id=` exists only on the ctor) **around the whole runtest protocol** in
the child probe; hermetic's own plugin wraps only the call phase, so fixture
nondeterminism would escape. Children pass `-p no:hermetic` so its auto plugin
stands down. Because hermetic defaults every subsystem *on*, a merged config
starts from `SANDBOX_OFF` and each axis switches only its own subsystem back
on — otherwise a "time-only" probe would silently also seed the RNG and block
the network, and every attribution would be wrong.

Axis mapping: `clock="virtual"` frozen at adversarial instants (day/month/year
rollovers, leap day, DST) plus a 1-second tick as the coarse slow-machine
simulation; `rng="all"` with a seed sweep; `network="block"`; `fs="isolate"`
with `chdir=False` (changing the working directory would break repo-relative
fixture paths and manufacture unrelated failures).

Async tests skip the clock axis — a virtual clock never advances an awaited
sleep, so the test would hang forever. The child probe makes that call locally
(`inspect.iscoroutinefunction` plus asyncio/anyio/trio markers) and reports it
back as probe metadata, which surfaces as a report warning. Async-wait is a
large share of real-world flakes, so this is a coverage gap to state plainly,
not to paper over.

### The test-order axis (IMPLEMENTED)

Structurally unlike the others: every sandbox axis perturbs a *single* test,
but the order axis runs *many* tests — the victim after a prefix of the suite —
because a polluter is a different test entirely. It exists because test-order
dependence tops the research taxonomy of Python flakes, and its output is the
most actionable the tool produces: a named polluter, not just an axis.

The engine's `diagnose()` gains a `prefix` (the ordered nodeids collected
before the victim, from suite mode). The phase runs only when the victim
**passes alone** but a prefix precedes it — the "passes alone, fails in the
suite" pattern. It confirms `[prefix + victim]` reproduces, then delta-debugs
the prefix with a binary search (`_bisect_polluter`) down to a minimal
polluter (~log₂n runs for a single polluter; the smallest proven reproducing
window for interactions), and verifies n/n.

Three mechanics that had to be right:

- **Multi-test runs need their own timeout.** A run of a 500-test prefix
  legitimately takes far longer than the victim alone; sizing its timeout on
  the victim-alone duration would kill it and record a fabricated hang. The
  first full-prefix run is measured and becomes the `timeout_floor` for the
  rest of the phase.
- **The victim is the last nodeid.** `ProbeConfig` already carried a nodeid
  tuple; the child probe enforces the exact order and attributes the outcome
  to the last one, so `[*prefix, victim]` "just works" against the existing
  runner.
- **It runs before the per-test axes**, because in suite mode a polluter is
  the likeliest explanation of a test that fails only in company. If the
  prefix does *not* reproduce (the trigger runs after the victim, or needs a
  parallel xdist worker), it says so and falls through to the sandbox axes
  rather than forcing a wrong answer.

The order repro carries the exact nodeid sequence in the blob; `--doctor-repro`
reorders the collected items to match (defending against pytest's own
definition-order sorting) and judges only the victim.

Hardening the order axis (an adversarial review confirmed 10 defects, all
fixed) turned on one principle: **classify an order run by the victim's own
recorded phases, never by the subprocess exit code.** A prefix test that fails
makes pytest exit non-zero even when the victim passed; a prefix test that
hangs or crashes kills the subprocess before the victim runs at all. So each
run carries the nodeid its phases belong to (`last_nodeid`), and the victim
counts as failing only when it is the last recorded nodeid *and* its own
setup/call phase failed. A hang or crash of the prefix is therefore never
fabricated into an order dependency — it is "the victim never ran," which
falls through honestly. Three more consequences of the same review:

- **The first full-prefix run gets a timeout sized from the prefix length**,
  not from the victim-alone duration. Without it, a long-but-innocent prefix
  was killed at the 30-second floor and the fabricated hang drove a false
  verdict. The hang-exclusion above is the backstop when the estimate is still
  too low: a killed prefix run simply doesn't reproduce.
- **A counterfactual runs the victim alone again** after verification. If it
  fails there too, the flake is independent of order (a latent hashseed/rng
  bug a quiet baseline happened to miss), so the order claim is withdrawn and
  the per-test axes take over.
- **Large prefixes travel in a file, not argv/env.** Order runs name the test
  *files* on the command line (deduplicated) and pass the exact nodeid list in
  a temp file the probe reads, keeping the child launch under the OS
  command-line limit; the `Popen` is guarded so any residual `OSError` becomes
  a child-error rather than aborting the diagnosis.

### The interleave axis (IMPLEMENTED — opt-in extra)

The most structurally different axis. Every other axis perturbs the test's
*environment*; this one replaces the test's *execution* with a deterministic
thread scheduler ([interleave-test](https://pypi.org/project/interleave-test/))
that searches for an interleaving under which the test fails — a race or a
deadlock — then verifies it by replay.

- **The adapter.** A child-side `pytest_pyfunc_call` override turns the test
  into a zero-arg model: `model = lambda: pyfuncitem.obj(**resolved_fixtures)`,
  exactly what pytest itself would call. Fixtures are resolved once; the model
  body is re-run many times inside `explore()`. On a found race the model
  re-raises the user's own exception (with its traceback) so the normal
  report/fingerprint path records a natural failure; on no race it returns True
  → a pass. Async and unittest-style tests are declined.
- **One subprocess, N internal runs.** `explore()`'s `timeout=` is a
  *per-schedule* watchdog, not a total budget (validated), so the parent
  subprocess kill-timeout bounds the search; the search child is given a
  generous `timeout_floor` (the order-axis pattern) and does not feed the
  victim-alone duration. Strategies escalate cheapest-first
  (`random → pct → dfs`); DFS's `exhausted=True` is the strongest bounded
  negative.
- **Gating.** The baseline probe wraps `threading.Thread.start` with a counter
  and reports `thread_starts` + `is_async`; the axis runs only when ≥2 threads
  were actually started and the test is synchronous. The child needs
  interleave-test importable (Python ≥3.12, the `interleave` extra); when it
  is not, the axis reports itself unavailable rather than "no race."
- **The honest discriminator (hardened after review).** The axis runs only on a
  *quiet* baseline (the test passes alone), so the claim is always *latent* — a
  real race the scheduler surfaced — never confounded with a mixed baseline
  caused by an rng/hash flake. Four gates prevent a confident false-positive
  "race", each closing a reproduced failure mode:
  - **`patch_time=False`.** interleave-test's virtual clock made real-time
    assertions (`elapsed > 0`, timestamp ordering) fail under *every* schedule
    for reasons unrelated to any race. The real clock is kept.
  - **Only a genuine user exception is a race.** A per-schedule timeout is
    reported as `kind='hang'` (the model blocked on something the scheduler
    cannot model — I/O, a long computation); it is treated as *inconclusive*,
    never raised as a race or deadlock. A tooling error (`ReplayDivergence` on a
    stale schedule) is excluded too.
  - **Fresh-subprocess replay must reproduce it.** A schedule that does not
    replay deterministically is not claimed — this also catches the case where
    `explore()` re-runs the body N times in one process and a *mutable fixture*
    accumulates state (the accumulation-failure does not survive a one-shot
    replay in a fresh process).
  - **The report never says "no race exists"** — only "no failing interleaving
    under the modelled primitives," listing what is uncovered.
- **Coverage honesty (stated in every negative).** Only threads and locks
  created at *call time* are modelled. Import-time bindings (`lock =
  threading.Lock()` at module level, `from threading import Lock`, a `Thread`
  subclass defined at import), thread-pool internals, and C-level threads are
  invisible. Modules that grab a lock during the patch window are pre-warmed so
  their locks bind to the real classes and do not crash at interpreter
  shutdown.
- **Repro.** The `fd1:` blob carries the schedule JSON verbatim (opaque data
  consumed only by `replay()`, never a Sandbox — so the rebuild-from-definition
  security rule is satisfied), plus the exact Python version. `--doctor-repro`
  and `@pytest.mark.flakedoctor_repro` both replay in-process through the same
  adapter.
- **Optional dependency.** `[project.optional-dependencies] interleave =
  ["interleave-test>=0.1.0; python_version >= '3.12'"]`; the core stays
  `requires-python = ">=3.10"` and never imports `interleave_test` at plugin
  load.

### The repro marker (IMPLEMENTED)

`@pytest.mark.flakedoctor_repro("fd1:...")` is the paste-into-code repro, the
Hypothesis `@reproduce_failure` analogue. The report prints it for sandbox-axis
flakes; pasting it above the test makes the flake reproduce deterministically
on ordinary runs, so it becomes a version-controlled artifact that fails in CI
and code review until fixed.

`_MarkerPlugin` (a hookwrapper) is registered only on *normal* runs — not under
`--doctor`/`--doctor-repro`, where the doctor's own machinery owns the run, and
never in diagnostic children (`FLAKEDOCTOR_CHILD=1`), where it would double-
apply. It decodes the blob with the victim's nodeid (so a marker pasted on the
wrong test is inert), re-applies the `hermetic.Sandbox` for a sandbox axis, and
for hashseed/order — which cannot be re-applied purely in-process — reports how
to reproduce instead of silently doing nothing. The marker *name* is registered
unconditionally (even in children) so a marked test never trips an unknown-mark
warning.

### Repro blobs (IMPLEMENTED — and mandatory, not optional)

hermetic's CLI exposes `--hermetic-seed/--hermetic-all/--hermetic-record/
--hermetic-replay` and nothing for clock, network, or filesystem. A diagnosed
time-axis failure therefore *cannot* be expressed as flags, which is what
forced repro blobs into this round rather than a later one.

Format: `fd1:` + urlsafe-b64(zlib(canonical JSON)), carrying per-axis payload
versions, the tool version, the Python minor, a nodeid digest, the observed
fingerprint, the confirmation count, and `TZ` when it is set. Decoding is
strict — unknown axis, newer payload version, or a blob recorded for a
different test all raise `ReproFormatError` rather than quietly applying less
than the blob claims. Values are **rebuilt from axis definitions** rather than
taken verbatim, so a crafted blob cannot smuggle arbitrary kwargs (`record=`,
`replay=`) into a `Sandbox` call. `--doctor-repro` applies the perturbation
in-process so the failure is debuggable, and a repro that passes exits non-zero
with `DID NOT REPRODUCE`.

Verified corrections from the feasibility review:

- hermetic journals record **only clock reads and urandom draws** — no
  network/fs telemetry. The capability sniff must use a `Thread.start`
  counter and harvest `NetworkBlockedError.address` from one blocked run.
- Frozen wall time converts through the **machine's local timezone** and
  deterministic mode never pins TZ — repro artifacts must carry TZ/locale.
- Wanted upstream (small PR): blocked-network-attempt counter on
  `NetworkPolicy` so user `except ConnectionError` can't swallow the signal.

### cancelscope — benched until v0.3

Subprocess timeout+kill strictly supersedes cooperative cancellation for this
tool (interrupts C-level blocks, deadlocks, segfault loops). Verified
conflict: cancelscope's deadline monitor reads the patched `time.monotonic`,
so under hermetic's frozen clock deadlines never fire. It returns in the
in-process fast path as the per-rerun budget — `move_on_after` + post-call
`checkpoint()`, entered *outside* the sandbox, with a real-clock watchdog
thread calling `scope.cancel()` (cancellation rides a `threading.Event`,
which hermetic cannot virtualize — verified).

### interleave-test — the v0.2 axis

`explore()` already monkeypatches `threading.*` and `time.sleep`, so
stdlib-threading code needs no opt-in (verified). Constraints that shape the
integration: Python ≥3.12 floor (`sys.monitoring` at import); patch-window
import poisoning ⇒ **subprocess-per-exploration with import pre-warming**;
zero-arg model contract ⇒ fixture-closure adapter; `include=` needed for
installed packages; `Schedule.to_json()` carries no Python version ⇒ the blob
must pin `py_exact` with seed-re-search fallback. Permanent coverage holes to
document, not paper over: import-time `from threading import Lock` bindings,
Thread subclasses defined before the patch window, `ThreadPoolExecutor`,
untracked threads. Until then, v0.1 keeps a coarse concurrency signal:
`sys.setswitchinterval` stress (detection-grade only).

## Testing the doctor without a flaky test suite

A tool that diagnoses flakiness must not flake itself, and its corpora are
adversarial by construction — so each one is chosen for a *deterministic*
statistical profile, not merely an improbable one:

- The **uncontrolled-flake** corpus (asserting the tool refuses to guess) uses
  process-id parity. Consecutive children get consecutive pids, so it
  alternates exactly: baseline reliably 5/10 (MIXED), every 2-rep sweep
  exactly 1/2 (never a strong candidate), verification 5/10 (never clears the
  elevation bar). Randomness would be self-defeating here now that the rng
  axis controls it.
- The **rng** corpus computes its forbidden value from hermetic at test time
  rather than hardcoding one, so it cannot drift out of sync with hermetic's
  seeding, and its unseeded failure rate is 1e-6.
- The **time** corpus anchors on the real year rather than on "is today
  month-end?", so the unperturbed baseline is green on every calendar day.

Rule of thumb: if a corpus's pass/fail depends on chance, compute the flake
probability of the assertion around it and keep it below ~1-in-10000, or
restructure the corpus until the outcome is deterministic.

## Robustness contract (hard-won)

An adversarial review of the week-1 slice (six reviewer dimensions, every
finding attacked by a refute-by-default skeptic) confirmed 28 defects, all
fixed. The invariants worth stating so they are not re-broken:

- **Children run at the rootdir, not the invocation dir.** Nodeids are
  rootdir-relative, so running children anywhere else makes every child exit 4
  — the whole tool silently reduced to "could not run the test" whenever
  pytest was invoked from a subdirectory. The printed repro command, by
  contrast, is respelled relative to the user's shell so it can be pasted.
- **A timeout is never shrunk below the observed run duration.** A run killed
  by a budget-truncated timeout would be recorded as a hang, i.e. a fabricated
  failure. When the remaining budget cannot fund a full run, stop and report
  `incomplete` instead.
- **A timeout does not by itself mean "hang".** `communicate()` waits for pipe
  EOF, not process exit; a background process inheriting the pipes can outlive
  the test. If the probe recorded a teardown, the test finished — classify it
  normally and report the process leak.
- **Work and persistence never live in `pytest_terminal_summary`.** The suite-
  mode diagnosis and the JSON write happen in `pytest_sessionfinish`, so
  `-p no:terminal` cannot silently discard a completed diagnosis; the terminal
  hook is a pure renderer.
- **`PYTHONPATH` is appended, never prepended.** Prepending our install dir
  (site-packages, when installed) flips the user's deliberate module shadowing
  and makes children diagnose different code than the user runs.
- **Child environment is scrubbed of `PYTEST_ADDOPTS`.** The child's argv is
  fully constructed here; inherited addopts can only distort measurement, and
  options owned by the plugins we disable (`-n auto`) break the child outright.
- **Child output is decoded lossily** (`utf-8`/`backslashreplace`): stray bytes
  on a child's stderr must not abort a multi-minute diagnosis.
- **Every exit path kills the child.** A `finally` guard covers Ctrl-C and any
  other unwind; children live in their own process group, so the terminal's
  SIGINT never reaches them.
- **A doomed configuration is detected on run 1**, not after the full baseline.
- **Repro commands are shell-quoted** (`shlex.quote`, PowerShell `''`):
  parametrized nodeids routinely contain `$`, quotes, and backticks.
- **Crash detection covers Windows** NTSTATUS codes, not just POSIX signals.
- **Bad `--doctor-json` paths fail at configure time**, before the diagnosis
  burns minutes of work.

## Child process contract

```
env:  FLAKEDOCTOR_CHILD=1, FLAKEDOCTOR_PROBE=<json>, FLAKEDOCTOR_RESULT_FILE=<jsonl>,
      per-axis env deltas (value None = remove, e.g. baseline strips PYTHONHASHSEED)
argv: python -m pytest -q --tb=no -p flakedoctor._probe
      -p no:randomly -p no:rerunfailures -p no:flaky -p no:xdist -p no:hermetic
      <nodeids...>
```

Plugin autoload stays ON (user fixtures must work); only measurement
distorters are neutralized. Results are file-based JSON lines (one per test
phase), written append-per-line so a later segfault cannot lose completed
phases. Kill is process-group based: `killpg` on POSIX,
`CREATE_NEW_PROCESS_GROUP` + `taskkill /T` on Windows. Repro commands are
printed in platform-appropriate syntax (env-prefix on POSIX, `$env:` on
PowerShell).

## Report contract

`flakedoctor-report` v1 (`--doctor-json`): additive-only within version 1;
consumers ignore unknown keys. Carries verdict + claim, the full evidence
table with raw per-config counts (auditable confidence), stats including the
CP lower bound, warnings, env (python/platform/inherited hashseed). Verdict
codes: `flaky-hashseed`, `flaky-unattributed`, `deterministic-failure`,
`hang`, `not-flaky`, `skipped`, `usage-error`, `child-error`, `incomplete`.
New axes add new `flaky-<axis>` codes.

## Known gaps (tracked, not hidden)

- **Async tests**: the virtual-clock axis must skip them; ~45% of real flakes
  are async-wait. Needs an honest banner + an asyncio virtual-time story.
- **Rerun safety**: re-running a side-effectful test 35-100 times can damage
  external state. Planned: a warning when the sniff observes network, and a
  `flakedoctor_rerun_unsafe` marker.
- **xdist-context failures**: a flake observed under `-n 8` may not reproduce
  under serial diagnosis; detect and say so.
- **Flaky fixtures / parametrized siblings**: "your session fixture is flaky,
  not your test" should become a first-class verdict.
- **Environmental axes**: TZ/locale sweeps are cheap (env vars); "fails only
  in CI" needs the doctor to run in CI (`--doctor-json` is the on-ramp).
- **pytest private-API drift**: current code sticks to public hooks; keep a
  pytest 7/8/9 CI matrix when adding rerun tricks.
- **Repro staleness**: blobs will pin tool version + python minor + git state,
  and a passing repro exits loudly with DID NOT REPRODUCE (Hypothesis
  semantics).

## Roadmap

- **v0.1** (rest of): hermetic axes + control sweep + leave-one-out; order
  axis with polluter bisection; capability sniff; repro blobs
  (`fd1:` + canonical JSON + zlib + b64, TZ included), cache replay, marker.
- **v0.2**: interleave axis (subprocess-per-exploration); hermetic
  NetworkPolicy counter PR; time-boundary shrinking ("fails when now >= X").
- **v0.3**: in-process fast path (cancelscope returns); xdist-simulation axis;
  fixture-cache invalidation axis.
- **v0.4**: free-threading safety auditor (`flakedoctor audit <pkg>` on
  3.13t/3.14t, PCT via interleave-test); GitHub Action posting the block on
  PRs.

## Open questions

- Exit-code semantics in diagnosis mode (currently 0 on any completed
  diagnosis; CI users may want nonzero for confirmed flakes).
- Name distribution: `pytest-flakedoctor` (dist) + `flakedoctor` (import,
  brand, console script); both verified available on PyPI 2026-07-18 —
  reserve on first release.
