"""Run-count arithmetic and honest confidence bounds.

flakedoctor never claims a repro is "100% deterministic"; it prints the
observed count and an exact one-sided lower confidence bound on the true
failure rate (Clopper-Pearson). For an n/n result the bound reduces to
alpha**(1/n) — e.g. 10/10 at 95% confidence means the true repro rate is
at least ~0.74.

Comparisons between two small samples (does controlling an axis *lower* the
failure rate? does provoking it *raise* the rate?) use a one-sided Fisher
exact test, NOT the observed baseline rate as if it were the known truth.
The baseline is itself a small sample: treating 5/10 as exactly p=0.5 makes
0/5 clean control runs look significant (0.5**5 = 0.03), when the honest
two-sample test gives ~0.084 — not significant. Fisher conditions on the
margins and needs no dependencies.
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


def fisher_exact_one_sided(k1: int, n1: int, k2: int, n2: int, alternative: str) -> float:
    """One-sided Fisher exact test on the 2x2 table

        group 1:  k1 failures,  n1 - k1 passes    (the baseline sample)
        group 2:  k2 failures,  n2 - k2 passes    (the perturbed sample)

    Conditioning on the margins, the number of group-2 failures follows a
    hypergeometric distribution. Returns:

    * ``alternative="greater"`` — P(group 2 fails at least as often as observed):
      evidence that the perturbation *raised* the failure rate (elevation).
    * ``alternative="less"`` — P(group 2 fails at most as often as observed):
      evidence that it *lowered* the rate (stabilization).

    Small p means the two samples differ in the tested direction by more than
    sampling noise in either small sample explains.
    """
    if n1 <= 0 or n2 <= 0:
        return 1.0
    total_fail = k1 + k2
    total = n1 + n2
    # P(group-2 failures == x), x over its hypergeometric support.
    lo = max(0, total_fail - n1)
    hi = min(total_fail, n2)

    def pmf(x: int) -> float:
        return comb(n2, x) * comb(n1, total_fail - x) / comb(total, total_fail)

    if alternative == "greater":
        xs = range(k2, hi + 1)
    elif alternative == "less":
        xs = range(lo, k2 + 1)
    else:  # pragma: no cover - guarded by callers
        raise ValueError("alternative must be 'greater' or 'less'")
    return min(1.0, sum(pmf(x) for x in xs))


def runs_needed_to_stabilize(baseline_failed: int, baseline_runs: int, alpha: float = 0.05) -> int:
    """Minimum clean control runs to call a stabilization significant.

    The smallest m for which an all-passing control sample (0/m) is
    significantly below the baseline rate under a one-sided Fisher exact test.
    Because Fisher accounts for the baseline's own small-sample uncertainty,
    this is larger than the old ``log(alpha)/log(1-rate)`` estimate — e.g. a
    5/10 baseline needs 7 clean runs, not 5, before "controlling this axis
    stabilized the test" is more than a lead. Capped so a near-clean baseline
    (which realistically can't be shown stabilized) returns a large sentinel
    rather than looping forever.
    """
    if baseline_runs <= 0 or baseline_failed <= 0:
        return 0
    cap = 1000
    for m in range(1, cap + 1):
        if fisher_exact_one_sided(baseline_failed, baseline_runs, 0, m, "less") < alpha:
            return m
    return cap


def elevation_pvalue(
    failed: int, runs: int, baseline_failed: int, baseline_runs: int, alpha: float = 0.05
) -> float:
    """One-sided Fisher exact p-value that the perturbation raised the rate.

    Compares the perturbed sample (`failed`/`runs`) against the baseline
    sample (`baseline_failed`/`baseline_runs`) as two independent samples,
    rather than testing the perturbed count against the baseline point estimate
    as if it were the true rate. A plain "it failed more this time" comparison
    is not enough — an ordinary coin-flip flake produces streaks that look like
    elevation, and claiming a cause for them is the worst failure mode this
    tool has. `alpha` is unused (the caller thresholds the returned p-value);
    it is kept for signature symmetry with the other bounds.
    """
    if runs <= 0 or baseline_runs <= 0:
        return 1.0
    return fisher_exact_one_sided(baseline_failed, baseline_runs, failed, runs, "greater")
