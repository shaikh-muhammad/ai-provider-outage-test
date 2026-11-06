"""Read-only G9 metrics and frozen decisions; no capture or provider entry points."""
from collections import Counter
import math
import os
from pathlib import Path
import re
from statistics import median

from bench import run_official as official
from outage_test import harness

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "manifest-f6feed1eb49f48e58affb7732d47cf61.json"
MANIFEST_SHA = "404b8f22313496adb2d44ab6c60243ee64aa45d6ac0023fc35a759d76d8a224d"
CAPTURE = dict(version=1, git_commit="e4e2edde036305b02658a73bd57f93b574dc6cd8",
               sha256="fa0db6727f3a77b8a0c4fc925a896537032e8114a9879659534bfd363b434b21")
require = official.require
FORMULAS = {
    "success_fraction": "SUCCESS final requests / all planned requests; fraction",
    "attempts_per_logical_request": "client physical attempts / all planned requests; attempts/request",
    "extra_attempts": "sum(max(0, client attempts for job - 1)); attempts",
    "extra_attempts_per_logical_request": "sum(max(0, provider receipts for job - 1)) / all planned requests; attempts/request",
    "outage_provider_attempts_per_outage_issued_job": "provider arrivals in [20,35) / planned jobs intended in [20,35); attempts/request",
    "burst_attempts_per_planned_job": "provider arrivals in [20,30) / planned S1 burst jobs; descriptive attempts/request",
    "outside_outage_success_fraction": "SUCCESS among jobs intended outside [20,35) / all such jobs; fraction",
    "duplicate_provider_executions": "sum(max(0, executed receipts for job - 1)); executions",
    "provider_executions": "count(receipts with executed=true); work units, cost proxy only, no monetary price model",
    "recovery_seconds": "earliest t in 40..60 with D>0 and 20*A>=19*D, minus 35; D=issued in [t-5,t), A=SUCCESS completed <=t; null if absent",
    "latency_p50_seconds": "nearest rank ceil(0.50*N) over all logical latencies, including failures; seconds",
    "latency_p95_seconds": "nearest rank ceil(0.95*N) over all logical latencies, including failures; seconds",
    "latency_p99_seconds": "nearest rank ceil(0.99*N) over all logical latencies, including failures; seconds",
    "loop_p99_seconds": "nearest rank ceil(0.99*N) over raw loop samples; seconds",
    "launch_p99_seconds": "nearest rank ceil(0.99*N) over raw launch samples; seconds",
    "outcome_*": "count of each final outcome per run; requests",
    "timeline": "provider receipts by floor(arrival_time); attempts per one-second bin; never pooled",
    "spread": "(max-min)/median; flag >0.10; zero median with nonzero range is flagged with null ratio",
}


def read(path):
    require(path.absolute() == path.resolve(), "symlink analysis input")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return official.read_fd(fd)
    finally:
        os.close(fd)


def audit(root=ROOT, registration=None):
    """The registration injection is for offline fixtures; CLI uses real preflight."""
    reg = registration if registration is not None else official.preflight()
    require(official.digest(read(ROOT / "bench/run_official.py")) == CAPTURE["sha256"], "capture runner source changed")
    base = root / "results/official"
    raw = read(base / MANIFEST)
    require(official.digest(raw) == MANIFEST_SHA, "unreviewed manifest")
    manifest = official.decoded(raw)
    expected = {MANIFEST: MANIFEST_SHA, official.RECOVERY_FILE:
                "fe659406963d7b54baa1c5edf77864db98d6a48b42e811af1e13fbb4d28e9e84"}
    for row in manifest["slots"]:
        for attempt in row["attempts"]:
            name = attempt["path"]
            require(re.fullmatch(r"S[0-3]/C[0-4]/run-[1-3]-[a-f0-9]{32}\.json", name) is not None)
            require(name not in expected, "duplicate attempt path")
            expected[name] = attempt["sha256"]
    paths = list(base.rglob("*"))
    require(not any(p.is_symlink() for p in paths), "symlink official inventory")
    require({p.relative_to(base).as_posix() for p in paths if p.is_file()} == set(expected), "official inventory mismatch")
    for name, sha in expected.items():
        require(official.digest(read(base / name)) == sha, "attempt hash mismatch: " + name)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        attempts, manifests = official.discover(fd, reg, CAPTURE)
    finally:
        os.close(fd)
    require(manifests == [official.manifest(attempts, reg, CAPTURE)] and manifest == manifests[0])
    require(manifest["status"] == "COMPLETE" and len(expected) == 55, "incomplete official evidence")
    require([a["status"] for a in attempts[30]] == ["ABANDONED", "VALID"], "wrong recovery accounting")
    documents, excluded, previous_end = [], [], {}
    for row in manifest["slots"]:
        for attempt in row["attempts"]:
            if attempt["status"] == "ABANDONED":
                excluded.append(dict(slot=row["slot"], **attempt))
                continue
            doc = official.decoded(read(base / attempt["path"]))
            group = doc["slot"]["ordinal"] <= 29
            start = doc["metadata"]["health"]["context"]["t0"]
            require(start >= previous_end.get(group, 0), "registered execution order mismatch")
            previous_end[group] = start + max(r["completion_offset"] for r in doc["run"]["records"])
            if attempt["status"] == "VALID":
                documents.append((attempt, doc))
            else:
                excluded.append(dict(slot=row["slot"], **attempt, reasons=doc["validity"]["reasons"]))
    require(len(documents) == 51 and Counter(a["status"] for a in excluded) == {"INVALID": 1, "ABANDONED": 1})
    return reg, documents, excluded, {"results/official/" + n: h for n, h in sorted(expected.items())}


def percentile(values, fraction):
    require(bool(values) and all(harness.finite(v) for v in values), "invalid percentile samples")
    return sorted(values)[math.ceil(fraction * len(values)) - 1]


def recovery(records, policy):
    for t in policy["observation_times_seconds"]:
        issued = [r for r in records if t - policy["window_seconds"] <= r["launch_offset"] < t]
        done = sum(r["success"] and r["completion_offset"] <= t for r in issued)
        if issued and policy["success_denominator"] * done >= policy["success_numerator"] * len(issued):
            return t - policy["outage_end_seconds"]
    return None


def metrics(doc, policy):
    rows, ledger, scenario = doc["run"]["records"], doc["ledger"], doc["slot"]["scenario"]
    n = len(doc["run"]["schedule"])
    require(n > 0 and len(rows) == n, "missing logical requests")
    receipts = Counter(r["job_id"] for r in ledger)
    executions = Counter(r["job_id"] for r in ledger if r["executed"])
    outside = [r for r in rows if not 20 <= r["intended_offset"] < 35]
    outage = sum(20 <= r["intended_offset"] < 35 for r in rows)
    burst = sum(20 <= r["intended_offset"] < 30 for r in rows)
    require(scenario != "S2" or (outage > 0 and len(outside) > 0), "missing outage denominator")
    require(scenario != "S1" or burst > 0, "missing burst denominator")
    health = doc["metadata"]["health"]["measured"]
    values = dict(success_fraction=sum(r["success"] for r in rows) / n,
        attempts_per_logical_request=sum(len(r["attempts"]) for r in rows) / n,
        extra_attempts=sum(max(0, len(r["attempts"]) - 1) for r in rows),
        extra_attempts_per_logical_request=sum(max(0, receipts[r["job_id"]] - 1) for r in rows) / n,
        duplicate_provider_executions=sum(max(0, count - 1) for count in executions.values()),
        provider_executions=sum(executions.values()),
        outside_outage_success_fraction=sum(r["success"] for r in outside) / len(outside) if scenario == "S2" else None,
        outage_provider_attempts_per_outage_issued_job=sum(20 <= r["arrival_time"] < 35 for r in ledger) / outage if scenario == "S2" else None,
        burst_attempts_per_planned_job=sum(20 <= r["arrival_time"] < 30 for r in ledger) / burst if scenario == "S1" else None,
        recovery_seconds=recovery(rows, policy) if scenario == "S2" else None,
        loop_p99_seconds=percentile(health["loop_samples"], .99),
        launch_p99_seconds=percentile(health["launch_samples"], .99))
    values.update({f"latency_p{p}_seconds": percentile([r["latency"] for r in rows], p / 100) for p in (50, 95, 99)})
    values.update({"outcome_" + outcome: sum(r["outcome"] == outcome for r in rows) for outcome in sorted(harness.OUTCOMES)})
    bins = Counter(math.floor(r["arrival_time"]) for r in ledger)
    return values, [bins[t] for t in range(max(bins, default=0) + 1)]


def spread(values):
    if any(v is None for v in values):
        return dict(values=values, median=None, min=None, max=None, relative_spread=None, high_spread=False)
    middle, width = median(values), max(values) - min(values)
    ratio = width / middle if middle else None
    return dict(values=values, median=middle, min=min(values), max=max(values), relative_spread=ratio,
                high_spread=ratio > .10 if ratio is not None else width > 0)


def verdict(base, candidate, guards, *, control=False, efficacy=True, threshold=.2):
    """Demonstrated harm wins over uncertainty; undefined values never become zero."""
    guard_harm = any(g is False for g in guards)
    if len(base) != 3 or len(candidate) != 3 or any(v is None for v in base + candidate):
        return "HARMFUL" if guard_harm else "INCONCLUSIVE", None, None
    b, c, width = median(base), median(candidate), max(base) - min(base)
    improvement, noise = ((b - c) / b, width / b) if b else (None, None)
    harm = c > width if b == 0 else improvement <= -threshold and -improvement > noise
    if guard_harm or harm:
        return "HARMFUL", improvement, noise
    if any(g is None for g in guards) or not efficacy:
        return "INCONCLUSIVE", improvement, noise
    if control or b == 0:
        return "NO MEASURABLE EFFECT", improvement, noise
    if improvement >= threshold and improvement > noise:
        return "EFFECTIVE", improvement, noise
    if abs(improvement) < threshold or improvement <= noise:
        return "NO MEASURABLE EFFECT", improvement, noise
    return "INCONCLUSIVE", improvement, noise


def comparison(scenario, base, candidate, cells, decision):
    left, right = cells[scenario + "/" + base]["runs"], cells[scenario + "/" + candidate]["runs"]
    primary = decision["metrics"][scenario]["primary"]
    guards = []
    def guard(name, round_number, b, c, limit, passed):
        guards.append(dict(name=name, round=round_number, baseline=b, candidate=c, limit=limit, passed=passed))
    if scenario == "S0":
        b, c = (median([r["metrics"]["latency_p99_seconds"] for r in runs]) for runs in (left, right))
        limit = b + max(.1 * b, .020)
        guard("median_p99_seconds", None, b, c, limit, c <= limit)
    for a, b in zip(left, right, strict=True):
        require(a["round"] == b["round"] and a["seed"] == b["seed"], "unpaired comparison")
        x, y, number = a["metrics"], b["metrics"], a["round"]
        pairs = [("success_fraction", -.01 if scenario == "S1" else 0)] if scenario != "S2" else [("outside_outage_success_fraction", 0)]
        for name, margin in pairs:
            guard(name, number, x[name], y[name], x[name] + margin, y[name] >= x[name] + margin)
        if scenario in ("S1", "S2"):
            name = "latency_p99_seconds"
            guard(name, number, x[name], y[name], x[name], y[name] <= x[name])
        if scenario == "S0":
            name = "extra_attempts_per_logical_request"
            guard("C4_zero_extras", number, x[name], y[name], 0, y[name] == 0)
        if scenario == "S3":
            guard("extra_attempts", number, x["extra_attempts"], y["extra_attempts"], x["extra_attempts"], y["extra_attempts"] <= x["extra_attempts"])
        if scenario == "S2":
            btime, ctime = x["recovery_seconds"], y["recovery_seconds"]
            passed = None if btime is None else ctime is not None and ctime <= btime + 5
            guard("recovery_seconds", number, btime, ctime, None if btime is None else btime + 5, passed)
    efficacy = scenario != "S3" or candidate != "C4" or all(
        r["metrics"][primary] == 0 and c["metrics"][primary] > 0
        for r, c in zip(right, cells["S3/C0"]["runs"], strict=True))
    label, improvement, noise = verdict([r["metrics"][primary] for r in left], [r["metrics"][primary] for r in right],
        [g["passed"] for g in guards], control=scenario == "S0", efficacy=efficacy, threshold=decision["relative_threshold"])
    return dict(scenario=scenario, baseline=base, candidate=candidate, primary=primary, verdict=label,
                I=improvement, S=noise, efficacy_gate=efficacy, guardrails=guards)


def analyze(root=ROOT, registration=None):
    reg, documents, excluded, sources = audit(root, registration)
    decision, cells = reg["thresholds"]["decision"], {}
    for attempt, doc in documents:
        slot = doc["slot"]
        values, timeline = metrics(doc, decision["recovery"])
        cell = cells.setdefault(slot["scenario"] + "/" + slot["config"], dict(runs=[]))
        cell["runs"].append(dict(round=slot["round"], seed=slot["workload_seed"], slot=slot["ordinal"],
            path="results/official/" + attempt["path"], sha256=attempt["sha256"], runner=doc["runner"],
            planned_requests=len(doc["run"]["schedule"]), provider_receipts=len(doc["ledger"]), metrics=values, timeline=timeline))
    for cell in cells.values():
        cell["runs"].sort(key=lambda r: r["round"])
        require([r["round"] for r in cell["runs"]] == [1, 2, 3], "incomplete cell")
        cell["summary"] = {key: spread([r["metrics"][key] for r in cell["runs"]]) for key in cell["runs"][0]["metrics"]}
    comparisons = []
    for scenario in reg["scenarios"]["scenarios"]:
        for role in ("primary", "secondary", "stepwise"):
            if scenario == "S0" and role != "primary":
                continue
            for pair in decision["comparisons"][role]:
                base, candidate = pair.split(":")
                comparisons.append(dict(role=role, **comparison(scenario, base, candidate, cells, decision)))
    return dict(schema_version=1, registration=reg["fingerprint"], capture_runner=CAPTURE, inputs=sources,
        valid_runs=51, total_attempts=53, excluded=excluded, formulas=FORMULAS, frozen_decision=decision,
        cells=dict(sorted(cells.items())), comparisons=comparisons)
