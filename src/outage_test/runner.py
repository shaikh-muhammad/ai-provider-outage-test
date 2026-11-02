"""Exploratory loopback runner: python -m outage_test.runner S0 C0 9001.

CLI uses verified prospective pilot-v2 calibration; launch p99 stays <=50ms.
Legacy API calls retain v1's 20ms limit unless pilot_v2=True is requested.
These pilot limits are NOT FROZEN. One run per invocation; no automatic retry.
P12's writer retains counters/environment/lifecycle in health.context so its
strict top-level run/metadata schemas remain unchanged. No token is retained.
"""

import asyncio
from dataclasses import asdict
import hashlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import subprocess
import sys
import time

import aiohttp

from .client import Client, HTTPTransport
from .harness import FAULT_FIELDS, finite, generate_workload, p99, run_schedule, validate_run, write_pilot
from .provider import DEFAULTS

ROOT = Path(__file__).resolve().parents[2]
LIMITS = dict(loop_lag_p99=.020, launch_lag_p99=.050, frozen=False)
PILOT_V2_REFERENCES = dict(
    calibration_file="exploratory-pilot-v2-calibration-940b5ecc11294745935d626b3405ce02.json",
    calibration_sha256="c5e02ca40311bd65319f721969b44a5bb60ec0ab3ba75a5e2e69c50598f894ec",
    heldout_file="exploratory-pilot-v2-heldout-89d2b85402874a5abc7069678cfc3ff3.json",
    heldout_sha256="7f8e89ceddcb308d94f37399279f92995b29c792bb8df3174cf67147c2e69cb6")
ROUTES = {"/health", "/admin/reset", "/admin/configure", "/admin/start", "/admin/ledger", "/admin/counters"}


def readiness(raw):
    try:
        if type(raw) is not bytes or len(raw) > 1024:
            raise ValueError()
        value = json.loads(raw)
        if type(value) is not dict or set(value) != {"client_port", "admin_port"}:
            raise ValueError()
        if any(type(v) is not int or not 1 <= v <= 65535 for v in value.values()):
            raise ValueError()
        if value["client_port"] == value["admin_port"]:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("invalid provider readiness") from None


async def stop_provider(process):
    unexpected = process.returncode is not None
    forced = False
    if not unexpected:
        try:
            process.terminate()
        except ProcessLookupError:
            unexpected = True
    try:
        await asyncio.wait_for(process.wait(), 15)
    except TimeoutError:
        forced = True
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait_for(process.wait(), 5)
    return dict(pid=process.pid, returncode=process.returncode, reaped=True,
                forced_kill=forced, unexpected_exit=unexpected)


async def start_provider():
    process = None
    try:
        async with asyncio.timeout(10):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "outage_test.provider", cwd=ROOT,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=2048)
            token = secrets.token_hex(32)
            process.stdin.write(token.encode() + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            ports = readiness(await process.stdout.readline())
        return process, ports, token
    except BaseException:
        if process is not None:
            await stop_provider(process)
        raise


def admin_response(path, value):
    if type(value) is not dict:
        raise ValueError("non-object admin response")
    if path == "/admin/ledger":
        if (set(value) != {"config", "t0", "rejected", "attempts"} or
            type(value["config"]) is not dict or set(value["config"]) != set(DEFAULTS) or
            not finite(value["t0"]) or type(value["rejected"]) is not int or value["rejected"] < 0 or
            type(value["attempts"]) is not list or len(value["attempts"]) > 40000):
            raise ValueError("invalid ledger envelope")
    elif path == "/admin/counters":
        if (set(value) != {"buckets", "rejected"} or type(value["buckets"]) is not dict or
            type(value["rejected"]) is not int or value["rejected"] < 0):
            raise ValueError("invalid counter envelope")
        for name, counts in value["buckets"].items():
            if (not name.isascii() or not name.isdecimal() or type(counts) is not dict or
                set(counts) - {"receipts", "executed", "resource_overflow", "SERVED", "RATE_LIMITED", "ERROR_503", "HUNG", "LOST_REPLY", "DEDUPED"} or
                any(type(n) is not int or n < 0 for n in counts.values())):
                raise ValueError("invalid counters")
    elif value != {"status": "ok"}:
        raise ValueError("invalid acknowledgement")
    return value


async def request(session, port, path, token=None, value=None, status=200):
    if type(port) is not int or not 1 <= port <= 65535 or path not in ROUTES:
        raise ValueError("invalid loopback endpoint")
    if token is not None and not path.startswith("/admin/"):
        raise ValueError("admin credential on client endpoint")
    method = "POST" if path in {"/admin/reset", "/admin/configure", "/admin/start"} else "GET"
    headers = {} if token is None else {"X-Admin-Token": token}
    async with session.request(method, f"http://127.0.0.1:{port}{path}",
            headers=headers, json=value, allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=5)) as response:
        if response.status != status:
            raise ValueError("unexpected loopback HTTP status")
        if status != 200:
            return None
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > 16 * 1024 * 1024:
                raise ValueError("admin body too large")
        decoded = json.loads(body)
        json.dumps(decoded, allow_nan=False)
        return admin_response(path, decoded)


async def prepare(session, ports, token, configuration, clock=time.monotonic):
    client, admin = ports["client_port"], ports["admin_port"]
    await request(session, admin, "/admin/ledger", status=401)
    for path in ROUTES - {"/health"}:
        await request(session, client, path, status=404)
    await request(session, client, "/health")
    await request(session, admin, "/admin/reset", token, {})
    await request(session, admin, "/admin/configure", token, configuration)
    applied = await request(session, admin, "/admin/ledger", token)
    if applied["config"] != configuration:
        raise ValueError("provider configuration mismatch")
    t0 = clock()  # Captured before sending; never a future host timestamp.
    await request(session, admin, "/admin/start", token, {"t0": t0})
    return t0


async def sample_loop(samples, stop, clock=time.monotonic, sleep=asyncio.sleep, *, accounting=None):
    """Measure each fixed 10ms deadline, including overdue catch-up ticks.

    Actual wake time for tick i is origin + i*.01 + samples[i-1]. A stop can
    leave trailing ticks unobserved; account for them instead of inventing lag.
    """
    origin = clock()
    try:
        while not stop.is_set():
            due = origin + (len(samples) + 1) * .01
            await sleep(max(0, due - clock()))
            if stop.is_set():
                break
            samples.append(max(0, clock() - due))
            if len(samples) >= 70000:
                raise ValueError("health sample ceiling exceeded")
    finally:
        if accounting is not None:
            stopped = accounting.get("stop_requested_at", clock())
            ticks = max(0, int((stopped - origin) / .01))
            accounting.update(method="fixed-anchor-catch-up", origin=origin,
                stop_requested_at=stopped, finished_at=clock(), sample_count=len(samples),
                scheduled_ticks=ticks, unobserved_tail_ticks=max(0, ticks - len(samples)))


def verified_pilot_policy(directory=ROOT / "results" / "pilot", references=None):
    refs = PILOT_V2_REFERENCES if references is None else references
    if set(refs) != set(PILOT_V2_REFERENCES):
        raise ValueError("invalid calibration references")
    documents = []
    for kind in ("calibration", "heldout"):
        name, digest = refs[kind + "_file"], refs[kind + "_sha256"]
        if (type(name) is not str or not re.fullmatch(r"exploratory-pilot-v2-" + kind + r"-[a-f0-9]{32}\.json", name) or
            type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise ValueError("invalid calibration filename or hash")
        raw = (Path(directory) / name).read_bytes()
        if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("calibration hash mismatch")
        documents.append(json.loads(raw))
    cal, held = documents
    if (cal["policy"] != "prospective-pilot-v2" or held["policy"] != cal["policy"] or
        cal["exploratory"] is not True or held["exploratory"] is not True or
        cal["phase"] != "selection" or held["phase"] != "held-out" or
        cal["frozen"] is not False or held["frozen"] is not False or len(cal["windows"]) != 3):
        raise ValueError("invalid calibration provenance")
    training = [1000 * p99(w["raw_samples"]) for w in cal["windows"]]
    maximum = max(training)
    candidate = 5 * math.ceil((maximum + 5) / 5)
    held_value = 1000 * p99(held["window"]["raw_samples"])
    if (not 20 <= candidate <= 40 or candidate != cal["selected_loop_lag_ms"] or
        maximum != cal["maximum_idle_p99_ms"] or cal["safety_band_passed"] is not True or
        cal["sample_interval"] != .01 or cal["launch_lag_ms"] != 50 or
        held["selected_loop_lag_ms"] != candidate or held_value > candidate or held["passed"] is not True or
        held["selection"] != dict(path="results/pilot/" + refs["calibration_file"], sha256=refs["calibration_sha256"])):
        raise ValueError("calibration selection or held-out check failed")
    return dict(name="prospective-pilot-v2", **refs, training_p99_ms=training,
                maximum_idle_p99_ms=maximum, selected_loop_lag_ms=candidate,
                heldout_p99_ms=held_value, predeclaration_sha256=cal["predeclaration"]["sha256"])


def measured_health(loop_samples, records, cpu_seconds, context):
    policy = context.get("pilot_policy")
    limits = LIMITS.copy() if policy is None else dict(loop_lag_p99=policy["selected_loop_lag_ms"] / 1000, launch_lag_p99=.050, frozen=False)
    launch = [row["launch_lag"] for row in records]
    loop_value = p99(loop_samples) if loop_samples else None
    launch_value = p99(launch) if launch else None
    return dict(loop_lag_healthy=loop_value is not None and loop_value <= limits["loop_lag_p99"],
        launch_lag_healthy=launch_value is not None and launch_value <= limits["launch_lag_p99"],
        measured=dict(loop_samples=loop_samples, launch_samples=launch,
            loop_lag_p99=loop_value, launch_lag_p99=launch_value, cpu_seconds=cpu_seconds,
            loop_sample_count=len(loop_samples), launch_sample_count=len(launch),
            sample_interval=.01, limits=limits), context=context)


def environment():
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    versions = {}
    for lock in ("requirements-run.lock", "requirements-dev.lock"):
        for line in (ROOT / lock).read_text().splitlines():
            if "==" in line:
                name = line.split("==")[0]
                versions[name] = metadata.version(name)
    return dict(os=platform.system(), os_release=platform.freedesktop_os_release().get("PRETTY_NAME"),
                kernel=platform.release(), python=platform.python_version(), cpu_count=os.cpu_count(),
                dependencies=versions, git_commit=commit)


async def exercise(session, ports, token, configuration, config, jobs, context, evidence):
    t0 = await prepare(session, ports, token, config)
    context["t0"] = t0
    stop = asyncio.Event()
    accounting = {}
    sampler = asyncio.create_task(sample_loop(evidence["samples"], stop, accounting=accounting))
    cpu_start = time.process_time()
    try:
        client = Client(configuration, HTTPTransport(session, f"http://127.0.0.1:{ports['client_port']}"), config["seed"])
        evidence["run"].update(await run_schedule(jobs, client.submit, t0))
    finally:
        evidence["cpu_seconds"] = time.process_time() - cpu_start
        accounting["stop_requested_at"] = time.monotonic()
        stop.set()
        await sampler
        if "method" in accounting:
            context["sampling"] = accounting
    ledger = await request(session, ports["admin_port"], "/admin/ledger", token)
    context["provider_t0"] = ledger["t0"]
    evidence["ledger"].extend(ledger["attempts"])
    context["provider_config"] = ledger["config"]
    counters = await request(session, ports["admin_port"], "/admin/counters", token)
    context["provider_counters"] = counters
    if ledger["t0"] != t0 or ledger["config"] != config or ledger["rejected"] or counters["rejected"]:
        context["errors"].append("provider state mismatch")
    if sum(bucket.get("receipts", 0) for bucket in counters["buckets"].values()) != len(ledger["attempts"]):
        context["errors"].append("provider counter/ledger mismatch")


async def run_pilot(scenario="S0", configuration="C0", seed=9001, *, pilot_v2=False):
    jobs = generate_workload(scenario, seed)
    if configuration not in ("C0", "C1", "C2", "C3", "C4"):
        raise ValueError("invalid configuration")
    config = DEFAULTS | dict(scenario=scenario, seed=seed, idempotency=scenario == "S3")
    context = dict(environment=environment(), provider_counters={}, provider_config={},
                   lifecycle={}, errors=[], t0=None, provider_t0=None,
                   registration="unavailable: registration and registered checksums not created")
    if pilot_v2:
        context["pilot_policy"] = verified_pilot_policy()
    evidence = dict(run=dict(schedule=[asdict(j) for j in jobs], records=[]), ledger=[], samples=[], cpu_seconds=0.0)
    process = None
    started = time.monotonic()
    try:
        process, ports, token = await start_provider()
        # No hidden default 100-connection ceiling; task and provider bounds remain.
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0), trust_env=False) as session:
            await exercise(session, ports, token, configuration, config, jobs, context, evidence)
    except Exception as error:
        context["errors"].append(type(error).__name__)
    finally:
        if process is not None:
            context["lifecycle"] = await stop_provider(process)
    run, ledger = evidence["run"], evidence["ledger"]
    health = measured_health(evidence["samples"], run["records"], evidence["cpu_seconds"], context)
    expected = {key: config[key] for key in FAULT_FIELDS}
    applied = {key: context["provider_config"].get(key) for key in FAULT_FIELDS}
    meta = dict(health=health, expected_faults=expected, applied_faults=applied)
    directory = ROOT / "results" / "pilot"
    directory.mkdir(parents=True, exist_ok=True)
    path = write_pilot(directory, scenario, configuration, 1, run, ledger, meta)
    validity = validate_run(run, ledger, meta)
    return dict(result_file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        validity=validity, jobs=len(run["records"]), attempts=sum(len(r["attempts"]) for r in run["records"]),
        receipts=len(ledger), executions=sum(bool(r["executed"]) for r in ledger),
        health={k: v for k, v in health["measured"].items() if k not in ("loop_samples", "launch_samples")},
        lifecycle=context["lifecycle"], wall_seconds=time.monotonic() - started)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: python -m outage_test.runner SCENARIO CONFIG SEED")
    summary = asyncio.run(run_pilot(sys.argv[1], sys.argv[2], int(sys.argv[3]), pilot_v2=True))
    print(json.dumps(summary, allow_nan=False))
    raise SystemExit(0 if summary["validity"]["status"] == "VALID" else 1)
