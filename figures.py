"""Figures for the report: python figures.py  (reads results/, writes figures/)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from loadguard import study  # noqa: E402

OUT = ROOT / "figures"
COL = {"uncontrolled": "#9aa0a6", "equal_share": "#0a7d5a", "edf_habit": "#1a73e8", "edf_true": "#7b1fa2"}
LAB = {"uncontrolled": "uncontrolled", "equal_share": "equal share (today)",
       "edf_habit": "departure from habits", "edf_true": "departure known (app)"}
plt.rcParams.update({"font.size": 8.5, "axes.spines.top": False, "axes.spines.right": False,
                     "figure.dpi": 200, "savefig.bbox": "tight"})


def fig_capacity():
    r = json.loads((ROOT / "results" / "study_test.json").read_text())
    D = pd.DataFrame(r["draws"])
    Ks = sorted(D["K"].unique())
    fig, ax = plt.subplots(figsize=(3.5, 2.6))
    w = 0.2
    for j, p in enumerate(COL):
        v = [D[D.K == K][f"cap_{p}"] / K for K in Ks]
        med = [x.median() for x in v]
        lo = [m - x.quantile(0.1) for m, x in zip(med, v)]
        hi = [x.quantile(0.9) - m for m, x in zip(med, v)]
        ax.bar(np.arange(len(Ks)) + (j - 1.5) * w, med, w, yerr=[lo, hi], color=COL[p], label=LAB[p],
               error_kw=dict(lw=0.6, capsize=1.5))
    ax.set_xticks(range(len(Ks)), [f"{K} chargers" for K in Ks])
    ax.set_ylabel("panel kW needed per charger")
    ax.legend(frameon=False, fontsize=6.5)
    ax.set_title("≥ 99% of sessions fully charged", fontsize=8.5)
    fig.savefig(OUT / "fig_capacity.png")
    plt.close(fig)


def fig_day():
    """One real winter evening of one 20-charger building: uncontrolled vs equal share at its C*."""
    r = json.loads((ROOT / "results" / "study_test.json").read_text())
    d0 = [x for x in r["draws"] if x["K"] == 20][0]
    df = study.load_sessions()
    df["dep_habit"] = study.habit_departures(df)
    lo, hi = r["window"]
    w = df[(df["start"] >= lo) & (df["start"] < hi)]
    counts = w.groupby("user").size()
    w = w[w["user"].isin(counts[counts >= 5].index)]
    users = np.sort(w["user"].unique())
    pick = np.random.default_rng(1000 * 20 + 0).choice(users, size=20, replace=False)
    s = w[w["user"].isin(pick)]
    a, d, need = (s[c].to_numpy() for c in ("a", "d", "need"))
    t0 = pd.Timestamp("2018-12-01")
    res_u = study.simulate(a, d, need, np.inf, "uncontrolled")
    res_e = study.simulate(a, d, need, d0["cap_equal_share"], "equal_share")
    seg_u = pd.DataFrame(res_u.segments, columns=["t0", "t1", "kw"])
    day = seg_u.loc[seg_u["kw"].idxmax(), "t0"]               # the evening of the uncontrolled peak
    start = np.floor((day - 6) / 24) * 24 + 12                 # noon before
    fig, ax = plt.subplots(figsize=(3.5, 2.4))
    for res, p in ((res_u, "uncontrolled"), (res_e, "equal_share")):
        xs, ys = [], []
        for t_a, t_b, kw in res.segments:
            if t_b < start or t_a > start + 24:
                continue
            xs += [max(t_a, start) - start, min(t_b, start + 24) - start]
            ys += [kw, kw]
        ax.plot(xs, ys, color=COL[p], lw=1.0, label=LAB[p])
    ax.axhline(d0["cap_equal_share"], color=COL["equal_share"], ls=":", lw=0.8)
    ax.text(23.5, d0["cap_equal_share"] + 0.6, f"managed panel {d0['cap_equal_share']:.1f} kW", ha="right",
            fontsize=6.5, color=COL["equal_share"])
    ax.set_xlabel(f"hours from noon, {(t0 + pd.Timedelta(hours=start)).date()}")
    ax.set_ylabel("site load, kW")
    ax.set_xlim(0, 24)
    ax.legend(frameon=False, fontsize=6.5, loc="upper left")
    fig.savefig(OUT / "fig_day.png")
    plt.close(fig)


def fig_load():
    rows = json.loads((ROOT / "results" / "load.json").read_text())
    D = pd.DataFrame(rows)
    fig, ax = plt.subplots(2, 1, figsize=(3.5, 4.2))
    variants = [((False, 0.0), "v1: one rebalance per event", "#9aa0a6"),
                ((True, 0.0), "+ coalescing", "#f29900"),
                ((True, 0.24), "+ whole-amp limits (recommended)", "#0a7d5a")]
    for (c, q), lab, col in variants:
        g = D[(D.coalesce == c) & (D.quantum_kw == q)].sort_values("n")
        ax[0].plot(g.n, g.commands_per_arrival, "o-", color=col, label=lab, ms=3)
        ax[1].plot(g.n, g.arrivals_wall_s, "o-", color=col, ms=3)
    ax[0].set_ylabel("SetChargingProfile\ncommands per arrival")
    ax[0].legend(frameon=False, fontsize=6.5)
    ax[1].set_ylabel("seconds to settle\nafter N arrivals")
    ax[1].set_xlabel("chargers on one CSMS (N)")
    ax[0].set_title("Load test: zero panel overloads in every run", fontsize=8.5)
    fig.savefig(OUT / "fig_load.png")
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    fig_load()
    if (ROOT / "results" / "study_test.json").exists():
        fig_capacity()
        fig_day()
    print("figures in", OUT)
