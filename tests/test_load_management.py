"""Load-management behaviour: fairness, work conservation, demand response, deadline awareness."""
import asyncio

import pytest

from checks import check_full_car_releases_capacity
from conftest import lab, settle
from loadguard.csms import Config
from loadguard.sim import EV


async def test_equal_share_when_oversubscribed():
    async with lab(Config(cap_kw=12.0), n=4) as (csms, fleet):
        await asyncio.gather(*(s.plug_in(EV(need_kwh=50)) for s in fleet.stations))
        await settle(0.6)
        powers = sorted(round(s.power_kw, 3) for s in fleet.stations)
        assert powers == [3.0, 3.0, 3.0, 3.0], powers


async def test_full_power_when_capacity_allows():
    async with lab(Config(cap_kw=40.0), n=3) as (csms, fleet):
        await asyncio.gather(*(s.plug_in(EV(need_kwh=50)) for s in fleet.stations))
        await settle(0.6)
        assert all(abs(s.power_kw - 7.2) < 1e-6 for s in fleet.stations), [s.power_kw for s in fleet.stations]


async def test_departing_car_frees_capacity_for_the_others():
    async with lab(Config(cap_kw=12.0), n=4) as (csms, fleet):
        await asyncio.gather(*(s.plug_in(EV(need_kwh=50)) for s in fleet.stations))
        await settle(0.5)
        await fleet.stations[0].plug_out()
        await settle(0.5)
        rest = [round(s.power_kw, 3) for s in fleet.stations[1:]]
        assert rest == [4.0, 4.0, 4.0], rest


async def test_full_battery_frees_capacity_for_the_others():
    """A car that is full (SuspendedEV) must not keep its share: work conservation.
    This test failed on the first CSMS version (4 of 12 kW stranded until the full car left)."""
    await check_full_car_releases_capacity(Config(cap_kw=12.0))


async def test_demand_response_event_lowers_the_site_within_a_second():
    """An OpenADR-style curtailment lowers the cap; the physical load must follow quickly."""
    async with lab(Config(cap_kw=20.0), n=4, panel_kw=20.0) as (csms, fleet):
        await asyncio.gather(*(s.plug_in(EV(need_kwh=50)) for s in fleet.stations))
        await settle(0.5)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await csms.set_cap(8.0)
        elapsed = loop.time() - t0
        assert fleet.site_kw <= 8.0 + 1e-6, fleet.site_kw
        assert elapsed < 1.0, f"curtailment took {elapsed:.2f} s"


async def test_earliest_departure_gets_priority():
    async with lab(Config(cap_kw=8.0, policy="edf"), n=3) as (csms, fleet):
        a, b, c = fleet.stations
        await a.plug_in(EV(need_kwh=50, stay_h=10))
        await b.plug_in(EV(need_kwh=50, stay_h=2))         # leaves first
        await c.plug_in(EV(need_kwh=50, stay_h=6))
        await settle(0.5)
        assert b.power_kw == pytest.approx(7.2)
        assert c.power_kw == pytest.approx(0.8)
        assert a.power_kw == pytest.approx(0.0)
