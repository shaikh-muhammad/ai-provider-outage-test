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


README = """# AI Provider Outage Test

## 1. Pitch

A measured local simulator showing when retries, deadlines, a circuit breaker and idempotency help—or make things worse. Real official results, including failures, are below.

## 2. The problem, in plain language

An application can overload a struggling service by repeatedly asking again. It can also repeat work when a reply disappears after the work finishes. This project measures those tradeoffs against a simple baseline. All traffic stays on loopback (`127.0.0.1`); no real AI provider is called.

## 3. Actual measured results

{findings}

{tradeoffs}

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

{ladder}

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
"""


def render(data):
    data = dict(data, timeline=timeline(data), analysis_sources={name: analysis.official.digest(analysis.read(ROOT / name))
                     for name in ("bench/analysis.py", "bench/report.py")})
    return dict(zip(OUTPUTS, [README.format(findings=findings(data), tradeoffs=tradeoffs(data), ladder=ladder(data)).encode(), results(data).encode(),
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
