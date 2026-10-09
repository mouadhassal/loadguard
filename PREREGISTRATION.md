# Pre-registration: the value of load management for multifamily EV panels

**Frozen:** 9 October 2026, before the test window (Oct 2019 to Jan 2020) was simulated.
**Author:** Mouadh Abderrahmane Assal.
**Integrity:** `study_run.py test` refuses to run without this file and writes its SHA-256 into `results/study_test.json`.

## Data

The data is SINTEF's "Residential electric vehicle charging datasets from apartment buildings" (Mendeley Data, doi:10.17632/jbks2rcwyj.2, CC BY 4.0), file `Dataset 1_EV charging reports.csv`.

- **Sessions:** only `User_type == Private` sessions are used. Those are residents' own chargers in apartment-building garages in Trondheim.
- **Need:** each session's energy need is min(recorded kWh, 7.2 kW × stay).
- **Pool:** users need at least 5 sessions inside the window.

## Development (done before freezing)

The development window was 2019-01-01 to 2019-10-01, with 26 users and 1,820 sessions. Results are in `results/study_dev.json`.

| K chargers | equal_share / uncontrolled | edf_habit / equal_share (95% CI) | edf_true / uncontrolled | demand saving $/charger/yr (GS) |
|---|---|---|---|---|
| 10 | 0.41 | 0.95 (0.88, 1.01) | 0.24 | 173 |
| 20 | 0.38 | 0.87 (0.84, 0.89) | 0.19 | 121 |

## Frozen protocol

**Window and buildings**
- **Test window:** 2019-10-01 to 2020-02-01, sessions by plug-in time.
- **Buildings:** K ∈ {10, 20, 40}. Each of 100 random buildings per K draws K residents without replacement (seed 1000·K + draw). Every resident has a 7.2 kW charger, and all sessions are replayed on their real dates.

**Simulation**
- **Engine:** exact event-driven replay (`loadguard.study.simulate`).
- **Allocation:** the same `loadguard.alloc` functions the CSMS runs.

**Policies**
| Policy | How it allocates |
|---|---|
| `uncontrolled` | every car at 7.2 kW |
| `equal_share` | max-min fair under the cap; no departure information |
| `edf_habit` | earliest-departure-first, with each session's departure estimated causally from the same resident's earlier sessions in the same part of the day (median of the last 30) |
| `edf_true` | EDF with the true plug-out time (the value of the app asking "when do you leave?") |

**Capacity needed, C\*.** For each policy, C\* is the smallest panel kW at which at least 99% of sessions are fully served (within 50 Wh of their need). It is found by bisection to 0.05 kW. Uncontrolled C\* is the instantaneous peak.

**Demand charge**
- **Peak measured:** the window-mean of each month's maximum 15-minute site load, uncontrolled versus `equal_share` at its C\*.
- **Rate:** Toronto Hydro General Service 50–999 kW, effective 1 Jan 2026. Distribution $10.5170/kVA + transmission connection $2.8938/kW + network $4.4306/kW = **$17.84 per kW per 30 days** (PF = 1). The EV-charging transmission rates are reported alongside.
- **Caveat:** this values the EV load's own peak, an upper bound on its contribution to a building's coincident peak.

## Hypotheses (test window)

- **S1:** median C\*(equal_share) / C\*(uncontrolled) ≤ **0.55** at every K.
- **S2:** `edf_habit` needs less capacity than `equal_share`. The median paired ratio is < 1 and its 95% bootstrap CI upper bound is < 1, at **K = 20 and K = 40**.
- **S3:** knowing true departures is worth more than habits: median C\*(edf_true) / C\*(equal_share) ≤ **0.75** at every K.
- **S4:** the demand-charge saving of `equal_share` versus uncontrolled is ≥ **$60 per charger per year** at the GS rate, at K = 40.

All four are reported whatever they show.

**Exploratory, declared now**
- chargers that fit on a 36 kW panel (100 A, 208 V three-phase) under each policy;
- the EVC-rate dollar values;
- per-K tables.

## The engineering lab (not a hypothesis test, reported as measured)

**Test suite.** `pytest` covers protocol, safety, resilience, billing, property-based and browser tests. Mutation testing (`tests/test_mutants.py`) requires every safety check to fail on a known-bad controller, for the right reason.

**Bugs the suite caught in my own CSMS during development, reported as found:**
1. Limits rounded to nearest instead of down: 6 × 2,666.7 W = 16.0002 kW on a 16 kW panel.
2. No work conservation: a full car kept 4 of 12 kW stranded until it was unplugged.
3. O(N²) command fan-out found by the load test. Coalescing plus 1 A quantisation cut it 5.2× at 200 chargers.
