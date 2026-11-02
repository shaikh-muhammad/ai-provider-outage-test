"""Runner unit checks: mocked processes/HTTP and deterministic clocks only."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from outage_test import runner
from outage_test.harness import PlannedJob, p99, validate_run
from outage_test.provider import DEFAULTS
from test_harness import fixture


def context():
    return dict(environment={"git_commit": "synthetic"}, provider_counters={},
                provider_config=DEFAULTS.copy(), errors=[], registration="unavailable", t0=100, provider_t0=100,
                lifecycle=dict(pid=123, reaped=True, returncode=0,
                               forced_kill=False, unexpected_exit=False))


def measured():
    run, ledger, meta = fixture()
    meta["health"] = runner.measured_health([0, .001, .003], run["records"], .1, context())
    return run, ledger, meta


@pytest.mark.parametrize("raw", [b"", b"[]", b"null", b"{", b"x" * 1025,
    b'{"client_port":1}', b'{"client_port":true,"admin_port":2}',
    b'{"client_port":0,"admin_port":2}', b'{"client_port":65536,"admin_port":2}',
    b'{"client_port":1,"admin_port":1}', b'{"client_port":1,"admin_port":2,"extra":1}'])
def test_readiness_rejects_invalid(raw):
    with pytest.raises(ValueError, match="readiness"):
        runner.readiness(raw)


def test_startup_private_token_and_failure_cleanup():
    async def run():
        process = Mock(pid=123, returncode=None)
        process.stdin.drain = AsyncMock()
        process.stdout.readline = AsyncMock(return_value=b'{"client_port":1234,"admin_port":5678}\n')
        with patch.object(runner.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)) as spawn, \
             patch.object(runner.secrets, "token_hex", return_value="0" * 64):
            actual, ports, token = await runner.start_provider()
            assert actual is process and ports == dict(client_port=1234, admin_port=5678)
            assert token == "0" * 64
            process.stdin.write.assert_called_once_with((token + "\n").encode())
            process.stdin.close.assert_called_once()
            assert spawn.call_args.args == (runner.sys.executable, "-m", "outage_test.provider")
            assert token not in str(spawn.call_args)
            process.stdout.readline.return_value = b"invalid"
            with patch.object(runner, "stop_provider", AsyncMock()) as cleanup:
                with pytest.raises(ValueError):
                    await runner.start_provider()
                cleanup.assert_awaited_once_with(process)
    asyncio.run(run())


def test_harvest_error_preserves_completed_records_and_measured_cpu():
    async def run():
        run_data, receipts, _ = fixture()
        evidence = dict(run={"schedule": [], "records": []}, ledger=[], samples=[], cpu_seconds=0)
        ledger = dict(config=DEFAULTS.copy(), attempts=receipts, t0=100, rejected=0)
        with patch.object(runner, "prepare", AsyncMock(return_value=100)), \
             patch.object(runner, "sample_loop", AsyncMock()), \
             patch.object(runner, "run_schedule", AsyncMock(return_value=run_data)), \
             patch.object(runner, "request", AsyncMock(side_effect=[ledger, ValueError("counter failure")])), \
             patch.object(runner.time, "process_time", side_effect=[1, 1.5]):
            with pytest.raises(ValueError, match="counter failure"):
                await runner.exercise(None, dict(client_port=1, admin_port=2), "synthetic", "C0",
                                      DEFAULTS.copy(), [], context(), evidence)
        assert evidence["run"] == run_data and evidence["ledger"] == receipts
        assert evidence["cpu_seconds"] == .5
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["graceful", "kill", "exited"])
def test_bounded_cleanup(mode):
    async def run():
        process = Mock(pid=123, returncode=9 if mode == "exited" else None)
        calls = []
        async def wait():
            calls.append(1)
            if mode == "kill" and len(calls) == 1:
                raise TimeoutError
            process.returncode = -9 if mode == "kill" else (9 if mode == "exited" else 0)
            return process.returncode
        process.wait = wait
        result = await runner.stop_provider(process)
        assert result["reaped"] and result["returncode"] == process.returncode
        assert result["forced_kill"] == (mode == "kill")
        assert result["unexpected_exit"] == (mode == "exited")
        assert process.terminate.call_count == (0 if mode == "exited" else 1)
        assert process.kill.call_count == (1 if mode == "kill" else 0)
    asyncio.run(run())


def test_sampling_and_nearest_rank_p99():
    async def run():
        now, samples, stop = [10.0], [], asyncio.Event()
        async def sleep(delay):
            now[0] += delay + .003
            if len(samples) == 3:
                stop.set()
        await runner.sample_loop(samples, stop, lambda: now[0], sleep)
        assert samples == pytest.approx([.003] * 3)
        assert p99(samples) == pytest.approx(.003)
    asyncio.run(run())
    assert p99(list(range(100))) == 98
    for values in ([], [float("nan")], [-1]):
        with pytest.raises(ValueError):
            p99(values)


@pytest.mark.parametrize("damage", ["none", "missing", "p99", "count", "cpu", "flag",
    "unhealthy", "lifecycle", "error", "sample_mismatch", "frozen"])
def test_measured_metadata_validation(damage):
    run, ledger, meta = measured()
    h = meta["health"]
    if damage == "missing": del h["measured"]["loop_samples"]
    elif damage == "p99": h["measured"]["loop_lag_p99"] = 0
    elif damage == "count": h["measured"]["loop_sample_count"] = 99
    elif damage == "cpu": h["measured"]["cpu_seconds"] = float("nan")
    elif damage == "flag": h["loop_lag_healthy"] = False
    elif damage == "unhealthy":
        h["measured"]["loop_samples"] = [.1] * 3
        h["measured"]["loop_lag_p99"] = .1
        h["loop_lag_healthy"] = False
    elif damage == "lifecycle": h["context"]["lifecycle"]["unexpected_exit"] = True
    elif damage == "error": h["context"]["errors"].append("synthetic failure")
    elif damage == "sample_mismatch":
        h["measured"]["launch_samples"] = [.001]
        h["measured"]["launch_lag_p99"] = .001
    elif damage == "frozen": h["measured"]["limits"]["frozen"] = True
    result = validate_run(run, ledger, meta)
    assert result["status"] == ("VALID" if damage == "none" else "INVALID")


def test_admin_response_schemas():
    run, ledger, _ = fixture()
    value = dict(config=DEFAULTS.copy(), t0=10, rejected=0, attempts=ledger)
    assert runner.admin_response("/admin/ledger", value) == value
    assert runner.admin_response("/admin/counters", dict(buckets={"0": {"receipts": 1}}, rejected=0))
    for path, value in (("/health", []), ("/health", {"status": "wrong"}),
        ("/admin/ledger", {}), ("/admin/counters", {"buckets": [], "rejected": 0}),
        ("/admin/counters", {"buckets": {"0": {"unknown": 1}}, "rejected": 0})):
        with pytest.raises(ValueError):
            runner.admin_response(path, value)


def test_http_request_status_bounds_and_admin_header_isolation():
    async def run():
        response = SimpleNamespace(status=200)
        async def chunks(size):
            yield b'{"status":"ok"}'
        response.content = SimpleNamespace(iter_chunked=chunks)
        class Reply:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): return False
        session = Mock()
        session.request.return_value = Reply()
        assert await runner.request(session, 1234, "/health") == {"status": "ok"}
        call = session.request.call_args
        assert call.args == ("GET", "http://127.0.0.1:1234/health")
        assert call.kwargs["headers"] == {} and call.kwargs["allow_redirects"] is False
        with pytest.raises(ValueError):
            await runner.request(session, 1234, "/health", token="synthetic")
        with pytest.raises(ValueError):
            await runner.request(session, 1234, "/outside")
        response.status = 401
        assert await runner.request(session, 5678, "/admin/ledger", status=401) is None
        with pytest.raises(ValueError):
            await runner.request(session, 5678, "/admin/ledger")
    asyncio.run(run())


def test_prepare_checks_isolation_and_uses_past_t0():
    async def run():
        config = DEFAULTS.copy()
        async def respond(session, port, path, token=None, value=None, status=200):
            return dict(config=config) if path == "/admin/ledger" and status == 200 else None
        with patch.object(runner, "request", AsyncMock(side_effect=respond)) as request:
            assert await runner.prepare(None, dict(client_port=1, admin_port=2), "synthetic", config, lambda: 123) == 123
            assert request.call_args.kwargs == {}
            assert request.call_args.args[-1] == {"t0": 123}
            assert all(call.args[3:] == () for call in request.call_args_list if call.kwargs.get("status") in (401, 404))
    asyncio.run(run())


def test_pilot_preserves_evidence_and_cleans_up(tmp_path):
    async def run():
        run_data, receipts, _ = fixture()
        receipts[0]["executed"] = True
        process = Mock()
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        async def exercise(session, ports, token, configuration, config, jobs, context, evidence):
            evidence.update(run=deepcopy(run_data), ledger=deepcopy(receipts), samples=[0, .001], cpu_seconds=.1)
            context.update(t0=100, provider_t0=100)
            context["provider_config"] = config.copy()
            context["provider_counters"] = dict(buckets={"0": {"receipts": 1}}, rejected=0)
        lifecycle = context()["lifecycle"]
        with patch.object(runner, "ROOT", tmp_path), patch.object(runner, "environment", return_value={"git_commit": "synthetic"}), \
             patch.object(runner, "generate_workload", return_value=[PlannedJob("a", "synthetic", 0)]), \
             patch.object(runner, "start_provider", AsyncMock(return_value=(process, {}, "private-fixture-control-value"))), \
             patch.object(runner, "stop_provider", AsyncMock(return_value=lifecycle)) as cleanup, \
             patch.object(runner, "exercise", side_effect=exercise), \
             patch.object(runner.aiohttp, "TCPConnector") as connector, \
             patch.object(runner.aiohttp, "ClientSession", return_value=Session()) as session:
            summary = await runner.run_pilot("S0", "C0", 9001)
        cleanup.assert_awaited_once_with(process)
        connector.assert_called_once_with(limit=0)
        assert session.call_args.kwargs["trust_env"] is False
        doc = json.loads(Path(summary["result_file"]).read_bytes())
        assert summary["validity"]["status"] == "VALID" and summary["jobs"] == summary["receipts"] == 1
        h = doc["metadata"]["health"]
        assert h["measured"]["cpu_seconds"] == .1 and h["context"]["provider_counters"]["buckets"]
        assert "unavailable" in h["context"]["registration"]
        assert "private-fixture-control-value" not in json.dumps(doc)
    asyncio.run(run())


def test_sampler_fixed_deadlines_do_not_accumulate_wake_delay():
    async def run():
        now, samples, waits, accounting = [0.0], [], [], {}
        stop = asyncio.Event()
        async def sleep(delay):
            waits.append(delay)
            now[0] += delay + .003
            if len(samples) == 5:
                accounting["stop_requested_at"] = now[0]
                stop.set()
        await runner.sample_loop(samples, stop, lambda: now[0], sleep, accounting=accounting)
        assert samples == pytest.approx([.003] * 5)
        assert waits == pytest.approx([.01] + [.007] * 5)
        assert accounting["sample_count"] == 5
        assert accounting["scheduled_ticks"] == 6 and accounting["unobserved_tail_ticks"] == 1
        assert now[0] == pytest.approx(.063)  # Old recurrence would finish at .078.
    asyncio.run(run())


def test_sampler_retains_large_pause_and_observes_overdue_ticks():
    async def run():
        now, samples, waits, accounting = [0.0], [], [], {}
        stop = asyncio.Event()
        async def sleep(delay):
            waits.append(delay)
            now[0] += delay + (.035 if not waits[:-1] else 0)
            if len(samples) == 5:
                accounting["stop_requested_at"] = now[0]
                stop.set()
        await runner.sample_loop(samples, stop, lambda: now[0], sleep, accounting=accounting)
        assert samples == pytest.approx([.035, .025, .015, .005, 0])
        assert waits[:4] == pytest.approx([.01, 0, 0, 0])
        assert accounting["origin"] == 0 and accounting["method"] == "fixed-anchor-catch-up"
        assert accounting["scheduled_ticks"] == accounting["sample_count"] + accounting["unobserved_tail_ticks"]
    asyncio.run(run())


@pytest.mark.parametrize("damage", [None, "count", "tail", "window"])
def test_sampling_accounting_is_validated(damage):
    run, ledger, meta = measured()
    s = dict(method="fixed-anchor-catch-up", origin=100, stop_requested_at=100.041,
             finished_at=100.042, sample_count=3, scheduled_ticks=4, unobserved_tail_ticks=1)
    meta["health"]["context"]["sampling"] = s
    if damage == "count": s["sample_count"] = 2
    elif damage == "tail": s["unobserved_tail_ticks"] = 0
    elif damage == "window": s["finished_at"] = 99
    assert validate_run(run, ledger, meta)["status"] == ("VALID" if damage is None else "INVALID")


@pytest.mark.parametrize("held_p99,passed", [(.025, True), (.031, False)])
def test_pilot_v2_artifact_hashes_selection_and_health(tmp_path, held_p99, passed):
    import hashlib
    refs = {}
    def save(kind, doc):
        name = "exploratory-pilot-v2-" + kind + "-" + "a" * 32 + ".json"
        raw = json.dumps(doc).encode()
        with (tmp_path / name).open("xb") as stream:
            stream.write(raw)
        refs[kind + "_file"], refs[kind + "_sha256"] = name, hashlib.sha256(raw).hexdigest()
    save("calibration", dict(policy="prospective-pilot-v2", exploratory=True, phase="selection", frozen=False,
        windows=[dict(raw_samples=[v]) for v in (.020, .021, .022)], selected_loop_lag_ms=30,
        maximum_idle_p99_ms=22.0, safety_band_passed=True, sample_interval=.01, launch_lag_ms=50,
        predeclaration={"sha256": "a" * 64}))
    save("heldout", dict(policy="prospective-pilot-v2", exploratory=True, phase="held-out", frozen=False,
        window=dict(raw_samples=[held_p99]), selected_loop_lag_ms=30, passed=passed,
        selection=dict(path="results/pilot/" + refs["calibration_file"], sha256=refs["calibration_sha256"])))
    if not passed:
        with pytest.raises(ValueError, match="held-out"):
            runner.verified_pilot_policy(tmp_path, refs)
        return
    policy = runner.verified_pilot_policy(tmp_path, refs)
    assert policy["selected_loop_lag_ms"] == 30
    run, ledger, meta = measured()
    meta["health"]["context"]["pilot_policy"] = policy
    meta["health"] = runner.measured_health([0, .001, .003], run["records"], .1, meta["health"]["context"])
    assert meta["health"]["measured"]["limits"] == dict(loop_lag_p99=.030, launch_lag_p99=.050, frozen=False)
    assert validate_run(run, ledger, meta)["status"] == "VALID"
    meta["health"]["measured"]["limits"]["loop_lag_p99"] = .040
    assert validate_run(run, ledger, meta)["status"] == "INVALID"
    with pytest.raises(ValueError, match="hash mismatch"):
        runner.verified_pilot_policy(tmp_path, refs | {"calibration_sha256": "0" * 64})
    with pytest.raises(ValueError, match="filename"):
        runner.verified_pilot_policy(tmp_path, refs | {"calibration_file": "../escape.json"})
