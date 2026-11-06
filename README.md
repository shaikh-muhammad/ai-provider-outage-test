# AI Provider Outage Test

## 1. Pitch

A measured local simulator showing when retries, deadlines, a circuit breaker and idempotency help—or make things worse. Real official results, including failures, are below.

## 2. The problem, in plain language

An application can overload a struggling service by repeatedly asking again. It can also repeat work when a reply disappears after the work finishes. This project measures those tradeoffs against a simple baseline. All traffic stays on loopback (`127.0.0.1`); no real AI provider is called.

## 3. Actual measured results

- **S0: NO MEASURABLE EFFECT.** C0 0 [0, 0] → C4 0 [0, 0] extra provider attempts per planned request. All registered guardrails pass.
- **S1: HARMFUL.** C0 1.27727 [1.24091, 1.27727] → C4 1.22045 [1.21364, 1.25909] attempts per planned request. Failed guardrails: 99th-percentile request latency.
- **S2: HARMFUL.** C0 2.78 [2.70667, 2.91333] → C4 0.12 [0.12, 0.126667] outage attempts per planned outage request. Failed guardrails: recovery time, success outside the outage.
- **S3: EFFECTIVE.** C0 90 [82, 98] † → C4 0 [0, 0] duplicate provider executions. All registered guardrails pass.

In S1, C0/C4 median p99 request latency is 1.615/3.025 seconds. In S2, success outside the outage is 100.00%/88.67%; median recovery is 5/10 seconds after the outage ends. These costs matter even when fewer calls reach the provider. A p99 is the 99th-percentile time to a final request outcome; failed requests count too.

Numbers are median [min, max] across three VALID runs per cell; † marks >10% relative spread. These are simulator observations, not claims of statistical significance. [Full measurements and guardrails](docs/results.md) include every configuration and paired round; [chart data](docs/chart_data.json) links every value to hashed raw evidence.

## 4. Architecture

`frozen workload + seed → client C0–C4 → loopback provider → request records + provider ledger → validation → offline report`

The workload schedules synthetic text jobs. Each physical HTTP attempt has a matching server receipt. The ledger distinguishes an attempted call from work actually executed. Raw timing samples check whether the shared machine kept up. The frozen registration fixes scenarios, seeds, health limits and decision rules before capture.

## 5. Four protections

- **C0 baseline:** up to four attempts, five-second per-attempt timeouts and fixed half-second retry waits.
- **C1 deadlines:** a six-second total budget and two-second attempt timeouts limit how long one request waits.
- **C2 backoff:** randomized waits spread retries out; a valid Retry-After header asks the client to wait longer.
- **C3 circuit breaker:** after enough failures it temporarily rejects local calls, then admits a probe. Fewer calls can also mean fewer successes.
- **C4 idempotency:** a stable per-job key lets this simulator replay a saved result instead of executing the same work twice. Every step inherits the previous protections.

## 6. Scenarios and retry timeline

S0 is healthy service; S1 is a burst against a rate limit; S2 is a 15-second outage with errors and hung calls; S3 loses some replies after execution. S0 tests only C0/C4; the other scenarios test C0–C4. Seeds 12001–12003 pair the three rounds.

![S2 provider attempts per second, representative median runs and three-run ranges](docs/retry-storm.svg)

The shaded [20,35) second interval is the registered outage. Solid blue is C0; dashed orange is C4. Each line uses its config's median primary-metric run (earliest round breaks ties); shaded bands show per-second min–max across three runs, not confidence intervals. Lines may use different rounds. Decisions use all three paired rounds, not this illustration.

## 7. Mechanism ladder

| Scenario | C0 reference: primary median [min, max] | C1 vs C0 | C2 vs C1 | C3 vs C2 | C4 vs C3* |
| --- | --- | --- | --- | --- | --- |
| S0 | 0 [0, 0] | not registered | not registered | not registered | NO MEASURABLE EFFECT |
| S1 | 1.27727 [1.24091, 1.27727] | HARMFUL | HARMFUL | HARMFUL | HARMFUL |
| S2 | 2.78 [2.70667, 2.91333] | NO MEASURABLE EFFECT | HARMFUL | HARMFUL | HARMFUL |
| S3 | 90 [82, 98] † | NO MEASURABLE EFFECT | NO MEASURABLE EFFECT | NO MEASURABLE EFFECT | EFFECTIVE |

*S0 C4 compares directly with C0; C1–C3 were not registered there. C0 is a reference, not a verdict. [All C0 comparisons and incremental decisions](docs/results.md#paired-decisions) use the frozen four classes. Fewer attempts alone do not establish a win: every guardrail must pass.

## 8. Failures and surprises

The report names every failed guardrail, including cases where reduced traffic trades away useful successes. One health-invalid S3/C2 attempt was preserved and excluded before its successful retry. A shutdown interrupted slot 30; the original empty reservation remains as an approved ABANDONED attempt, linked to an additive recovery record. Totals are 51 VALID results, one INVALID and one ABANDONED attempt, 53 attempts and 55 official artifacts. [Engineering log](docs/engineering-log.md) records decisions and setbacks.

## 9. Limitations

This is a **simulated provider, on loopback, on a single shared machine**, with **only three runs per cell and limited fault/load profiles**. There is **no guarantee these results transfer to real AI providers**. Timing and arrival-dependent rate limiting vary with host scheduling. Three different seeds do not isolate pure environmental noise. Idempotency relies on the simulator's explicit support and proves no real-service exactly-once guarantee. Executed provider work is a cost proxy; no monetary price model is registered. Invalid/abandoned attempts are excluded, never erased. The recovery exception cannot reconstruct missing receipts or prove historical immutability without a capture-time anchor.

## 10. Reproduce at three levels

1. **Read:** inspect [results](docs/results.md), [chart data](docs/chart_data.json), [registration](bench/thresholds.yaml) and the saved [official evidence](results/official/). No execution is needed.
2. **Recompute:** use a full clone and the project's Python 3.14 environment. Activate with `source scripts/env.sh`; confirm `which python` is this project's `.venv/bin/python`. Install only locked binary wheels if setting up the environment: `python -m pip install --only-binary=:all: -r requirements-dev.lock`. Run `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check`. Omit `--check` to regenerate README, report, chart and chart data from saved evidence only.
3. **Verify implementation:** run the offline suite below. New experimental capture requires separate authorization under AGENTS.md; these reproduction instructions never rerun official experiments or call a real provider.

## 11. Testing

```sh
source scripts/env.sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m pytest -q --basetemp="$(dirname "$PROJECT_ROOT")/.pytest-tmp-ai-provider-outage-test" tests/
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check
python -m pip check
```

Tests independently recompute raw metrics and decisions, exercise harmful controls, zero denominators, recovery boundaries, tampered/missing evidence and provenance, and check report/README freshness plus byte-identical chart rerenders. CI runs the offline suite and a separate report freshness job; the original frozen workflow remains unchanged. Neither analysis nor its tests launches experiments.

## 12. AI and tools disclosure

Codex, using GPT-6.1 Sol, assisted with code, analysis and documentation under owner instructions. Python and the already locked Matplotlib render saved simulator data; no AI model produced benchmark responses. Generated findings are checked against raw records and frozen rules by offline tests. AI assistance is not independent peer review or evidence that the conclusions generalize.

## 13. Licence

[MIT](LICENSE). See the licence for permissions and warranty terms.
