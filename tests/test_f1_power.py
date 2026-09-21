"""F1 §6.5 power arithmetic — the E0 revision-8 anchor reproduction.

F1 §6.5 (frozen): "the runner MUST reproduce the E0 revision-8 anchor
quartet 0.3544 / 0.6800 / 0.5477 / 0.8794 exactly (same arithmetic) — a
pre-run requirement". This test is that requirement: the committed
``benchmarks/experiments/f1_task_scope/power.py`` module must yield the
quartet to all four decimals, plus the F1 §6.5 table itself (n = 192)
and the X-gold / G3b figures the registration quotes.

Nothing here is ever computed at run time (§4.3 statistics ban) — this
is analysis-support arithmetic proven BEFORE the first recorded run.
"""

from __future__ import annotations

from math import comb

from benchmarks.experiments.f1_task_scope.power import (
    ALPHA,
    E0_REVISION8_ANCHORS,
    binom_two_sided_p,
    unconditional_power,
)


def test_e0_revision8_anchor_quartet_exact() -> None:
    """The four unconditional anchors, to all four decimals (E0 §8 rev 8)."""
    for n, delta, expected in E0_REVISION8_ANCHORS:
        got = round(unconditional_power(n, delta, discordance=0.5), 4)
        assert got == expected, f"n={n}, delta={delta}: {got} != {expected}"


def test_f1_power_table_n192() -> None:
    """The F1 §6.5 table at n = 192 (unconditional, two-sided, alpha = 0.05)."""
    table = {
        (0.10, 0.3): 0.684,
        (0.10, 0.5): 0.463,
        (0.10, 0.7): 0.351,
        (0.15, 0.3): 0.969,
        (0.15, 0.5): 0.820,
        (0.15, 0.7): 0.676,
        (0.20, 0.3): 1.000,
        (0.20, 0.5): 0.975,
        (0.20, 0.7): 0.906,
    }
    for (delta, discordance), expected in table.items():
        got = round(unconditional_power(192, delta, discordance), 3)
        assert got == expected, f"delta={delta}, D={discordance}: {got} != {expected}"


def test_x_gold_and_g3b_figures() -> None:
    """X-gold (n=48, D=0.4) corridor-grade figures + the G3b sign rule p."""
    assert round(unconditional_power(48, 0.15, discordance=0.4), 3) == 0.299
    assert round(unconditional_power(48, 0.20, discordance=0.4), 3) == 0.528
    # G3b (§2.6b): one-sided P(X >= 6 | n=8, p=0.5) = 37/256 = 0.145
    tail = sum(comb(8, k) for k in range(6, 9)) / 2**8
    assert round(tail, 3) == 0.145


def test_binom_two_sided_p_baseline() -> None:
    """The sign-test p-value convention (the S1 jig arithmetic)."""
    assert binom_two_sided_p(0, 0) == 1.0
    assert binom_two_sided_p(8, 0) == 2 * (1 / 256)  # min(1, 2*tail)
    assert binom_two_sided_p(4, 4) == 1.0  # symmetric → min(1, 2*0.5)
    assert ALPHA == 0.05


def test_power_input_contracts() -> None:
    """Delta beyond D, or D outside (0,1], fails loud (no silent math)."""
    for bad in ((0.20, 0.1), (0.15, 0.0), (0.15, 1.5)):
        delta, discordance = bad
        try:
            unconditional_power(40, delta, discordance)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for delta={delta}, D={discordance}")
