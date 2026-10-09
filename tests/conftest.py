import asyncio
import contextlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from loadguard.csms import CSMS, Config  # noqa: E402
from loadguard.sim import Fleet  # noqa: E402


@contextlib.asynccontextmanager
async def lab(cfg: Config, n=4, panel_kw=None, speed=600.0, http=False, **station_kw):
    """A running CSMS plus a fleet of simulated stations; torn down afterwards."""
    csms = await CSMS(cfg).start(ws_port=0, http_port=0 if http else None)
    fleet = Fleet(f"ws://127.0.0.1:{csms.ws_port}", n, panel_kw if panel_kw is not None else cfg.cap_kw,
                  speed=speed, **station_kw)
    await fleet.start()
    await settle()
    try:
        yield csms, fleet
    finally:
        await fleet.close()
        await csms.stop()


async def settle(t=0.4):
    await asyncio.sleep(t)


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "csms.db")
