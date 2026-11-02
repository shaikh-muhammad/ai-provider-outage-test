"""Independent, offline audit of immutable exploratory evidence; not registration."""

from collections import Counter
from copy import deepcopy
import hashlib
import inspect
import json
import math
from pathlib import Path
import re

import pytest

from outage_test.harness import validate_run
from outage_test import runner

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "results" / "pilot"
EXPECTED = {
    "exploratory-S0-C0-1-9cce03d3509f47348dbb50835a725274.json": "5a93b627bad33ed04ac260817a42a0411ae62ab0fa8b9f9ea9e36b78013a1b44",
    "exploratory-S0-C0-1-0480e9f4abc84937842b95d12da479d3.json": "9f88a06840e86787662302353fbd051e2fe983f51532acb3516a6ca1ef84697f",
    "exploratory-S0-C0-1-10cb863235eb4a3fbe72b4f422598830.json": "c6bb417d0341ee0cb1c4c6446fb894885bf2b91d7fb0523e17011083375db0f2",
    "exploratory-pilot-v2-rule-f8abeb01b79d48b58380e058c6d55220.json": "fcf9436735e4cc5d836d526d40438204a7c32b5473dac092414993f43f3bb54d",
    "exploratory-pilot-v2-calibration-940b5ecc11294745935d626b3405ce02.json": "c5e02ca40311bd65319f721969b44a5bb60ec0ab3ba75a5e2e69c50598f894ec",
    "exploratory-pilot-v2-heldout-89d2b85402874a5abc7069678cfc3ff3.json": "7f8e89ceddcb308d94f37399279f92995b29c792bb8df3174cf67147c2e69cb6",
}


@pytest.fixture(scope="module")
def artifacts():
    return {name: json.loads((PILOT / name).read_bytes()) for name in EXPECTED}


@pytest.fixture(scope="module")
def smoke(artifacts):
    return next(d for d in artifacts.values() if d.get("validity", {}).get("status") == "VALID")


def nearest_p99(samples):
    assert samples and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in samples)
    return sorted(samples)[math.ceil(len(samples) * .99) - 1]


def audit(document, directory=PILOT):
    result = validate_run(document["run"], document["ledger"], document["metadata"])
    if result != dict(status="VALID", reasons=[]):
        raise ValueError("run evidence rejected: " + str(result["reasons"]))
    policy = document["metadata"]["health"]["context"]["pilot_policy"]
    refs = {key: policy[key] for key in runner.PILOT_V2_REFERENCES}
    assert runner.verified_pilot_policy(directory, refs) == policy


def test_manifest_is_exact_and_every_file_matches():
    manifest = json.loads((PILOT / "manifest.json").read_bytes())
    assert set(manifest) == {"schema_version", "exploratory", "registered", "official_threshold", "artifacts"}
    assert manifest["schema_version"] == 1 and manifest["exploratory"] is True
    assert manifest["registered"] is False and manifest["official_threshold"] is None
    entries = manifest["artifacts"]
    assert len(entries) == 6 and {e["name"]: e["sha256"] for e in entries} == EXPECTED
    for entry in entries:
        path = PILOT / entry["name"]
        assert path.is_file() and not path.is_symlink()
        raw = path.read_bytes()
        assert len(raw) == entry["size_bytes"] and len(raw) <= 16 * 1024 * 1024
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        if entry["kind"] == "smoke":
            d = json.loads(raw)
            assert (entry["scenario"], entry["configuration"], entry["seed"], entry["original_status"]) == (
                d["scenario"], d["configuration"], d["metadata"]["expected_faults"]["seed"], d["validity"]["status"])


@pytest.mark.parametrize("seed,status", [(9001, "INVALID"), (9002, "INVALID"), (9003, "VALID")])
def test_original_statuses_and_independent_revalidation(artifacts, seed, status):
    d = next(d for d in artifacts.values() if d.get("metadata", {}).get("expected_faults", {}).get("seed") == seed)
    assert d["exploratory"] is True and d["scenario"] == "S0" and d["configuration"] == "C0"
    assert d["validity"]["status"] == status
    assert validate_run(d["run"], d["ledger"], d["metadata"])["status"] == status
    if status == "VALID":
        audit(d)


def test_exact_counts_and_receipt_reconciliation(smoke):
    run, ledger = smoke["run"], smoke["ledger"]
    assert len(run["schedule"]) == len(run["records"]) == len(ledger) == 600
    assert len({j["job_id"] for j in run["schedule"]}) == 600
    assert Counter(r["outcome"] for r in run["records"]) == {"SUCCESS": 600}
    assert sum(r["success"] for r in run["records"]) == sum(r["executed"] for r in ledger) == 600
    attempts = [(a["attempt_id"], r["job_id"]) for r in run["records"] for a in r["attempts"]]
    receipts = [(r["correlation_id"], r["job_id"]) for r in ledger]
    assert len(attempts) == len(set(attempts)) == len({a[0] for a in attempts}) == 600
    assert Counter(attempts) == Counter(receipts) and len(set(receipts)) == 600
    assert len({r["attempt_id"] for r in ledger}) == 600
    assert all(r["finished"] and not r["resource_overflow"] and r["decision"] == "SERVED" for r in ledger)
    assert all(r["execution_ordinal"] == 1 and r["idempotency_key"] is None for r in ledger)
    counters = smoke["metadata"]["health"]["context"]["provider_counters"]
    assert counters["rejected"] == 0
    for name, expected in (("receipts", 600), ("executed", 600), ("DEDUPED", 0), ("resource_overflow", 0)):
        assert sum(b.get(name, 0) for b in counters["buckets"].values()) == expected


def test_responses_are_independently_derived_from_text(smoke):
    planned = {j["job_id"]: j for j in smoke["run"]["schedule"]}
    receipts = {r["correlation_id"]: r for r in smoke["ledger"]}
    for row in smoke["run"]["records"]:
        response, job = row["response"], planned[row["job_id"]]
        score = int.from_bytes(hashlib.sha256(job["text"].encode()).digest(), "big") / (2**256 - 1)
        assert set(response) == {"request_id", "job_id", "label", "score"}
        assert type(response["request_id"]) is str and response["request_id"]
        assert response["job_id"] == job["job_id"]
        assert type(response["score"]) is float and response["score"] == score
        assert response["label"] == ("POSITIVE" if score >= .5 else "NEGATIVE")
        assert response["request_id"] == receipts[row["attempts"][0]["attempt_id"]]["attempt_id"]


def test_faults_measured_health_and_sampling(smoke):
    meta, health = smoke["metadata"], smoke["metadata"]["health"]
    measured, context = health["measured"], health["context"]
    assert meta["applied_faults"] == meta["expected_faults"]
    assert meta["applied_faults"] == {k: context["provider_config"][k] for k in meta["expected_faults"]}
    assert context["t0"] == context["provider_t0"] and context["errors"] == []
    assert measured["limits"] == dict(frozen=False, loop_lag_p99=.035, launch_lag_p99=.050)
    for name in ("loop", "launch"):
        samples = measured[name + "_samples"]
        assert len(samples) == measured[name + "_sample_count"]
        assert measured[name + "_lag_p99"] == nearest_p99(samples) <= measured["limits"][name + "_lag_p99"]
        assert health[name + "_lag_healthy"] is True
    assert measured["launch_samples"] == [r["launch_lag"] for r in smoke["run"]["records"]]
    sampling = context["sampling"]
    assert sampling["method"] == "fixed-anchor-catch-up" and measured["sample_interval"] == .01
    assert sampling["scheduled_ticks"] == int((sampling["stop_requested_at"] - sampling["origin"]) / .01)
    assert sampling["scheduled_ticks"] == sampling["sample_count"] + sampling["unobserved_tail_ticks"]
    assert sampling["sample_count"] == measured["loop_sample_count"] and sampling["unobserved_tail_ticks"] == 0


def test_prospective_calibration_uses_only_training_windows(artifacts, smoke):
    rule = next(d for d in artifacts.values() if d.get("phase") == "predeclaration")
    cal = next(d for d in artifacts.values() if d.get("phase") == "selection")
    held = next(d for d in artifacts.values() if d.get("phase") == "held-out")
    assert cal["rule"] == rule["rule"] and cal["predeclaration"]["sha256"] in EXPECTED.values()
    assert cal["frozen"] is held["frozen"] is False and len(cal["windows"]) == 3
    maximum = max(nearest_p99(w["raw_samples"]) * 1000 for w in cal["windows"])
    assert maximum == cal["maximum_idle_p99_ms"]
    candidate = math.ceil((maximum + 5) / 5) * 5
    assert 20 <= candidate == cal["selected_loop_lag_ms"] == held["selected_loop_lag_ms"] == 35 <= 40
    assert nearest_p99(held["window"]["raw_samples"]) * 1000 <= candidate and held["passed"] is True
    assert cal["launch_lag_ms"] == 50 and cal["sample_interval"] == .01
    assert hashlib.sha256(inspect.getsource(runner.sample_loop).encode()).hexdigest() == cal["sampler_sha256"]
    end = rule["recorded_monotonic"]
    for window in cal["windows"] + [held["window"]]:
        a = window["accounting"]
        assert a["origin"] >= end and 30 <= window["elapsed_monotonic_seconds"] < 31
        assert nearest_p99(window["raw_samples"]) == window["p99"]
        assert len(window["raw_samples"]) == a["sample_count"]
        assert a["sample_count"] + a["unobserved_tail_ticks"] == a["scheduled_ticks"]
        end = a["finished_at"]
    assert runner.verified_pilot_policy() == smoke["metadata"]["health"]["context"]["pilot_policy"]


@pytest.mark.parametrize("path,value", [
    (("ledger", 0, "job_id"), "unknown"), (("ledger", 0, "executed"), False),
    (("ledger", 0, "decision"), "HUNG"), (("ledger", 0, "execution_ordinal"), 2),
    (("ledger", 0, "finished"), False), (("ledger", 0, "resource_overflow"), True),
    (("run", "records", 0, "response", "score"), -1),
    (("metadata", "applied_faults", "outage_end"), 36),
    (("metadata", "health", "measured", "loop_lag_p99"), 0),
    (("metadata", "health", "measured", "loop_samples"), [0]),
    (("metadata", "health", "context", "provider_counters", "buckets", "0", "executed"), 0),
])
def test_tampered_evidence_fails(smoke, tmp_path, path, value):
    bad = deepcopy(smoke)
    parent = bad
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    target = tmp_path / "damaged.json"
    target.write_text(json.dumps(bad, allow_nan=False))
    with pytest.raises(ValueError):
        audit(json.loads(target.read_bytes()))


@pytest.mark.parametrize("field", ["attempt_id", "correlation_id"])
def test_duplicate_provider_identifiers_fail(smoke, field):
    bad = deepcopy(smoke)
    bad["ledger"][1][field] = bad["ledger"][0][field]
    assert validate_run(bad["run"], bad["ledger"], bad["metadata"])["status"] == "INVALID"


@pytest.mark.parametrize("damage", ["reference", "hash", "bytes"])
def test_calibration_corruption_rejected(smoke, tmp_path, damage):
    bad = deepcopy(smoke)
    p = bad["metadata"]["health"]["context"]["pilot_policy"]
    for kind in ("calibration", "heldout"):
        name = p[kind + "_file"]
        (tmp_path / name).write_bytes((PILOT / name).read_bytes())
    if damage == "reference": p["calibration_file"] = "../calibration.json"
    elif damage == "hash": p["calibration_sha256"] = "0" * 64
    else:
        with (tmp_path / p["calibration_file"]).open("ab") as stream:
            stream.write(b" ")
    with pytest.raises(ValueError):
        audit(bad, tmp_path)


def test_no_official_results_or_credentials():
    assert not any(p.is_file() for p in (ROOT / "results" / "official").rglob("*"))
    patterns = {
        "private-key": rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
        "github": rb"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})",
        "openai": rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}",
        "aws": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        "slack": rb"\bxox[baprs]-[A-Za-z0-9-]{10,}",
        "admin-token": rb'(?i)(?:"(?:admin_token|token|X-Admin-Token)"\s*:|X-Admin-Token\s*[:=])',
    }
    paths = list(PILOT.glob("*.json")) + [p for p in (ROOT / "logs").rglob("*") if p.is_file()]
    for path in paths:
        raw = path.read_bytes()
        for category, pattern in patterns.items():
            assert not re.search(pattern, raw), (category, str(path.relative_to(ROOT)))
        if path.suffix == ".json":
            def reject_constant(value):
                raise ValueError("nonfinite JSON number")
            json.loads(raw, parse_constant=reject_constant)
