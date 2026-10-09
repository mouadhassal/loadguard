"""Pre-registered savings study (see PREREGISTRATION.md). Stages:

    python study_run.py dev      development window (Jan-Sep 2019): explore, set thresholds
    python study_run.py test     frozen test window (Oct 2019-Jan 2020), run once; needs PREREGISTRATION.md
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from loadguard import study  # noqa: E402

WINDOWS = {"dev": ("2019-01-01", "2019-10-01"), "test": ("2019-10-01", "2020-02-01")}
KS = {"dev": [10, 20], "test": [10, 20, 40]}
DRAWS = {"dev": 40, "test": 100}
POLICIES = ["uncontrolled", "equal_share", "edf_habit", "edf_true"]
TARGET = 0.99
RATE_GS = 10.5170 + 2.8938 + 4.4306       # Toronto Hydro GS 50-999 kW, $/kW per 30 days (2026), PF = 1
RATE_EVC = 10.5170 + 0.4919 + 0.7532      # same class on the EV-charging (EVC) transmission rates
MIN_SESSIONS = 5


def window(stage):
    df = study.load_sessions()
    df["dep_habit"] = study.habit_departures(df)       # causal: uses each user's earlier sessions only
    lo, hi = WINDOWS[stage]
    w = df[(df["start"] >= lo) & (df["start"] < hi)]
    counts = w.groupby("user").size()
    return w[w["user"].isin(counts[counts >= MIN_SESSIONS].index)]


def one_draw(args):
    stage, K, draw = args
    w = window(stage)
    users = np.sort(w["user"].unique())
    rng = np.random.default_rng(1000 * K + draw)
    pick = rng.choice(users, size=K, replace=False)
    s = w[w["user"].isin(pick)]
    a, d, need, hab = (s[c].to_numpy() for c in ("a", "d", "need", "dep_habit"))
    out = dict(K=K, draw=draw, sessions=len(s), kwh=float(need.sum()))
    for p in POLICIES:
        pol, key = {"edf_habit": ("edf", hab), "edf_true": ("edf", None)}.get(p, (p, None))
        cap = study.capacity_needed(a, d, need, pol, TARGET, dep_key=key)
        res = study.simulate(a, d, need, np.inf if p == "uncontrolled" else cap, pol, key)
        lo, hi = WINDOWS[stage]
        mp = study.monthly_peaks_15min(res)
        mp = mp[(mp.index >= lo) & (mp.index < hi)]
        out[f"cap_{p}"] = cap
        out[f"served_{p}"] = study.served_share(res, need)
        out[f"peak15_{p}"] = float(mp.mean())
    return out


def run(stage):
    if stage == "test":
        pre = ROOT / "PREREGISTRATION.md"
        if not pre.exists():
            sys.exit("freeze PREREGISTRATION.md first")
        sha = hashlib.sha256(pre.read_bytes()).hexdigest()
    else:
        sha = "dev"
    w = window(stage)
    jobs = [(stage, K, i) for K in KS[stage] for i in range(DRAWS[stage])]
    with ProcessPoolExecutor(6) as ex:
        rows = list(ex.map(one_draw, jobs, chunksize=4))
    R = pd.DataFrame(rows)
    summary = {}
    rng = np.random.default_rng(7)
    for K, g in R.groupby("K"):
        s = {"draws": len(g), "users_in_pool": int(w["user"].nunique()), "sessions_in_window": len(w)}
        for p in POLICIES:
            s[f"cap_{p}_median"] = float(g[f"cap_{p}"].median())
            s[f"ratio_{p}_vs_uncontrolled_median"] = float((g[f"cap_{p}"] / g["cap_uncontrolled"]).median())
        ratio = (g["cap_edf_habit"] / g["cap_equal_share"]).to_numpy()
        boots = [np.median(rng.choice(ratio, len(ratio))) for _ in range(5000)]
        s["edf_habit_over_equal_median"] = float(np.median(ratio))
        s["edf_habit_over_equal_ci95"] = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]
        s["edf_habit_better_share"] = float(np.mean(ratio < 1))
        saved_kw = (g["peak15_uncontrolled"] - g["peak15_equal_share"]).to_numpy()
        per_year = 365 / 30
        s["demand_saving_per_charger_year_GS"] = float(np.median(saved_kw) * RATE_GS * per_year / K)
        s["demand_saving_per_charger_year_EVC"] = float(np.median(saved_kw) * RATE_EVC * per_year / K)
        s["kw_per_charger_needed"] = {p: float(g[f"cap_{p}"].median() / K) for p in POLICIES}
        summary[int(K)] = s
    out = dict(prereg_sha256=sha, window=WINDOWS[stage], target=TARGET, rate_GS=RATE_GS, rate_EVC=RATE_EVC,
               summary=summary, draws=rows)
    (ROOT / "results").mkdir(exist_ok=True)
    path = ROOT / "results" / f"study_{stage}.json"
    path.write_text(json.dumps(out, indent=1, default=float))
    print(json.dumps(summary, indent=1))
    print("wrote", path)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "dev")
