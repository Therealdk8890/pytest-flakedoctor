# pytest-flakedoctor

**Every other tool retries your flaky test. flakedoctor tells you *why* it's
flaky — and hands you a command that makes it fail every time.**

Point it at a test that fails one run in twenty. It re-runs the test in fresh
subprocesses under controlled perturbation, bisects which axis of
nondeterminism triggers the failure, verifies the repro statistically, checks
the counterfactual, and prints this:

```
━━━ flakedoctor ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 test_billing.py::test_invoice_ids_stable
 py3.14.4 · darwin · 35 runs · 8.5s

 DIAGNOSIS  hash-order dependent (PYTHONHASHSEED)
   Failed 3/10 isolated baseline runs; fails 10/10 with PYTHONHASHSEED=2
   and passed the counterfactual with PYTHONHASHSEED=0. The test's
   outcome depends on Python's hash randomization: dict/set iteration
   order (or hash-based ordering) leaks into an assertion or into order-
   sensitive logic. PYTHONHASHSEED=2 pins the order that triggers the
   failure.

 EVIDENCE                                                  runs  failed
   baseline (isolated, no perturbation)                     10       3
   provoke: PYTHONHASHSEED=0                                 2       0
   provoke: PYTHONHASHSEED=1                                 2       0
   provoke: PYTHONHASHSEED=2                                 2       2
   provoke: PYTHONHASHSEED=3855983186                        2       2
   provoke: PYTHONHASHSEED=2386784001                        2       0
   VERIFY: PYTHONHASHSEED=2                                 10      10   ✓ deterministic
   counterfactual: PYTHONHASHSEED=0                          5       0   ✓ passes

 REPRO (fails 10/10; ≥74% repro rate at 95% confidence)
   PYTHONHASHSEED=2 pytest "test_billing.py::test_invoice_ids_stable"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Three minutes ago this failed once a week. Now it fails on demand. Paste the
command into the bug report and fix it.

## Install

```sh
pip install pytest-flakedoctor                 # order, time, rng, network, fs, hashseed
pip install "pytest-flakedoctor[interleave]"   # + the thread-interleaving axis (Python ≥3.12)
```

Requires Python ≥3.10. Then point `--doctor` at a flaky test:

```sh
pytest path/to/test.py::test_name --doctor
```

## Usage

```sh
pytest tests/test_billing.py::test_invoice_ids_stable --doctor   # diagnose this test
pytest --doctor                       # run the suite; diagnose the first failure
flakedoctor tests/test_x.py::test_y   # console-script sugar for the first form
```

Options (also settable via ini):

| Flag | Default | Meaning |
|---|---|---|
| `--doctor-runs=N` | 10 | baseline sample size |
| `--doctor-budget=SECS` | 300 | wall-clock budget; the planner degrades gracefully |
| `--doctor-json=PATH` | — | machine-readable `flakedoctor-report` v1 (CI artifact) |

## Axes

| Axis | What it catches | How it's controlled |
|---|---|---|
| `order` | a *polluter* test that leaks state the victim depends on being absent | run the victim after a shrinking prefix of the suite |
| `interleave` | thread races and deadlocks — a schedule that makes a threaded test fail | drive the test through a deterministic scheduler searching interleavings |
| `time` | month/day/year rollovers, DST, leap day, "assumes the code runs fast" | virtual clock frozen at adversarial instants, plus tick jitter |
| `rng` | unseeded `random`/`secrets`/`uuid4`, colliding fixture data | all randomness seeded from one seed |
| `network` | hidden live-network dependencies | outbound connections blocked |
| `fs` | ambient files, `$HOME` leftovers, cross-test residue | isolated filesystem tree |
| `hashseed` | dict/set iteration order leaking into behavior | `PYTHONHASHSEED` per subprocess |

The four sandbox axes are powered by [hermetic](https://pypi.org/project/hermetic-sandbox/)
(installed automatically; distribution `hermetic-sandbox`, import name `hermetic`).

### The interleave axis (opt-in)

For threaded tests, the doctor can search for the exact thread schedule that
makes them fail — a race or a deadlock — using
[interleave-test](https://pypi.org/project/interleave-test/)'s deterministic
scheduler, then hand you a schedule that reproduces it every time:

```
 DIAGNOSIS  race condition (thread interleaving)
   The test passes 10/10 times on its own, but a specific thread
   interleaving makes it fail — a real race the scheduler usually hides.
   The found schedule reproduces it 3/3 times. Cause: AssertionError at
   test_counter.py:16 (lost update). Add synchronization ...
```

It's an **opt-in extra** — `pip install pytest-flakedoctor[interleave]` — and
needs Python ≥3.12 (the core supports ≥3.10). It only activates when a test
actually starts threads, verifies every finding by deterministic replay before
claiming anything, and is honest about what it *can't* see: threads or locks
created at import time, thread-pool internals, and C-level threads are outside
the modelled primitives, so it reports "no failing interleaving under the
modelled primitives," never "no race exists."

### The order axis

Test-order dependence is the most common cause of flaky tests in Python, and
its diagnosis is the most satisfying: not "something is nondeterministic" but a
*named culprit*. Run the whole suite under `--doctor`; when a test passes alone
but failed in the suite, the doctor runs it after the collected prefix, then
binary-searches that prefix down to the polluter:

```
 DIAGNOSIS  test-order dependent
   The test passes 10/10 times on its own, but fails when run after the
   test test_registry.py::test_register_plugin — fails every run in this
   order. This is a test-order dependency: that test leaves behind state
   the victim depends on being absent.

 EVIDENCE                                                  runs  failed
   baseline (isolated, no perturbation)                     10       0
   after full suite prefix (4 tests)                         3       3
   VERIFY: after test_registry.py::test_register_plugin     10      10   ✓ deterministic
```

The repro command runs just `[polluter, victim]` in the order that fails.

## How it works

Every diagnostic run is a **fresh pytest subprocess** — that buys clean
module/fixture state per run, hang/crash immunity via OS kill, and axes that
are impossible to vary in-process (hash randomization is fixed at interpreter
start). The loop:

1. **Baseline** — N isolated, unperturbed runs. Deterministic failure and
   hangs are called out immediately; a mixed result means a live flake.
2. **Control sweep** — determinize every axis at once. If the flakiness
   disappears, the cause is one of them, and controlling each axis alone
   identifies *which* — the stabilizer. That axis gets swept first.
3. **Provocation sweep** — pin adversarial values on the suspect axis. Every
   failure is *fingerprinted* (phase, exception type, crash site) so a
   perturbation that breaks the test differently than the observed flake never
   earns causal credit — and failures raised by the sandbox itself
   (`NetworkBlockedError` and friends) are reported as perturbation-induced,
   never as your bug.
4. **Verification gate** — the winning value must fail n/n fresh runs. The
   report never claims 100%; it prints the count and the exact
   Clopper-Pearson lower bound.
5. **Counterfactual gate** — a benign value on the same axis must actually
   pass, or the verdict is downgraded to "elevates failure rate".

## Reproducing

hermetic's CLI can express a seed but not a clock, network, or filesystem
configuration, so a diagnosed perturbation travels as a compact blob:

```sh
pytest tests/test_billing.py::test_x --doctor-repro=fd1:eNodTs0KwjAYe5dc...
```

That applies the perturbation **in-process**, so you can attach a debugger and
get a normal traceback. Blobs are versioned per axis and decode strictly: an
unknown axis, a newer payload version, or a blob recorded for a different test
fails loudly rather than quietly doing less than it claims. A repro that passes
exits non-zero with `DID NOT REPRODUCE` (Hypothesis semantics) — staleness is
never silent.

For the sandbox axes (time/rng/network/fs), the report also prints a marker:

```python
@pytest.mark.flakedoctor_repro("fd1:eNoVTksKw...")
def test_invoice_period_label():
    ...
```

Paste it above the test and commit it. Now the flake reproduces
deterministically on every ordinary `pytest` run — in CI and in code review —
until someone fixes it, exactly like Hypothesis's `@reproduce_failure`. (It's
inert during `--doctor` runs and in the doctor's own subprocesses; hashseed and
order repros can't be re-applied by a marker, so those print the command to use
instead.)

Verdicts are honest: `flaky-time` / `flaky-rng` / `flaky-network` / `flaky-fs` /
`flaky-hashseed` (each either *observed* — it explains the failure you actually
saw — or *latent*, a bug the doctor provoked), plus `deterministic-failure`,
`hang`, `flaky-unattributed` ("real flake, cause not in covered axes — here's
the evidence"), `incomplete`, and `not-flaky`.

The tool is built to refuse a diagnosis it can't support. An elevated failure
rate has to clear a significance test against the baseline rate, so an ordinary
coin-flip flake never gets blamed on whichever hash seed happened to streak;
"passing" evidence must come from runs that actually executed the test, not
merely from runs that didn't fail; and any provocation failure left
unattributed is reported rather than dropped.

## Status

Alpha, published on [PyPI](https://pypi.org/project/pytest-flakedoctor/).
Working today: the subprocess engine, the full control → provoke → verify →
counterfactual loop, all seven axes (order, interleave, time, rng, network, fs,
hashseed), failure fingerprinting, repro blobs, the repro marker, terminal +
JSON reports, and suite mode.

Roadmap (see [DESIGN.md](DESIGN.md) for the full architecture):

- **later** — in-process fast path for slow-import suites; xdist-parallel
  order simulation; free-threading safety auditor
  (`flakedoctor audit <package>` on 3.13t/3.14t); GitHub Action posting
  diagnoses on PRs.

Known gaps, stated plainly: async tests skip the clock axis (a virtual clock
hangs awaited sleeps), and async-wait is a large share of real-world flakes;
a time-based repro may not transfer across machines unless `TZ` is set, which
the report warns about; and re-running a side-effectful test dozens of times
is not yet gated behind a safety check.

## Development

```sh
PYTHONPATH=src python -m pytest tests/          # full suite (~1 min; spawns real children)
PYTHONPATH=src python -m pytest tests/ -k "not e2e and not runner"   # fast logic tests
```

MIT license.
