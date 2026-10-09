"""Safety: the panel never overloads and billing is exact, on the safe CSMS configuration."""
from checks import (check_arrivals_never_overload, check_billing_matches_meter,
                    check_cap_respected_in_steady_state, check_csms_outage_is_safe)
from loadguard.csms import Config


async def test_arrivals_never_overload():
    await check_arrivals_never_overload(Config(cap_kw=16.0))


async def test_cap_respected_in_steady_state():
    await check_cap_respected_in_steady_state(Config(cap_kw=16.0))


async def test_csms_outage_is_safe(tmp_db):
    await check_csms_outage_is_safe(Config(cap_kw=16.0), tmp_db)


async def test_billing_matches_meter():
    await check_billing_matches_meter(Config(cap_kw=16.0))


async def test_billing_survives_retransmitted_events():
    await check_billing_matches_meter(Config(cap_kw=16.0), duplicate_events=True)


async def test_billing_survives_a_network_dropout():
    await check_billing_matches_meter(Config(cap_kw=16.0), offline_s=0.6)
