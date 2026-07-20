# Changelog

All notable changes to `pytest-flakedoctor` are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] — 2026-07-20

### Changed

- **Stabilization and elevation significance now use a one-sided Fisher exact
  test** instead of testing the observed count against the baseline *point
  estimate* as if it were the true rate. The old approach overstated evidence
  with small samples — e.g. it called 0/5 clean control runs against a 5/10
  baseline significant (`0.5**5 = 0.03`) when the honest two-sample p-value is
  ~0.084. This only affected the softer "stabilizer" / "elevates failure rate"
  language, not the deterministic-repro path (which already used an exact
  Clopper–Pearson bound), but it conflicted with the project's core promise of
  never claiming more than the evidence supports. A 5/10 baseline now correctly
  needs 7 clean control runs, not 5, before a stabilizer is called conclusive.

### Fixed

- Documentation: added an install section, corrected the axis count (seven, not
  five/six), and removed the "not yet on PyPI" line now that the package is
  published.

## [0.1.0] — 2026-07-20

The first release. `pytest <nodeid> --doctor` diagnoses *why* a test is flaky
and hands you a deterministic reproduction — it never guesses a cause the
numbers don't support, and it says "I don't know" when they don't.

### Added

- **The diagnosis loop.** A control → provoke → verify → counterfactual pipeline
  that isolates one axis at a time and reports a Clopper–Pearson confidence
  bound, so a claim is backed by a measured reproduction rate.
- **Seven perturbation axes**, each catching a distinct cause of flakiness:
  - `order` — a *polluter* test that leaks state a later test depends on, found
    by running the victim after a shrinking prefix of the suite.
  - `interleave` — a thread race or deadlock, found by driving the test through
    a deterministic scheduler that searches interleavings (opt-in; see below).
  - `time` — month/day/year rollovers, DST, leap day, "assumes the code runs
    fast", via a virtual clock frozen at adversarial instants.
  - `rng` — unseeded `random`/`secrets`/`uuid4` and colliding fixture data.
  - `network` — hidden live-network dependencies.
  - `fs` — ambient files, `$HOME` leftovers, cross-test residue.
  - `hashseed` — dict/set iteration order leaking into behavior, via
    `PYTHONHASHSEED`.
- **Deterministic repro.** Every diagnosis emits a `fd1:` blob that replays the
  exact failing condition with `--doctor-repro`, plus a paste-in
  `@pytest.mark.flakedoctor_repro(...)` marker that reproduces it on a normal
  run and stays inert under `--doctor`.
- **Honest refusals.** When no axis explains the flake, the doctor says so
  rather than inventing a cause; every negative names what it could *not* rule
  out.

### Notes

- The `time`/`rng`/`network`/`fs` axes are powered by
  [hermetic-sandbox](https://pypi.org/project/hermetic-sandbox/) (import name
  `hermetic`), installed automatically.
- The `interleave` axis is an opt-in extra —
  `pip install "pytest-flakedoctor[interleave]"` — powered by
  [interleave-test](https://pypi.org/project/interleave-test/) and requiring
  Python ≥3.12. The core supports Python ≥3.10. The axis only activates when a
  test actually starts threads, verifies every finding by deterministic replay
  before claiming a race, and reports "no failing interleaving under the
  modelled primitives" rather than "no race exists" — import-time primitives,
  thread-pool internals, and C-level threads are outside what it models.

[Unreleased]: https://github.com/Therealdk8890/pytest-flakedoctor/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Therealdk8890/pytest-flakedoctor/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Therealdk8890/pytest-flakedoctor/releases/tag/v0.1.0
