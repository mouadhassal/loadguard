"""What is load management worth to a multifamily EV panel? Event-driven replay of real sessions.

Data: SINTEF "Residential electric vehicle charging datasets from apartment buildings" (Mendeley
Data, doi:10.17632/jbks2rcwyj.2, CC BY 4.0): 6,878 sessions with plug-in, plug-out and kWh from
apartment-building garages in Trondheim, Dec 2018 to Jan 2020. Private chargers only.

A building = K residents drawn at random from the pool, each with their own 7.2 kW charger, all
sessions replayed on their real calendar dates (so coincidences are real). Every policy is the
same function the CSMS runs (loadguard.alloc), so the controller tested in the lab is the one valued here.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .alloc import Demand, allocate

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw_sintef" / "sessions.csv"
P_MAX = 7.2           # kW, a 30 A Level 2 charger at 240 V
FULL_TOL = 0.05       # kWh: a session is "fully served" if it gets within 50 Wh of its need


def load_sessions() -> pd.DataFrame:
    df = pd.read_csv(RAW, sep=";", decimal=",")
    df = df[df["User_type"] == "Private"].copy()
    df["start"] = pd.to_datetime(df["Start_plugin"], format="%d.%m.%Y %H:%M")
    df["end"] = pd.to_datetime(df["End_plugout"], format="%d.%m.%Y %H:%M")
    df = df[df["end"] > df["start"]]
    t0 = pd.Timestamp("2018-12-01")
    df["a"] = (df["start"] - t0).dt.total_seconds() / 3600       # hours since t0
    df["d"] = (df["end"] - t0).dt.total_seconds() / 3600
    # energy the session needed; a 7.2 kW charger cannot deliver more than 7.2 kW x stay
    df["need"] = np.minimum(df["El_kWh"], P_MAX * (df["d"] - df["a"]))
    df["user"] = df["User_ID"]
    return df[["session_ID", "user", "Garage_ID", "start", "end", "a", "d", "need"]].sort_values("a").reset_index(drop=True)


def habit_departures(df: pd.DataFrame) -> np.ndarray:
    """Causal estimate of each session's plug-out time from the SAME user's earlier sessions:
    median stay of their previous sessions that began in the same part of the day (fallback: everyone's)."""
    part = pd.cut(df["start"].dt.hour, [-1, 5, 11, 16, 23], labels=False).to_numpy()
    stay = (df["d"] - df["a"]).to_numpy()
    est = np.empty(len(df))
    hist: dict = {}
    glob: dict = {}
    for i, (u, p) in enumerate(zip(df["user"].to_numpy(), part)):
        h = hist.get((u, p)) or glob.get(p)
        est[i] = df["a"].iat[i] + (float(np.median(h[-30:])) if h else 8.0)
        hist.setdefault((u, p), []).append(stay[i])
        glob.setdefault(p, []).append(stay[i])
    return est


@dataclass
class Result:
    delivered: np.ndarray
    segments: list          # (t0, t1, total_kW) piecewise-constant site load


def simulate(a, d, need, cap, policy, dep_key=None) -> Result:
    """Exact event-driven replay: rates only change at arrivals, departures and completions."""
    n = len(a)
    order = np.argsort(a, kind="stable")
    rem = need.astype(float).copy()
    got = np.zeros(n)
    active: set = set()
    t = a[order[0]] if n else 0.0
    nxt = 0
    segments = []
    deps = []                                               # heap of (departure, i)
    key = d if dep_key is None else dep_key
    while nxt < n or active:
        while nxt < n and a[order[nxt]] <= t + 1e-12:      # arrivals now
            i = order[nxt]
            active.add(i)
            heapq.heappush(deps, (d[i], i))
            nxt += 1
        while deps and deps[0][0] <= t + 1e-12:            # departures now
            _, i = heapq.heappop(deps)
            active.discard(i)
        if not active:
            if nxt < n:
                t = a[order[nxt]]
                continue
            break
        dem = [Demand(str(i), P_MAX, rem[i], float(key[i])) for i in active]
        rate = allocate(policy, dem, cap)
        r = {i: rate[str(i)] for i in active}
        t_next = min(a[order[nxt]] if nxt < n else np.inf, deps[0][0] if deps else np.inf)
        for i, ri in r.items():                             # next completion
            if ri > 1e-12 and rem[i] > 1e-12:
                t_next = min(t_next, t + rem[i] / ri)
        dt = t_next - t
        total = 0.0
        for i, ri in r.items():
            e = min(ri * dt, rem[i])
            rem[i] -= e
            got[i] += e
            total += ri if rem[i] > 1e-12 or e > 0 else 0.0
        segments.append((t, t_next, sum(r.values())))
        for i in [i for i in active if rem[i] <= 1e-9]:
            rem[i] = 0.0                                    # full: stays plugged but draws nothing
        t = t_next
    return Result(got, segments)


def served_share(res: Result, need) -> float:
    return float(np.mean(res.delivered >= need - FULL_TOL))


def instantaneous_peak(res: Result) -> float:
    return max((s[2] for s in res.segments), default=0.0)


def monthly_peaks_15min(res: Result, t0=pd.Timestamp("2018-12-01")) -> pd.Series:
    """Monthly maximum of the 15-minute average site load (how demand charges are metered)."""
    if not res.segments:
        return pd.Series(dtype=float)
    end = max(s[1] for s in res.segments)
    nb = int(np.ceil(end * 4)) + 1
    energy = np.zeros(nb)                                    # kWh per 15-min bin
    for t0_, t1_, kw in res.segments:
        if kw <= 0:
            continue
        b0, b1 = int(t0_ * 4), int(t1_ * 4)
        if b0 == b1:
            energy[b0] += kw * (t1_ - t0_)
        else:
            energy[b0] += kw * ((b0 + 1) / 4 - t0_)
            energy[b0 + 1:b1] += kw * 0.25
            if b1 < nb:
                energy[b1] += kw * (t1_ - b1 / 4)
    kw15 = energy * 4
    idx = t0 + pd.to_timedelta(np.arange(nb) * 15, unit="min")
    return pd.Series(kw15, index=idx).resample("MS").max()


def capacity_needed(a, d, need, policy, target=0.99, dep_key=None, lo=0.0, hi=None, tol=0.05):
    """Smallest panel kW at which >= target of sessions are fully served (bisection; monotone in cap)."""
    if policy == "uncontrolled":
        return instantaneous_peak(simulate(a, d, need, np.inf, "uncontrolled"))
    if hi is None:
        hi = instantaneous_peak(simulate(a, d, need, np.inf, "uncontrolled")) + 1e-6
    if served_share(simulate(a, d, need, hi, policy, dep_key), need) < target:
        return hi
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if served_share(simulate(a, d, need, mid, policy, dep_key), need) >= target:
            hi = mid
        else:
            lo = mid
    return hi
