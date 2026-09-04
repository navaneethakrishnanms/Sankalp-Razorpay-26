"""
Statistics helpers shared by eval/harness.py and eval/baselines.py.

Wilson score interval, not the normal approximation — it stays sane near
0% and 100%, which is exactly where several Stage 3 results are expected
to sit (uncatchable-by-construction subpopulations near 0%, block-everything
at exactly 100%). See eval/PRE_REGISTERED.md's "Statistical reporting" section.
"""

from __future__ import annotations

import dataclasses
import math


@dataclasses.dataclass(frozen=True)
class RateEstimate:
    successes: int
    n:          int
    rate:        float
    ci_low:       float
    ci_high:       float

    def as_dict(self) -> dict:
        return {
            "successes": self.successes, "n": self.n,
            "rate": round(self.rate, 4),
            "ci_low": round(self.ci_low, 4), "ci_high": round(self.ci_high, 4),
        }

    def __str__(self) -> str:
        return f"{self.rate:.1%} [{self.ci_low:.1%}, {self.ci_high:.1%}] (n={self.n})"


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval by default (z=1.96)."""
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def rate(successes: int, n: int) -> RateEstimate:
    if n == 0:
        return RateEstimate(0, 0, 0.0, 0.0, 0.0)
    lo, hi = wilson_ci(successes, n)
    return RateEstimate(successes, n, successes / n, lo, hi)
