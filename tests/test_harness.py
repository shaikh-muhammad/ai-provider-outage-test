"""Hand-checked offline scheduling and evidence fixtures; NOT FROZEN."""

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import json
from types import SimpleNamespace

import pytest

from outage_test.client import Attempt, LogicalResult
from outage_test.harness import PlannedJob, generate_workload, run_schedule, validate_run, write_pilot
from test_client import success


@pytest.mark.parametrize("scenario,total", [("S0", 600), ("S1", 440), ("S2", 600), ("S3", 600)])
def test_default_workloads(scenario, total):
    jobs = generate_workload(scenario, 7)
    assert len(jobs) == total and jobs[0].offset == 0
    assert len({j.job_id for j in jobs}) == total
    assert all(1 <= len(j.job_id) <= 256 and 1 <= len(j.text) <= 512 for j in jobs)
    assert all(0 <= j.offset < 60 for j in jobs)
    assert all(a.offset < b.offset for a, b in zip(jobs, jobs[1:]))
    if scenario == "S1":
        assert [sum(start <= j.offset < end for j in jobs) for start, end in ((0, 20), (20, 30), (30, 60))] == [120, 140, 180]
        assert jobs[119].offset == pytest.approx(20 - 1/6)
        assert jobs[120].offset == 20 and jobs[121].offset == pytest.approx(20 + 1/14)
        assert jobs[259].offset == pytest.approx(30 - 1/14)
        assert jobs[260].offset == 30 and jobs[-1].offset == pytest.approx(60 - 1/6)
    else:
        assert [j.offset for j in jobs] == [i / 10 for i in range(600)]
    with pytest.raises(FrozenInstanceError):
        jobs[0].offset = 1


def test_workload_seed_and_configuration_fairness():
    reference = generate_workload("S1", 42)
    for configuration in ("C0", "C1", "C2", "C3", "C4"):
        assert generate_workload("S1", 42) == reference
    different = generate_workload("S1", 43)
    assert reference != different and reference[0].text != different[0].text
    assert [j.offset for j in reference] == [j.offset for j in different]
    assert len(generate_workload("S1", 7, count=440)) == 440
    assert len(generate_workload("S0", 7, duration=2, rate=3, count=6)) == 6


@pytest.mark.parametrize("kwargs", [dict(scenario="S4"), dict(seed=-1), dict(seed=True),
    dict(seed=2**64), dict(duration=0), dict(duration=601), dict(duration=float("nan")),
    dict(rate=0), dict(rate=1.5), dict(rate=True), dict(rate=float("inf")),
    dict(rate=1000, duration=60), dict(count=599), dict(count=True), dict(burst_rate=0)])
def test_invalid_workload_inputs(kwargs):
    with pytest.raises(ValueError):
        generate_workload(**(dict(scenario="S0", seed=7) | kwargs))


def test_unknown_parameters_and_s1_count_are_rejected():
    with pytest.raises(TypeError):
        generate_workload("S0", 7, configuration="C4")
    with pytest.raises(ValueError):
        generate_workload("S1", 7, count=600)


def fixture():
    job = PlannedJob("a", "synthetic", 0)
    attempt = asdict(Attempt("wire-a", 1, 100, 100.2, 200, None, 2))
    run = dict(schedule=[asdict(job)], records=[dict(job_id="a", intended_offset=0,
        launch_offset=0, launch_lag=0, completion_offset=.2, latency=.2,
        outcome="SUCCESS", success=True, response=success("a"), attempts=[attempt], error=None)])
    ledger = [dict(attempt_id="server-99", correlation_id="wire-a", job_id="a",
                   finished=True, resource_overflow=False)]
    faults = dict(scenario="S2", seed=7, outage_start=20, outage_end=35,
                  rate=10, bucket_capacity=10, loss_fraction=.15)
    meta = dict(health=dict(loop_lag_healthy=True, launch_lag_healthy=True),
                expected_faults=faults, applied_faults=deepcopy(faults))
    return run, ledger, meta


def test_open_loop_launch_lag_intended_latency_and_projection():
    async def run():
        now, entered, release = [101.5], asyncio.Queue(), asyncio.Event()
        jobs = tuple(PlannedJob(str(i), "synthetic", i) for i in range(3))
        async def sleep(delay):
            now[0] += delay
        async def submit(job_id, text):
            start = now[0]
            entered.put_nowait(job_id)
            await release.wait()
            return LogicalResult(job_id, "SUCCESS", now[0] - start,
                [Attempt("wire-" + job_id, 1, start, now[0], 200, None, 2)], success(job_id))
        task = asyncio.create_task(run_schedule(jobs, submit, 100, clock=lambda: now[0], sleep=sleep))
        try:
            assert [await entered.get() for _ in jobs] == ["0", "1", "2"]
            assert not task.done()  # Every job launched while all completions are blocked.
            now[0] = 105
            release.set()
            evidence = await task
            assert evidence["schedule"] == [asdict(j) for j in jobs]
            rows = evidence["records"]
            assert [r["launch_offset"] for r in rows] == [1.5, 1.5, 2]
            assert [r["launch_lag"] for r in rows] == [1.5, .5, 0]
            assert [r["latency"] for r in rows] == [5, 4, 3]
            assert [r["completion_offset"] for r in rows] == [5] * 3
            assert rows[0]["attempts"][0] == asdict(Attempt("wire-0", 1, 101.5, 105, 200, None, 2))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_submit_exception_stays_invalid_and_cancellation_cleans_tasks():
    async def run():
        async def sleep(delay):
            pass
        async def broken(job_id, text):
            raise RuntimeError("synthetic")
        jobs = [PlannedJob("a", "synthetic", 0)]
        evidence = await run_schedule(jobs, broken, 0, clock=lambda: 0, sleep=sleep)
        assert evidence["records"][0]["outcome"] is None
        assert evidence["records"][0]["error"] == "RuntimeError"
        assert validate_run(evidence, [], fixture()[2])["status"] == "INVALID"
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def blocked(job_id, text):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        task = asyncio.create_task(run_schedule(jobs, blocked, 0, clock=lambda: 0, sleep=sleep))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
    asyncio.run(run())


@pytest.mark.parametrize("jobs", [[], [PlannedJob("a", "x", 0)] * 10001,
    [PlannedJob("a", "x", 1), PlannedJob("b", "x", 0)],
    [PlannedJob("a", "x", 0)] * 2, [PlannedJob("a", "x", float("nan"))]])
def test_invalid_schedule(jobs):
    with pytest.raises(ValueError):
        asyncio.run(run_schedule(jobs, None, 0, clock=lambda: 0))


def test_valid_reconciliation_uses_correlation_not_server_id():
    assert validate_run(*fixture()) == dict(status="VALID", reasons=[])


@pytest.mark.parametrize("damage", ["missing_logical", "extra_logical", "duplicate_logical", "outcome", "missing_outcome",
    "response", "duplicate_physical", "missing_ledger", "extra_ledger", "duplicate_receipt",
    "wrong_job", "wrong_window", "missing_faults", "missing_health", "loop_lag", "launch_lag", "overflow", "unfinished"])
def test_invalid_evidence(damage):
    run, ledger, meta = fixture()
    row = run["records"][0]
    if damage == "missing_logical": run["records"] = []
    elif damage == "extra_logical": run["records"].append(dict(row, job_id="extra"))
    elif damage == "duplicate_logical": run["records"].append(deepcopy(row))
    elif damage == "outcome": row["outcome"] = "UNKNOWN"
    elif damage == "missing_outcome": del row["outcome"]
    elif damage == "response": row["response"] = None
    elif damage == "duplicate_physical": row["attempts"].append(dict(row["attempts"][0], ordinal=2))
    elif damage == "missing_ledger":
        row["outcome"], row["success"], row["response"] = "FAILED_AFTER_RETRIES", False, None
        row["attempts"][0].update(status=None, failure="TIMEOUT")
        ledger.clear()
    elif damage == "extra_ledger": ledger.append(dict(ledger[0], attempt_id="extra", correlation_id="unknown"))
    elif damage == "duplicate_receipt": ledger.append(dict(ledger[0]))
    elif damage == "wrong_job": ledger[0]["job_id"] = "b"
    elif damage == "wrong_window": meta["applied_faults"]["outage_end"] = 34
    elif damage == "missing_faults": del meta["applied_faults"]["outage_start"]
    elif damage == "missing_health": del meta["health"]
    elif damage == "loop_lag": meta["health"]["loop_lag_healthy"] = False
    elif damage == "launch_lag": meta["health"]["launch_lag_healthy"] = False
    elif damage == "overflow": ledger[0]["resource_overflow"] = True
    elif damage == "unfinished": ledger[0]["finished"] = False
    result = validate_run(run, ledger, meta)
    assert result["status"] == "INVALID" and result["reasons"]


@pytest.mark.parametrize("run,ledger,meta", [({}, [], {}), (None, [], None),
    ({"schedule": [], "records": []}, [], {}), ({"schedule": [1], "records": [1]}, [], {})])
def test_malformed_evidence_fails_closed(run, ledger, meta):
    assert validate_run(run, ledger, meta)["status"] == "INVALID"


def test_exclusive_pilot_writer_preserves_valid_and_invalid(tmp_path, monkeypatch):
    directory = tmp_path / "results" / "pilot"
    directory.mkdir(parents=True)
    monkeypatch.setattr("outage_test.harness.uuid.uuid4", lambda: SimpleNamespace(hex="a" * 32))
    data = fixture()
    path = write_pilot(directory, "S2", "C4", 1, *data)
    saved = path.read_bytes()
    doc = json.loads(saved)
    assert doc["exploratory"] is True and doc["validity"]["status"] == "VALID"
    assert doc["run"]["schedule"] == data[0]["schedule"]
    with pytest.raises(FileExistsError):
        write_pilot(directory, "S2", "C4", 1, *data)
    assert path.read_bytes() == saved
    data[1].clear()
    other = write_pilot(directory, "S2", "C4", 2, *data)
    assert json.loads(other.read_bytes())["validity"]["status"] == "INVALID"


@pytest.mark.parametrize("scenario,config,slot", [("../S2", "C0", 1), ("S2", "../C4", 1),
    ("S4", "C0", 1), ("S2", "C5", 1), ("S2", "C4", "../1"), ("S2", "C4", True), ("S2", "C4", 101)])
def test_unsafe_filename_fields_rejected(tmp_path, scenario, config, slot):
    with pytest.raises(ValueError):
        write_pilot(tmp_path, scenario, config, slot, *fixture())
    assert not list(tmp_path.iterdir())


def test_official_traversal_and_symlink_directories_rejected(tmp_path):
    pilot = tmp_path / "results" / "pilot"
    pilot.mkdir(parents=True)
    official = pilot.parent / "official"
    official.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(pilot.parent, target_is_directory=True)
    for directory in (official, pilot / ".." / "pilot", alias / "pilot"):
        with pytest.raises(ValueError):
            write_pilot(directory, "S2", "C0", 1, *fixture())
    assert not list(official.iterdir()) and not list(pilot.iterdir())
