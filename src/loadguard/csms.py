"""Reference CSMS: OCPP 2.0.1 over WebSocket, with site-level dynamic load management.

The CSMS is the *system under test*. Its safety-relevant design choices are switchable so
the test suite can be mutation-tested against known-bad variants:

    default_mode  "headroom"     idle chargers' TxDefaultProfile = their share of unallocated headroom (safe)
                  "static_max"   TxDefaultProfile = charger max at boot (common, unsafe during CSMS outages)
    update_order  "decrease_first"  send and confirm every limit decrease before any increase (safe)
                  "increase_first"  the reverse (mutant: transient overload)
    dedupe        True  bill from the cumulative meter register and ignore repeated (transactionId, seqNo)
                        events (safe); False sums per-event interval energy without dedupe (mutant)
    policy        "equal_share" | "edf" | "uncontrolled" (the last one ignores the cap: mutant)
    work_conserving  True: a car reporting SuspendedEV gives its share back (False strands capacity)

State lives in SQLite so a CSMS restart keeps transactions, billing and commanded limits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from ocpp.routing import on
from ocpp.v201 import ChargePoint, call, call_result, datatypes, enums
from websockets.asyncio.server import serve

from .alloc import Demand, allocate

log = logging.getLogger("loadguard.csms")
STATIC = Path(__file__).parent / "static"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Config:
    cap_kw: float = 22.0
    policy: str = "equal_share"
    default_mode: str = "headroom"
    update_order: str = "decrease_first"
    dedupe: bool = True
    work_conserving: bool = True        # reallocate when a car reports SuspendedEV (False = mutant)
    coalesce: bool = True               # merge rebalance triggers that queue up (False = one run per trigger)
    quantum_kw: float = 0.0             # >0: limits in whole steps (0.24 kW = 1 A at 240 V) to cut command fan-out
    evse_max_kw: float = 7.2
    heartbeat_s: int = 30
    db_path: str = ":memory:"
    allowed_tokens: tuple = ()           # empty = accept every token except BLOCKED*

    @classmethod
    def safe(cls, **kw):
        return cls(**kw)


@dataclass
class Tx:
    id: str
    station: str
    active: bool = True
    wh_first: float | None = None
    wh_last: float | None = None
    power_w: float = 0.0
    departure: float | None = None
    need_kwh: float | None = None
    offline_events: int = 0
    events: int = 0
    interval_wh: float = 0.0
    suspended: bool = False             # EV not drawing power (full, or paused by the car)
    by_interval: bool = False           # mutant billing: sum of per-event interval energy
    seqs: set = field(default_factory=set)

    @property
    def kwh(self) -> float:
        if self.by_interval:
            return self.interval_wh / 1000
        if self.wh_first is None or self.wh_last is None:
            return 0.0
        return max(self.wh_last - self.wh_first, 0.0) / 1000


class StationLink(ChargePoint):
    """One connected charging station, as seen by the CSMS."""

    def __init__(self, sid, connection, csms: "CSMS"):
        super().__init__(sid, connection, response_timeout=10)
        self.csms = csms

    @on("BootNotification")
    async def on_boot(self, charging_station, reason, **kw):
        asyncio.create_task(self.csms.after_boot(self.id))
        return call_result.BootNotification(current_time=now_iso(), interval=self.csms.cfg.heartbeat_s,
                                            status=enums.RegistrationStatusEnumType.accepted)

    @on("Heartbeat")
    async def on_heartbeat(self, **kw):
        return call_result.Heartbeat(current_time=now_iso())

    @on("StatusNotification")
    async def on_status(self, timestamp, connector_status, evse_id, connector_id, **kw):
        self.csms.status[self.id] = connector_status
        return call_result.StatusNotification()

    @on("Authorize")
    async def on_authorize(self, id_token, **kw):
        ok = self.csms.token_ok(id_token["id_token"])
        st = enums.AuthorizationStatusEnumType.accepted if ok else enums.AuthorizationStatusEnumType.blocked
        return call_result.Authorize(id_token_info=datatypes.IdTokenInfoType(status=st))

    @on("TransactionEvent")
    async def on_transaction_event(self, event_type, timestamp, trigger_reason, seq_no, transaction_info, **kw):
        info = self.csms.on_tx_event(self.id, event_type, seq_no, transaction_info, kw)
        if info is not None and event_type == "Started" and "id_token" in kw:
            ok = self.csms.token_ok(kw["id_token"]["id_token"])
            st = enums.AuthorizationStatusEnumType.accepted if ok else enums.AuthorizationStatusEnumType.blocked
            return call_result.TransactionEvent(id_token_info=datatypes.IdTokenInfoType(status=st))
        return call_result.TransactionEvent()

    @on("MeterValues")
    async def on_meter_values(self, evse_id, meter_value, **kw):
        return call_result.MeterValues()


class CSMS:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.links: dict[str, StationLink] = {}
        self.status: dict[str, str] = {}
        self.txs: dict[str, Tx] = {}
        self.limits: dict[str, float] = {}       # last commanded limit per station (kW)
        self.billed_kwh: dict[str, float] = {}
        self.lock = asyncio.Lock()
        self._gen = 0
        self._done_gen = 0
        self.commands = 0
        self._ws_server = None
        self._runner = None
        self.db = sqlite3.connect(cfg.db_path)
        self._init_db()
        self._load()

    # ---------------------------------------------------------------- persistence
    def _init_db(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS stations(id TEXT PRIMARY KEY, limit_kw REAL);
            CREATE TABLE IF NOT EXISTS txs(id TEXT PRIMARY KEY, station TEXT, active INT, wh_first REAL,
                                           wh_last REAL, departure REAL, need_kwh REAL, offline_events INT);
            CREATE TABLE IF NOT EXISTS events(tx TEXT, seq INT, PRIMARY KEY(tx, seq));
        """)

    def _load(self):
        for sid, lim in self.db.execute("SELECT id, limit_kw FROM stations"):
            self.status.setdefault(sid, "Unavailable")
            if lim is not None:
                self.limits[sid] = lim
        for row in self.db.execute("SELECT * FROM txs"):
            t = Tx(id=row[0], station=row[1], active=bool(row[2]), wh_first=row[3], wh_last=row[4],
                   departure=row[5], need_kwh=row[6], offline_events=row[7] or 0)
            t.seqs = {s for (s,) in self.db.execute("SELECT seq FROM events WHERE tx=?", (t.id,))}
            self.txs[t.id] = t

    def _save_tx(self, t: Tx):
        self.db.execute("INSERT OR REPLACE INTO txs VALUES (?,?,?,?,?,?,?,?)",
                        (t.id, t.station, int(t.active), t.wh_first, t.wh_last, t.departure, t.need_kwh,
                         t.offline_events))
        self.db.commit()

    # ---------------------------------------------------------------- OCPP side
    def token_ok(self, token: str) -> bool:
        if token.upper().startswith("BLOCKED"):
            return False
        return not self.cfg.allowed_tokens or token in self.cfg.allowed_tokens

    async def on_connect(self, connection):
        sid = connection.request.path.strip("/").split("/")[-1]
        link = StationLink(sid, connection, self)
        self.links[sid] = link
        self.status.setdefault(sid, "Available")
        self.db.execute("INSERT OR IGNORE INTO stations VALUES (?, NULL)", (sid,))
        self.db.commit()
        asyncio.create_task(self.rebalance())
        try:
            await link.start()
        except Exception:                               # connection closed
            pass
        finally:
            if self.links.get(sid) is link:
                del self.links[sid]

    async def after_boot(self, sid):
        if self.cfg.default_mode == "static_max":       # the classic "factory default" approach
            await self._send_default(sid, self.cfg.evse_max_kw)
        await self.rebalance()

    def on_tx_event(self, sid, event_type, seq_no, transaction_info, kw):
        tid = transaction_info["transaction_id"]
        t = self.txs.get(tid) or Tx(id=tid, station=sid, by_interval=not self.cfg.dedupe)
        self.txs[tid] = t
        if self.cfg.dedupe and seq_no in t.seqs:
            return None                                 # duplicate delivery: already counted
        t.seqs.add(seq_no)
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (tid, seq_no))
        t.events += 1
        if kw.get("offline"):
            t.offline_events += 1
        cd = kw.get("custom_data") or {}
        if "departure" in cd:
            t.departure = float(cd["departure"])
        if "need_kwh" in cd:
            t.need_kwh = float(cd["need_kwh"])
        for mv in kw.get("meter_value") or []:
            for sv in mv["sampled_value"]:
                m = sv.get("measurand", "Energy.Active.Import.Register")
                if m == "Energy.Active.Import.Register":     # cumulative register: order-free, idempotent
                    wh = float(sv["value"])
                    t.wh_first = wh if t.wh_first is None else min(t.wh_first, wh)
                    t.wh_last = wh if t.wh_last is None else max(t.wh_last, wh)
                elif m == "Energy.Active.Import.Interval":   # energy since the previous event
                    t.interval_wh += float(sv["value"])
                elif m == "Power.Active.Import":
                    t.power_w = float(sv["value"])
        state = transaction_info.get("charging_state")
        if state is not None:
            was = t.suspended
            t.suspended = self.cfg.work_conserving and state in ("SuspendedEV", "EVConnected", "Idle")
            if t.suspended != was and not kw.get("offline"):
                asyncio.create_task(self.rebalance())   # a full car must not strand its share
        if event_type == "Started":
            t.active = True
        if event_type == "Ended":
            t.active = False
            t.power_w = 0.0
            self.billed_kwh[tid] = t.kwh
        self._save_tx(t)
        if event_type in ("Started", "Ended") and not kw.get("offline"):
            asyncio.create_task(self.rebalance())
        return t

    # ---------------------------------------------------------------- load management
    def active_txs(self):
        return [t for t in self.txs.values() if t.active and t.station in self.status]

    def plan(self) -> dict[str, float]:
        """Target limit for every known station: TxProfile for active ones, TxDefault for idle ones."""
        act = self.active_txs()
        demands = [Demand(t.station, 0.0 if t.suspended else self.cfg.evse_max_kw,
                          None if t.need_kwh is None else max(t.need_kwh - t.kwh, 0.0), t.departure) for t in act]
        target = allocate(self.cfg.policy, demands, self.cfg.cap_kw)
        busy = {t.station for t in act}
        idle = [s for s in self.status if s not in busy]
        if self.cfg.quantum_kw > 0:                       # whole-amp steps, always rounded DOWN
            q = self.cfg.quantum_kw
            target = {s: math.floor(kw / q + 1e-9) * q for s, kw in target.items()}
        if self.cfg.default_mode == "headroom" and idle:
            spare = max(self.cfg.cap_kw - sum(target.values()), 0.0)
            for s in idle:
                v = min(spare / len(idle), self.cfg.evse_max_kw)
                target[s] = math.floor(v / self.cfg.quantum_kw + 1e-9) * self.cfg.quantum_kw if self.cfg.quantum_kw > 0 else v
        return target

    async def rebalance(self):
        """Recompute and push limits. With coalescing, triggers that pile up while a rebalance is
        running are served by ONE later run that sees all of them (the load test showed the naive
        one-run-per-trigger version needs O(N^2) commands for N arrivals)."""
        self._gen += 1
        mine = self._gen
        async with self.lock:
            if self.cfg.coalesce and self._done_gen >= mine:
                return                                   # a run that started after us already covered this
            covering = self._gen
            await self._rebalance_once()
            self._done_gen = covering

    async def _rebalance_once(self):
        target = self.plan()
        changes = [(s, kw) for s, kw in target.items()
                   if s in self.links and abs(self.limits.get(s, self.cfg.evse_max_kw) - kw) > 1e-6]
        down = [c for c in changes if c[1] < self.limits.get(c[0], self.cfg.evse_max_kw)]
        up = [c for c in changes if c not in down]
        phases = [down, up] if self.cfg.update_order == "decrease_first" else [up, down]
        for phase in phases:
            await asyncio.gather(*(self._command(s, kw) for s, kw in phase))

    async def _command(self, sid, kw):
        busy = {t.station: t for t in self.active_txs()}
        if sid in busy:
            await self._send_tx_profile(sid, busy[sid].id, kw)
        elif self.cfg.default_mode == "headroom":
            await self._send_default(sid, kw)

    def _profile(self, purpose, kw, tx_id=None, pid=1):
        sched = datatypes.ChargingScheduleType(
            id=pid, charging_rate_unit=enums.ChargingRateUnitEnumType.watts,
            # Round DOWN to 0.1 W: rounding to nearest let 6 x 2666.7 W = 16.0002 kW exceed a 16 kW panel
            # (caught by test_arrivals_never_overload on the first run).
            charging_schedule_period=[datatypes.ChargingSchedulePeriodType(
                start_period=0, limit=math.floor(kw * 10000 + 1e-9) / 10)])
        return datatypes.ChargingProfileType(
            id=pid, stack_level=0, charging_profile_purpose=purpose,
            charging_profile_kind=enums.ChargingProfileKindEnumType.relative,
            charging_schedule=[sched], transaction_id=tx_id)

    async def _send(self, sid, profile, kw):
        link = self.links.get(sid)
        if link is None:
            return
        try:
            res = await link.call(call.SetChargingProfile(evse_id=1, charging_profile=profile))
            if res.status == "Accepted":
                self.limits[sid] = kw
                self.db.execute("UPDATE stations SET limit_kw=? WHERE id=?", (kw, sid))
                self.db.commit()
                self.commands += 1
        except Exception as e:                          # station went away mid-call
            log.warning("SetChargingProfile to %s failed: %s", sid, e)

    async def _send_default(self, sid, kw):
        await self._send(sid, self._profile(enums.ChargingProfilePurposeEnumType.tx_default_profile, kw, pid=1), kw)

    async def _send_tx_profile(self, sid, tx_id, kw):
        await self._send(sid, self._profile(enums.ChargingProfilePurposeEnumType.tx_profile, kw, tx_id, pid=2), kw)

    async def set_cap(self, cap_kw: float):
        self.cfg.cap_kw = cap_kw
        await self.rebalance()

    async def remote_start(self, sid, token="APP-USER"):
        link = self.links[sid]
        res = await link.call(call.RequestStartTransaction(
            id_token=datatypes.IdTokenType(id_token=token, type=enums.IdTokenEnumType.central),
            remote_start_id=int(time.time() * 1000) % 2**31, evse_id=1))
        return res.status

    async def remote_stop(self, sid):
        t = next((t for t in self.active_txs() if t.station == sid), None)
        if t is None:
            return "Rejected"
        res = await self.links[sid].call(call.RequestStopTransaction(transaction_id=t.id))
        return res.status

    # ---------------------------------------------------------------- REST + dashboard
    def snapshot(self) -> dict:
        act = {t.station: t for t in self.active_txs()}
        st = []
        for sid in sorted(self.status):
            t = act.get(sid)
            st.append(dict(id=sid, connected=sid in self.links, status=self.status.get(sid),
                           limit_kw=round(self.limits.get(sid, self.cfg.evse_max_kw), 3),
                           power_kw=round(t.power_w / 1000, 3) if t else 0.0,
                           tx=t.id if t else None, kwh=round(t.kwh, 3) if t else 0.0))
        return dict(cap_kw=self.cfg.cap_kw, policy=self.cfg.policy,
                    site_kw=round(sum(s["power_kw"] for s in st), 3), stations=st,
                    transactions=[dict(id=t.id, station=t.station, active=t.active, kwh=round(t.kwh, 4),
                                       offline_events=t.offline_events) for t in self.txs.values()])

    def rest_app(self) -> web.Application:
        app = web.Application()

        async def state(_):
            return web.json_response(self.snapshot())

        async def cap(req):
            body = await req.json()
            await self.set_cap(float(body["cap_kw"]))
            return web.json_response({"cap_kw": self.cfg.cap_kw})

        async def start(req):
            return web.json_response({"status": await self.remote_start(req.match_info["sid"])})

        async def stop(req):
            return web.json_response({"status": await self.remote_stop(req.match_info["sid"])})

        async def index(_):
            return web.FileResponse(STATIC / "index.html")

        app.router.add_get("/", index)
        app.router.add_get("/api/state", state)
        app.router.add_post("/api/cap", cap)
        app.router.add_post("/api/stations/{sid}/start", start)
        app.router.add_post("/api/stations/{sid}/stop", stop)
        return app

    async def start(self, host="127.0.0.1", ws_port=9000, http_port=None):
        self._ws_server = await serve(self.on_connect, host, ws_port, subprotocols=["ocpp2.0.1"])
        self.ws_port = self._ws_server.sockets[0].getsockname()[1]
        if http_port is not None:
            self._runner = web.AppRunner(self.rest_app())
            await self._runner.setup()
            site = web.TCPSite(self._runner, host, http_port)
            await site.start()
            self.http_port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._ws_server is not None:
            self._ws_server.close()
            for link in list(self.links.values()):
                try:
                    await link._connection.close()
                except Exception:
                    pass
            await self._ws_server.wait_closed()
        if self._runner is not None:
            await self._runner.cleanup()
        self.links.clear()


async def main(cap_kw=22.0, ws_port=9000, http_port=8080):
    csms = await CSMS(Config(cap_kw=cap_kw, db_path="loadguard.db")).start(ws_port=ws_port, http_port=http_port)
    print(f"CSMS on ws://127.0.0.1:{csms.ws_port}  dashboard http://127.0.0.1:{csms.http_port}")
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
