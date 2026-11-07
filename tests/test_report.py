"""Independent saved-evidence arithmetic, failure cases and derivative freshness."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from statistics import median
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from bench import analysis, charts, report, run_official as official
from outage_test import runner
from test_evidence import official_state


@pytest.fixture(autouse=True)
def no_capture(monkeypatch):
    for module, name in ((official, "execute"), (runner, "start_provider")):
        monkeypatch.setattr(module, name, Mock(side_effect=AssertionError("capture forbidden")))


def hashes():
    base = analysis.ROOT / "results/official"
    return {p.relative_to(base).as_posix(): hashlib.sha256(analysis.read(p)).hexdigest()
            for p in base.rglob("*") if p.is_file()}


@pytest.fixture(scope="module")
def registered():
    # Synthetic ancestry injection, as in test_official, keeps tests offline in shallow CI.
    # Production report --check uses real preflight and requires full Git history.
    return official.preflight(ancestry=lambda commit: commit == official.FREEZE_COMMIT)


@pytest.fixture(scope="module")
def measured(registered):
    before = hashes()
    data = analysis.analyze(registration=registered)
    yield data
    assert hashes() == before and len(before) == 55


@pytest.fixture(scope="module")
def rendered(measured):
    return report.render(measured)


def independent(doc):
    records, receipts = doc["run"]["records"], doc["ledger"]
    n = len(doc["run"]["schedule"])
    per_job = {r["job_id"]: [p for p in receipts if p["job_id"] == r["job_id"]] for r in records}
    result = dict(success_fraction=len([r for r in records if r["outcome"] == "SUCCESS"]) / n,
        attempts_per_logical_request=sum(len(r["attempts"]) for r in records) / n,
        extra_attempts=sum(max(len(r["attempts"]) - 1, 0) for r in records),
        extra_attempts_per_logical_request=sum(max(len(rs) - 1, 0) for rs in per_job.values()) / n,
        duplicate_provider_executions=sum(max(sum(p["executed"] for p in rs) - 1, 0) for rs in per_job.values()),
        provider_executions=len([p for p in receipts if p["executed"]]),
        outside_outage_success_fraction=None, outage_provider_attempts_per_outage_issued_job=None,
        burst_attempts_per_planned_job=None, recovery_seconds=None)
    scenario = doc["slot"]["scenario"]
    if scenario == "S2":
        outside = [r for r in records if r["intended_offset"] < 20 or r["intended_offset"] >= 35]
        result["outside_outage_success_fraction"] = sum(r["success"] for r in outside) / len(outside)
        result["outage_provider_attempts_per_outage_issued_job"] = sum(20 <= p["arrival_time"] < 35 for p in receipts) / 150
        for t in range(40, 61):
            issued = [r for r in records if t - 5 <= r["launch_offset"] < t]
            completed = [r for r in issued if r["outcome"] == "SUCCESS" and r["completion_offset"] <= t]
            if issued and len(completed) / len(issued) >= .95:
                result["recovery_seconds"] = t - 35
                break
    if scenario == "S1":
        result["burst_attempts_per_planned_job"] = sum(20 <= p["arrival_time"] < 30 for p in receipts) / 140
    for p in (50, 95, 99):
        result[f"latency_p{p}_seconds"] = sorted(r["latency"] for r in records)[math.ceil(p * n / 100) - 1]
    for kind in ("loop", "launch"):
        samples = doc["metadata"]["health"]["measured"][kind + "_samples"]
        result[kind + "_p99_seconds"] = sorted(samples)[math.ceil(len(samples) * .99) - 1]
    result.update({"outcome_" + k: sum(r["outcome"] == k for r in records) for k in analysis.harness.OUTCOMES})
    return result


def test_all_metrics_sources_rounds_and_summary_independently(measured):
    assert len(measured["cells"]) == 17 and len(measured["inputs"]) == 55
    assert Counter(r["status"] for r in measured["excluded"]) == {"ABANDONED": 1, "INVALID": 1}
    for cell in measured["cells"].values():
        assert [r["round"] for r in cell["runs"]] == [1, 2, 3]
        values = []
        for r in cell["runs"]:
            raw = analysis.read(analysis.ROOT / r["path"])
            assert hashlib.sha256(raw).hexdigest() == r["sha256"] == measured["inputs"][r["path"]]
            doc = json.loads(raw)
            assert r["seed"] == 12000 + r["round"] == doc["slot"]["fault_seed"]
            expected = independent(doc)
            assert r["metrics"] == expected
            counts = Counter(math.floor(p["arrival_time"]) for p in doc["ledger"])
            assert r["timeline"] == [counts[i] for i in range(max(counts) + 1)]
            values.append(expected)
        for key, summary in cell["summary"].items():
            samples = [v[key] for v in values]
            assert summary["values"] == samples
            if None not in samples:
                assert (summary["median"], summary["min"], summary["max"]) == (median(samples), min(samples), max(samples))
                width = max(samples) - min(samples)
                assert summary["high_spread"] == (width > .1 * median(samples))
            else:
                assert summary["median"] is None


def independent_decision(c, cells):
    s = c["scenario"]
    left, right = (cells[s + "/" + config]["runs"] for config in (c["baseline"], c["candidate"]))
    a, b = [r["metrics"] for r in left], [r["metrics"] for r in right]
    if s == "S0":
        latency = median(r["latency_p99_seconds"] for r in a)
        bad = any(y["extra_attempts_per_logical_request"] != 0 or y["success_fraction"] < x["success_fraction"] for x, y in zip(a, b))
        bad |= median(r["latency_p99_seconds"] for r in b) > latency + max(.1 * latency, .02)
        return "HARMFUL" if bad else "NO MEASURABLE EFFECT"
    if s == "S1":
        bad = any(y["success_fraction"] < x["success_fraction"] - .01 or y["latency_p99_seconds"] > x["latency_p99_seconds"] for x, y in zip(a, b))
    elif s == "S2":
        bad = any(y["outside_outage_success_fraction"] < x["outside_outage_success_fraction"] or
                  y["recovery_seconds"] > x["recovery_seconds"] + 5 or y["latency_p99_seconds"] > x["latency_p99_seconds"] for x, y in zip(a, b))
    else:
        bad = any(y["success_fraction"] < x["success_fraction"] or y["extra_attempts"] > x["extra_attempts"] for x, y in zip(a, b))
    primary = c["primary"]
    base = [x[primary] for x in a]
    center, candidate = median(base), median(y[primary] for y in b)
    improvement, spread = (center - candidate) / center, (max(base) - min(base)) / center
    if bad or improvement <= -.2 and -improvement > spread:
        return "HARMFUL"
    return "EFFECTIVE" if improvement >= .2 and improvement > spread else "NO MEASURABLE EFFECT"


def test_registered_comparisons_independent_decisions_and_pairing(measured):
    assert len(measured["comparisons"]) == 25
    assert {c["scenario"]: c["verdict"] for c in measured["comparisons"] if c["role"] == "primary"} == dict(
        S0="NO MEASURABLE EFFECT", S1="HARMFUL", S2="HARMFUL", S3="EFFECTIVE")
    for c in measured["comparisons"]:
        assert c["verdict"] == independent_decision(c, measured["cells"])
        assert all(g["round"] in (None, 1, 2, 3) for g in c["guardrails"])
        if c["scenario"] != "S0":
            assert c["I"] is not None and c["S"] is not None


@pytest.mark.parametrize("base,candidate,guards,expected", [
    ([0]*3, [0]*3, [True], "NO MEASURABLE EFFECT"),
    ([0]*3, [1]*3, [True], "HARMFUL"),
    ([0, 0, 10], [1]*3, [True], "NO MEASURABLE EFFECT"),
    ([100]*3, [80]*3, [True], "EFFECTIVE"),
    ([100]*3, [120]*3, [True], "HARMFUL"),
    ([80, 100, 120], [80]*3, [True], "NO MEASURABLE EFFECT"),
    ([100]*3, [50]*3, [False], "HARMFUL"),
    ([100]*3, [50]*3, [None], "INCONCLUSIVE"),
    ([100]*2, [50]*2, [True], "INCONCLUSIVE"),
    ([None]*3, [50]*3, [False], "HARMFUL"),
])
def test_zero_safe_precedence_and_uncertainty(base, candidate, guards, expected):
    assert analysis.verdict(base, candidate, guards)[0] == expected


@pytest.mark.parametrize("damage", ["extras", "success", "median_latency", "single_latency_outlier"])
def test_control_harm_not_hidden_by_medians(measured, damage):
    cells = deepcopy(measured["cells"])
    runs = cells["S0/C4"]["runs"]
    if damage == "extras": runs[0]["metrics"]["extra_attempts_per_logical_request"] = .1
    elif damage == "success": runs[0]["metrics"]["success_fraction"] = .9
    elif damage == "median_latency":
        for r in runs: r["metrics"]["latency_p99_seconds"] = 1
    else: runs[0]["metrics"]["latency_p99_seconds"] = 1
    c = analysis.comparison("S0", "C0", "C4", cells, measured["frozen_decision"])
    assert c["verdict"] == ("NO MEASURABLE EFFECT" if damage == "single_latency_outlier" else "HARMFUL")


@pytest.mark.parametrize("base,candidate,expected", [(5, None, "HARMFUL"), (None, 5, "INCONCLUSIVE"), (None, None, "INCONCLUSIVE")])
def test_missing_recovery_and_s3_gate(measured, base, candidate, expected):
    cells = deepcopy(measured["cells"])
    for config, value in (("C0", base), ("C1", candidate)):
        for r in cells["S2/" + config]["runs"]: r["metrics"]["recovery_seconds"] = value
    assert analysis.comparison("S2", "C0", "C1", cells, measured["frozen_decision"])["verdict"] == expected
    cells["S3/C0"]["runs"][0]["metrics"]["duplicate_provider_executions"] = 0
    assert analysis.comparison("S3", "C3", "C4", cells, measured["frozen_decision"])["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("successes,complete,expected", [(19, 40, 5), (18, 40, 6), (19, 40.01, 6), (20, 61, None)])
def test_recovery_uses_actual_issuance_and_observed_completions(registered, successes, complete, expected):
    rows = [dict(launch_offset=35 + i / 4, intended_offset=0, success=i >= 20 - successes, completion_offset=complete) for i in range(20)]
    assert analysis.recovery(rows, registered["thresholds"]["decision"]["recovery"]) == expected
    assert analysis.recovery([], registered["thresholds"]["decision"]["recovery"]) is None


def test_receipt_extras_not_hidden_by_missing_job_and_zero_denominator(measured):
    r = measured["cells"]["S0/C0"]["runs"][0]
    doc = json.loads(analysis.read(analysis.ROOT / r["path"]))
    doc["ledger"] = [deepcopy(doc["ledger"][0]), deepcopy(doc["ledger"][0])]
    values, _ = analysis.metrics(doc, measured["frozen_decision"]["recovery"])
    assert values["extra_attempts_per_logical_request"] == 1 / 600
    doc["slot"]["scenario"] = "S2"
    for row in doc["run"]["records"]: row["intended_offset"] = 0
    with pytest.raises(ValueError, match="missing outage denominator"):
        analysis.metrics(doc, measured["frozen_decision"]["recovery"])
    doc["run"] = dict(schedule=[], records=[])
    with pytest.raises(ValueError, match="missing logical"):
        analysis.metrics(doc, measured["frozen_decision"]["recovery"])
    assert analysis.spread([0, 0, 1])["high_spread"] and analysis.spread([0, 0, 1])["relative_spread"] is None


@pytest.fixture(scope="module")
def evidence_copy(tmp_path_factory):
    root = tmp_path_factory.mktemp("g9-evidence")
    base = analysis.ROOT / "results/official"
    for path in base.rglob("*"):
        if path.is_file():
            target = root / "results/official" / path.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(analysis.read(path))
    return root


def test_fresh_checkout_audit_preserves_all_evidence_metadata(evidence_copy, registered):
    directory = evidence_copy / "results/official"
    before = official_state(directory)
    analysis.audit(evidence_copy, registered)
    assert official_state(directory) == before


@pytest.mark.parametrize("damage", ["missing", "bytes", "sidecar", "reservation", "manifest", "extra", "symlink"])
def test_tampered_missing_or_ambiguous_evidence_rejected(evidence_copy, registered, damage):
    base = evidence_copy / "results/official"
    target = next(base.glob("S0/C0/run-*.json"))
    if damage == "sidecar": target = base / official.RECOVERY_FILE
    elif damage == "reservation": target = base / official.RECOVERY_PATH
    elif damage == "manifest": target = base / analysis.MANIFEST
    original = target.read_bytes()
    extra = base / "extra.json"
    try:
        if damage == "missing": target.unlink()
        elif damage == "extra": extra.write_bytes(b"{}")
        elif damage == "symlink":
            target.unlink()
            target.symlink_to(base / analysis.MANIFEST)
        else: target.write_bytes(original + b" ")
        with pytest.raises((ValueError, OSError)):
            analysis.audit(evidence_copy, registered)
    finally:
        if target.is_symlink(): target.unlink()
        target.write_bytes(original)
        if extra.exists(): extra.unlink()


@pytest.mark.parametrize("damage", ["runner", "seed", "unknown_field"])
def test_provenance_or_schema_cannot_be_trusted_from_labels(measured, registered, damage):
    r = measured["cells"]["S3/C4"]["runs"][1]
    doc = json.loads(analysis.read(analysis.ROOT / r["path"]))
    if damage == "runner": doc["runner"]["git_commit"] = "0" * 40
    elif damage == "seed": doc["slot"]["fault_seed"] += 1
    else: doc["unknown"] = "unregistered"
    with pytest.raises(ValueError): official.assess(doc, registered, analysis.CAPTURE)


def test_freshness_and_byte_identical_chart_rerender(measured, rendered):
    report.persist(rendered, check=True)
    chart = report.timeline(measured)
    assert report.chart_bytes(chart, measured) == rendered["docs/retry-storm.svg"]
    for config, trace in chart["traces"].items():
        runs = measured["cells"]["S2/" + config]["runs"]
        primary = measured["frozen_decision"]["metrics"]["S2"]["primary"]
        chosen = next(r for r in runs if r["round"] == trace["round"])
        assert chosen["metrics"][primary] == median(r["metrics"][primary] for r in runs)
        assert all(lo <= x <= hi for lo, x, hi in zip(trace["min"], trace["values"], trace["max"], strict=True))
    assert len([line for line in rendered["README.md"].decode().splitlines() if line.startswith("## ")]) == 13


def test_readme_only_edit_preserves_other_derivatives(measured, rendered, monkeypatch):
    monkeypatch.setattr(report, "README", report.README + "\nEditorial freshness fixture.\n")
    changed = report.render(measured)
    assert changed["README.md"] != rendered["README.md"]
    assert changed == report.render(measured)
    assert {name: raw for name, raw in changed.items() if name != "README.md"} == {
        name: raw for name, raw in rendered.items() if name != "README.md"}


def test_readme_case_study_tracks_saved_measurements(measured, rendered):
    text = rendered["README.md"].decode()
    assert "```mermaid\nflowchart LR" in text
    assert not any(line.startswith("## " + str(n) + ".") for line in text.splitlines() for n in range(1, 14))
    assert "90 → 0 duplicate executions per run" in text
    assert "1.62 → 3.02 seconds p99 latency" in text
    changed = deepcopy(measured)
    changed["cells"]["S3/C4"]["summary"]["duplicate_provider_executions"].update(median=7, min=7, max=7)
    assert "90 → 7 duplicate executions per run" in report.readme_highlights(changed)
    assert "7 [7, 7]" in report.readme_results(changed)
    for target in ("docs/results.md", "docs/chart_data.json", "docs/engineering-log.md", "docs/retry-storm.svg"):
        assert target in text and (analysis.ROOT / target).is_file()


@pytest.mark.parametrize("name", report.OUTPUTS)
def test_stale_outputs_fail_without_writing(rendered, tmp_path, name):
    for path, raw in rendered.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw + (b"stale" if path == name else b""))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="stale report"):
        report.persist(rendered, check=True, root=tmp_path)
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_reader_and_failed_cli_are_readonly(monkeypatch, capsys):
    opening = analysis.os.open
    def readonly(path, flags, *args, **kwargs):
        assert not flags & (analysis.os.O_WRONLY | analysis.os.O_RDWR | analysis.os.O_CREAT | analysis.os.O_TRUNC)
        assert flags & analysis.os.O_NOATIME
        return opening(path, flags, *args, **kwargs)
    monkeypatch.setattr(analysis.os, "open", readonly)
    assert analysis.read(analysis.ROOT / "results/official" / analysis.MANIFEST)
    monkeypatch.setattr(analysis, "analyze", Mock(side_effect=ValueError("fixture audit failure")))
    assert report.main(["--check"]) == 1
    assert "fixture audit failure" in capsys.readouterr().err


def test_both_svgs_are_deterministic_accessible_and_zero_based(measured, rendered):
    pairs = [(charts.chart_bytes(report.timeline(measured), measured), "docs/retry-storm.svg"),
             (charts.ladder_bytes(measured), "docs/mechanism-ladder.svg")]
    for raw, path in pairs:
        assert raw == rendered[path]
        svg = ElementTree.fromstring(raw)
        assert svg.attrib["role"] == "img" and svg.attrib["aria-labelledby"] == "chart-title chart-desc"
        assert svg.find("{http://www.w3.org/2000/svg}title").text
        assert svg.find("{http://www.w3.org/2000/svg}desc").text
        identifiers = {element.attrib["id"] for element in svg.iter() if "id" in element.attrib}
        assert all(value[5:-1] in identifiers for element in svg.iter()
                   if (value := element.attrib.get("clip-path", "")).startswith("url(#"))
        assert b"<text" in raw and b"HARMFUL" in rendered["docs/mechanism-ladder.svg"]
    for fig in (charts.figure(report.timeline(measured), measured), charts.ladder_figure(measured)):
        assert all(ax.get_ylim()[0] == 0 for ax in fig.axes)


def test_ladder_shows_actual_goal_and_safety_values(measured):
    fig = charts.ladder_figure(measured)
    assert len(fig.axes) == 9
    for row, (scenario, metrics, _) in enumerate(charts.PANELS):
        for col, metric in enumerate(metrics):
            scale = 100 if metric.endswith("success_fraction") else 1
            expected = [measured["cells"][f"{scenario}/C{i}"]["summary"][metric]["median"] * scale for i in range(5)]
            ax = fig.axes[row*3+col]
            dots = [line for line in ax.lines if line.get_marker() == "o"]
            assert [line.get_ydata()[0] for line in dots] == expected
            assert [text.get_text() for text in ax.texts] == [f"{value:.3g}" for value in expected]
    assert "recovery: r2" in charts.guard_notes(measured, "S2")
    assert "C3 → C4 [+] EFFECTIVE" in charts.guard_notes(measured, "S3")


@pytest.mark.parametrize("damage", ["guard", "verdict", "summary", "comparison_inventory"])
def test_chart_rejects_changed_guardrails_and_summaries(measured, damage):
    data = deepcopy(measured)
    if damage == "guard": data["comparisons"][1]["guardrails"][0]["passed"] = False
    elif damage == "verdict": data["comparisons"][1]["verdict"] = "EFFECTIVE"
    elif damage == "summary": data["cells"]["S1/C0"]["summary"]["latency_p99_seconds"]["median"] = 0
    else: data["comparisons"].pop()
    with pytest.raises(ValueError, match="chart"):
        charts.ladder_bytes(data)


def test_outage_costs_and_all_scientific_fields_preserved(measured, rendered):
    text = rendered["README.md"].decode()
    assert "200 [183, 203] fail-fast outcomes per 600 jobs" in text
    assert "89.3% → 66.5%" in text and "100% → 88.7%" in text
    assert "round 2 took 11 seconds, exceeding its 10-second limit" in text
    assert "success 89.3% → 66.5%" in charts.cost_caption(measured)
    assert "C4 fail-fast outcomes 200 per 600 jobs" in charts.cost_caption(measured)
    assert "I did not implement or test any fallback" in text
    derived = json.loads(rendered["docs/chart_data.json"])
    assert derived.pop("timeline") == report.timeline(measured)
    sources = derived.pop("analysis_sources")
    assert sources["bench/charts.py"] == hashlib.sha256(analysis.read(analysis.ROOT / "bench/charts.py")).hexdigest()
    assert derived == measured
    primary = measured["cells"]["S1/C0"]["summary"]["attempts_per_logical_request"]["median"]
    assert str(primary) in rendered["docs/results.md"].decode()
