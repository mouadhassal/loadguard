"""Apply the pre-registered decision rules (PREREGISTRATION.md) to results/study_test.json."""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent


def main():
    r = json.loads((ROOT / "results" / "study_test.json").read_text())
    S = {int(k): v for k, v in r["summary"].items()}
    D = pd.DataFrame(r["draws"])
    true_over_equal = {K: float((g.cap_edf_true / g.cap_equal_share).median()) for K, g in D.groupby("K")}
    v = {
        "prereg_sha256": r["prereg_sha256"],
        "S1": {"ratios": {K: S[K]["ratio_equal_share_vs_uncontrolled_median"] for K in S},
               "passed": all(S[K]["ratio_equal_share_vs_uncontrolled_median"] <= 0.55 for K in S)},
        "S2": {"median": {K: S[K]["edf_habit_over_equal_median"] for K in S},
               "ci95": {K: S[K]["edf_habit_over_equal_ci95"] for K in S},
               "passed": all(S[K]["edf_habit_over_equal_median"] < 1 and S[K]["edf_habit_over_equal_ci95"][1] < 1
                             for K in (20, 40))},
        "S3": {"edf_true_over_equal": true_over_equal, "passed": all(x <= 0.75 for x in true_over_equal.values())},
        "S4": {"per_charger_year_GS": {K: S[K]["demand_saving_per_charger_year_GS"] for K in S},
               "per_charger_year_EVC": {K: S[K]["demand_saving_per_charger_year_EVC"] for K in S},
               "passed": S[40]["demand_saving_per_charger_year_GS"] >= 60},
    }
    # exploratory: chargers that fit a 36 kW panel (100 A, 208 V 3-phase), linear interpolation of median C*(K)
    panel = 36.0
    fit = {}
    for p in ("uncontrolled", "equal_share", "edf_habit", "edf_true"):
        ks = sorted(S)
        caps = [S[K][f"cap_{p}_median"] for K in ks]
        if caps[-1] <= panel:
            fit[p] = f">= {ks[-1]} (beyond tested range)"
        else:
            fit[p] = float(np.interp(panel, caps, ks))
    v["exploratory_chargers_on_36kW_panel"] = fit
    (ROOT / "results" / "verdicts.json").write_text(json.dumps(v, indent=1, default=float))
    for h in ("S1", "S2", "S3", "S4"):
        print(h, "PASSED" if v[h]["passed"] else "FAILED", {k: x for k, x in v[h].items() if k != "passed"})
    print("chargers on a 36 kW panel:", fit)


if __name__ == "__main__":
    main()
