"""Safety and correctness checks, written once and run against both the safe CSMS and its mutants.

Each check raises AssertionError when the property fails. test_safety.py runs them on the safe
configuration (they must pass); test_mutants.py runs them on deliberately broken
configurations (they must fail). A check that cannot fail on a known-bad controller is not a test.
"""
import asyncio

from conftest import lab, settle
from loadguard.csms import CSMS, Config
from loadguard.sim import EV


async def check_arrivals_never_overload(cfg: Config):
    """Cars arrive one by one; the physical panel must never exceed its rating, even for a millisecond."""
    async with lab(cfg, n=6, panel_kw=cfg.cap_kw) as (csms, fleet):
        for st in fleet.stations:
            await st.plug_in(EV(need_kwh=40))
            await asyncio.sleep(0.15)
        await settle(0.5)
        assert not fleet.violations, f"panel overloaded {len(fleet.violations)}x, peak {fleet.peak_kw:.2f} kW > {cfg.cap_kw}"


async def check_csms_outage_is_safe(cfg: Config, db_path):
    """While the CSMS is down, stations keep their last limits and new cars plug in. The panel must hold."""
    cfg.db_path = db_path
    async with lab(cfg, n=6, panel_kw=cfg.cap_kw) as (csms, fleet):
        for st in fleet.stations[:3]:
            await st.plug_in(EV(need_kwh=40))
            await asyncio.sleep(0.15)
        await settle(0.4)
        await csms.stop()                                   # CSMS crashes
        for st in fleet.stations[3:]:
            await st.plug_in(EV(need_kwh=40))               # residents keep arriving
            await asyncio.sleep(0.1)
        await settle(0.4)
        assert not fleet.violations, f"overload during CSMS outage: peak {fleet.peak_kw:.2f} kW > {cfg.cap_kw}"


async def check_billing_matches_meter(cfg: Config, duplicate_events=False, offline_s=0.0):
    """Billed kWh per session equals the energy the station actually delivered (within 0.5%)."""
    async with lab(cfg, n=3, duplicate_events=duplicate_events) as (csms, fleet):
        for st in fleet.stations:
            await st.plug_in(EV(need_kwh=6))
        if offline_s:
            await fleet.stations[0].go_offline()
            await asyncio.sleep(offline_s)
            await fleet.stations[0].go_online()
        await asyncio.sleep(1.2)
        for st in fleet.stations:
            await st.plug_out()
        await settle(0.6)
        assert len(fleet.finished) == 3
        for f in fleet.finished:
            billed = csms.billed_kwh.get(f["id"])
            assert billed is not None, f"session {f['id'][:8]} was never billed"
            assert abs(billed - f["delivered"]) <= 0.005 * max(f["delivered"], 1e-6) + 1e-6, \
                f"billed {billed:.4f} kWh but delivered {f['delivered']:.4f} kWh"


async def check_full_car_releases_capacity(cfg: Config):
    """Work conservation: once a car is full, the cars still charging get its share."""
    async with lab(cfg, n=3) as (csms, fleet):
        small, big1, big2 = fleet.stations
        await small.plug_in(EV(need_kwh=0.5))
        await big1.plug_in(EV(need_kwh=50))
        await big2.plug_in(EV(need_kwh=50))
        await asyncio.sleep(2.0)
        assert small.power_kw == 0.0
        expect = min(cfg.cap_kw / 2, 7.2)
        assert abs(big1.power_kw - expect) < 1e-6 and abs(big2.power_kw - expect) < 1e-6, \
            f"stranded capacity: cars at {big1.power_kw:.2f}/{big2.power_kw:.2f} kW, site {fleet.site_kw:.2f} of {cfg.cap_kw} kW"


async def check_cap_respected_in_steady_state(cfg: Config):
    """With more demand than capacity, the steady-state site load stays at or below the cap."""
    async with lab(cfg, n=5, panel_kw=cfg.cap_kw) as (csms, fleet):
        await asyncio.gather(*(st.plug_in(EV(need_kwh=50)) for st in fleet.stations))
        await settle(0.8)
        assert fleet.site_kw <= cfg.cap_kw + 1e-6, f"steady state {fleet.site_kw:.2f} kW > cap {cfg.cap_kw}"
