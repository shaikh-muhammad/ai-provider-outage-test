"""Deterministic G9 derivatives. python -m bench.report [--check]; no experiments."""
import argparse
import json
import os
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
    parts = ["# G9: official simulator results", "## Start here\n\nA scripted local service was tested under four situations.\nRetries can repeat completed work or increase waiting.\nJob IDs removed repeated work after lost replies.\nRate limits and outages still produced worse safety outcomes.\nThese small-sample simulations do not establish real-provider guarantees.",
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
        rows.append([dict(S0="S0 — Healthy service", S1="S1 — Rate-limit burst", S2="S2 — Outage", S3="S3 — Lost replies")[scenario], units[scenario], *values, comparison["verdict"]])
    return table(["Situation", "Goal measured", "C0 baseline retries", "C4 full protection", "Verdict"], rows)


def readme_highlights(data):
    def pair(scenario, metric):
        values = [data["cells"][scenario + "/" + c]["summary"][metric]["median"] for c in ("C0", "C4")]
        return " → ".join(f"{value:.3g}" for value in values)
    return (f'- **Lost replies: {pair("S3", "duplicate_provider_executions")} duplicate executions per run** (repeated work).\n'
            f'- **Rate limiting: {pair("S1", "latency_p99_seconds")} seconds waiting time for the slowest 1% of outcomes.**\n'
            f'- **Outage success: {100*data["cells"]["S2/C0"]["summary"]["success_fraction"]["median"]:.3g}% → '
            f'{100*data["cells"]["S2/C4"]["summary"]["success_fraction"]["median"]:.3g}%.**')


def readme_outage(data):
    def value(config, key):
        return data["cells"]["S2/" + config]["summary"][key]["median"]
    summary = data["cells"]["S2/C4"]["summary"]
    jobs = data["cells"]["S2/C4"]["runs"][0]["planned_requests"]
    recovery = next(g for c in data["comparisons"] if c["scenario"] == "S2" and c["role"] == "primary"
                    for g in c["guardrails"] if g["name"] == "recovery_seconds" and g["passed"] is False)
    return (f'In S2 outage, C4 full protection had **{fmt(summary["outcome_FAILED_FAST"], ".3g")} fail-fast outcomes per {jobs} jobs**. '
            'Fail-fast means returning a failure without further retries; some jobs had already made attempts. '
            'It is not a successful fallback. '
            f'Overall success fell **{100*value("C0", "success_fraction"):.3g}% → {100*value("C4", "success_fraction"):.3g}%**; '
            f'outside-outage success fell **{100*value("C0", "outside_outage_success_fraction"):.3g}% → '
            f'{100*value("C4", "outside_outage_success_fraction"):.3g}%**. '
            f'Median recovery after the outage rose **{value("C0", "recovery_seconds"):.3g} → '
            f'{value("C4", "recovery_seconds"):.3g} seconds**, with C4 ranging '
            f'{summary["recovery_seconds"]["min"]:.3g}–{summary["recovery_seconds"]["max"]:.3g} seconds. '
            f'A median does not decide the safety check: round {recovery["round"]} took '
            f'{recovery["candidate"]:.3g} seconds, exceeding its {recovery["limit"]:.3g}-second limit.')


def readme_strength(data):
    labels = [charts.strength(c).split(" — ")[0] for c in data["comparisons"]
              if c["role"] == "stepwise" and c["verdict"] == "HARMFUL"]
    return (f'Of {len(labels)} HARMFUL comparisons between successive protection steps, '
            f'{labels.count("MARGINAL")} are MARGINAL and {labels.count("CLEAR")} CLEAR.')


HERO_ALT = "S2 outage: blue C0 baseline retries and dashed orange C4 full protection; calls, shaded outage and three-run ranges. Fewer calls came with lower success and slower recovery."
LADDER_ALT = "Six panels compare baseline retries through full protection: rate-limit calls and waiting time; outage calls and success outside the outage; lost-reply repeated work and extra calls. Ranges and verdicts are shown."
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

A controlled experiment on a question every team using AI services runs into: when a service fails, do the usual retry protections actually help — and what do they cost?

## Key findings

Software commonly retries failed calls. This project built a simulated AI service and tested retry protections in four scripted situations. The measurements track successful requests, waiting time, calls reaching the service and repeated work.

{highlights}

**Sending fewer retries is not the same as serving users better. Measure successful work.** This is a simulation with three runs per setup and situation. It does not demonstrate real-provider performance or production guarantees.

[Measured results](docs/results.md) · [Chart data](docs/chart_data.json) · [Engineering log](docs/engineering-log.md) · [Reproduction instructions](#how-to-reproduce)

## What was tested

- **S0 — Healthy service:** normal operation.
- **S1 — Rate-limit burst:** temporary “too many requests” replies.
- **S2 — Outage:** service unavailable from 20 to 35 seconds in the scripted run.
- **S3 — Lost replies:** work completes but its reply is lost; retries may repeat the work.

Five setups add protections cumulatively; each includes the protections above it:

- **C0 — Baseline retries:** up to 4 calls, a 5-second per-call limit and fixed half-second waits.
- **C1 — Adds a time limit:** 6 seconds overall and 2 seconds per call.
- **C2 — Adds smarter waiting:** progressively longer, randomized retry waits, honoring the service’s retry hint.
- **C3 — Adds a circuit breaker:** temporarily stop calls after repeated failures, then let one test call through.
- **C4 — Adds job IDs:** recognize repeated requests instead of performing completed work again.

Each registered setup/situation has three runs. Results show the middle value (**median**) and [minimum, maximum] observed range, without pooling requests across runs. Healthy service tests baseline retries and full protection; the other situations test all five setups. Matching runs share fixed workload and failure seeds.

## Results

{results_table}

Values use 2–3 significant figures; † flags more than 10% relative spread. A call is an attempt to complete a request (one planned job). The “slowest 1%” waiting measure, **p99**, is the 99th-percentile time to a final outcome, including failures. Full precision remains in the [detailed results](docs/results.md). **EFFECTIVE:** the main goal improves sufficiently without a safety measure getting worse. **HARMFUL:** a safety measure got worse, even where the main goal improved. **NO MEASURABLE EFFECT:** no sufficiently large change was observed. **INCONCLUSIVE:** the evidence does not decide. Exact [registered rules](bench/thresholds.yaml) also require improvement beyond observed baseline variation; healthy service is a control, never an efficacy win.

## Chart 1 — Outage: fewer calls, more failed requests

{hero}

Blue **C0 baseline retries** and dashed orange **C4 full protection** show calls reaching the service. Shading marks the scripted 20–35-second outage; bands show the three-run minimum and maximum, not confidence intervals. Lines represent each setup’s middle outage-call result: baseline retries round {c0_round}, full protection round {c4_round}. These are not a matched pair. Recovery summaries describe all runs, not events on those lines. Fewer calls during the outage came with more failed requests and slower recovery. Zero calls does not mean requests succeeded.

## Chart 2 — How each protection step performed

{ladder_chart}

Dots show middle values; whiskers show the observed three-run ranges. Numeric axes start at zero. Each protection adds to earlier ones, so these comparisons do not isolate individual mechanisms. The top row shows the goal; the bottom row shows a safety measure that can overturn an apparent improvement. Words and icons retain the registered verdicts.

## Three practical lessons

1. **Count successful work, not just traffic.** The rate-limit burst had fewer calls but longer waits. Lost-reply protection prevented repeated work.
2. **Read the cost of failing quickly alongside the savings.** {outage}
3. **Fast failure needs an alternative:** a clear error, queue or backup service. **No fallback was implemented or tested here.**

{strength_summary} MARGINAL means every failing safety check worsened by less than 5.0% after rounding relative changes to one decimal percentage point; otherwise CLEAR. These labels never change HARMFUL. The [complete comparison table](docs/results.md#verdict-strength) gives each failing check, run, values and change.

## How the experiment works

```mermaid
flowchart LR
    W["Fixed jobs and failures"] --> C["Client setup"]
    C --> P["Simulated service"]
    C --> R["Client records"]
    P --> L["Provider records"]
    R --> V["Validation"]
    L --> V
    V --> E["Saved evidence"]
    E --> A["Analysis and charts"]
```

The simulated service’s record (**ledger**) separates received calls from completed executions. A retry can receive a saved reply without performing the job again. Rules were fixed before official runs; [thresholds and decision rules](bench/thresholds.yaml) define measurements and safety limits. The design has {cells} registered setup/situation combinations and seeds {seed_min}–{seed_max}; a seed controls randomized choices, not host scheduling.

## Data quality

The saved record contains **{valid} valid official runs**. {invalid} invalid attempt failed timing checks, was retained and repeated; {abandoned} abandoned attempt was retained and excluded. There are {attempts} attempts and {artifacts} official artifacts in total. A VM shutdown left an empty reservation without receipts. Its initiator is unknown. Original approval records and historical capture-time proof remain unavailable; authenticated approval history is NOT VERIFIED. The [recovery decision](docs/recovery-decision-20261105.md) and [engineering log](docs/engineering-log.md) explain the retained records, exclusions and unavailable history.

## Limitations

- Simulated service, not a real AI provider; three runs per setup/situation and limited failure profiles.
- Timing variation, shared-machine scheduling and arrival-dependent failures affect outcomes even with fixed seeds.
- Job-ID effectiveness depends on the simulated provider’s implementation.
- Execution counts describe repeated work, not monetary costs.
- No production availability, real-service exactly-once or independent unconditional approval is established.

## How to reproduce

1. **Read:** inspect [saved results](docs/results.md), [chart data](docs/chart_data.json), [registration](bench/registration.lock.json) and [official evidence](results/official/).
2. **Recompute:** from the root of a full clone, create and activate a Python 3.14 environment, install pinned dependencies and check saved evidence. Verify `which python` points into `.venv` after activation; omit `--check` to regenerate reports and charts.

```sh
python3.14 -m venv .venv
source scripts/env.sh
python -m pip install --only-binary=:all: -r requirements-dev.lock
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check
```

3. **New experiments:** require separate approval, fresh output paths and preservation of official files, workloads and failure schedules. See [AGENTS.md](AGENTS.md); this does not authorize a real-provider call.

## Tests

```sh
source scripts/env.sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m pytest -q --basetemp="$(dirname "$PWD")/.pytest-tmp-ai-provider-outage-test" tests/
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python -m bench.report --check
python -m pip check
```

Verification covers independent recomputation, invalid/tampered evidence, risky calls, secrets, chart freshness and repeatable rendering. Tests launch no official experiments.

## Tools and credits

Built with OpenAI Codex assistance and Python / Matplotlib; owner-supplied tools were VS Code Codex, GPT-6.1 Sol. **No AI model generated benchmark responses.** The service is scripted. The owner reviewed the previous presentation. This revision awaits owner review; AI assistance and automated checks are not a substitute for independent review. [bench/report.py](bench/report.py) generates this README; [bench/charts.py](bench/charts.py) draws the charts. Project materials use the [MIT licence](LICENSE); dependencies retain their own licences.

<details>
<summary>Glossary</summary>

**Request/job:** one planned unit of work. **Attempt/call:** one network call made for that job. **p99:** time to a final outcome at the 99th percentile, including failures. **Circuit breaker:** temporarily blocks calls after repeated failures, then permits a test call. **Backoff/jitter:** progressively longer retry waits with random variation. **Job ID/idempotency:** reuse an identifier so the service can recognize completed work. **Fail-fast:** return a failure without further retries; earlier attempts may already have occurred. **Recovery time:** time after the outage until a registered five-second window reaches 95% success; an empty window cannot qualify. **Safety check:** a limit that must pass alongside the goal measurement. **Run/setup/scenario:** one collection, a cumulative client configuration, and a scripted situation. **Valid/invalid/abandoned:** accepted evidence, evidence rejected by fixed checks, or an interrupted attempt retained but excluded. **Seed:** a fixed input controlling random choices. **Provider record/ledger:** the service’s record of received calls and actual executions. **Marginal/clear:** the size labels defined above for failing checks; neither changes a registered verdict.

</details>
"""



def render(data):
    data = dict(data, timeline=timeline(data), analysis_sources={name: analysis.official.digest(analysis.read(ROOT / name))
                     for name in ("bench/analysis.py", "bench/report.py", "bench/charts.py")})
    text = README.format(highlights=readme_highlights(data), results_table=readme_results(data), outage=readme_outage(data),
        hero=picture("retry-storm", HERO_ALT, "docs/"), ladder_chart=picture("protection-ladder", LADDER_ALT, "docs/"),
        strength_summary=readme_strength(data), cells=len(data["cells"]),
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
