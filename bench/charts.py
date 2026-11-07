"""Deterministic presentation of saved metrics; no capture or new decision rules."""
from io import BytesIO
from statistics import median
import re
from xml.sax.saxutils import escape

from bench import analysis

COLORS = ("#0072B2", "#0072B2", "#0072B2", "#0072B2", "#A84400")
ICONS = {"HARMFUL": "[!]", "EFFECTIVE": "[+]", "NO MEASURABLE EFFECT": "[=]", "INCONCLUSIVE": "[?]"}
GUARDS = {"latency_p99_seconds": "p99", "outside_outage_success_fraction": "outside success",
          "recovery_seconds": "recovery", "success_fraction": "success", "extra_attempts": "extra attempts"}
PANELS = (
    ("S1", ("attempts_per_logical_request", "latency_p99_seconds", "success_fraction"),
     ("Rate limit: client attempts / job", "Safety: p99 time to outcome (s)", "Safety: successful jobs (%)")),
    ("S2", ("outage_provider_attempts_per_outage_issued_job", "outside_outage_success_fraction", "recovery_seconds"),
     ("Outage: provider attempts / outage job", "Safety: outside-outage success (%)", "Safety: recovery after outage (s)")),
    ("S3", ("duplicate_provider_executions", "success_fraction", "extra_attempts"),
     ("Lost replies: duplicate executions", "Safety: successful jobs (%)", "Safety: extra client attempts")),
)


def cost_caption(data):
    def value(config, metric):
        return data["cells"]["S2/" + config]["summary"][metric]["median"]
    guard = next(g for c in data["comparisons"] if c["scenario"] == "S2" and c["role"] == "primary"
                 for g in c["guardrails"] if g["name"] == "recovery_seconds" and g["passed"] is False)
    jobs = data["cells"]["S2/C4"]["runs"][0]["planned_requests"]
    return (f'Across three runs: success {100*value("C0", "success_fraction"):.3g}% → {100*value("C4", "success_fraction"):.3g}%; '
            f'outside-outage success {100*value("C0", "outside_outage_success_fraction"):.3g}% → '
            f'{100*value("C4", "outside_outage_success_fraction"):.3g}% (medians).\n'
            f'Recovery {value("C0", "recovery_seconds"):.3g} → {value("C4", "recovery_seconds"):.3g} s; '
            f'C4 fail-fast outcomes {value("C4", "outcome_FAILED_FAST"):.3g} per {jobs} jobs (medians).\n'
            f'C4 HARMFUL: outside-success checks fail; recovery round {guard["round"]} takes '
            f'{guard["candidate"]:.3g} s, exceeding its {guard["limit"]:.3g} s limit.')


def figure(chart, data):
    validate(data)
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    fig = Figure(figsize=(10, 6.5), layout="constrained")
    ax = fig.subplots()
    ax.axvspan(*chart["fault_window_seconds"], color="#E3E6EA", label="Outage: 20–35 s")
    for config, color, style, label, height in (
        ("C0", COLORS[0], "-", "C0 · baseline retries", .82),
        ("C4", COLORS[-1], "--", "C4 · all protections", .03),
    ):
        trace = chart["traces"][config]
        n = len(trace["values"])
        xs = list(range(n + 1))
        ax.fill_between(xs, trace["min"] + trace["min"][-1:], trace["max"] + trace["max"][-1:],
                        step="post", color=color, alpha=.16)
        ax.step(xs, trace["values"] + trace["values"][-1:], where="post", color=color,
                linestyle=style, linewidth=2)
        anchor = trace["values"].index(max(trace["values"])) if config == "C0" else min(n-1, chart["fault_window_seconds"][1]+5)
        ax.annotate(label + f'\nRepresentative round {trace["round"]}', xy=(anchor+.5, trace["values"][anchor]),
                    xytext=(.64 if config == "C0" else .02, height), textcoords="axes fraction", color=color, fontsize=13, weight="bold",
                    arrowprops=dict(arrowstyle="->", color=color), bbox=dict(facecolor="white", edgecolor="none", alpha=.9))
    ax.text(27.5, .95, "OUTAGE", transform=ax.get_xaxis_transform(), ha="center", va="top", fontsize=12)
    ax.set(xlim=(0, n), ylim=(0, max(max(t["max"]) for t in chart["traces"].values()) * 1.14),
           xlabel="Seconds from run start", ylabel="Calls reaching the provider per second",
           title="Fewer provider calls do not establish more successful requests")
    ax.grid(axis="y", alpha=.2)
    fig.supxlabel("Bands: three-run min–max, not confidence intervals. Lines are not a matched pair.\n" + cost_caption(data), fontsize=10)
    return fig


def validate(data):
    """Fail closed on edited verdicts, guardrails, pairing or plotted summaries."""
    analysis.require(len(data["comparisons"]) == 25 and len({(c["scenario"], c["role"], c["baseline"], c["candidate"])
                     for c in data["comparisons"]}) == 25, "chart comparison inventory mismatch")
    for comparison in data["comparisons"]:
        actual = analysis.comparison(comparison["scenario"], comparison["baseline"], comparison["candidate"],
                                     data["cells"], data["frozen_decision"])
        analysis.require(comparison == dict(role=comparison["role"], **actual), "chart guardrail mismatch")
    for scenario, metrics, _ in PANELS:
        for config in range(5):
            cell = data["cells"][f"{scenario}/C{config}"]
            analysis.require([r["round"] for r in cell["runs"]] == [1, 2, 3], "chart needs three paired runs")
            for metric in metrics:
                values = [r["metrics"][metric] for r in cell["runs"]]
                summary = cell["summary"][metric]
                analysis.require(values == summary["values"] and
                    (median(values), min(values), max(values)) == (summary["median"], summary["min"], summary["max"]),
                    "chart summary mismatch")


def guard_notes(data, scenario):
    notes = []
    for comparison in data["comparisons"]:
        if comparison["scenario"] != scenario or comparison["role"] != "stepwise":
            continue
        bad = {}
        for guard in comparison["guardrails"]:
            if guard["passed"] is False:
                bad.setdefault(GUARDS[guard["name"]], []).append(str(guard["round"]))
        detail = "; ".join(f'{name}: r{",".join(rounds)}' for name, rounds in bad.items()) or "no failed safety check"
        notes.append(f'{comparison["baseline"]} → {comparison["candidate"]} '
                     f'{ICONS[comparison["verdict"]]} {comparison["verdict"]}: {detail}')
    return "\n".join(notes)


def panel(ax, data, scenario, metric, title):
    scale = 100 if metric.endswith("success_fraction") else 1
    summaries = [data["cells"][f"{scenario}/C{i}"]["summary"][metric] for i in range(5)]
    for i, summary in enumerate(summaries):
        value, lo, hi = [scale * summary[key] for key in ("median", "min", "max")]
        ax.errorbar(i, value, yerr=[[value-lo], [hi-value]], fmt="o", color=COLORS[i], capsize=4,
                    markersize=6, linewidth=1.5, clip_on=False)
        ax.annotate(f"{value:.3g}", (i, hi), xytext=(0, 7), textcoords="offset points", ha="center", fontsize=11)
    ceiling = max(s["max"] * scale for s in summaries)
    ax.set(xticks=range(5), xticklabels=[f"C{i}" for i in range(5)], xlim=(-.5, 4.5),
           ylim=(0, max(ceiling * 1.3, 1)), title=title)
    if scale == 100:
        ax.set(ylim=(0, 125), yticks=[0, 25, 50, 75, 100])
    ax.grid(axis="y", alpha=.2)
    ax.set_axisbelow(True)


def ladder_figure(data):
    validate(data)
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    fig = Figure(figsize=(8, 24))
    grid = fig.add_gridspec(3, 1, left=.10, right=.98, bottom=.10, top=.94, hspace=.45)
    for row, (scenario, metrics, titles) in enumerate(PANELS):
        inner = grid[row].subgridspec(3, 1, hspace=.5)
        axes = [fig.add_subplot(inner[col]) for col in range(3)]
        for ax, metric, title in zip(axes, metrics, titles, strict=True):
            panel(ax, data, scenario, metric, title)
        primary = next(c for c in data["comparisons"] if c["scenario"] == scenario and c["role"] == "primary")
        bounds = grid[row].get_position(fig)
        fig.text(.5, bounds.y1+.02, f'{scenario} · C0 → C4 {ICONS[primary["verdict"]]} {primary["verdict"]}',
                 fontsize=15, weight="bold", ha="center")
        fig.text(.02, bounds.y0-.022, guard_notes(data, scenario), fontsize=9, va="top")
    fig.suptitle("Lower goal metrics can still fail safety checks\nC0–C4 cumulative protections · three runs per cell", fontsize=15, y=.995)
    fig.supxlabel("Dots: median; whiskers: min–max of three runs, not confidence intervals. All axes start at zero.\n"
                  "C0 retries · C1 deadline · C2 randomized backoff · C3 circuit breaker · C4 idempotency\n"
                  "Stepwise checks use full-precision paired runs, not rounded medians.\n"
                  "[!] harm · [+] effective · [=] no measurable effect", fontsize=10)
    return fig


def svg(fig, title, description):
    output = BytesIO()
    fig.savefig(output, format="svg", metadata={"Date": None, "Creator": "Project 3 saved-evidence report",
                "Title": title, "Description": description})
    text = output.getvalue().decode().replace('<svg ', '<svg role="img" aria-labelledby="chart-title chart-desc" ', 1)
    # Subfigure layout can hash sub-pixel bbox differences into otherwise identical
    # clipping IDs. Stable ordinal IDs preserve the emitted clipping geometry exactly.
    for i, identifier in enumerate(re.findall(r'<clipPath id="([^"]+)"', text)):
        text = text.replace(f'url(#{identifier})', f'url(#clip-{i})').replace(
            f'<clipPath id="{identifier}"', f'<clipPath id="clip-{i}"')
    end = text.index(">", text.index("<svg ")) + 1
    text = text[:end] + f'\n<title id="chart-title">{escape(title)}</title>\n<desc id="chart-desc">{escape(description)}</desc>' + text[end:]
    return "\n".join(line.rstrip() for line in text.splitlines()).encode() + b"\n"


def chart_bytes(chart, data):
    import matplotlib
    with matplotlib.rc_context(dict(matplotlib.rcParamsDefault) | {"svg.hashsalt": "project3-g9", "font.family": "DejaVu Sans", "svg.fonttype": "none"}):
        return svg(figure(chart, data), "S2 outage: calls reaching the provider", "C0 solid blue; C4 dashed orange. "
                   "Outage at 20–35 seconds. Bands are three-run min–max ranges. Lower call counts are not successful-request counts.")


def ladder_bytes(data):
    import matplotlib
    with matplotlib.rc_context(dict(matplotlib.rcParamsDefault) | {"svg.hashsalt": "project3-ladder", "font.family": "DejaVu Sans", "svg.fonttype": "none"}):
        return svg(ladder_figure(data), "S1–S3 cumulative protection ladder and safety checks", "C0–C4 goal and safety metrics: "
                   "medians and min–max across three runs. S1 and S2 C0/C4 verdicts are HARMFUL; S3 is EFFECTIVE. "
                   "Exclamation icons identify harm; plus identifies effective; equals identifies no measurable effect. "
                   "Read paired-round failure annotations; rounded medians do not determine verdicts.")
