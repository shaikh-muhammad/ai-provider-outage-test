"""Deterministic G9 derivatives. python -m bench.report [--check]; no experiments."""
import argparse
import json
import os
from pathlib import Path
import sys

from bench import analysis, charts

ROOT = analysis.ROOT
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "tmp/matplotlib"))
OUTPUTS = ("README.md", "docs/results.md", "docs/chart_data.json", "docs/retry-storm.svg", "docs/mechanism-ladder.svg")


def fmt(summary, precision=""):
    if summary["median"] is None:
        return "not observed / not applicable"
    return (f'{summary["median"]:{precision}} [{summary["min"]:{precision}}, {summary["max"]:{precision}}]'
            + (" †" if summary["high_spread"] else ""))


def table(headers, rows):
    return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"] +
                     ["| " + " | ".join(map(str, row)) + " |" for row in rows])


def findings(data):
    lines = []
    labels = dict(S0="extra provider attempts per planned request", S1="attempts per planned request",
                  S2="outage attempts per planned outage request", S3="duplicate provider executions")
    guards = dict(latency_p99_seconds="99th-percentile request latency", outside_outage_success_fraction="success outside the outage",
                  recovery_seconds="recovery time", extra_attempts="extra attempts", success_fraction="success fraction",
                  median_p99_seconds="median 99th-percentile latency", C4_zero_extras="zero extra attempts in every C4 run")
    for c in data["comparisons"]:
        if c["role"] != "primary":
            continue
        s, metric = c["scenario"], c["primary"]
        base, hardened = (data["cells"][s + "/" + config]["summary"][metric] for config in ("C0", "C4"))
        bad = sorted({guards[g["name"]] for g in c["guardrails"] if g["passed"] is False})
        lines.append(f'- **{s}: {c["verdict"]}.** C0 {fmt(base)} → C4 {fmt(hardened)} '
                     f'{labels[s]}.' + (" Failed guardrails: " + ", ".join(bad) + "." if bad else " All registered guardrails pass."))
    return "\n".join(lines)


def tradeoffs(data):
    def value(s, c, key):
        return data["cells"][s + "/" + c]["summary"][key]["median"]
    return (f'In S1, C0/C4 median p99 request latency is {value("S1", "C0", "latency_p99_seconds"):.3f}/'
            f'{value("S1", "C4", "latency_p99_seconds"):.3f} seconds. '
            f'In S2, success outside the outage is {100*value("S2", "C0", "outside_outage_success_fraction"):.2f}%/'
            f'{100*value("S2", "C4", "outside_outage_success_fraction"):.2f}%; median recovery is '
            f'{value("S2", "C0", "recovery_seconds")}/{value("S2", "C4", "recovery_seconds")} seconds after the outage ends. '
            'These costs matter even when fewer calls reach the provider. '
            'A p99 is the 99th-percentile time to a final request outcome; failed requests count too.')


def ladder(data):
    rows = []
    for s in ("S0", "S1", "S2", "S3"):
        primary = data["frozen_decision"]["metrics"][s]["primary"]
        row = [s, fmt(data["cells"][s + "/C0"]["summary"][primary])]
        for c in ("C1", "C2", "C3", "C4"):
            role = "primary" if s == "S0" else "stepwise"
            match = next((x for x in data["comparisons"] if x["scenario"] == s and x["candidate"] == c and x["role"] == role), None)
            row.append(match["verdict"] if match else "not registered")
        rows.append(row)
    return table(["Scenario", "C0 reference: primary median [min, max]", "C1 vs C0", "C2 vs C1", "C3 vs C2", "C4 vs C3*"], rows)


def timeline(data):
    traces = {}
    primary = data["frozen_decision"]["metrics"]["S2"]["primary"]
    n = max(len(r["timeline"]) for c in ("C0", "C4") for r in data["cells"]["S2/" + c]["runs"])
    for config in ("C0", "C4"):
        runs = data["cells"]["S2/" + config]["runs"]
        chosen = sorted(runs, key=lambda r: (r["metrics"][primary], r["round"]))[1]
        # Equal medians choose the earliest round, rather than implying a unique representative.
        chosen = min((r for r in runs if r["metrics"][primary] == chosen["metrics"][primary]), key=lambda r: r["round"])
        series = [r["timeline"] + [0] * (n - len(r["timeline"])) for r in runs]
        traces[config] = dict(round=chosen["round"], path=chosen["path"], sha256=chosen["sha256"],
            values=chosen["timeline"] + [0] * (n - len(chosen["timeline"])),
            min=[min(values) for values in zip(*series, strict=True)], max=[max(values) for values in zip(*series, strict=True)])
    return dict(scenario="S2", selection="median registered primary metric per config; earliest round breaks ties; lines may use different paired rounds",
                fault_window_seconds=[20, 35], bin_seconds=1, traces=traces)


chart_bytes = charts.chart_bytes


def results(data):
    parts = ["# G9: official simulator results",
        "I measured successful requests as well as calls reaching a local simulated provider. Fewer calls can mean failed requests. "
        "The registered safety checks therefore decide whether a protection helped: C4 is HARMFUL in S1/S2 and EFFECTIVE in S3. "
        "These are three-run simulator observations, not real-provider guarantees.\n\n"
        "[Outage timeline](retry-storm.svg) · [S1–S3 goals and safety checks](mechanism-ladder.svg) · "
        "[Readable case study](../README.md). Technical tables retain unrounded numeric values; "
        "[chart_data.json](chart_data.json) preserves per-run values, input hashes, formulas and verdicts.",
        findings(data), tradeoffs(data),
        "## Reading the measurements\n\nEach entry is median [minimum, maximum] of three VALID per-run metrics; no pooling. "
        "† flags relative spread >10%; a zero median with a nonzero range is also flagged. "
        "Ranges describe these three seeds and host scheduling, not confidence intervals or isolated environmental noise. "
        "Small n=3 limits precision. Full precision, per-round values, source paths/SHA-256 hashes and formulas are in [chart_data.json](chart_data.json).",
        "## Per-cell measurements\n\nSuccess is a fraction of all planned jobs (440 in S1, 600 otherwise). Latencies include failures and are seconds. "
        "Each scenario's primary metric and units are frozen in `bench/thresholds.yaml`."]
    columns = ("success_fraction", "latency_p50_seconds", "latency_p95_seconds", "latency_p99_seconds")
    parts.append(table(["Cell", "Registered primary", "Success fraction", "p50 s", "p95 s", "p99 s"],
        [[name, fmt(cell["summary"][data["frozen_decision"]["metrics"][name[:2]]["primary"]]),
          *[fmt(cell["summary"][key]) for key in columns]] for name, cell in data["cells"].items()]))
    columns = ("attempts_per_logical_request", "extra_attempts", "duplicate_provider_executions", "provider_executions", "loop_p99_seconds", "launch_p99_seconds")
    parts.append(table(["Cell", "Attempts/request", "Extra attempts", "Duplicate executions", "Executed work units", "Loop p99 s", "Launch p99 s"],
        [[name, *[fmt(cell["summary"][key]) for key in columns]] for name, cell in data["cells"].items()]))
    parts.append("Executed work units count actual provider executions, a simulated cost proxy only; deduplicated replies are not executions. "
                 "No price or money estimate is registered. S0 extras use per-job authoritative receipts, including zero-receipt jobs.")
    parts.append(table(["S2 cell", "Outside-outage success fraction", "Recovery s after outage end"],
        [[name, fmt(cell["summary"]["outside_outage_success_fraction"]), fmt(cell["summary"]["recovery_seconds"])]
         for name, cell in data["cells"].items() if name.startswith("S2/")]))
    parts.append(table(["S1 cell", "Burst arrivals / 140 planned burst jobs"],
        [[name, fmt(cell["summary"]["burst_attempts_per_planned_job"])] for name, cell in data["cells"].items() if name.startswith("S1/")]))
    parts.append(table(["Cell", "Nonzero final outcome counts: median [min, max]"],
        [[name, "; ".join(k.removeprefix("outcome_") + ": " + fmt(v) for k, v in cell["summary"].items()
                         if k.startswith("outcome_") and v["max"] > 0)] for name, cell in data["cells"].items()]))
    parts += ["## Paired decisions", "B=median(baseline), C=median(candidate), I=(B−C)/B, S=(max(baseline)−min(baseline))/B. "
        "Efficacy requires I≥20% and I>S with every registered guardrail passing; harm takes precedence. "
        "B=0 makes I and S null and uses raw values. S0 is a healthy control, never an efficacy win. "
        "S3 C4 also requires zero duplicates in every C4 run and positive paired C0 duplicates. "
        "Guardrails compare the same round/seed; only S0 latency compares the three-run p99 medians. "
        "All guardrail inputs, limits and pass/false/null results appear in chart_data.json."]
    parts.append(table(["Scenario", "Role", "Pair", "Verdict", "I", "S", "Failed guardrails (round)"],
        [[c["scenario"], c["role"], c["baseline"] + ":" + c["candidate"], c["verdict"],
          "null" if c["I"] is None else str(c["I"]), "null" if c["S"] is None else str(c["S"]),
          "; ".join(f'{g["name"]} ({g["round"] or "median"})' for g in c["guardrails"] if g["passed"] is False) or "none"]
         for c in data["comparisons"]]))
    parts += ["## Mechanism ladder", "![S1–S3 goals and safety checks across C0–C4, medians and three-run ranges](mechanism-ladder.svg)", ladder(data), "*S0 C4 compares with C0; C1–C3 are not registered for S0. "
              "Each step inherits earlier protections; effects cannot be attributed to isolated mechanisms without further experiments.",
              "## Retry timeline", "![Provider attempts per second during S2, C0 versus C4 with three-run ranges](retry-storm.svg)",
              "Lines select each config's median registered S2 primary run; ties use the earliest round. "
              "Representatives may use different rounds; paired verdicts still use all three matching rounds. "
              "Bands show per-second min–max across all three runs. The complete receipt-arrival horizon is shown, with a zero-based y axis. "
              "The shaded [20,35) second outage is registered; no traffic is rerun for this chart.",
              "## Recovery and accounting", "S2 checks whole seconds t=40..60. D counts all requests actually issued in [t−5,t); "
              "A counts those successful and completed by t. The earliest D>0 and 20A≥19D returns t−35 seconds. "
              "No qualifying window returns null; future completions and empty windows cannot establish recovery. "
              "Finite baseline and missing candidate recovery is harmful; missing baseline recovery is inconclusive unless another guard demonstrates harm.",
              "51 VALID results in 17 cells; 53 attempts across 51 slots. All 55 official artifacts reconcile with the COMPLETE manifest. "
              "Slot 30 S3/C4 round 2 preserves ABANDONED attempt 1 and VALID attempt 2 under the approved "
              "[shutdown recovery decision](recovery-decision-20261105.md). Slot 46 S3/C2 round 3 preserves INVALID attempt 1 "
              "(loop p99 107.938 ms; launch p99 107.356 ms, above 35/50 ms) and VALID attempt 2. Excluded attempts never enter statistics. "
              "Slots 1–29 keep original runner provenance; subsequent attempts use the approved recovery runner. "
              "The empty reservation has no receipts or historical capture-time hash anchor; the exception does not recover the lost execution.",
              "## Limits and reproduction", "This is a simulated provider on loopback, on a single shared machine, with only three runs per cell "
              "and limited fault/load profiles. Seeded policies do not guarantee identical arrival-dependent faults. "
              "S1's token bucket depends on arrival timing. The simulator explicitly supports idempotency; this proves no exactly-once "
              "guarantee for real AI providers. Outcomes may not transfer to real services. No significance or real-price claim is made.",
              "Run `source scripts/env.sh` then `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check`. "
              "Omit `--check` to regenerate these derivatives from saved evidence. Neither command starts a provider or benchmark."]
    return "\n\n".join(parts) + "\n"


def readme_results(data):
    rows = []
    units = dict(S0="Extra provider attempts / planned request", S1="Client attempts / planned request",
                 S2="Outage attempts / planned outage request", S3="Duplicate provider executions / run")
    for comparison in data["comparisons"]:
        if comparison["role"] != "primary":
            continue
        scenario, metric = comparison["scenario"], comparison["primary"]
        values = [fmt(data["cells"][scenario + "/" + config]["summary"][metric], ".3g") for config in ("C0", "C4")]
        rows.append([scenario, units[scenario], *values, comparison["verdict"]])
    return table(["Scenario", "Metric and unit", "C0 baseline", "C4 full ladder", "Registered verdict"], rows)


def readme_highlights(data):
    def pair(scenario, metric):
        values = [data["cells"][scenario + "/" + c]["summary"][metric]["median"] for c in ("C0", "C4")]
        return " → ".join(f"{value:.3g}" for value in values)
    return (f'**Lost replies: {pair("S3", "duplicate_provider_executions")} duplicate executions per run. '
            f'Rate-limit burst: {pair("S1", "latency_p99_seconds")} seconds p99 latency.**')


def readme_outage(data):
    def value(config, key):
        return data["cells"]["S2/" + config]["summary"][key]["median"]
    summary = data["cells"]["S2/C4"]["summary"]
    jobs = data["cells"]["S2/C4"]["runs"][0]["planned_requests"]
    recovery = next(g for c in data["comparisons"] if c["scenario"] == "S2" and c["role"] == "primary"
                    for g in c["guardrails"] if g["name"] == "recovery_seconds" and g["passed"] is False)
    return (f'In S2, C4 had **{fmt(summary["outcome_FAILED_FAST"], ".3g")} fail-fast outcomes per {jobs} jobs**. '
            'Fail-fast means returning a failure without further retries; some jobs had already made attempts. '
            'It is not a successful fallback. '
            f'Overall success fell **{100*value("C0", "success_fraction"):.3g}% → {100*value("C4", "success_fraction"):.3g}%**; '
            f'outside-outage success fell **{100*value("C0", "outside_outage_success_fraction"):.3g}% → '
            f'{100*value("C4", "outside_outage_success_fraction"):.3g}%**. '
            f'Median recovery after the outage was **{value("C0", "recovery_seconds"):.3g} → '
            f'{value("C4", "recovery_seconds"):.3g} seconds**, with C4 ranging '
            f'{summary["recovery_seconds"]["min"]:.3g}–{summary["recovery_seconds"]["max"]:.3g} seconds. '
            f'A median does not decide the safety check: round {recovery["round"]} took '
            f'{recovery["candidate"]:.3g} seconds, exceeding its {recovery["limit"]:.3g}-second limit.')


README = """# AI Provider Outage Test

I stopped duplicate work after lost replies, but the same protection stack made my rate-limit and outage results worse. I measured both the calls reaching my simulated provider and the requests that actually succeeded.

The figures compare my baseline with the full protection stack. **p99 latency** is the 99th-percentile time to a final outcome, including failures.

{highlights}

This is a local simulation on **127.0.0.1**, not a test of a commercial AI API. One planned job is a **request**; it can make several network **attempts**. **C0** is my baseline retry policy. **C4** includes all four protections below.

[Detailed measurements](docs/results.md) · [Source data](docs/chart_data.json) · [Engineering log](docs/engineering-log.md) · [Reproduce](#reproduce-the-work)

## What I observed

A **guardrail** is a safety condition that must pass alongside a lower call count. I fixed these rules before capture; HARMFUL takes precedence when a safety check fails. The **median** is the middle of three run values; brackets show the minimum and maximum.

{results_table}

Values are median [minimum, maximum] across three valid runs, rounded for reading. † flags more than 10% relative spread. The detailed report retains numeric precision. S0 is a healthy control, not an efficacy win.

## Why I built it

Retries can repeat useful work after a lost reply or add pressure during a failure. I wanted to see when deadlines, randomized retry waits, temporary call rejection and reusable job keys helped—and what successful work they cost.

## How it works

```mermaid
flowchart LR
    W["Fixed workload, seeds and failures"] --> C["Client C0–C4"]
    C --> P["Simulated provider · 127.0.0.1"]
    C --> R["Request outcomes and attempts"]
    P --> L["Receipts and executed work"]
    R --> V["Reconcile and validate"]
    L --> V
    V --> E["Preserved evidence"]
    E --> A["Read-only analysis and charts"]
```

I reconcile client outcomes with the provider's **ledger**, its record of received calls and executed jobs. A received retry is not necessarily new execution. The separate admin interface requires a per-run token; bodies, concurrency and run time are bounded.

## What I tested

I registered **17 cells**, with three paired runs per cell and workload/failure seeds **12001–12003**. S0 uses C0/C4; S1–S3 use C0–C4. Configurations share workload seeds and failure parameters within a round. Arrival-dependent rate limiting can still vary with timing.

| Scenario | Condition | Goal metric |
| --- | --- | --- |
| S0 | Healthy service | Extra provider attempts per planned request |
| S1 | Burst against a rate limit | Client attempts per planned request |
| S2 | Errors and hung calls during 20–35 s | Provider calls during the outage per planned outage request |
| S3 | Replies lost after work finishes | Duplicate provider executions per run |

I compare paired runs without pooling requests. An EFFECTIVE result needs at least 20% improvement, larger than the baseline's observed relative range, and every safety check passing. The [registration](bench/thresholds.yaml) defines denominators, limits and verdicts.

## The protections I added

| Configuration | Added protection | Meaning here |
| --- | --- | --- |
| C0 | Baseline retries | Up to four attempts, 5 s per attempt, fixed 0.5 s waits |
| C1 | Deadline | A 6 s total budget and at most 2 s per attempt |
| C2 | Backoff with jitter | Randomized waits; honor valid retry delays on rate-limit replies |
| C3 | Circuit breaker | Temporarily reject calls locally after enough relevant failures, then admit a probe |
| C4 | Idempotency key | Reuse a stable job key to replay a completed result without executing again |

Each step inherits earlier protections. These comparisons do not isolate a mechanism from interactions with the rest of the stack.

## What each step changed

![S2 provider calls per second: C0 baseline and C4 protections, shaded 20–35 s outage and three-run ranges](docs/retry-storm.svg)

Lines use each configuration's median registered outage-call metric run: C0 round 1 and C4 round 2. They are not a matched pair. Bands show per-second min–max across all three runs, not confidence intervals. Zero means no calls reached the provider, not that requests succeeded.

{outage}

![S1 rate-limit, S2 outage and S3 lost-reply goals and safety metrics across C0–C4, with paired-round failure labels](docs/mechanism-ladder.svg)

Dots are three-run medians; whiskers show min–max. Each scenario shows its goal and decisive safety checks; axes start at zero. Words and icons identify harm [!], effectiveness [+] or no measurable effect [=]. Labels report failed paired rounds even when rounded values look equal. [All comparisons](docs/results.md#paired-decisions) retain full inputs.

## What I learned

I would reject the full stack for these S1/S2 workloads under my registered rules. S1's worse p99 latency defeats the lower attempt count. In S2, fewer calls come with fewer successful jobs and slower recovery. In S3, C4 eliminates duplicate execution in every run while preserving success.

A production caller would need an explicit fallback policy: for example, a bounded queue, a clearly signalled unavailable response or an alternate service with separate safety and cost checks. **I did not implement or test any fallback.** Fast failure alone does not supply one.

## Failures and recovery

I preserved one health-invalid S3/C2 attempt and its valid retry. A shutdown left a zero-byte reservation with no receipts. The [recovery decision](docs/recovery-decision-20261105.md) retains it as ABANDONED attempt 1 and counts later attempts against the original cap.

The complete manifest accounts for **51 VALID runs, one INVALID and one ABANDONED attempt: 53 attempts and 55 official artifacts**. Excluded attempts never enter valid statistics. The [dated engineering log](docs/engineering-log.md) records setbacks and rationale. The lost run and historical capture-time integrity proof cannot be reconstructed.

## Limits of the evidence

This is a **simulated provider on one shared machine**, with **three runs per cell and limited fault/load profiles**. Ranges are descriptive, not confidence intervals or significance tests. Host scheduling and arrival-dependent rate limits affect outcomes.

The simulator explicitly supports idempotency; that proves no real-service exactly-once guarantee. Executed work is a cost proxy, with no monetary pricing model. **These results may not transfer to real AI providers.** I claim neither production availability nor independent audit approval.

## Reproduce the work

**Inspect:** read the [report](docs/results.md), [chart data](docs/chart_data.json), [registration](bench/registration.lock.json) and [official artifacts](results/official/). No capture is needed.

**Recompute:** use a full clone and Python 3.14. Activate with `source scripts/env.sh`; verify `which python` points into this project's `.venv`. For a new environment, install locked binary wheels with `python -m pip install --only-binary=:all: -r requirements-dev.lock`. Run `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check` to reconcile saved evidence and verify derivatives. Omitting `--check` regenerates them without launching a provider.

**Reproduce an experiment:** first agree separate capture authorization and a fresh destination under [AGENTS.md](AGENTS.md). Preserve registered workloads, seeds and failure schedules and existing official files. These instructions do not authorize overwriting evidence or calling a real provider.

## Testing

```sh
source scripts/env.sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m pytest -q --basetemp=/home/sm/Projects/.pytest-tmp-ai-provider-outage-test tests/
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check
python -m pip check
```

Tests independently recompute metrics and decisions, check negative and tamper cases, enforce security patterns, and verify deterministic charts and freshness. These checks launch no experiments.

## Provenance, tools and licence

The manifest preserves original and recovery runner identities. Source paths, SHA256 hashes, formulas and full-precision measurements are in the chart data. [bench/report.py](bench/report.py) generates this README; [bench/charts.py](bench/charts.py) renders the charts.

I used Codex with GPT-6.1 Sol for code, analysis and documentation, plus Python and locked Matplotlib. No AI model supplied benchmark responses. AI assistance and automated checks do not replace independent review. Project materials use the [MIT licence](LICENSE); dependencies retain their own licences.

## Terms used here

A **paired round** uses matching workload/failure seeds across configurations. **Recovery** is the first registered five-second window meeting the 95% success condition, checked at whole seconds after the outage. An empty window cannot establish recovery; no qualifying observation is recorded as missing.
"""


def render(data):
    data = dict(data, timeline=timeline(data), analysis_sources={name: analysis.official.digest(analysis.read(ROOT / name))
                     for name in ("bench/analysis.py", "bench/report.py", "bench/charts.py")})
    return dict(zip(OUTPUTS, [README.format(highlights=readme_highlights(data), results_table=readme_results(data),
        outage=readme_outage(data)).encode(), results(data).encode(),
        (json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(), chart_bytes(data["timeline"], data), charts.ladder_bytes(data)], strict=True))


def persist(outputs, *, check, root=ROOT):
    require = analysis.require
    require(set(outputs) == set(OUTPUTS), "unexpected report outputs")
    for name in OUTPUTS:
        path = root / name
        require(path.absolute() == path.resolve(), "symlink report output")
        if path.exists():
            require(path.is_file() and path.stat().st_nlink == 1, "unsafe report output")
        if check:
            require(path.exists() and path.read_bytes() == outputs[name], "stale report: " + name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(outputs[name])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify derivatives without writing")
    args = parser.parse_args(argv)
    try:
        persist(render(analysis.analyze()), check=args.check)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print("BLOCKED: " + str(error), file=sys.stderr)
        return 1
    print("G9 freshness PASS" if args.check else "G9 generated from 51 VALID saved runs; 53 attempts accounted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
