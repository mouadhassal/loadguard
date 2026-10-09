"""Mutation testing of the safety suite: every check must FAIL on a controller with a known flaw.

If a mutant survives (its check passes), the test suite has a blind spot. Each kill must also be
for the right reason: the failure message has to name the property the mutant breaks.
"""
import pytest

from checks import (check_arrivals_never_overload, check_billing_matches_meter,
                    check_cap_respected_in_steady_state, check_csms_outage_is_safe,
                    check_full_car_releases_capacity)
from loadguard.csms import Config

MUTANTS = [
    # (id, what is broken, the check that must catch it, words the failure must contain)
    ("M1-static-default", "TxDefaultProfile left at charger max (factory default)",
     lambda db: check_csms_outage_is_safe(Config(cap_kw=16.0, default_mode="static_max"), db), "outage"),
    ("M2-increase-first", "limit increases sent before decreases",
     lambda db: check_arrivals_never_overload(Config(cap_kw=16.0, update_order="increase_first")), "overloaded"),
    ("M3-no-dedupe", "billing sums per-event energy and ignores seqNo (double counts retransmissions)",
     lambda db: check_billing_matches_meter(Config(cap_kw=16.0, dedupe=False), duplicate_events=True), "billed"),
    ("M4-ignores-cap", "allocator ignores the panel cap",
     lambda db: check_cap_respected_in_steady_state(Config(cap_kw=16.0, policy="uncontrolled")), "steady state"),
    ("M5-strands-capacity", "ignores SuspendedEV, so a full car keeps its share (the first CSMS version)",
     lambda db: check_full_car_releases_capacity(Config(cap_kw=12.0, work_conserving=False)), "stranded"),
]


@pytest.mark.parametrize("mid,flaw,run,reason", MUTANTS, ids=[m[0] for m in MUTANTS])
async def test_mutant_is_killed(mid, flaw, run, reason, tmp_db):
    with pytest.raises(AssertionError) as err:
        await run(tmp_db)
    assert reason in str(err.value), f"{mid} died for the wrong reason: {err.value}"
    print(f"\n{mid}: {flaw}\n  killed: {str(err.value).splitlines()[0]}")
