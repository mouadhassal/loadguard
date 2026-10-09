# Test strategy: building an automated QA practice for an EV-charging platform

This is the strategy I would bring to SWTCH's QA Automation role ("design and build the automated E2E testing practice from the ground up"). Every layer below is demonstrated in this repo on a reference OCPP 2.0.1 system. The point is the method, not my toy CSMS.

## 1. What can go wrong, ranked by cost

| Risk | Why it matters for a charging platform | Property to test | In this repo |
|---|---|---|---|
| **Panel overload** | a tripped breaker darkens a garage; a fire-code issue for the property | site load ≤ panel rating at every instant, including transients and cloud outages | `test_safety.py`, mutants M1, M2, M4 |
| **Wrong bills** | money and trust; property managers resell energy | billed kWh = metered kWh under retries, dropouts and restarts | `test_safety.py`, `test_resilience.py`, mutant M3 |
| **Stranded capacity** | the product promise is "10× more chargers on the same panel" | work conservation: unused share flows to cars that need it | `test_load_management.py`, mutant M5 |
| **Cloud outage behaviour** | chargers must keep working safely offline | last limits hold, new sessions start safely, events are replayed in order | `test_resilience.py` |
| **Protocol drift** | multi-vendor hardware (OCPP 1.6 and 2.0.1) | schema-valid messages, correct use-case sequences, CALLERRORs on garbage | `test_protocol.py` |
| **UI regressions** | operators and residents act through the apps | the critical journeys work end to end | `test_ui.py` (Playwright) |
| **Scale** | fleets grow; demand-response events hit everyone at once | latency and command fan-out versus fleet size | `bench/load_test.py` |

## 2. Test layers (the pyramid, adapted to hardware)

1. **Unit and property-based** (milliseconds, every commit). Allocation, billing maths and schedule composition. Hypothesis generates thousands of sites per run (`test_alloc_properties.py`).
2. **Contract / protocol** (seconds). Every OCPP message is validated against the official JSON schemas on both sides. Tests are named by OCPP 2.0.1 use-case ID (B01, C01, E, F01/F03, K01) so coverage maps to the spec and to the OCA's OCTT certification tool. REST APIs get the same treatment with schema-driven tests.
3. **Integration against a simulated fleet** (tens of seconds). Virtual chargers that speak real OCPP over real WebSockets. They model physics (energy integrates power) and inject faults: network drops, retransmitted events, reboots, slow acknowledgements. The physical site meter is checked on every power change, not sampled.
4. **End to end** (minutes). Playwright drives the web dashboard against the running stack, with stable `data-testid` selectors and auto-waiting assertions instead of sleeps. Mobile apps would get the same journeys through Maestro or Appium.
5. **Load, soak and chaos** (nightly). N chargers from one process, measuring boot latency, command fan-out and convergence time, and asserting zero overloads.
6. **Hardware-in-the-loop** (before release). One bench charger per vendor and firmware line, driven by the same scenarios, because simulators lie about vendor quirks.

## 3. Tests that test the tests

- **Mutation testing of safety properties.** Every safety check is run against a deliberately broken controller and must fail, and must fail for the right reason. `tests/test_mutants.py` kills 5 of 5 mutants. A safety test that cannot fail is decoration.
- **Every escaped bug becomes a mutant.** When a defect escapes, its fix ships with a regression test *and* a mutant that reintroduces it, so the regression test is proven to catch it. That is how M5 (stranded capacity) entered this suite.

## 4. CI gates

| When | What runs | Budget |
|---|---|---|
| every PR | unit, property, protocol, mutants | < 5 min |
| merge to main | simulated-fleet integration and Playwright smoke | < 15 min |
| nightly | full E2E, load test (50 → 500 chargers), soak, flaky-test report | < 1 h |
| release candidate | hardware-in-the-loop bench, demand-response drill, upgrade and rollback | half a day |

**Flaky tests.** A flaky test is quarantined within one day, gets an owner and a ticket, and returns or is deleted within two weeks. No `sleep()` waits for state; wait for the condition.

## 5. What I would measure

- **Escapes:** defects found in production per release, and time to a regression test.
- **Safety:** the mutation score on safety-critical modules (target 100%).
- **Coverage:** OCPP use-case coverage per supported protocol version.
- **Flakiness:** the flake rate (target < 1%), and median CI time per gate.
- **Load-test trend:** p95 command latency and fan-out per arrival at 100, 250 and 500 chargers.

## 6. First 90 days at SWTCH

- **Days 1–30.** Map the system and the top 10 risks with Product. Stand up a charger simulator against staging. Write the first safety and billing properties. Put protocol and unit tests behind a PR gate. Ship 5 Playwright journeys for the operator and resident apps.
- **Days 31–60.** Build simulated fleets per hardware vendor, with fault injection. Add mutation tests for load management and billing. Add a nightly load test with trend charts. Write the flaky-test policy and run the first quarantine.
- **Days 61–90.** Add a hardware-in-the-loop bench with one charger per vendor. Add drills for demand-response (OpenADR) events and cloud outages. Write a testing handbook, pair with each engineer on writing tests, and make release readiness a dashboard rather than a meeting.

## 7. What this repo found in its own system

The lab's first runs caught three real defects in my reference CSMS, all now fixed and guarded:

1. **Rounding.** Limits were rounded to the nearest 0.1 W instead of down, so 6 × 2,666.7 W = 16.0002 kW went onto a 16 kW panel.
2. **No work conservation.** A full car kept its share: 4 of 12 kW was stranded until it was unplugged. This is mutant M5.
3. **Quadratic command fan-out.** Every arrival re-sent limits to nearly every charger: 29,700 commands for 200 arrivals. Quantising limits to whole amps cut that 5.2× and settled 3.9× faster, with zero overloads.
