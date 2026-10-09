"""Property-based tests of the allocation policies (Hypothesis generates thousands of sites)."""
from hypothesis import given, settings, strategies as st

from loadguard.alloc import Demand, edf, equal_share

demand = st.builds(Demand, id=st.uuids().map(str), max_kw=st.floats(0, 22, allow_nan=False),
                   need_kwh=st.one_of(st.none(), st.floats(0, 100, allow_nan=False)),
                   departure=st.one_of(st.none(), st.floats(0, 1e6, allow_nan=False)))
sites = st.tuples(st.lists(demand, max_size=40, unique_by=lambda d: d.id), st.floats(0, 300, allow_nan=False))
TOL = 1e-6


def wants(d):
    return d.max_kw > 1e-9 and (d.need_kwh is None or d.need_kwh > 1e-9)


@settings(max_examples=2000)
@given(sites)
def test_never_exceeds_cap_or_charger_limit(site):
    ds, cap = site
    for policy in (equal_share, edf):
        a = policy(ds, cap)
        assert sum(a.values()) <= cap + TOL
        assert all(-TOL <= a[d.id] <= d.max_kw + TOL for d in ds)
        assert all(a[d.id] == 0 for d in ds if not wants(d))


@settings(max_examples=2000)
@given(sites)
def test_work_conserving(site):
    ds, cap = site
    full = min(cap, sum(d.max_kw for d in ds if wants(d)))
    for policy in (equal_share, edf):
        assert abs(sum(policy(ds, cap).values()) - full) <= 1e-6 * max(1, full)


@settings(max_examples=2000)
@given(sites)
def test_equal_share_is_max_min_fair(site):
    ds, cap = site
    a = equal_share(ds, cap)
    act = [d for d in ds if wants(d)]
    level = max((a[d.id] for d in act if a[d.id] < d.max_kw - TOL), default=None)
    if level is not None:   # every car not at its own max sits at the same (highest) level
        assert all(a[d.id] >= level - 1e-6 for d in act if a[d.id] < d.max_kw - TOL)
        assert all(a[d.id] <= level + 1e-6 for d in act)


@settings(max_examples=2000)
@given(sites)
def test_edf_serves_earlier_departures_first(site):
    ds, cap = site
    a = edf(ds, cap)
    act = sorted([d for d in ds if wants(d) and d.departure is not None], key=lambda d: (d.departure, d.id))
    for i, d in enumerate(act):
        if a[d.id] > TOL:
            assert all(a[e.id] >= e.max_kw - 1e-6 for e in act[:i]), "a later departure was served first"
