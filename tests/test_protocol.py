"""OCPP 2.0.1 protocol behaviour of the CSMS (use-case IDs from the OCPP 2.0.1 specification)."""
import asyncio
import json

import aiohttp
import websockets
from ocpp.v201 import call, datatypes, enums

from conftest import lab, settle
from loadguard.csms import Config
from loadguard.sim import EV


async def test_B01_boot_is_accepted_with_heartbeat_interval():
    async with lab(Config(heartbeat_s=17), n=2) as (csms, fleet):
        res = await fleet.stations[0].cp.call(call.BootNotification(
            charging_station=datatypes.ChargingStationType(model="X", vendor_name="Y"),
            reason=enums.BootReasonEnumType.power_up))
        assert res.status == "Accepted" and res.interval == 17
        snap = csms.snapshot()
        assert [s["id"] for s in snap["stations"]] == ["CS000", "CS001"]
        assert all(s["connected"] and s["status"] == "Available" for s in snap["stations"])


async def test_C01_blocked_token_cannot_start_charging():
    async with lab(Config(), n=1) as (csms, fleet):
        started = await fleet.stations[0].plug_in(EV(token="BLOCKED-42"))
        await settle(0.2)
        assert started is False
        assert fleet.stations[0].power_kw == 0.0 and not csms.active_txs()


async def test_E_transaction_events_have_contiguous_seq_numbers():
    async with lab(Config(), n=1) as (csms, fleet):
        st = fleet.stations[0]
        await st.plug_in(EV(need_kwh=5))
        await asyncio.sleep(0.8)
        await st.plug_out()
        await settle(0.3)
        (t,) = csms.txs.values()
        assert t.seqs == set(range(len(t.seqs))) and len(t.seqs) >= 4
        assert not t.active


async def test_K01_tx_profile_for_another_transaction_is_rejected():
    async with lab(Config(), n=1) as (csms, fleet):
        st = fleet.stations[0]
        await st.plug_in(EV(need_kwh=20))
        await settle(0.3)
        before = st.power_kw
        res = await csms.links["CS000"].call(call.SetChargingProfile(
            evse_id=1, charging_profile=csms._profile(enums.ChargingProfilePurposeEnumType.tx_profile, 0.5,
                                                      tx_id="not-this-transaction", pid=9)))
        assert res.status == "Rejected" and st.power_kw == before


async def test_malformed_messages_get_call_errors_and_do_not_crash_the_csms():
    async with lab(Config(), n=1) as (csms, fleet):
        async with websockets.connect(f"ws://127.0.0.1:{csms.ws_port}/ROGUE", subprotocols=["ocpp2.0.1"]) as ws:
            await ws.send(json.dumps([2, "m1", "TransactionEvent", {"eventType": "Started"}]))   # fields missing
            r1 = json.loads(await ws.recv())
            await ws.send(json.dumps([2, "m2", "FlyToTheMoon", {}]))                              # unknown action
            r2 = json.loads(await ws.recv())
        assert r1[0] == 4 and r1[1] == "m1" and r1[2] in ("FormationViolation", "OccurrenceConstraintViolation",
                                                          "TypeConstraintViolation", "ProtocolError")
        assert r2[0] == 4 and r2[2] in ("NotImplemented", "NotSupported")
        assert await fleet.stations[0].plug_in(EV(need_kwh=1))           # still serving real stations


async def test_F01_F03_remote_start_and_stop_over_the_rest_api():
    async with lab(Config(), n=2, http=True) as (csms, fleet):
        base = f"http://127.0.0.1:{csms.http_port}"
        async with aiohttp.ClientSession() as http:
            r = await (await http.post(f"{base}/api/stations/CS001/start")).json()
            assert r["status"] == "Accepted"
            await settle(0.5)
            assert fleet.by_id["CS001"].tx is not None and fleet.by_id["CS001"].power_kw > 0
            state = await (await http.get(f"{base}/api/state")).json()
            assert next(s for s in state["stations"] if s["id"] == "CS001")["status"] == "Occupied"
            r = await (await http.post(f"{base}/api/stations/CS001/stop")).json()
            assert r["status"] == "Accepted"
            await settle(0.5)
        assert fleet.by_id["CS001"].tx is None and len(csms.billed_kwh) == 1
