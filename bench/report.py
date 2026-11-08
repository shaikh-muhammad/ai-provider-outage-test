"""Deterministic G9 derivatives. python -m bench.report [--check]; no experiments."""
import argparse
import json
import os
from pathlib import Path
import sys

from bench import analysis, charts

ROOT = analysis.ROOT
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "tmp/matplotlib"))
SVG_NAMES = tuple(f"docs/{name}-{theme}.svg" for name in ("retry-storm", "protection-ladder") for theme in charts.THEMES)
OUTPUTS = ("README.md", "docs/results.md", "docs/chart_data.json", *SVG_NAMES)


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
        "The full protection stack eliminated duplicate work after lost replies, but failed the registered safety checks during rate limits and outages. "
        "Successful requests matter alongside calls reaching the local simulated provider. Fewer calls can mean failed requests. "
        "The registered safety checks therefore decide whether a protection helped: C4 is HARMFUL in S1/S2 and EFFECTIVE in S3. "
        "These are three-run simulator observations, not real-provider guarantees.\n\n"
        "[Outage timeline](#retry-timeline) · [S1–S3 goals and safety checks](#mechanism-ladder) · "
        "[Readable case study](../README.md). Technical tables retain unrounded numeric values; "
        "[chart_data.json](chart_data.json) preserves per-run values, input hashes, formulas and verdicts.",
        findings(data), tradeoffs(data),
        "## Reading the measurements\n\nEach entry is median [minimum, maximum] of three VALID per-run metrics; no pooling. "
        "† flags relative spread >10%; a zero median with a nonzero range is also flagged. "
        "Ranges describe these three seeds and host scheduling, not confidence intervals or isolated environmental noise. "
        "Small n=3 limits precision. Full precision, per-round values, source paths/SHA-256 hashes and formulas are in [chart_data.json](chart_data.json).",
        "## Per-cell measurements\n\nSuccess is a fraction of all planned jobs (440 in S1, 600 otherwise). Latencies include failures and are seconds. "
        "Each scenario's primary metric (goal measurement) and units are frozen in `bench/thresholds.yaml`. "
        "Success fraction means successful jobs divided by planned jobs; p50/p95/p99 are percentile times to a final outcome. "
        "Loop and launch lag measure delays in the event loop and request scheduling."]
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
    parts += ["## Verdict strength", strength_table(data, precision=""), STRENGTH_NOTE,
              "## Mechanism ladder", picture("protection-ladder", LADDER_ALT), ladder(data), "*S0 C4 compares with C0; C1–C3 are not registered for S0. "
              "Each step inherits earlier protections; effects cannot be attributed to isolated mechanisms without further experiments.",
              "## Retry timeline", picture("retry-storm", HERO_ALT),
              "Lines select each config's median registered S2 primary run; ties use the earliest round. "
              "Representatives may use different rounds; paired verdicts still use all three matching rounds. "
              "Bands show per-second min–max across all three runs. The complete receipt-arrival horizon is shown, with a zero-based y axis. "
              "The shaded [20,35) second outage is registered; no traffic is rerun for this chart.",
              "## Recovery and accounting", "S2 checks whole seconds t=40..60. D counts all requests actually issued in [t−5,t); "
              "A counts those successful and completed by t. The earliest D>0 and 20A≥19D returns t−35 seconds. "
              "No qualifying window returns null; future completions and empty windows cannot establish recovery. "
              "Finite baseline and missing candidate recovery is harmful; missing baseline recovery is inconclusive unless another guard demonstrates harm.",
              "51 VALID results in 17 cells; 53 attempts across 51 slots. All 55 official artifacts reconcile with the COMPLETE manifest. "
              "Slot 30 S3/C4 round 2 preserves ABANDONED attempt 1 and VALID attempt 2 under the recorded "
              "[shutdown recovery decision](recovery-decision-20261105.md). Slot 46 S3/C2 round 3 preserves INVALID attempt 1 "
              "(loop p99 107.938 ms; launch p99 107.356 ms, above 35/50 ms) and VALID attempt 2. Excluded attempts never enter statistics. "
              "Slots 1–29 keep original runner provenance; subsequent attempts use the recorded recovery runner. "
              "Original registration/recovery approval locations are unknown; authenticated approval history is NOT VERIFIED. "
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
    return (f'- **Lost replies: {pair("S3", "duplicate_provider_executions")} duplicate executions per run.**\n'
            f'- **Rate-limit burst: {pair("S1", "latency_p99_seconds")} seconds p99 latency.**\n'
            f'- **Outage success: {100*data["cells"]["S2/C0"]["summary"]["success_fraction"]["median"]:.3g}% → '
            f'{100*data["cells"]["S2/C4"]["summary"]["success_fraction"]["median"]:.3g}%.**')


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


HERO_ALT = "S2 outage: blue C0 and dashed orange C4 provider calls, shaded 20–35 second outage and three-round ranges; fewer calls came with lower success and slower recovery."
LADDER_ALT = "Six panels compare C0–C4 goals and safety: S1 attempts and p99 latency, S2 outage calls and outside-outage success, S3 duplicate executions and extra attempts; ranges and registered verdicts are shown."
STRENGTH_NOTE = ("MARGINAL means every failing safety check worsened by less than 5.0% after rounding its relative change to one decimal percentage point; otherwise CLEAR. "
                 "The worst failing paired-round check is shown. A relative decrease in success is not a percentage-point decrease. "
                 "These descriptions do not change HARMFUL or any registered threshold. EFFECTIVE, NO MEASURABLE EFFECT and INCONCLUSIVE retain their registered meanings.")


def picture(name, alt, prefix=""):
    return (f'<picture>\n<source media="(prefers-color-scheme: dark)" srcset="{prefix}{name}-dark.svg">\n'
            f'<source media="(prefers-color-scheme: light)" srcset="{prefix}{name}-light.svg">\n'
            f'<img src="{prefix}{name}-light.svg" alt="{alt}" width="100%">\n</picture>')


def strength_table(data, precision=".3g"):
    return table(["Scenario", "Protection step", "Registered verdict", "Worst failing safety check and size"],
        [[c["scenario"], c["baseline"] + " → " + c["candidate"], c["verdict"], charts.strength(c, precision)]
         for c in data["comparisons"] if c["role"] == "stepwise"])


README = """# AI Provider Outage Test

## Findings

Retry protections can prevent duplicate work while making other failures worse. In this local simulator, the full stack eliminated repeated execution after lost replies, but reduced successful work during an outage and increased delay during a rate-limit burst. Lower provider traffic alone did not establish a better service.

{highlights}

[Detailed measurements](docs/results.md) · [Source data](docs/chart_data.json) · [Engineering log](docs/engineering-log.md) · [Reproduce](#reproduction)

## Outage: fewer calls, more failed requests

{hero}

Lines use each configuration's median outage-call metric run: C0 round {c0_round} and C4 round {c4_round}. They are not a matched pair. Bands show per-second min–max across all three rounds. Zero means no calls reached the provider, not that requests succeeded. Recovery summaries describe all three runs, not events on the representative lines.

## Essential terms

**Request:** one planned job; an **attempt** is one network call made for that job.<br>
**C0:** baseline retries; **C4:** the complete, cumulative protection stack.<br>
**p99 latency:** the 99th-percentile time to a final outcome, including failures.<br>
**Median and range:** the middle run value, followed by [minimum, maximum].<br>
**Guardrail:** a safety check that must pass alongside an improved goal metric.<br>
**Recovery:** the first registered five-second window reaching 95% success after the outage; an empty window cannot qualify.

## Results across four failure conditions

{results_table}

S0 is healthy service; S1 is a rate-limit burst; S2 combines errors and hung calls during 20–35 seconds; S3 loses replies after work finishes. Values are median [minimum, maximum] across three VALID runs, rounded to 2–3 significant figures. † flags more than 10% relative spread. The detailed report retains full precision. S0 is a control, not an efficacy win.

## How the protection ladder performed

{ladder_chart}

Dots show medians and whiskers min–max across three runs. All numeric axes start at zero. Each step inherits earlier protections; the comparisons do not isolate individual mechanisms. Words and icons identify the registered verdicts; the table below explains the size of failing checks.

## Three practical lessons

- **Judge successful work, not traffic alone.** S1's worse p99 latency defeats its lower attempt count. S3 shows the useful case: stable job keys eliminate duplicate execution while preserving success.
- **Read outage costs alongside load reduction.** {outage}
- **Fast failure needs a product decision.** A failure without further retries can protect capacity, but it is not a successful fallback. A production caller would need an explicit policy, such as a bounded queue, a clearly signalled unavailable response or an alternate service with separate safety and cost checks. **No fallback was implemented or tested.**

## Problem and architecture

A lost reply can hide completed work, so retrying may execute it twice. During service failures, retries can also add load and delay the caller. This project measures both effects using a local simulated provider on **127.0.0.1**.

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

Client outcomes reconcile with the provider's **ledger**, its record of received calls and executed jobs. A received retry is not necessarily a new execution. The separate admin interface requires a per-run token; bodies, concurrency and run time are bounded.

The registered design has **{cells} cells**, three paired runs per cell and workload/failure seeds **{seed_min}–{seed_max}**. S0 uses C0/C4; S1–S3 use C0–C4. Configurations share workload seeds and failure parameters within each round. Requests are never pooled across rounds. An EFFECTIVE result requires at least 20% improvement, larger than the baseline's observed relative range, with every safety check passing. The [registration](bench/thresholds.yaml) defines denominators, limits and verdicts.

## C0–C4 protections

| Configuration | Added protection | Meaning here |
| --- | --- | --- |
| C0 | Baseline retries | Up to four attempts, 5 s per attempt, fixed 0.5 s waits |
| C1 | Deadline | A 6 s total budget and at most 2 s per attempt |
| C2 | Backoff with jitter | Randomized waits; honor valid retry delays on rate-limit replies |
| C3 | Circuit breaker | Temporarily reject calls locally after enough relevant failures, then admit a probe |
| C4 | Idempotency key | Reuse a stable job key to replay a completed result without executing again |

## Stepwise verdict strength

{strength_table}

{strength_note}

## Failures and recovery

One health-invalid S3/C2 attempt and its valid retry remain preserved. A shutdown left a zero-byte reservation with no receipts. The [recovery decision](docs/recovery-decision-20261105.md) retains it as ABANDONED attempt 1 and counts later attempts against the original cap. The shutdown initiator remains unknown; the owner states, “I did not shut down the VM.” Original registration/recovery approval locations and original G1/G7 decision records are unavailable; authenticated approval history is **NOT VERIFIED**.

The complete manifest accounts for **{valid} VALID runs, {invalid} INVALID and {abandoned} ABANDONED attempt: {attempts} attempts and {artifacts} official artifacts**. Excluded attempts never enter valid statistics. The [dated engineering log](docs/engineering-log.md) preserves setbacks and retrospective rationale. The lost run and historical capture-time integrity proof cannot be reconstructed.

## Limitations

This is a **simulated provider on one shared machine**, with **three runs per cell and limited fault/load profiles**. Ranges are descriptive, not confidence intervals or significance tests. Host scheduling and arrival-dependent rate limits affect outcomes even with seeded policies.

The simulator explicitly supports idempotency; that proves no real-service exactly-once guarantee. Executed work is a cost proxy, with no monetary pricing model. **These results may not transfer to real AI providers.** They establish neither production availability nor independent audit approval.

## Reproduction

1. **Inspect:** read the [report](docs/results.md), [chart data](docs/chart_data.json), [registration](bench/registration.lock.json) and [official artifacts](results/official/). No capture is needed.
2. **Recompute:** use a full clone and Python 3.14. Run `source scripts/env.sh`; verify `which python` points into this project's `.venv`. Install locked binary wheels with `python -m pip install --only-binary=:all: -r requirements-dev.lock`. Run `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check` to reconcile saved evidence and verify derivatives. Omitting `--check` regenerates them without launching a provider.
3. **Reproduce an experiment:** agree separate capture authorization and a fresh destination under [AGENTS.md](AGENTS.md). Preserve registered workloads, seeds, failure schedules and existing official files. These instructions do not authorize overwriting evidence or calling a real provider.

## Testing, tools and provenance

```sh
source scripts/env.sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m pytest -q --basetemp=/home/sm/Projects/.pytest-tmp-ai-provider-outage-test tests/
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check
python -m pip check
```

Tests independently recompute metrics and decisions, check negative and tamper cases, enforce security patterns, and verify deterministic charts and freshness. These checks launch no experiments.

The manifest preserves original and recovery runner identities. Source paths, SHA-256 hashes, formulas and full-precision measurements are in the chart data. [bench/report.py](bench/report.py) generates this README; [bench/charts.py](bench/charts.py) renders the charts.

Tools: **VS Code Codex, GPT-6.1 Sol**, Python and locked Matplotlib. **No AI model generated benchmark responses.** AI assistance and automated checks do not replace independent review. Project materials use the [MIT licence](LICENSE); dependencies retain their own licences.
"""


def render(data):
    data = dict(data, timeline=timeline(data), analysis_sources={name: analysis.official.digest(analysis.read(ROOT / name))
                     for name in ("bench/analysis.py", "bench/report.py", "bench/charts.py")})
    text = README.format(highlights=readme_highlights(data), results_table=readme_results(data), outage=readme_outage(data),
        hero=picture("retry-storm", HERO_ALT, "docs/"), ladder_chart=picture("protection-ladder", LADDER_ALT, "docs/"),
        strength_table=strength_table(data), strength_note=STRENGTH_NOTE, cells=len(data["cells"]),
        seed_min=min(r["seed"] for c in data["cells"].values() for r in c["runs"]),
        seed_max=max(r["seed"] for c in data["cells"].values() for r in c["runs"]),
        c0_round=data["timeline"]["traces"]["C0"]["round"], c4_round=data["timeline"]["traces"]["C4"]["round"],
        valid=data["valid_runs"], invalid=sum(r["status"] == "INVALID" for r in data["excluded"]),
        abandoned=sum(r["status"] == "ABANDONED" for r in data["excluded"]), attempts=data["total_attempts"], artifacts=len(data["inputs"]))
    images = [chart_bytes(data["timeline"], data, theme) for theme in charts.THEMES] + [charts.ladder_bytes(data, theme) for theme in charts.THEMES]
    return dict(zip(OUTPUTS, [text.encode(), results(data).encode(),
        (json.dumps(data, sort_keys=True, indent=2, allow_nan=False) + "\n").encode(), *images], strict=True))



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
