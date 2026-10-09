"""Charging-station simulator: OCPP 2.0.1 clients with physics, offline queues and fault injection.

Time runs `speed` times faster than the wall clock (speed=600 means 1 s = 10 simulated minutes).
Every change of a station's power goes through Fleet.on_change(), which checks the *physical*
site load against the panel rating. That makes a millisecond overload visible to the tests,
not just the averages the CSMS reports.
"""
from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import websockets
from ocpp.routing import on
from ocpp.v201 import ChargePoint, call, call_result, datatypes, enums

_ids = itertools.count(1)


def iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class EV:
    need_kwh: float = 20.0
    max_kw: float = 11.0
    stay_h: float | None = None        # plug-out after this many simulated hours (None = stay)
    token: str = "RESIDENT-1"


class _Client(ChargePoint):
    def __init__(self, st: "SimStation", connection):
        super().__init__(st.id, connection, response_timeout=10)
        self.st = st

    @on("SetChargingProfile")
    async def on_set_profile(self, evse_id, charging_profile, **kw):
        ok = self.st.apply_profile(charging_profile)
        s = enums.ChargingProfileStatusEnumType.accepted if ok else enums.ChargingProfileStatusEnumType.rejected
        return call_result.SetChargingProfile(status=s)

    @on("ClearChargingProfile")
    async def on_clear(self, **kw):
        self.st.default_kw = None
        self.st.tx_profile = None
        self.st.fleet.on_change()
        return call_result.ClearChargingProfile(status=enums.ClearChargingProfileStatusEnumType.accepted)

    @on("RequestStartTransaction")
    async def on_remote_start(self, id_token, remote_start_id, **kw):
        if self.st.tx is not None:
            return call_result.RequestStartTransaction(status=enums.RequestStartStopStatusEnumType.rejected)
        asyncio.create_task(self.st.plug_in(self.st.fleet.default_ev(id_token["id_token"]), remote=True))
        return call_result.RequestStartTransaction(status=enums.RequestStartStopStatusEnumType.accepted)

    @on("RequestStopTransaction")
    async def on_remote_stop(self, transaction_id, **kw):
        if self.st.tx is None or self.st.tx["id"] != transaction_id:
            return call_result.RequestStopTransaction(status=enums.RequestStartStopStatusEnumType.rejected)
        asyncio.create_task(self.st.plug_out(enums.ReasonEnumType.remote))
        return call_result.RequestStopTransaction(status=enums.RequestStartStopStatusEnumType.accepted)


class SimStation:
    def __init__(self, sid, fleet: "Fleet", evse_max_kw=7.2, meter_interval=0.2, duplicate_events=False):
        self.id = sid
        self.fleet = fleet
        self.evse_max_kw = evse_max_kw
        self.meter_interval = meter_interval
        self.duplicate_events = duplicate_events
        self.cp = None
        self.ws = None
        self.connected = False
        self.auto_reconnect = True
        self.default_kw: float | None = None      # TxDefaultProfile
        self.tx_profile: tuple | None = None       # (transaction_id, kW)
        self.tx: dict | None = None
        self.register_wh = 0.0
        self.power_kw = 0.0
        self.delivered_kwh_truth = 0.0             # ground truth for billing tests
        self.outbox: list = []
        self._last_sim = None
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ connection
    async def connect(self, boot=True):
        self.ws = await websockets.connect(f"{self.fleet.url}/{self.id}", subprotocols=["ocpp2.0.1"],
                                           open_timeout=5, ping_interval=None)
        self.cp = _Client(self, self.ws)
        self.connected = True
        self._tasks.append(asyncio.create_task(self._recv()))
        if boot:
            await self.cp.call(call.BootNotification(
                charging_station=datatypes.ChargingStationType(model="LG-Sim", vendor_name="loadguard"),
                reason=enums.BootReasonEnumType.power_up))
            await self.cp.call(call.StatusNotification(
                timestamp=iso(), connector_status=enums.ConnectorStatusEnumType.available, evse_id=1, connector_id=1))
        await self.flush()

    async def _recv(self):
        try:
            await self.cp.start()
        except Exception:
            pass
        self.connected = False
        if self.auto_reconnect and not self.fleet.closing:
            self._tasks.append(asyncio.create_task(self._reconnect_loop()))

    async def _reconnect_loop(self):
        while self.auto_reconnect and not self.connected and not self.fleet.closing:
            await asyncio.sleep(0.1)
            try:
                await self.connect(boot=False)            # a network drop is not a reboot
            except Exception:
                continue

    async def go_offline(self):
        """Simulate a lost network link (LTE dropout): the station keeps charging and queues events."""
        self.auto_reconnect = False
        self.connected = False
        if self.ws is not None:
            await self.ws.close()

    async def go_online(self):
        self.auto_reconnect = True
        await self.connect(boot=False)

    async def flush(self):
        while self.outbox and self.connected:
            msg = self.outbox[0]
            try:
                await self.cp.call(msg)
                self.outbox.pop(0)
            except Exception:
                self.connected = False
                return

    async def send_tx(self, msg_kwargs):
        """Transaction messages are never lost: sent if online, otherwise queued with offline=true."""
        if self.connected and not self.outbox:
            msg = call.TransactionEvent(**msg_kwargs)
            try:
                res = await self.cp.call(msg)
                if self.duplicate_events:                 # retransmission after a lost ack
                    await self.cp.call(call.TransactionEvent(**msg_kwargs))
                return res
            except Exception:
                self.connected = False
        self.outbox.append(call.TransactionEvent(**{**msg_kwargs, "offline": True}))
        return None

    # ------------------------------------------------------------ physics
    def sim_now(self):
        return self.fleet.sim_now()

    def limit_kw(self):
        if self.tx is not None and self.tx_profile and self.tx_profile[0] == self.tx["id"]:
            lim = self.tx_profile[1]
        elif self.default_kw is not None:
            lim = self.default_kw
        else:
            lim = self.evse_max_kw
        return min(lim, self.evse_max_kw)

    def advance(self):
        now = self.sim_now()
        if self._last_sim is not None and self.power_kw > 0 and self.tx is not None:
            dt_h = (now - self._last_sim) / 3600
            left = self.tx["ev"].need_kwh - self.tx["delivered"]
            e = min(self.power_kw * dt_h, max(left, 0.0))
            self.tx["delivered"] += e
            self.delivered_kwh_truth += e
            self.register_wh += e * 1000
        self._last_sim = now
        self._set_power()

    def _set_power(self):
        if self.tx is None:
            p = 0.0
        elif self.tx["delivered"] >= self.tx["ev"].need_kwh - 1e-9:
            p = 0.0
            if not self.tx.get("full"):                   # battery full: tell the CSMS (OCPP E10)
                self.tx["full"] = True
                msg = self._tx_msg(enums.TransactionEventEnumType.updated,
                                   enums.TriggerReasonEnumType.charging_state_changed)
                msg["transaction_info"] = datatypes.TransactionType(
                    transaction_id=self.tx["id"], charging_state=enums.ChargingStateEnumType.suspended_ev)
                self._tasks.append(asyncio.create_task(self.send_tx(msg)))
        else:
            p = min(self.limit_kw(), self.tx["ev"].max_kw)
        if abs(p - self.power_kw) > 1e-12:
            self.power_kw = p
            self.fleet.on_change(self)

    def apply_profile(self, prof) -> bool:
        self.advance()
        kw = prof["charging_schedule"][0]["charging_schedule_period"][0]["limit"] / 1000
        purpose = prof["charging_profile_purpose"]
        if purpose == "TxDefaultProfile":
            self.default_kw = kw
        elif purpose == "TxProfile":
            if self.tx is None or prof.get("transaction_id") != self.tx["id"]:
                return False
            self.tx_profile = (self.tx["id"], kw)
        else:
            return False
        self._set_power()
        return True

    # ------------------------------------------------------------ sessions
    def _meter(self, interval_wh=None):
        sv = [datatypes.SampledValueType(value=round(self.register_wh, 3),
                                         measurand=enums.MeasurandEnumType.energy_active_import_register),
              datatypes.SampledValueType(value=round(self.power_kw * 1000, 1),
                                         measurand=enums.MeasurandEnumType.power_active_import)]
        if interval_wh is not None:
            sv.append(datatypes.SampledValueType(value=round(interval_wh, 3),
                                                 measurand=enums.MeasurandEnumType.energy_active_import_interval))
        return [datatypes.MeterValueType(timestamp=iso(), sampled_value=sv)]

    def _tx_msg(self, event, trigger, **extra):
        t = self.tx
        seq = t["seq"]
        t["seq"] += 1
        interval = self.register_wh - t["reg_at_last"]
        t["reg_at_last"] = self.register_wh
        return dict(event_type=event, timestamp=iso(), trigger_reason=trigger, seq_no=seq,
                    transaction_info=datatypes.TransactionType(transaction_id=t["id"]),
                    meter_value=self._meter(interval), evse=datatypes.EVSEType(id=1, connector_id=1), **extra)

    async def plug_in(self, ev: EV, remote=False) -> bool:
        if self.tx is not None:
            return False
        if self.connected:
            try:
                res = await self.cp.call(call.Authorize(
                    id_token=datatypes.IdTokenType(id_token=ev.token, type=enums.IdTokenEnumType.iso14443)))
                if res.id_token_info["status"] != "Accepted":
                    return False
            except Exception:
                pass                                     # offline: local authorisation, CSMS rechecks later
        self.advance()
        self.tx = dict(id=str(uuid.uuid4()), ev=ev, delivered=0.0, seq=0, reg_at_last=self.register_wh,
                       start_sim=self.sim_now(), billed_from=self.register_wh)
        self.tx_profile = None
        custom = {"vendorId": "loadguard", "needKwh": ev.need_kwh}
        if ev.stay_h is not None:
            custom["departure"] = self.sim_now() + ev.stay_h * 3600
        self.advance()
        await self.send_tx(self._tx_msg(
            enums.TransactionEventEnumType.started,
            enums.TriggerReasonEnumType.remote_start if remote else enums.TriggerReasonEnumType.cable_plugged_in,
            id_token=datatypes.IdTokenType(id_token=ev.token, type=enums.IdTokenEnumType.iso14443),
            custom_data=custom))
        if self.connected:
            try:
                await self.cp.call(call.StatusNotification(timestamp=iso(), evse_id=1, connector_id=1,
                                                           connector_status=enums.ConnectorStatusEnumType.occupied))
            except Exception:
                pass
        tid = self.tx["id"]
        self._tasks.append(asyncio.create_task(self._meter_loop(tid)))
        if ev.stay_h is not None:
            self._tasks.append(asyncio.create_task(self._leave_at(tid, self.sim_now() + ev.stay_h * 3600)))
        return True

    async def _meter_loop(self, tid):
        while self.tx is not None and self.tx["id"] == tid:
            await asyncio.sleep(self.meter_interval)
            if self.tx is None or self.tx["id"] != tid:
                break
            self.advance()
            await self.send_tx(self._tx_msg(enums.TransactionEventEnumType.updated,
                                            enums.TriggerReasonEnumType.meter_value_periodic))

    async def _leave_at(self, tid, depart_sim):
        while self.tx is not None and self.tx["id"] == tid and self.sim_now() < depart_sim:
            await asyncio.sleep(0.02)
        if self.tx is not None and self.tx["id"] == tid:
            await self.plug_out(enums.ReasonEnumType.ev_disconnected)

    async def plug_out(self, reason=enums.ReasonEnumType.ev_disconnected):
        if self.tx is None:
            return
        self.advance()
        msg = self._tx_msg(enums.TransactionEventEnumType.ended, enums.TriggerReasonEnumType.ev_communication_lost)
        msg["transaction_info"] = datatypes.TransactionType(transaction_id=self.tx["id"], stopped_reason=reason)
        done = self.tx
        self.tx = None
        self.tx_profile = None
        self._set_power()
        self.fleet.finished.append(dict(station=self.id, id=done["id"], delivered=done["delivered"],
                                        need=done["ev"].need_kwh))
        await self.send_tx(msg)
        if self.connected:
            try:
                await self.cp.call(call.StatusNotification(timestamp=iso(), evse_id=1, connector_id=1,
                                                           connector_status=enums.ConnectorStatusEnumType.available))
            except Exception:
                pass

    async def close(self):
        self.auto_reconnect = False
        for t in self._tasks:
            t.cancel()
        if self.ws is not None:
            await self.ws.close()


class Fleet:
    """A building's worth of stations plus a physical meter on the panel."""

    def __init__(self, url, n, panel_kw, speed=600.0, prefix="CS", evse_max_kw=7.2, **station_kw):
        self.url = url
        self.panel_kw = panel_kw
        self.speed = speed
        self.closing = False
        self.wall0 = time.monotonic()
        self.stations = [SimStation(f"{prefix}{i:03d}", self, evse_max_kw, **station_kw) for i in range(n)]
        self.by_id = {s.id: s for s in self.stations}
        self.peak_kw = 0.0
        self.violations: list[tuple[float, float]] = []   # (wall time, site kW) above the panel rating
        self.finished: list[dict] = []

    def sim_now(self):
        return (time.monotonic() - self.wall0) * self.speed

    @property
    def site_kw(self):
        return sum(s.power_kw for s in self.stations)

    def on_change(self, st=None):
        kw = self.site_kw
        self.peak_kw = max(self.peak_kw, kw)
        if kw > self.panel_kw + 1e-6:
            self.violations.append((time.monotonic(), kw))

    def default_ev(self, token="APP-USER"):
        return EV(need_kwh=15.0, max_kw=11.0, token=token)

    async def start(self):
        await asyncio.gather(*(s.connect() for s in self.stations))
        return self

    async def close(self):
        self.closing = True
        await asyncio.gather(*(s.close() for s in self.stations), return_exceptions=True)
