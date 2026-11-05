"""The arithmetic the §10.2 load numbers are made of, kept away from the socket harness.

`scripts.load_test` drives real clients and collects raw millisecond samples; this module decides
what a sample *means*. It is separate because that decision is where a performance report can be
wrong without anything looking broken: an interpolated P95, a drift taken as an average, an
"everyone agrees" that compares counts. Each one is hand-checkable here, so a reviewer can recompute
any published number from the sorted samples.

Nearest rank is the percentile definition used throughout — the value at index
`ceil(p/100 * n) - 1` of the sorted samples. It never reports a number that was not measured, which
matters at the sample sizes a local harness reaches: with ten clients, an interpolated P95 prints a
latency nobody's socket ever produced.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Stat:
    """One row of the §10.2 table: the number, the target it is graded against, the verdict."""

    label: str
    p95: float | None
    target: float
    count: int
    meets_target: bool


def percentile(samples: Sequence[float], p: int) -> float | None:
    """Nearest-rank quantile. `None` for no samples — an unmeasured thing has no latency."""
    if not samples:
        return None
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), math.ceil(p / 100 * len(ordered))))
    return ordered[rank - 1]


def drift_ms(arrivals: Sequence[float]) -> float:
    """§10.2's "drift *between* clients": the spread across one frame's recipients.

    A room where forty-eight clients land within 2 ms and two land 90 ms later has an
    unremarkable mean and an 80 ms drift, and the drift is the number the PRD caps.
    """
    if len(arrivals) < 2:
        return 0.0
    return max(arrivals) - min(arrivals)


def fanout_ms(emit_ts: float, arrivals: Sequence[float]) -> float | None:
    """§10.2's "server receipt to *last* client delivery": the slowest recipient decides it."""
    if not arrivals:
        return None
    return max(arrivals) - emit_ts


def agreement(states: Sequence[tuple]) -> list[int]:
    """Indices of clients whose final state differs from the first client's.

    §6.1's criterion is that no client and the server disagree on the final order after a vote race,
    so the harness needs a dissent list it can print, not a boolean that says only "someone did".
    The reference is the first state rather than the majority one: with a real room the first client
    is the voter whose action produced the order, so a dissent list against it names the clients
    that were left behind rather than renaming the winner.
    """
    if not states:
        return []
    reference = states[0]
    return [i for i, state in enumerate(states) if state != reference]


def summarize(label: str, samples: Sequence[float], target_ms: float) -> Stat:
    """Grade a batch of samples against one §10.2 target.

    No samples is a miss, never a pass: a harness that failed to measure would otherwise report a
    clean sheet against every latency budget in the PRD.
    """
    p95 = percentile(samples, 95)
    return Stat(
        label=label,
        p95=p95,
        target=target_ms,
        count=len(samples),
        meets_target=p95 is not None and p95 <= target_ms,
    )
