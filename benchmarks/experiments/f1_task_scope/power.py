"""F1 §6.5 power arithmetic — the E0 revision-8 convention, verbatim.

This module exists so the committed TEST can prove the runner wave's
arithmetic reproduces the E0 revision-8 anchor quartet EXACTLY (F1
§6.5: "the runner MUST reproduce the E0 revision-8 anchor quartet
0.3544 / 0.6800 / 0.5477 / 0.8794 exactly (same arithmetic) — a
pre-run requirement"). It is ANALYSIS-SUPPORT code: nothing here is
ever computed at run time, written into run artifacts, or consulted
before/during a recorded run (§4.3 statistics ban; §6.6 single-look).

The arithmetic (E0 §8 revision 8 item 1, unconditional convention):

* the discordant-pair count ``b`` is random, ``b ~ Binomial(n, D)``;
* given ``b``, the one-sided win count ``x ~ Binomial(b, p)`` with
  ``p = (1 + delta / D) / 2`` (``delta`` = the paired rate difference;
  ``D`` = the expected discordance share);
* rejection = the exact two-sided sign-test p-value on the observed
  ``(x, b - x)`` split at alpha = 0.05 — the same ``2 * min-tail``
  binomial the S1 stand's McNemar jig uses
  (``benchmarks/stands/s1_quality/scenarios.py::_binom_two_sided_p``);
* power = sum over ``b`` of ``P(b) * P(reject | b)`` — unconditional
  (discordance marginalized), not conditional on ``b = n/2``.
"""

from __future__ import annotations

from math import comb

ALPHA = 0.05

#: The E0 §8 revision-8 item-1 anchor quartet (unconditional exact
#: two-sided, D = 0.5): (n, delta_pp, power). The committed test pins
#: the F1 arithmetic to these EXACT values.
E0_REVISION8_ANCHORS: tuple[tuple[int, float, float], ...] = (
    (40, 0.20, 0.3544),
    (80, 0.20, 0.6800),
    (40, 0.25, 0.5477),
    (80, 0.25, 0.8794),
)


def binom_two_sided_p(b: int, c: int) -> float:
    """Exact two-sided sign-test p-value on discordant pairs (no RNG)."""
    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    tail = sum(comb(n, k) for k in range(0, m + 1)) / (2**n)
    return min(1.0, 2 * tail)


def _reject_prob_given_b(b: int, p: float, alpha: float = ALPHA) -> float:
    """P(exact two-sided sign test rejects | b discordant, win prob p)."""
    if b == 0:
        return 0.0
    reject = sum(
        comb(b, x) * p**x * (1 - p) ** (b - x)
        for x in range(0, b + 1)
        if binom_two_sided_p(x, b - x) < alpha
    )
    return reject


def unconditional_power(n: int, delta: float, discordance: float, alpha: float = ALPHA) -> float:
    """Unconditional exact two-sided power (the E0 revision-8 convention).

    ``delta`` — the paired rate difference (e.g. 0.15 for +15 pp);
    ``discordance`` — D, the expected share of discordant pairs.
    """
    if not 0.0 < discordance <= 1.0:
        raise ValueError("discordance D must be in (0, 1]")
    if not 0.0 < delta <= discordance:
        raise ValueError(f"delta ({delta}) must lie in (0, D] ({discordance})")
    p = (1 + delta / discordance) / 2
    return sum(
        comb(n, b)
        * discordance**b
        * (1 - discordance) ** (n - b)
        * _reject_prob_given_b(b, p, alpha)
        for b in range(0, n + 1)
    )
