"""Offline checks of a proposal, never permission to run official experiments."""
import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import re
from statistics import median

import pytest
import yaml
from outage_test import client, harness, provider, runner

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "results/pilot"
SOURCES = "client provider faults harness runner".split()
SOURCE_NAMES = [f"src/outage_test/{n}.py" for n in SOURCES] + [
    "requirements-run.lock", "requirements-dev.lock", ".github/workflows/ci.yml"]
RULES = """exactly_one_final_record_per_planned_job unique_job_and_attempt_and_receipt_ids
one_to_one_client_attempt_provider_receipt_job_match no_missing_or_extra_receipts
no_overflow_unfinished_or_aborted_receipts expected_applied_faults_identical
full_provider_configuration_matches_registration provider_exit_zero_reaped_no_forced_kill_or_unexpected_exit
no_runner_or_logical_record_errors raw_health_samples_and_counts_reconcile
sampling_ticks_and_unobserved_tail_accounted full_environment_timing_and_source_provenance
fail_closed_absent_malformed_nonfinite_or_unhealthy""".split()
POLICY = dict(max_attempts=int, attempt_timeout_seconds=float, logical_deadline_seconds=(float, type(None)),
              fixed_retry_delay_seconds=(float, type(None)), full_jitter=bool, honor_retry_after=bool,
              circuit_breaker=bool, idempotency=bool)
PUB = {k: str for k in "label replacement plan_source_sha256 plan_public_sha256 log_source_sha256 log_public_sha256 log_header summary_sha256".split()}
PUB.update(originals_intentionally_unpublished=bool, plan_replacements=int, log_replacements=int)
METRIC = dict(primary=str, definition=str, guardrails=[str])
GUARDS = [
    ["candidate_physical_attempts <= baseline_physical_attempts", "candidate_p99 <= baseline_p99 + max(0.10 * baseline_p99, 0.020 seconds)", "candidate_success_fraction >= baseline_success_fraction"],
    ["candidate_success_fraction >= baseline_success_fraction - 0.01", "candidate_p99_logical_latency_seconds <= baseline_p99_logical_latency_seconds"],
    ["candidate_outside_outage_success_fraction >= baseline_outside_outage_success_fraction", "candidate_recovery_seconds <= baseline_recovery_seconds + 5.0", "candidate_p99_logical_latency_seconds <= baseline_p99_logical_latency_seconds"],
    ["candidate_success_fraction >= baseline_success_fraction", "candidate_extra_attempts <= baseline_extra_attempts"]]
T_SCHEMA = dict(schema_version=1, status="PROPOSED", frozen=False,
    metadata=dict(approval=str, source_head=str, brief=str, proposed_source_sha256={n: str for n in SOURCE_NAMES},
                  publication=PUB, limitations=[str]), clients={f"C{i}": POLICY for i in range(5)},
    client_semantics=dict(retryable_http=[(int, str)], retryable_failures=[str], terminal=[str],
        backoff=dict(base_seconds=float, cap_seconds=float, policy=str), retry_after=str, deadline=str,
        breaker=dict(window=int, minimum_samples=int, failure_fraction=float, open_seconds=int,
                     relevant_failures=[str], neutral=[str], semantics=str), idempotency=str),
    validity=dict(sample_interval_seconds=float, sampling=str, loop_lag_p99_seconds=float,
                  launch_lag_p99_seconds=float, percentile=str, rules=[str], calibration=str),
    invalid_handling=dict(max_total_attempts_per_slot=3, preserve_all_invalid=True,
        exclude_invalid_from_statistics=True, exhausted_slot_and_cell="INCOMPLETE",
        retry_same_seed_parameters_and_limits=True, retry_order=str),
    decision=dict(valid_runs=3, aggregation=str, formulas={k: str for k in "BCIS"}, relative_threshold=0.20,
        precedence=[str], effective=str, harmful=str, no_measurable_effect=str, inconclusive=str,
        zero_baseline=dict(both_zero=str, candidate_positive=str, ratios=str), missing_recovery=str,
        metric_definitions={k: str for k in "success_fraction extra_attempts outside_outage_success_fraction recovery_seconds p99".split()},
        metrics={s: METRIC | ({"efficacy_gate": str} if s == "S3" else {}) for s in harness.SCENARIOS},
        comparisons=dict(primary=[str], secondary=[str], stepwise=[str], applicability=str)),
    implementation_dependencies=dict(P17=[str], P19=[str]))
PARAMS = dict(rate=10.0, bucket_capacity=10.0, outage_start=20.0, outage_end=35.0,
              loss_fraction=0.15, capacity=128, max_pending=256)
S_SCHEMA = dict(schema_version=1, status="PROPOSED", frozen=False,
    provider=dict(host="127.0.0.1", parameters=PARAMS, seed=str, scenario=str, idempotency=str,
        behavior=dict(service_base_seconds=0.08, service_factor_range=[float], request_body_max_bytes=2048,
            normal_deadline_seconds=12, hung_hold_seconds=6, retry_after_seconds=1, admin=str,
            **{s: str for s in harness.SCENARIOS})),
    scenarios={s: dict(duration_seconds=60, segments=[[int]], jobs=int, configurations=[str]) for s in harness.SCENARIOS},
    design=dict(valid_runs_per_cell=3, total_cells=17, total_valid_results=51, rounds=[dict(round=int, seed=int)],
        excluded_pilot_seeds=[int], order_rng_seed=72026, algorithm=str, workload=str),
    order=[dict(ordinal=int, scenario=str, config=str, round=int, run_slot=int, workload_seed=int, fault_seed=int)])


def shape(value, spec):
    if isinstance(spec, dict):
        assert type(value) is dict and value.keys() == spec.keys()
        for key in spec:
            shape(value[key], spec[key])
    elif isinstance(spec, list):
        assert type(value) is list and 1 <= len(value) <= 100
        for item in value:
            shape(item, spec[0])
    elif isinstance(spec, type) or isinstance(spec, tuple):
        assert type(value) in (spec if isinstance(spec, tuple) else (spec,))
        if type(value) is str:
            assert 1 <= len(value) <= 4096
    else:
        assert type(value) is type(spec) and value == spec


def documents():
    return [yaml.safe_load((ROOT / "bench" / name).read_bytes()) for name in ("thresholds.yaml", "scenarios.yaml")]


def validate(t, s):
    shape(t, T_SCHEMA)
    shape(s, S_SCHEMA)
    assert t["validity"]["rules"] == RULES
    assert [t["validity"][k] for k in ("sample_interval_seconds", "loop_lag_p99_seconds", "launch_lag_p99_seconds")] == [.01, .035, .05]
    assert t["decision"]["formulas"] == dict(B="median(baseline)", C="median(candidate)", I="(B-C)/B", S="(max(baseline)-min(baseline))/B")
    assert t["decision"]["precedence"] == ["HARMFUL", "EFFECTIVE", "NO MEASURABLE EFFECT", "INCONCLUSIVE"]
    assert [m["guardrails"] for m in t["decision"]["metrics"].values()] == GUARDS
    assert s["design"]["rounds"] == [dict(round=i, seed=12000+i) for i in range(1, 4)]
    cells = [(n, c) for n in harness.SCENARIOS for c in (["C0", "C4"] if n == "S0" else [f"C{i}" for i in range(5)])]
    assert len(set(cells)) == 17 and len(s["order"]) == 51
    rng = random.Random(72026)
    for round_number in range(1, 4):
        ordered = cells.copy()
        rng.shuffle(ordered)
        rows = s["order"][(round_number-1)*17:round_number*17]
        assert [(r["scenario"], r["config"]) for r in rows] == ordered
        assert Counter((r["scenario"], r["config"]) for r in rows) == Counter(cells)
        assert sum(a["config"] != b["config"] for a, b in zip(rows, rows[1:])) >= 10
        for index, row in enumerate(rows, (round_number-1)*17+1):
            assert row == dict(ordinal=index, scenario=row["scenario"], config=row["config"], round=round_number,
                               run_slot=round_number, workload_seed=12000+round_number, fault_seed=12000+round_number)
            assert row["workload_seed"] not in s["design"]["excluded_pilot_seeds"]
    assert set(s["design"]["excluded_pilot_seeds"]) == {9001, 9002, 9003, *range(9101, 9107)}


def test_registration_schema_and_design():
    validate(*documents())
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load("!!python/object:unknown {}")


@pytest.mark.parametrize("change", range(11))
def test_corruption_rejected(change):
    t, s = deepcopy(documents())
    if change == 0: t["validity"]["rules"].pop()
    elif change == 1: t["validity"]["rules"].append("unregistered_exception")
    elif change == 2: t["validity"]["loop_lag_p99_seconds"] = .04
    elif change == 3: s["order"][0] = s["order"][1].copy()
    elif change == 4: s["order"][0]["fault_seed"] = 9001
    elif change == 5: s["order"].pop()
    elif change == 6: t["decision"]["zero_baseline"]["unknown"] = True
    elif change == 7: t["invalid_handling"]["max_total_attempts_per_slot"] = 4
    elif change == 8: s["provider"]["parameters"]["rate"] = True
    elif change == 9: s["scenarios"]["S1"]["unknown"] = 1
    else: t["decision"]["metrics"]["S2"]["guardrails"][1] = "allow slower recovery"
    with pytest.raises(AssertionError): validate(t, s)


@pytest.mark.parametrize("scenario", harness.SCENARIOS)
def test_workloads_and_provider_parameters(scenario):
    _, s = documents()
    row = s["scenarios"][scenario]
    assert row["configurations"] == (["C0", "C4"] if scenario == "S0" else [f"C{i}" for i in range(5)])
    segments = [[0,20,6], [20,30,14], [30,60,6]] if scenario == "S1" else [[0,60,10]]
    assert row["segments"] == segments and row["jobs"] == (440 if scenario == "S1" else 600)
    for seed in (12001, 12002, 12003):
        jobs = harness.generate_workload(scenario, seed)
        assert len(jobs) == row["jobs"] and [j.offset for j in jobs] == [a+i/hz for a,b,hz in segments for i in range((b-a)*hz)]
        assert jobs[0].job_id == hashlib.sha256(json.dumps([scenario, seed, 0]).encode()).hexdigest()
        assert all(harness.generate_workload(scenario, r["workload_seed"]) == jobs for r in s["order"] if r["scenario"] == scenario and r["workload_seed"] == seed)
    assert provider.DEFAULTS == dict(scenario="S0", seed=0, idempotency=False, **PARAMS)
    assert runner.verified_pilot_policy()["selected_loop_lag_ms"] / 1000 == documents()[0]["validity"]["loop_lag_p99_seconds"]


@pytest.mark.parametrize("index", range(5))
def test_actual_client_policy(index):
    t, _ = documents()
    cfg, now, calls = f"C{index}", [0.0], []
    async def transport(data, headers, timeout):
        calls.append((headers, timeout))
        return client.HTTPReply(503, {}, b"")
    async def advance(delay): now[0] += delay
    c = client.Client(cfg, transport, seed=12001, clock=lambda: now[0], sleep=advance)
    result = asyncio.run(c.submit("job", "synthetic"))
    p = t["clients"][cfg]
    assert p == dict(max_attempts=4, attempt_timeout_seconds=5.0 if index == 0 else 2.0,
        logical_deadline_seconds=None if index == 0 else 6.0, fixed_retry_delay_seconds=.5 if index < 2 else None,
        full_jitter=index >= 2, honor_retry_after=index >= 2, circuit_breaker=index >= 3, idempotency=index == 4)
    assert len(calls) == p["max_attempts"] and all(timeout == p["attempt_timeout_seconds"] for _, timeout in calls)
    assert all((h.get("Idempotency-Key") == hashlib.sha256(b"job").hexdigest()) == p["idempotency"] for h, _ in calls)
    assert (c.breaker is not None) == p["circuit_breaker"] and result.outcome == "FAILED_AFTER_RETRIES"
    rec = client.Attempt("a", 1, 0, 0, 429, None, 2, retry_after="1")
    assert c.delay(rec, random.Random(1))[0] == (.5 if index < 2 else 1)
    assert client.retry_after("date") is None and client.retry_after("１") is None
    assert all(client.retryable(status=n) == (n == 429 or n >= 500) for n in (200,400,401,408,429,500,503,599))
    assert [client.retryable(failure=n) for n in ("TIMEOUT", "CONNECTION_FAILURE", "CANCELLED", "PROGRAMMING_ERROR")] == [True, True, False, False]
    assert 0 <= client.backoff(8, random.Random(0)) <= t["client_semantics"]["backoff"]["cap_seconds"] == 2.0
    assert client.backoff(1, random.Random(1)) == .033591061028100305
    assert t["client_semantics"]["backoff"]["base_seconds"] == .25
    async def timed_transport(data, headers, timeout):
        now[0] += timeout
        return client.HTTPReply(503, {}, b"")
    now[0] = 0.0
    timed = client.Client(cfg, timed_transport, clock=lambda: now[0], sleep=advance)
    outcome = asyncio.run(timed.submit("job", "synthetic"))
    assert outcome.outcome == ("FAILED_AFTER_RETRIES" if index == 0 else "DEADLINE_EXCEEDED")
    assert now[0] == (21.5 if index == 0 else p["logical_deadline_seconds"])


def test_breaker_and_percentile():
    t, _ = documents()
    p = t["client_semantics"]["breaker"]
    assert (p["window"], p["minimum_samples"], p["failure_fraction"], p["open_seconds"]) == (20,10,.5,5)
    now = [0.0]
    b = client.Breaker(lambda: now[0])
    assert b.history.maxlen == p["window"]
    for i in range(10): b.observe(False, client.Attempt("a",1,0,0,503 if i < 5 else 200,None,2), i >= 5)
    assert b.state == "OPEN" and b.admit() is None
    now[0] = p["open_seconds"]
    assert b.admit() is True and b.admit() is None
    b.observe(True, client.Attempt("a",1,0,0,429,None,2), False)
    assert b.state == "HALF_OPEN" and b.admit() is True
    b.observe(True, client.Attempt("a",1,0,0,200,None,2), True)
    assert b.state == "CLOSED" and not b.history
    assert harness.p99(list(range(100))) == 98


def test_decision_and_guardrail_contract():
    d = documents()[0]["decision"]
    assert d["zero_baseline"] == dict(both_zero="NO MEASURABLE EFFECT unless a guardrail is violated",
        candidate_positive="HARMFUL if candidate median exceeds raw baseline range; otherwise NO MEASURABLE EFFECT, unless a guardrail is violated",
        ratios="I and S are null when B=0; compare raw nonnegative values; never divide by zero")
    assert [m["primary"] for m in d["metrics"].values()] == ["p99_logical_latency_seconds", "attempts_per_logical_request", "outage_provider_attempts_per_outage_issued_job", "duplicate_provider_executions"]
    assert [len(m["guardrails"]) for m in d["metrics"].values()] == [3,2,3,2]
    assert d["metrics"]["S3"]["efficacy_gate"] == "C4_EFFECTIVE requires zero duplicates in each of three C4 runs and positive duplicates in each corresponding C0 run"
    assert d["comparisons"]["stepwise"] == ["C0:C1", "C1:C2", "C2:C3", "C3:C4"]
    assert d["relative_threshold"] == .2 and d["valid_runs"] == 3


@pytest.mark.parametrize("base,candidate,guard,expected", [([0,0,0],0,True,"NO MEASURABLE EFFECT"),
    ([0,0,0],1,True,"HARMFUL"), ([0,0,10],1,True,"NO MEASURABLE EFFECT"),
    ([100,100,100],80,True,"EFFECTIVE"), ([100,100,100],120,True,"HARMFUL"),
    ([80,100,120],80,True,"NO MEASURABLE EFFECT"), ([100]*3,50,False,"HARMFUL")])
def test_hand_checked_decision_examples(base, candidate, guard, expected):
    # Proposal reference calculation; official aggregation remains a P19 dependency.
    b, spread = median(base), max(base)-min(base)
    if not guard: actual = "HARMFUL"
    elif b == 0: actual = "HARMFUL" if candidate > spread else "NO MEASURABLE EFFECT"
    else:
        i, s, threshold = (b-candidate)/b, spread/b, documents()[0]["decision"]["relative_threshold"]
        actual = ("HARMFUL" if i <= -threshold and -i > s else "EFFECTIVE" if i >= threshold and i > s
                  else "NO MEASURABLE EFFECT" if abs(i) < threshold or i <= s else "INCONCLUSIVE")
    assert actual == expected


def test_public_evidence_and_source_hashes():
    t, _ = documents()
    pub = t["metadata"]["publication"]
    summary_raw = (PILOT / "exploratory-g6-summary-8c58d7960a9e4824a0a7421a9fa2038e.json").read_bytes()
    assert hashlib.sha256(summary_raw).hexdigest() == pub["summary_sha256"] == "8906b5608503f1fc593f283e304ff516effd90ff98d9648809299b2f7f51d66d"
    assert pub["plan_source_sha256"] == "e2045505ba484101d82d7e0d05d01bbc8896a3e7976f89a3bd8de5df3cebc5ff"
    assert pub["log_source_sha256"] == "d5d03cff8302ac052465ac92980bb8dc4a68c03b9914e1b14944b55f0799a822"
    plan_raw, log_raw = [(PILOT / n).read_bytes() for n in ("exploratory-g6-plan-public.json", "exploratory-g6-batch-public.txt")]
    assert hashlib.sha256(plan_raw).hexdigest() == pub["plan_public_sha256"]
    assert hashlib.sha256(log_raw).hexdigest() == pub["log_public_sha256"]
    assert log_raw.decode().splitlines()[0] == pub["log_header"]
    plan, summary = json.loads(plan_raw), json.loads(summary_raw)
    lines = log_raw.decode().splitlines()[1:]
    begins = [json.loads(l[10:]) for l in lines if l.startswith("RUN_BEGIN ")]
    ends = [json.loads(l[9:]) for l in lines if l.startswith("RUN_EXIT ")]
    receipts = [json.loads(l) for l in lines if l.startswith("{")]
    assert len(begins) == len(ends) == len(receipts) == len(plan["runs"]) == len(summary["runs"]) == 9
    candidates = [ROOT / "bench/thresholds.yaml", ROOT / "bench/scenarios.yaml", Path(__file__), PILOT / "exploratory-g6-plan-public.json", PILOT / "exploratory-g6-batch-public.txt", PILOT / "exploratory-g6-summary-8c58d7960a9e4824a0a7421a9fa2038e.json"]
    for p, r, begin, end, receipt in zip(plan["runs"], summary["runs"], begins, ends, receipts, strict=True):
        assert all(p[k] == r[k] for k in ("order", "scenario", "configuration", "seed", "command", "workload_sha256"))
        assert begin == {k: p[k] for k in ("order", "scenario", "configuration", "seed")} and end == dict(order=p["order"], exit_code=0)
        assert re.fullmatch(r"results/pilot/exploratory-S[0-3]-C[0-4]-1-[a-f0-9]{32}\.json", r["result_file"])
        path = ROOT / r["result_file"]
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == r["sha256"] == receipt["sha256"]
        assert receipt["result_file"] == "<PROJECT_ROOT>/" + r["result_file"]
        data = json.loads(raw)
        assert data["run"]["schedule"] == [asdict(j) for j in harness.generate_workload(p["scenario"], p["seed"])]
        assert harness.validate_run(data["run"], data["ledger"], data["metadata"]) == {"status": "VALID", "reasons": []}
        candidates.append(path)
    assert lines[-2:] == ["BATCH_COMPLETED completed=9", "BATCH_EXIT exit_code=0 tee_exit=0"]
    assert len(candidates) == 15 and all(not re.search(rb"/(?:home|Users)/[^/\s]+/", p.read_bytes()) for p in candidates)
    for name, digest in t["metadata"]["proposed_source_sha256"].items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
