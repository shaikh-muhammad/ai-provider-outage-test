"""Deterministic presentation of saved metrics; registered decisions stay unchanged."""
from io import BytesIO
import math
import re
from textwrap import fill
from xml.sax.saxutils import escape

from bench import analysis

THEMES = {
    "light": dict(bg="#FFFFFF", fg="#202B3C", grid="#CDD5DF", c0="#0072B2", c4="#A84400", band="#E3E8EF"),
    "dark": dict(bg="#111923", fg="#EDF2F8", grid="#526173", c0="#63B9F2", c4="#FFB16C", band="#354252"),
}
ICONS = {"HARMFUL": "[!]", "EFFECTIVE": "[+]", "NO MEASURABLE EFFECT": "[=]", "INCONCLUSIVE": "[?]"}
GUARDS = {"latency_p99_seconds": "p99", "outside_outage_success_fraction": "outside success",
          "recovery_seconds": "recovery", "success_fraction": "success", "extra_attempts": "extra attempts"}
PANELS = (
    ("S1", ("attempts_per_logical_request", "latency_p99_seconds"), ("Calls per planned request", "Safety: slowest 1% wait (seconds)")),
    ("S2", ("outage_provider_attempts_per_outage_issued_job", "outside_outage_success_fraction"),
     ("Calls per outage request", "Safety: success outside outage (%)")),
    ("S3", ("duplicate_provider_executions", "extra_attempts"), ("Repeated work per run", "Safety: extra calls per run")),
)


def strength(comparison, precision=".3g"):
    """Describe harm size, never change a registered verdict or its inputs."""
    if comparison["verdict"] != "HARMFUL":
        return "—"
    failures = [g for g in comparison["guardrails"] if g["passed"] is False]
    def change(g):
        b, c = g["baseline"], g["candidate"]
        return abs(c - b) / abs(b) * 100 if b and c is not None else math.inf
    worst = max(failures, key=change)
    label = "MARGINAL" if all(round(change(g), 1) < 5 for g in failures) else "CLEAR"
    b, c = worst["baseline"], worst["candidate"]
    fraction = worst["name"].endswith("success_fraction")
    def value(v):
        return "not observed" if v is None else f'{v * (100 if fraction else 1):{precision}}' + ("%" if fraction else "")
    direction = "decreased" if c is not None and c < b else "increased"
    amount = f'{change(worst):.1f}%' if math.isfinite(change(worst)) else "undefined relative change"
    name = dict(latency_p99_seconds="p99 latency (seconds)", outside_outage_success_fraction="outside-outage success",
                recovery_seconds="recovery (seconds)", success_fraction="success", extra_attempts="extra attempts")[worst["name"]]
    return f'{label} — {name}, round {worst["round"]}: {value(b)} → {value(c)}; {direction} {amount}'


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


def figure(chart, data, theme="light"):
    validate(data)
    from matplotlib.figure import Figure
    colors = THEMES[theme]
    fig = Figure(figsize=(12, 7.4))
    ax = fig.subplots()
    fig.subplots_adjust(left=.085, right=.975, bottom=.30, top=.88)
    ax.axvspan(*chart["fault_window_seconds"], color=colors["band"])
    for config, style, label, left, height in (("C0", "-", "baseline retries", .56, .98), ("C4", "--", "all protections", .73, .58)):
        color = colors[config.lower()]
        trace = chart["traces"][config]
        n = len(trace["values"])
        xs = list(range(n + 1))
        ax.fill_between(xs, trace["min"] + trace["min"][-1:], trace["max"] + trace["max"][-1:], step="post", color=color, alpha=.18)
        ax.step(xs, trace["values"] + trace["values"][-1:], where="post", color=color, linestyle=style, linewidth=2)
        anchor = trace["values"].index(max(trace["values"])) if config == "C0" else min(n-1, chart["fault_window_seconds"][1]+5)
        calls = data["cells"]["S2/" + config]["summary"]["outage_provider_attempts_per_outage_issued_job"]["median"]
        ax.annotate(f'{config} · {label}\nRound {trace["round"]} · {calls:.3g} outage calls\nper outage request (median)',
                    xy=(anchor+.5, trace["values"][anchor]), xytext=(left, height), textcoords="axes fraction",
                    color=color, fontsize=11, weight="bold", va="top", arrowprops=dict(arrowstyle="->", color=color),
                    bbox=dict(facecolor=colors["bg"], edgecolor="none", alpha=.95))
    ax.text(sum(chart["fault_window_seconds"])/2, .95, "OUTAGE · 20–35 s", transform=ax.get_xaxis_transform(), ha="center", va="top")
    ax.set(xlim=(0, n), ylim=(0, max(max(t["max"]) for t in chart["traces"].values()) * 1.17),
           xlabel="Seconds from run start", ylabel="Calls reaching the provider / second")
    ax.grid(axis="y", alpha=.35)
    fig.suptitle("Fewer provider calls came with fewer successful requests", fontsize=17, weight="bold", y=.97)
    fig.text(.5, .16, "Bands: three-round min–max, not confidence intervals. Representative lines are not a matched pair.\n" +
             cost_caption(data) + "\nRecovery summaries are not event markers on these representative traces.", ha="center", va="center", fontsize=10)
    return fig


def validate(data):
    """Fail closed on edited verdicts, guardrails, pairing or plotted summaries."""
    analysis.require(len(data["comparisons"]) == 25 and len({(c["scenario"], c["role"], c["baseline"], c["candidate"])
                     for c in data["comparisons"]}) == 25, "chart comparison inventory mismatch")
    for comparison in data["comparisons"]:
        actual = analysis.comparison(comparison["scenario"], comparison["baseline"], comparison["candidate"], data["cells"], data["frozen_decision"])
        analysis.require(comparison == dict(role=comparison["role"], **actual), "chart guardrail mismatch")
    for scenario, metrics, _ in PANELS:
        for config in range(5):
            cell = data["cells"][f"{scenario}/C{config}"]
            analysis.require([r["round"] for r in cell["runs"]] == [1, 2, 3], "chart needs three paired runs")
            for metric in set(metrics) | {"success_fraction", "recovery_seconds", "extra_attempts"}:
                values = [r["metrics"][metric] for r in cell["runs"]]
                analysis.require(cell["summary"][metric] == analysis.spread(values), "chart summary mismatch")


def guard_notes(data, scenario):
    notes = []
    for c in data["comparisons"]:
        if c["scenario"] != scenario or c["role"] != "stepwise":
            continue
        bad = {}
        for g in c["guardrails"]:
            if g["passed"] is False:
                bad.setdefault(GUARDS[g["name"]], []).append(str(g["round"]))
        detail = "; ".join(f'{name}: r{",".join(rounds)}' for name, rounds in bad.items())
        notes.append(f'{c["baseline"]} → {c["candidate"]} {ICONS[c["verdict"]]} {c["verdict"]}' +
                     ("\n" + fill(strength(c), 52) + "\n" + detail if bad else ": no failed safety check"))
    return "\n".join(notes)


def panel(ax, data, scenario, metric, title, theme="light"):
    scale = 100 if metric.endswith("success_fraction") else 1
    summaries = [data["cells"][f"{scenario}/C{i}"]["summary"][metric] for i in range(5)]
    for i, summary in enumerate(summaries):
        value, lo, hi = [scale * summary[key] for key in ("median", "min", "max")]
        color = THEMES[theme]["c4" if i == 4 else "c0"]
        ax.errorbar(i, value, yerr=[[value-lo], [hi-value]], fmt="o", color=color, capsize=4, markersize=6, linewidth=1.5, clip_on=False)
        ax.annotate(f"{value:.3g}", (i, hi), xytext=(0, 8), textcoords="offset points", ha="center", fontsize=10)
    ceiling = max(s["max"] * scale for s in summaries)
    ax.set(xticks=range(5), xticklabels=[f"C{i}" for i in range(5)], xlim=(-.5, 4.5), ylim=(0, max(ceiling * 1.28, 1)), title=title)
    if scale == 100:
        ax.set(ylim=(0, 125), yticks=[0, 25, 50, 75, 100])
    ax.grid(axis="y", alpha=.35)
    ax.set_axisbelow(True)


def ladder_figure(data, theme="light"):
    validate(data)
    from matplotlib.figure import Figure
    fig = Figure(figsize=(15, 12))
    grid = fig.add_gridspec(2, 3, left=.06, right=.98, bottom=.48, top=.89, hspace=.45, wspace=.27)
    for col, (scenario, metrics, titles) in enumerate(PANELS):
        for row, (metric, title) in enumerate(zip(metrics, titles, strict=True)):
            panel(fig.add_subplot(grid[row, col]), data, scenario, metric, title, theme)
        primary = next(c for c in data["comparisons"] if c["scenario"] == scenario and c["role"] == "primary")
        bounds = grid[0, col].get_position(fig)
        fig.text((bounds.x0+bounds.x1)/2, .94, f'{dict(S1="S1 · Rate-limit burst", S2="S2 · Outage", S3="S3 · Lost replies")[scenario]}\nC0 → C4 {ICONS[primary["verdict"]]} {primary["verdict"]}', ha="center", fontsize=14, weight="bold")
        fig.text(bounds.x0, .42, guard_notes(data, scenario), fontsize=8.5, va="top", linespacing=1.4)
    fig.text(.5, .025, "Dots: median; whiskers: min–max of three runs, not confidence intervals. Numeric axes start at zero.\n"
             "C0 baseline retries · C1 + time limit · C2 + smarter waiting · C3 + circuit breaker · C4 + job ID\n"
             "Stepwise labels describe safety-check size; every registered HARMFUL verdict still applies.\n"
             "S2 recovery and all remaining safety checks are retained in the report and paired-round notes.", ha="center", fontsize=10)
    return fig


def svg(fig, title, description):
    output = BytesIO()
    fig.savefig(output, format="svg", metadata={"Date": None, "Creator": "Project 3 saved-evidence report", "Title": title, "Description": description})
    text = output.getvalue().decode().replace('<svg ', '<svg role="img" aria-labelledby="chart-title chart-desc" ', 1)
    for i, identifier in enumerate(re.findall(r'<clipPath id="([^"]+)"', text)):
        text = text.replace(f'url(#{identifier})', f'url(#clip-{i})').replace(f'<clipPath id="{identifier}"', f'<clipPath id="clip-{i}"')
    end = text.index(">", text.index("<svg ")) + 1
    text = text[:end] + f'\n<title id="chart-title">{escape(title)}</title>\n<desc id="chart-desc">{escape(description)}</desc>' + text[end:]
    return "\n".join(line.rstrip() for line in text.splitlines()).encode() + b"\n"


def drawing(theme):
    import matplotlib
    c = THEMES[theme]
    return matplotlib.rc_context(dict(matplotlib.rcParamsDefault) | {"svg.hashsalt": "project3-v2", "font.family": "DejaVu Sans",
        "svg.fonttype": "path", "figure.facecolor": c["bg"], "axes.facecolor": c["bg"], "text.color": c["fg"],
        "axes.labelcolor": c["fg"], "axes.edgecolor": c["grid"], "xtick.color": c["fg"], "ytick.color": c["fg"], "grid.color": c["grid"]})


def chart_bytes(chart, data, theme="light"):
    with drawing(theme):
        return svg(figure(chart, data, theme), "S2 outage: calls reaching the provider", "C0 solid blue; C4 dashed orange. "
                   "Outage at 20–35 seconds; bands are three-round min–max ranges. Lower traffic came with lower success and slower recovery.")


def ladder_bytes(data, theme="light"):
    with drawing(theme):
        return svg(ladder_figure(data, theme), "S1–S3 protection goals and safety checks", "Six panels: attempts and p99; outage calls and outside-outage success; "
                   "duplicate executions and extra attempts. C0–C4 medians and min–max across three runs. C0/C4: S1/S2 HARMFUL; S3 EFFECTIVE. "
                   "Words and icons identify verdicts. MARGINAL/CLEAR describe failing safety-check sizes, never change the registered decisions.")
