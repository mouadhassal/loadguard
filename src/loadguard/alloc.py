"""Power allocation under a shared panel cap. One module, used by both the CSMS and the study.

Every policy returns {session_id: kW} with 0 <= kW <= max_kw and sum(kW) <= cap_kw.

- uncontrolled: every car gets its charger's max (only safe if the panel is sized for it)
- equal_share:  max-min fair water-filling. No information about departure time is needed.
                This is how building-level dynamic load management usually works.
- edf:          earliest-departure-first. Needs each driver's departure time (app or ISO 15118).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

EPS = 1e-9


@dataclass(frozen=True)
class Demand:
    id: str
    max_kw: float                       # charger/EV limit
    need_kwh: Optional[float] = None    # remaining energy; None = unknown (charge until told otherwise)
    departure: Optional[float] = None   # time the car leaves (any monotone clock); None = unknown


def _active(demands):
    return [d for d in demands if d.max_kw > EPS and (d.need_kwh is None or d.need_kwh > EPS)]


def uncontrolled(demands: list[Demand], cap_kw: float | None = None) -> dict[str, float]:
    out = {d.id: 0.0 for d in demands}
    for d in _active(demands):
        out[d.id] = d.max_kw
    return out


def equal_share(demands: list[Demand], cap_kw: float) -> dict[str, float]:
    """Water-filling: raise everyone's level together; cars that hit their max drop out."""
    out = {d.id: 0.0 for d in demands}
    act = sorted(_active(demands), key=lambda d: d.max_kw)
    left = max(cap_kw, 0.0)
    n = len(act)
    for i, d in enumerate(act):
        share = left / (n - i)
        give = min(d.max_kw, share)
        out[d.id] = give
        left -= give
    return out


def edf(demands: list[Demand], cap_kw: float) -> dict[str, float]:
    """Earliest departure first gets full power; unknown departures go last (in id order)."""
    out = {d.id: 0.0 for d in demands}
    key = lambda d: (d.departure is None, d.departure if d.departure is not None else 0.0, d.id)
    left = max(cap_kw, 0.0)
    for d in sorted(_active(demands), key=key):
        give = min(d.max_kw, left)
        out[d.id] = give
        left -= give
        if left <= EPS:
            break
    return out


POLICIES = {"uncontrolled": uncontrolled, "equal_share": equal_share, "edf": edf}


def allocate(policy: str, demands: list[Demand], cap_kw: float) -> dict[str, float]:
    return POLICIES[policy](demands, cap_kw)
