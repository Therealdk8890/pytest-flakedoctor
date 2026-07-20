"""Run-count arithmetic and honest confidence bounds.

flakedoctor never claims a repro is "100% deterministic"; it prints the
observed count and an exact one-sided lower confidence bound on the true
failure rate (Clopper-Pearson). For an n/n result the bound reduces to
alpha**(1/n) — e.g. 10/10 at 95% confidence means the true repro rate is
at least ~0.74.
"""

from __future__ import annotations

from math import comb

ALWAYS = "ALWAYS"
NEVER = "NEVER"
MIXED = "MIXED"


def category(failed: int, runs: int) -> str:
    if runs <= 0:
        raise ValueError("runs must be positive")
    if failed == runs:
        return ALWAYS
    if failed == 0:
        return NEVER
    return MIXED


def binom_tail_ge(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    return sum(comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k, n + 1))


def clopper_pearson_lower(k: int, n: int, alpha: float = 0.05) -> float:
    """Exact one-sided lower confidence bound for a binomial proportion.

    Returns L such that, with confidence 1 - alpha, the true failure
    probability is at least L given k observed failures in n runs.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= k <= n:
        raise ValueError("k must be in [0, n]")
    if k == 0:
        return 0.0
    if k == n:
        return alpha ** (1.0 / n)
    # L solves P(X >= k | p=L) = alpha; the tail is increasing in p, so bisect.
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if binom_tail_ge(k, n, mid) < alpha:
            lo = mid
        else:
            hi = mid
    return lo


def detection_probability(p: float, n: int) -> float:
    """P(at least one failure in n runs) for true failure rate p."""
    return 1.0 - (1.0 - p) ** n


def stabilization_pvalue(baseline_failed: int, baseline_runs: int, control_runs: int) -> float:
    """P(seeing zero failures in `control_runs`) if the baseline rate still held.

    A clean streak is not evidence on its own: at a 20% failure rate, five
    passes happen 33% of the time. Claiming "controlling this axis stabilized
    the test" on that basis points the user at the wrong cause.
    """
    if baseline_runs <= 0 or control_runs <= 0:
        return 1.0
    rate = baseline_failed / baseline_runs
    return (1.0 - rate) ** control_runs


def runs_needed_to_stabilize(baseline_failed: int, baseline_runs: int, alpha: float = 0.05) -> int:
    """How many clean runs it takes to call a stabilization significant."""
    if baseline_runs <= 0 or baseline_failed <= 0:
        return 0
    rate = baseline_failed / baseline_runs
    if rate >= 1.0:
        return 1
    from math import ceil, log

    return int(ceil(log(alpha) / log(1.0 - rate)))


def elevation_pvalue(
    failed: int, runs: int, baseline_failed: int, baseline_runs: int, alpha: float = 0.05
) -> float:
    """P(observing >= `failed` failures in `runs`) under the baseline rate.

    Small values mean the perturbation genuinely raised the failure rate. A
    plain "it failed more this time" comparison is not enough: an ordinary
    coin-flip flake produces streaks that look like elevation, and claiming a
    cause for them is the worst failure mode this tool has.

    With a clean baseline (0 failures) the point estimate 0.0 would make any
    single failure look decisive, so the exact upper confidence bound on the
    baseline rate is used instead.
    """
    if runs <= 0 or baseline_runs <= 0:
        return 1.0
    if baseline_failed == 0:
        # Upper 1-alpha bound for 0/n successes: 1 - alpha**(1/n).
        p0 = 1.0 - alpha ** (1.0 / baseline_runs)
    else:
        p0 = baseline_failed / baseline_runs
    return binom_tail_ge(failed, runs, min(max(p0, 0.0), 1.0))
