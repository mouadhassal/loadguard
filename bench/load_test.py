"""Load test: N simulated stations against one CSMS process.

    python bench/load_test.py 50 100 200 400      -> results/load.json

For each N: boot latency of all stations, then cars arrive one every 50 ms. We record how many
SetChargingProfile commands each arrival costs (fan-out), the time for the site to settle after the
last arrival, and any physical overload of the panel.
"""
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("PYTHONASYNCIODEBUG", "0")

from loadguard.csms import CSMS, Config  # noqa: E402
from loadguard.sim import EV, Fleet  # noqa: E402


async def run(n, coalesce=True, quantum_kw=0.0):
    cap = 7.2 * n * 0.4                                    # panel sized for 40% of chargers at full power
    csms = await CSMS(Config(cap_kw=cap, heartbeat_s=300, coalesce=coalesce, quantum_kw=quantum_kw)).start(ws_port=0)
    fleet = Fleet(f"ws://127.0.0.1:{csms.ws_port}", n, panel_kw=cap, speed=60.0, meter_interval=2.0)
    t0 = time.perf_counter()
    lat = []

    async def boot(s):
        a = time.perf_counter()
        await s.connect()
        lat.append(time.perf_counter() - a)

    sem = asyncio.Semaphore(100)

    async def guarded(s):
        async with sem:
            await boot(s)

    await asyncio.gather(*(guarded(s) for s in fleet.stations))
    t_boot = time.perf_counter() - t0
    await asyncio.sleep(0.5)
    c0 = csms.commands
    t1 = time.perf_counter()
    for s in fleet.stations:
        await s.plug_in(EV(need_kwh=60))
        await asyncio.sleep(0.05)
    while True:                                            # settle: no command for 0.5 s
        c = csms.commands
        await asyncio.sleep(0.5)
        if csms.commands == c:
            break
    t_arrivals = time.perf_counter() - t1
    cmds = csms.commands - c0
    out = dict(n=n, coalesce=coalesce, quantum_kw=quantum_kw, cap_kw=round(cap, 1), boot_total_s=round(t_boot, 2),
               boot_p50_ms=round(1000 * statistics.median(lat), 1),
               boot_p95_ms=round(1000 * sorted(lat)[int(0.95 * len(lat)) - 1], 1),
               commands=cmds, commands_per_arrival=round(cmds / n, 1), arrivals_wall_s=round(t_arrivals, 1),
               violations=len(fleet.violations), peak_kw=round(fleet.peak_kw, 2),
               site_kw_final=round(fleet.site_kw, 2))
    await fleet.close()
    await csms.stop()
    return out


async def main(ns):
    rows = []
    for n in ns:
        for coalesce, q in ((False, 0.0), (True, 0.0), (True, 0.24)):   # v1, +coalescing, +1 A quantisation
            r = await run(n, coalesce, q)
            r["utilisation"] = round(r["site_kw_final"] / r["cap_kw"], 4)
            print(r, flush=True)
            rows.append(r)
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "load.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    asyncio.run(main([int(x) for x in sys.argv[1:]] or [50, 100, 200]))
