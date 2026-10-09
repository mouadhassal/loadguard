"""Resilience: CSMS restarts, network dropouts, and what keeps working while the cloud is down."""
import asyncio

from conftest import settle
from loadguard.csms import CSMS, Config
from loadguard.sim import EV, Fleet


async def test_csms_restart_resumes_sessions_and_bills_correctly(tmp_db):
    csms = await CSMS(Config(cap_kw=16.0, db_path=tmp_db)).start(ws_port=0)
    port = csms.ws_port
    fleet = await Fleet(f"ws://127.0.0.1:{port}", 3, panel_kw=16.0).start()
    try:
        for st in fleet.stations:
            await st.plug_in(EV(need_kwh=30))
        await settle(0.6)
        await csms.stop()                                   # crash
        await asyncio.sleep(0.5)                            # stations keep charging at their last limits
        assert all(st.power_kw > 0 for st in fleet.stations), "charging stopped during the outage"
        csms = await CSMS(Config(cap_kw=16.0, db_path=tmp_db)).start(ws_port=port)   # same DB, same URL
        await settle(1.0)
        assert len(csms.active_txs()) == 3, "active transactions were lost across the restart"
        assert all(st.connected for st in fleet.stations), "stations did not reconnect"
        for st in fleet.stations:
            await st.plug_out()
        await settle(0.6)
        for f in fleet.finished:
            assert abs(csms.billed_kwh[f["id"]] - f["delivered"]) < 0.005 * f["delivered"] + 1e-6
        assert not fleet.violations
    finally:
        await fleet.close()
        await csms.stop()


async def test_network_dropout_replays_queued_events_in_order():
    csms = await CSMS(Config(cap_kw=16.0)).start(ws_port=0)
    fleet = await Fleet(f"ws://127.0.0.1:{csms.ws_port}", 2, panel_kw=16.0).start()
    try:
        st = fleet.stations[0]
        await st.plug_in(EV(need_kwh=30))
        await settle(0.3)
        await st.go_offline()
        await asyncio.sleep(0.8)                            # ~4 meter intervals queued
        assert len(st.outbox) >= 3
        await st.go_online()
        await settle(0.5)
        assert not st.outbox
        (t,) = [t for t in csms.txs.values() if t.station == st.id]
        assert t.offline_events >= 3
        assert t.seqs == set(range(len(t.seqs))), "sequence gap after replay"
        await st.plug_out()
        await settle(0.3)
        f = fleet.finished[0]
        assert abs(csms.billed_kwh[f["id"]] - f["delivered"]) < 0.005 * f["delivered"] + 1e-6
    finally:
        await fleet.close()
        await csms.stop()
