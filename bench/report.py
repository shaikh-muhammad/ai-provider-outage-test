"""Deterministic G9 derivatives. python -m bench.report [--check]; no experiments."""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import sys

from bench import analysis

ROOT = analysis.ROOT
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "tmp/matplotlib"))
OUTPUTS = ("README.md", "docs/results.md", "docs/chart_data.json", "docs/retry-storm.svg")


def fmt(summary):
    if summary["median"] is None:
        return "not observed / not applicable"
    return (f'{summary["median"]:.6g} [{summary["min"]:.6g}, {summary["max"]:.6g}]'
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


def figure(chart):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    fig = Figure(figsize=(10, 4.8), layout="constrained")
    ax = fig.subplots()
    ax.axvspan(*chart["fault_window_seconds"], color="#cccccc", alpha=.5, label="Registered outage [20, 35) s")
    for config, color, style in (("C0", "#0072B2", "-"), ("C4", "#D55E00", "--")):
        trace = chart["traces"][config]
        n = len(trace["values"])
        xs = list(range(n + 1))
        ax.fill_between(xs, trace["min"] + [trace["min"][-1]], trace["max"] + [trace["max"][-1]],
                        step="post", color=color, alpha=.16)
        ax.step(xs, trace["values"] + [trace["values"][-1]], where="post", color=color, linestyle=style,
                linewidth=1.7, label=f'{config}: representative median run, round {trace["round"]}')
    ax.set(xlim=(0, n), ylim=(0, max(max(t["max"]) for t in chart["traces"].values()) * 1.12),
           xlabel="Seconds from run start", ylabel="Provider attempts per 1-second bin",
           title="S2 outage: baseline C0 vs hardened C4")
    ax.grid(axis="y", alpha=.25)
    ax.legend(loc="upper right", fontsize=8)
    fig.supxlabel("Bands: per-second min–max across three runs; descriptive ranges, not confidence intervals.", fontsize=9)
    return fig


def chart_bytes(chart):
    import matplotlib
    with matplotlib.rc_context(dict(matplotlib.rcParamsDefault) | {"svg.hashsalt": "project3-g9", "font.family": "DejaVu Sans", "svg.fonttype": "none"}):
        output = BytesIO()
        figure(chart).savefig(output, format="svg", metadata={"Date": None, "Creator": "Project 3 G9",
            "Title": "S2 provider attempt timeline", "Description": "C0 solid blue, C4 dashed orange; range bands and registered outage shaded. Source data in chart_data.json."})
        return b"\n".join(line.rstrip() for line in output.getvalue().splitlines()) + b"\n"


def results(data):
    parts = ["# G9: official simulator results", findings(data), tradeoffs(data),
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
          "null" if c["I"] is None else f'{c["I"]:.6g}', "null" if c["S"] is None else f'{c["S"]:.6g}',
          "; ".join(f'{g["name"]} ({g["round"] or "median"})' for g in c["guardrails"] if g["passed"] is False) or "none"]
         for c in data["comparisons"]]))
    parts += ["## Mechanism ladder", ladder(data), "*S0 C4 compares with C0; C1–C3 are not registered for S0. "
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
        values = [fmt(data["cells"][scenario + "/" + config]["summary"][metric]) for config in ("C0", "C4")]
        rows.append([scenario, units[scenario], *values, comparison["verdict"]])
    return table(["Scenario", "Metric and unit", "C0 baseline", "C4 full ladder", "Registered verdict"], rows)


def readme_highlights(data):
    def pair(scenario, metric, precision):
        values = [data["cells"][scenario + "/" + c]["summary"][metric]["median"] for c in ("C0", "C4")]
        return " → ".join(f"{value:.{precision}f}" for value in values)
    return (f'**Lost replies: {pair("S3", "duplicate_provider_executions", 0)} duplicate executions per run. '
            f'Rate-limit burst: {pair("S1", "latency_p99_seconds", 3)} seconds p99 latency.**')


README = """# AI Provider Outage Test

**Deadlines, retry backoff, circuit breaking and idempotency — measured against a frozen local fault workload.**

I built a simulated provider to test a practical question: when does a more defensive client actually help? The full protection ladder eliminated duplicate executions after lost replies, but it was **HARMFUL** under the registered rate-limit and outage workloads. Fewer calls reached the provider; that alone did not make the client better.

{highlights}

[Measured report](docs/results.md) · [Engineering log](docs/engineering-log.md) · [Source data](docs/chart_data.json) · [Reproduce](#reproduce-the-work)

![S2 outage: baseline C0 and hardened C4 provider attempts per second, with three-run ranges](docs/retry-storm.svg)

The grey region is the registered outage, **[20, 35) seconds**. Blue is C0; dashed orange is C4. Each line represents its configuration's median primary-metric run; ties use the earliest round. Bands show per-second min–max across three runs, not confidence intervals. Representatives may use different rounds; decisions use all three paired rounds.

## Measured results

{results_table}

C0 is the baseline; C4 includes all four protections. Values are **median [min, max] across three VALID runs per cell**, computed per run before aggregation. † marks >10% relative spread. The table uses each scenario's frozen primary metric; an EFFECTIVE verdict also requires every applicable guardrail to pass.

{tradeoffs}

I retained these losses in the headline results. The registered rules give harm precedence over an apparent improvement. [All 17 cells and paired guardrails](docs/results.md#paired-decisions) remain available, including intermediate configurations.

## The problem I tested

Retrying a failed request can help one caller while adding pressure to a struggling service. A different problem occurs when work finishes but its reply disappears: retrying can execute the same job twice.

I used synthetic text jobs and scripted failures to separate those cases. Every request stays on **loopback (`127.0.0.1`)**. No real AI provider supplies responses, and these measurements make no claim about a commercial API.

## Architecture

```mermaid
flowchart LR
    R["Frozen workload, seeds and fault schedule"] --> H["Scheduler"]
    H --> C["Client C0–C4"]
    C --> P["Simulated provider · 127.0.0.1"]
    C --> Q["Logical-request records"]
    P --> L["Authoritative receipt and execution ledger"]
    H --> S["Raw timing and lifecycle samples"]
    Q --> V["Reconciliation and validity gate"]
    L --> V
    S --> V
    V --> E["Immutable official attempts and manifest"]
    E --> A["Offline analysis and report"]
```

The ledger records what reached the provider and what executed. Logical records capture the caller's final outcome, including failures. I reconcile the two before calculating results. The provider exposes a separate loopback admin interface protected by a per-run token; request bodies, concurrency and run time are bounded.

## Frozen experiment

I froze the [scenario design](bench/scenarios.yaml), [decision rules](bench/thresholds.yaml) and [registration hashes](bench/registration.lock.json) before official capture. The design has **17 cells**, three paired rounds and seeds **12001–12003**. S0 tests C0/C4; S1–S3 test C0–C4.

| Scenario | Scripted condition | Registered primary metric |
| --- | --- | --- |
| S0 · healthy control | Normal service | Extra provider attempts per planned request |
| S1 · rate-limit burst | Burst against a token bucket | Client attempts per planned request |
| S2 · outage | Errors and hung calls during [20, 35) s | Outage provider attempts per planned outage request |
| S3 · lost replies | Some replies disappear after execution | Duplicate provider executions |

The same workload seeds and failure parameters apply to each configuration in a round. Arrival-dependent rate limiting still varies with timing. I use nearest-rank latency percentiles, preserve paired guardrails and never pool requests across runs. A measured improvement needs to be at least 20% and exceed the baseline's relative range, with all guardrails passing. S0 is a control, not an efficacy win.

## The protection ladder

| Configuration | Added protection | Concrete policy |
| --- | --- | --- |
| C0 | Baseline retries | At most four attempts; 5 s attempt timeout; fixed 0.5 s retry wait |
| C1 | Deadlines | 6 s total budget; at most 2 s per attempt |
| C2 | Backoff with jitter | Randomized retry waits; honor valid Retry-After on rate-limit replies |
| C3 | Circuit breaker | Temporarily reject local calls after enough failures, then admit a probe |
| C4 | Idempotency keys | Reuse a stable job key so the simulator can replay completed work |

Each step inherits the earlier protections. The [frozen client semantics](bench/thresholds.yaml) specify retry eligibility, boundary conditions and breaker transitions. These are cumulative comparisons; they do not isolate a mechanism's effect from every interaction.

## Mechanism-by-mechanism results

{ladder}

*S0 C4 compares directly with C0; C1–C3 were not registered there. C0 is a reference, not a verdict. The [full report](docs/results.md#paired-decisions) also compares each registered candidate directly with C0. Only EFFECTIVE, NO MEASURABLE EFFECT, HARMFUL and INCONCLUSIVE are allowed.

## What I learned

**I would reject the full ladder for these S1/S2 workloads under the registered criteria.** S1's lower attempt count came with worse tail latency. In S2, suppressing traffic also suppressed useful requests outside the outage. The median recovery time alone hides a paired-round guardrail failure.

**I needed to count executions separately from retries.** In S3, idempotency removed duplicate work while retries still occurred. A successful final response does not establish that a job executed only once.

**I needed guardrails before seeing the outcome.** The outage chart makes C4's lower traffic look attractive. The paired success and recovery checks explain why that result is still HARMFUL. I report that tradeoff rather than treating quieter traffic as success.

## Failures and recovery

One S3/C2 attempt exceeded the frozen timing-health limits. I preserved it as INVALID and excluded it from statistics before its valid retry. The original batch also stopped during a shutdown, leaving slot 30's empty reservation without receipts.

The owner approved a narrow [recovery exception](docs/recovery-decision-20261105.md): retain the original reservation as ABANDONED attempt 1, preserve all completed records, and count later attempts against the existing cap. The final COMPLETE manifest accounts for **51 VALID runs, one INVALID and one ABANDONED attempt: 53 attempts and 55 official artifacts**. Neither excluded attempt is hidden or counted as a successful run.

The [engineering log](docs/engineering-log.md) also records rendering, CI and fresh-checkout metadata failures. The empty reservation cannot reconstruct the lost execution, and current hashes cannot prove historical immutability without a capture-time anchor.

## Limits of the evidence

This is a **simulated provider, on loopback, on one shared machine**, with **only three runs per cell and limited fault/load profiles**. There is **no guarantee these results transfer to real AI providers**. Three seeds and their ranges describe these observations; they are neither confidence intervals nor a significance test.

Host scheduling affects timings and arrival-dependent faults. Idempotency works because this simulator explicitly implements it; that establishes no exactly-once guarantee for another service. Executed work units are a simulated cost proxy, with no registered monetary price model. I have not claimed production availability, real-provider savings or independent audit approval.

## Reproduce the work

### Inspect the evidence

Read the [measured report](docs/results.md), [traceable chart data](docs/chart_data.json), [frozen registration](bench/registration.lock.json) and [official artifacts](results/official/). The chart data records source paths, SHA256 hashes, formulas and per-round values. This level needs no benchmark execution.

### Regenerate the report

Use a full clone and the project's Python 3.14 environment. Run `source scripts/env.sh`, then confirm `which python` points to this project's `.venv/bin/python`. For a new environment, install the pinned binary wheels with `python -m pip install --only-binary=:all: -r requirements-dev.lock`.

Run `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check` to verify saved outputs. Omit `--check` to regenerate the README, report, chart data and SVG from saved evidence. Neither command launches a provider.

### Reproduce an experiment

Fresh experimental capture needs separate authorization under [AGENTS.md](AGENTS.md), an agreed destination and the registered workload, seeds, policies and health gates. Existing official files must stay immutable; rerunning the official command in this checkout is not a clean replication. I have not rerun experiments for this case-study rewrite. Use the offline tests below to check implementation behavior without a capture.

## Testing

```sh
source scripts/env.sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m pytest -q --basetemp="$(dirname "$PROJECT_ROOT")/.pytest-tmp-ai-provider-outage-test" tests/
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check
python -m pip check
```

Tests independently recompute raw metrics and decisions, exercise harmful controls and zero denominators, reject tampered evidence and provenance, check recovery boundaries, and verify report/README freshness plus byte-identical chart rerenders. CI runs the offline suite and a separate freshness check. No experiment capture is part of those checks.

## Provenance, tools and licence

Official capture retains both the original runner identity and the approved recovery runner identity; the manifest and recovery sidecar link every attempt. Analysis recomputes from saved logical records and provider ledgers before rendering. This README is generated by [bench/report.py](bench/report.py); its source hash is recorded in the chart data.

I used Codex with GPT-6.1 Sol for code, analysis and documentation under owner instructions. Python and the already locked Matplotlib produce the report and chart. No AI model generated benchmark responses. Automated tests and AI assistance do not replace independent review.

Code and project materials are provided under the [MIT licence](LICENSE). Dependency licences remain those of their respective projects.

## Technical glossary

| Term | Meaning in this study |
| --- | --- |
| Logical request | One planned job, which may make several HTTP attempts or be rejected locally |
| Attempt | One physical client call; reconciled with a provider receipt |
| Amplification | Registered attempt count divided by the relevant planned-request count |
| p99 latency | Nearest-rank 99th-percentile time to a final outcome, including failures |
| Guardrail | A condition that must pass alongside a lower primary metric |
| Recovery | In S2, the first registered trailing window meeting the success threshold; details are in the report |
| Idempotency | Replaying a completed job's result instead of executing its work again |
"""


def render(data):
    data = dict(data, timeline=timeline(data), analysis_sources={name: analysis.official.digest(analysis.read(ROOT / name))
                     for name in ("bench/analysis.py", "bench/report.py")})
    return dict(zip(OUTPUTS, [README.format(highlights=readme_highlights(data), results_table=readme_results(data),
        tradeoffs=tradeoffs(data), ladder=ladder(data)).encode(), results(data).encode(),
        (json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(), chart_bytes(data["timeline"])], strict=True))


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
