# G9: official simulator results

- **S0: NO MEASURABLE EFFECT.** C0 0 [0, 0] → C4 0 [0, 0] extra provider attempts per planned request. All registered guardrails pass.
- **S1: HARMFUL.** C0 1.27727 [1.24091, 1.27727] → C4 1.22045 [1.21364, 1.25909] attempts per planned request. Failed guardrails: 99th-percentile request latency.
- **S2: HARMFUL.** C0 2.78 [2.70667, 2.91333] → C4 0.12 [0.12, 0.126667] outage attempts per planned outage request. Failed guardrails: recovery time, success outside the outage.
- **S3: EFFECTIVE.** C0 90 [82, 98] † → C4 0 [0, 0] duplicate provider executions. All registered guardrails pass.

In S1, C0/C4 median p99 request latency is 1.615/3.025 seconds. In S2, success outside the outage is 100.00%/88.67%; median recovery is 5/10 seconds after the outage ends. These costs matter even when fewer calls reach the provider. A p99 is the 99th-percentile time to a final request outcome; failed requests count too.

## Reading the measurements

Each entry is median [minimum, maximum] of three VALID per-run metrics; no pooling. † flags relative spread >10%; a zero median with a nonzero range is also flagged. Ranges describe these three seeds and host scheduling, not confidence intervals or isolated environmental noise. Small n=3 limits precision. Full precision, per-round values, source paths/SHA-256 hashes and formulas are in [chart_data.json](chart_data.json).

## Per-cell measurements

Success is a fraction of all planned jobs (440 in S1, 600 otherwise). Latencies include failures and are seconds. Each scenario's primary metric and units are frozen in `bench/thresholds.yaml`.

| Cell | Registered primary | Success fraction | p50 s | p95 s | p99 s |
| --- | --- | --- | --- | --- | --- |
| S0/C0 | 0 [0, 0] | 1 [1, 1] | 0.0920555 [0.0914057, 0.0978731] | 0.108714 [0.1011, 0.112455] † | 0.114755 [0.105353, 0.117224] † |
| S0/C4 | 0 [0, 0] | 1 [1, 1] | 0.0909203 [0.0908479, 0.0931706] | 0.104677 [0.101334, 0.110246] | 0.108465 [0.104811, 0.115745] † |
| S1/C0 | 1.27727 [1.24091, 1.27727] | 0.940909 [0.938636, 0.940909] | 0.096162 [0.0918515, 0.0968678] | 1.52002 [1.51438, 1.5203] | 1.61506 [1.52363, 1.61589] |
| S1/C1 | 1.24545 [1.24318, 1.25682] | 0.936364 [0.936364, 0.938636] | 0.0966812 [0.0926004, 0.0990738] | 1.51713 [1.51177, 1.52006] | 1.54334 [1.53288, 1.55115] |
| S1/C2 | 1.24091 [1.22045, 1.25682] | 0.952273 [0.947727, 0.954545] | 0.0932064 [0.0930777, 0.0941974] | 3.01682 [3.01039, 3.02706] | 3.1195 [3.10409, 3.12266] |
| S1/C3 | 1.23409 [1.19545, 1.23409] | 0.95 [0.945455, 0.952273] | 0.0933881 [0.0896901, 0.0948758] | 3.01552 [3.00839, 3.01661] | 3.10735 [3.01469, 3.11511] |
| S1/C4 | 1.22045 [1.21364, 1.25909] | 0.947727 [0.947727, 0.954545] | 0.092194 [0.0912982, 0.0974117] | 3.01206 [3.01123, 3.02039] | 3.0249 [3.02171, 3.13194] |
| S2/C0 | 2.78 [2.70667, 2.91333] | 0.893333 [0.888333, 0.896667] | 0.0979123 [0.0941056, 0.100979] | 11.1272 [11.0988, 11.1335] | 16.6376 [16.6019, 16.6575] |
| S2/C1 | 3.12667 [3.06667, 3.16] | 0.805 [0.8, 0.811667] | 0.0962817 [0.0940478, 0.0993761] | 6.00425 [6.00418, 6.00512] | 6.00747 [6.00672, 6.02106] |
| S2/C2 | 3.17333 [3.14667, 3.28] | 0.8 [0.793333, 0.8] | 0.0961028 [0.0943874, 0.0997588] | 6.00582 [6.0042, 6.00813] | 6.02139 [6.00649, 6.02167] |
| S2/C3 | 0.12 [0.12, 0.126667] | 0.695 [0.661667, 0.7] | 0.0873338 [0.0870603, 0.0907404] | 0.106175 [0.101081, 0.110924] | 2.00268 [2.00237, 2.00578] |
| S2/C4 | 0.12 [0.12, 0.126667] | 0.665 [0.661667, 0.695] | 0.0877767 [0.084516, 0.0911447] | 0.106935 [0.105322, 0.111174] | 2.00178 [2.0016, 2.00369] |
| S3/C0 | 90 [82, 98] † | 1 [1, 1] | 0.0925639 [0.0918702, 0.0961271] | 0.690334 [0.686257, 0.694387] | 0.700524 [0.692756, 0.702046] |
| S3/C1 | 90 [82, 98] † | 1 [1, 1] | 0.0935833 [0.0930927, 0.0987146] | 0.690673 [0.688442, 0.69691] | 0.703208 [0.694387, 0.70962] |
| S3/C2 | 90 [82, 98] † | 1 [1, 1] | 0.0926142 [0.0911209, 0.0962515] | 0.358298 [0.349474, 0.374323] | 0.415978 [0.407655, 0.425654] |
| S3/C3 | 90 [82, 98] † | 1 [1, 1] | 0.0955536 [0.0946089, 0.0978673] | 0.366861 [0.355198, 0.368191] | 0.416077 [0.41601, 0.425435] |
| S3/C4 | 0 [0, 0] | 1 [1, 1] | 0.0959276 [0.0943566, 0.0962519] | 0.278113 [0.273775, 0.28666] | 0.33174 [0.329656, 0.347266] |

| Cell | Attempts/request | Extra attempts | Duplicate executions | Executed work units | Loop p99 s | Launch p99 s |
| --- | --- | --- | --- | --- | --- | --- |
| S0/C0 | 1 [1, 1] | 0 [0, 0] | 0 [0, 0] | 600 [600, 600] | 0.010791 [0.00547912, 0.0252641] † | 0.00848603 [0.00473953, 0.0254036] † |
| S0/C4 | 1 [1, 1] | 0 [0, 0] | 0 [0, 0] | 600 [600, 600] | 0.00724917 [0.00693819, 0.0247861] † | 0.00665478 [0.00579384, 0.0257259] † |
| S1/C0 | 1.27727 [1.24091, 1.27727] | 122 [106, 122] † | 0 [0, 0] | 414 [413, 414] | 0.0093139 [0.00563378, 0.0259268] † | 0.00698003 [0.00478522, 0.0253226] † |
| S1/C1 | 1.24545 [1.24318, 1.25682] | 108 [107, 113] | 0 [0, 0] | 412 [412, 413] | 0.025214 [0.0110612, 0.0261123] † | 0.0246906 [0.0104187, 0.0250571] † |
| S1/C2 | 1.24091 [1.22045, 1.25682] | 106 [97, 113] † | 0 [0, 0] | 419 [417, 420] | 0.0261505 [0.00837565, 0.0265647] † | 0.0257879 [0.0068497, 0.0267035] † |
| S1/C3 | 1.23409 [1.19545, 1.23409] | 103 [86, 103] † | 0 [0, 0] | 418 [416, 419] | 0.0211309 [0.00883551, 0.0248983] † | 0.0181652 [0.00673161, 0.0234804] † |
| S1/C4 | 1.22045 [1.21364, 1.25909] | 97 [94, 114] † | 0 [0, 0] | 417 [417, 420] | 0.00655229 [0.00590327, 0.026555] † | 0.00540984 [0.00540029, 0.0267934] † |
| S2/C0 | 1.58833 [1.57333, 1.61667] | 353 [344, 370] | 0 [0, 0] | 536 [533, 538] | 0.0216308 [0.00903928, 0.0265196] † | 0.0209376 [0.0072309, 0.0260961] † |
| S2/C1 | 1.58167 [1.57833, 1.595] | 349 [347, 357] | 0 [0, 0] | 483 [480, 487] | 0.00991827 [0.0069842, 0.0220183] † | 0.00877494 [0.00519613, 0.0217333] † |
| S2/C2 | 1.59333 [1.58667, 1.61333] | 356 [352, 368] | 0 [0, 0] | 480 [476, 480] | 0.0216867 [0.00608133, 0.0264187] † | 0.0216608 [0.00577356, 0.0253187] † |
| S2/C3 | 0.726667 [0.691667, 0.73] | 6 [5, 7] † | 0 [0, 0] | 417 [397, 420] | 0.00830265 [0.00529219, 0.0246215] † | 0.00810794 [0.00431828, 0.0238709] † |
| S2/C4 | 0.695 [0.691667, 0.726667] | 6 [5, 7] † | 0 [0, 0] | 399 [397, 417] | 0.0222626 [0.00869446, 0.0249902] † | 0.0221225 [0.0069134, 0.0245733] † |
| S3/C0 | 1.15 [1.13667, 1.16333] | 90 [82, 98] † | 90 [82, 98] † | 690 [682, 698] | 0.00908596 [0.00580787, 0.0215432] † | 0.0085332 [0.00538082, 0.0218193] † |
| S3/C1 | 1.15 [1.13667, 1.16333] | 90 [82, 98] † | 90 [82, 98] † | 690 [682, 698] | 0.0104754 [0.00637802, 0.0218668] † | 0.00815215 [0.00570633, 0.0211327] † |
| S3/C2 | 1.15 [1.13667, 1.16333] | 90 [82, 98] † | 90 [82, 98] † | 690 [682, 698] | 0.0217335 [0.00914505, 0.0248357] † | 0.0217661 [0.00832202, 0.0261603] † |
| S3/C3 | 1.15 [1.13667, 1.16333] | 90 [82, 98] † | 90 [82, 98] † | 690 [682, 698] | 0.00895189 [0.00865982, 0.0253715] † | 0.00829918 [0.00783891, 0.0247239] † |
| S3/C4 | 1.15 [1.13667, 1.16333] | 90 [82, 98] † | 0 [0, 0] | 600 [600, 600] | 0.0260392 [0.00735921, 0.0263259] † | 0.0257621 [0.00596545, 0.0257884] † |

Executed work units count actual provider executions, a simulated cost proxy only; deduplicated replies are not executions. No price or money estimate is registered. S0 extras use per-job authoritative receipts, including zero-receipt jobs.

| S2 cell | Outside-outage success fraction | Recovery s after outage end |
| --- | --- | --- |
| S2/C0 | 1 [1, 1] | 5 [5, 5] |
| S2/C1 | 1 [1, 1] | 5 [5, 5] |
| S2/C2 | 1 [1, 1] | 5 [5, 5] |
| S2/C3 | 0.926667 [0.882222, 0.933333] | 9 [8, 11] † |
| S2/C4 | 0.886667 [0.882222, 0.926667] | 10 [9, 11] † |

| S1 cell | Burst arrivals / 140 planned burst jobs |
| --- | --- |
| S1/C0 | 1.74286 [1.67143, 1.75714] |
| S1/C1 | 1.7 [1.69286, 1.7] |
| S1/C2 | 1.54286 [1.52857, 1.57143] |
| S1/C3 | 1.55714 [1.49286, 1.56429] |
| S1/C4 | 1.53571 [1.51429, 1.6] |

| Cell | Nonzero final outcome counts: median [min, max] |
| --- | --- |
| S0/C0 | SUCCESS: 600 [600, 600] |
| S0/C4 | SUCCESS: 600 [600, 600] |
| S1/C0 | FAILED_AFTER_RETRIES: 26 [26, 27]; SUCCESS: 414 [413, 414] |
| S1/C1 | FAILED_AFTER_RETRIES: 28 [27, 28]; SUCCESS: 412 [412, 413] |
| S1/C2 | FAILED_AFTER_RETRIES: 21 [20, 23] †; SUCCESS: 419 [417, 420] |
| S1/C3 | FAILED_AFTER_RETRIES: 22 [21, 24] †; SUCCESS: 418 [416, 419] |
| S1/C4 | FAILED_AFTER_RETRIES: 23 [20, 23] †; SUCCESS: 417 [417, 420] |
| S2/C0 | FAILED_AFTER_RETRIES: 64 [62, 67]; SUCCESS: 536 [533, 538] |
| S2/C1 | DEADLINE_EXCEEDED: 51 [50, 56] †; FAILED_AFTER_RETRIES: 64 [62, 67]; SUCCESS: 483 [480, 487] |
| S2/C2 | DEADLINE_EXCEEDED: 55 [53, 56]; FAILED_AFTER_RETRIES: 65 [64, 71] †; SUCCESS: 480 [476, 480] |
| S2/C3 | FAILED_AFTER_RETRIES: 0 [0, 1] †; FAILED_FAST: 183 [179, 203] †; SUCCESS: 417 [397, 420] |
| S2/C4 | FAILED_AFTER_RETRIES: 0 [0, 1] †; FAILED_FAST: 200 [183, 203]; SUCCESS: 399 [397, 417] |
| S3/C0 | SUCCESS: 600 [600, 600] |
| S3/C1 | SUCCESS: 600 [600, 600] |
| S3/C2 | SUCCESS: 600 [600, 600] |
| S3/C3 | SUCCESS: 600 [600, 600] |
| S3/C4 | SUCCESS: 600 [600, 600] |

## Paired decisions

B=median(baseline), C=median(candidate), I=(B−C)/B, S=(max(baseline)−min(baseline))/B. Efficacy requires I≥20% and I>S with every registered guardrail passing; harm takes precedence. B=0 makes I and S null and uses raw values. S0 is a healthy control, never an efficacy win. S3 C4 also requires zero duplicates in every C4 run and positive paired C0 duplicates. Guardrails compare the same round/seed; only S0 latency compares the three-run p99 medians. All guardrail inputs, limits and pass/false/null results appear in chart_data.json.

| Scenario | Role | Pair | Verdict | I | S | Failed guardrails (round) |
| --- | --- | --- | --- | --- | --- | --- |
| S0 | primary | C0:C4 | NO MEASURABLE EFFECT | null | null | none |
| S1 | primary | C0:C4 | HARMFUL | 0.044484 | 0.0284698 | latency_p99_seconds (1); latency_p99_seconds (2); latency_p99_seconds (3) |
| S1 | secondary | C0:C1 | HARMFUL | 0.024911 | 0.0284698 | latency_p99_seconds (1) |
| S1 | secondary | C0:C2 | HARMFUL | 0.0284698 | 0.0284698 | latency_p99_seconds (1); latency_p99_seconds (2); latency_p99_seconds (3) |
| S1 | secondary | C0:C3 | HARMFUL | 0.0338078 | 0.0284698 | latency_p99_seconds (1); latency_p99_seconds (2); latency_p99_seconds (3) |
| S1 | stepwise | C0:C1 | HARMFUL | 0.024911 | 0.0284698 | latency_p99_seconds (1) |
| S1 | stepwise | C1:C2 | HARMFUL | 0.00364964 | 0.0109489 | latency_p99_seconds (1); latency_p99_seconds (2); latency_p99_seconds (3) |
| S1 | stepwise | C2:C3 | HARMFUL | 0.00549451 | 0.029304 | latency_p99_seconds (2) |
| S1 | stepwise | C3:C4 | HARMFUL | 0.0110497 | 0.0313076 | latency_p99_seconds (1); latency_p99_seconds (2) |
| S2 | primary | C0:C4 | HARMFUL | 0.956835 | 0.0743405 | outside_outage_success_fraction (1); outside_outage_success_fraction (2); recovery_seconds (2); outside_outage_success_fraction (3) |
| S2 | secondary | C0:C1 | NO MEASURABLE EFFECT | -0.1247 | 0.0743405 | none |
| S2 | secondary | C0:C2 | NO MEASURABLE EFFECT | -0.141487 | 0.0743405 | none |
| S2 | secondary | C0:C3 | HARMFUL | 0.956835 | 0.0743405 | outside_outage_success_fraction (1); outside_outage_success_fraction (2); recovery_seconds (2); outside_outage_success_fraction (3) |
| S2 | stepwise | C0:C1 | NO MEASURABLE EFFECT | -0.1247 | 0.0743405 | none |
| S2 | stepwise | C1:C2 | HARMFUL | -0.0149254 | 0.0298507 | latency_p99_seconds (2); latency_p99_seconds (3) |
| S2 | stepwise | C2:C3 | HARMFUL | 0.962185 | 0.0420168 | outside_outage_success_fraction (1); outside_outage_success_fraction (2); recovery_seconds (2); outside_outage_success_fraction (3) |
| S2 | stepwise | C3:C4 | HARMFUL | 0 | 0.0555556 | latency_p99_seconds (2); outside_outage_success_fraction (3) |
| S3 | primary | C0:C4 | EFFECTIVE | 1 | 0.177778 | none |
| S3 | secondary | C0:C1 | NO MEASURABLE EFFECT | 0 | 0.177778 | none |
| S3 | secondary | C0:C2 | NO MEASURABLE EFFECT | 0 | 0.177778 | none |
| S3 | secondary | C0:C3 | NO MEASURABLE EFFECT | 0 | 0.177778 | none |
| S3 | stepwise | C0:C1 | NO MEASURABLE EFFECT | 0 | 0.177778 | none |
| S3 | stepwise | C1:C2 | NO MEASURABLE EFFECT | 0 | 0.177778 | none |
| S3 | stepwise | C2:C3 | NO MEASURABLE EFFECT | 0 | 0.177778 | none |
| S3 | stepwise | C3:C4 | EFFECTIVE | 1 | 0.177778 | none |

## Mechanism ladder

| Scenario | C0 reference: primary median [min, max] | C1 vs C0 | C2 vs C1 | C3 vs C2 | C4 vs C3* |
| --- | --- | --- | --- | --- | --- |
| S0 | 0 [0, 0] | not registered | not registered | not registered | NO MEASURABLE EFFECT |
| S1 | 1.27727 [1.24091, 1.27727] | HARMFUL | HARMFUL | HARMFUL | HARMFUL |
| S2 | 2.78 [2.70667, 2.91333] | NO MEASURABLE EFFECT | HARMFUL | HARMFUL | HARMFUL |
| S3 | 90 [82, 98] † | NO MEASURABLE EFFECT | NO MEASURABLE EFFECT | NO MEASURABLE EFFECT | EFFECTIVE |

*S0 C4 compares with C0; C1–C3 are not registered for S0. Each step inherits earlier protections; effects cannot be attributed to isolated mechanisms without further experiments.

## Retry timeline

![Provider attempts per second during S2, C0 versus C4 with three-run ranges](retry-storm.svg)

Lines select each config's median registered S2 primary run; ties use the earliest round. Representatives may use different rounds; paired verdicts still use all three matching rounds. Bands show per-second min–max across all three runs. The complete receipt-arrival horizon is shown, with a zero-based y axis. The shaded [20,35) second outage is registered; no traffic is rerun for this chart.

## Recovery and accounting

S2 checks whole seconds t=40..60. D counts all requests actually issued in [t−5,t); A counts those successful and completed by t. The earliest D>0 and 20A≥19D returns t−35 seconds. No qualifying window returns null; future completions and empty windows cannot establish recovery. Finite baseline and missing candidate recovery is harmful; missing baseline recovery is inconclusive unless another guard demonstrates harm.

51 VALID results in 17 cells; 53 attempts across 51 slots. All 55 official artifacts reconcile with the COMPLETE manifest. Slot 30 S3/C4 round 2 preserves ABANDONED attempt 1 and VALID attempt 2 under the approved [shutdown recovery decision](recovery-decision-20261105.md). Slot 46 S3/C2 round 3 preserves INVALID attempt 1 (loop p99 107.938 ms; launch p99 107.356 ms, above 35/50 ms) and VALID attempt 2. Excluded attempts never enter statistics. Slots 1–29 keep original runner provenance; subsequent attempts use the approved recovery runner. The empty reservation has no receipts or historical capture-time hash anchor; the exception does not recover the lost execution.

## Limits and reproduction

This is a simulated provider on loopback, on a single shared machine, with only three runs per cell and limited fault/load profiles. Seeded policies do not guarantee identical arrival-dependent faults. S1's token bucket depends on arrival timing. The simulator explicitly supports idempotency; this proves no exactly-once guarantee for real AI providers. Outcomes may not transfer to real services. No significance or real-price claim is made.

Run `source scripts/env.sh` then `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check`. Omit `--check` to regenerate these derivatives from saved evidence. Neither command starts a provider or benchmark.
