"""Frozen official capture and create-only resume. --execute needs separate authorization.

Preflight is offline and read-only. Interrupted/unsafe captures leave an exclusive
reservation that blocks resume rather than hiding a possibly executed attempt.
"""
import argparse
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import random
import re
import stat
import subprocess
import sys
import time
import uuid

import aiohttp
import yaml
from outage_test import harness, provider, runner

ROOT = Path(__file__).resolve().parents[1]
FREEZE_COMMIT = "ba0dc873b266f8b5753cbafb266e7c7ebdd38952"
LOCK_SHA = "25dcd33dec9057c5257901369e01e6743252031807dd75e464a930f57cce4a0a"
YAMLS = ("bench/thresholds.yaml", "bench/scenarios.yaml")
SOURCES = tuple(f"src/outage_test/{n}.py" for n in ("client", "provider", "faults", "harness", "runner"))
LIMIT = 16 * 1024 * 1024
MAX_JSON_DEPTH = 16  # Honest official records use at most seven container levels.
LEDGER_FIELDS = set("attempt_id job_id arrival_time idempotency_key correlation_id decision executed in_flight_at_arrival service_time execution_ordinal resource_overflow finished completion_time".split())


def require(condition, message="invalid official evidence"):
    if not condition:
        raise ValueError(message)


def encoded(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (RecursionError, OverflowError):
        raise ValueError("malformed official evidence") from None


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = dict(pairs)
    require(len(value) == len(pairs), "duplicate JSON field")
    return value


def bounded_json(raw):
    require(type(raw) is bytes and len(raw) <= LIMIT, "JSON size/type limit")
    # Mask complete quoted strings in the regex engine; count only container
    # boundaries in Python. The decoder still checks all JSON syntax afterward.
    structural = re.sub(rb'"[^"\\]*(?:\\.[^"\\]*)*"', b'""', raw)
    depth = 0
    for token in re.finditer(rb'[\[{]+|[\]}]+', structural):
        chars = token.group()
        depth += len(chars) if chars[0] in (91, 123) else -len(chars)
        require(0 <= depth <= MAX_JSON_DEPTH, "JSON nesting limit")
    require(depth == 0, "malformed official JSON")


def decoded(raw):
    bounded_json(raw)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        require(type(value) is dict, "official JSON must be an object")
        encoded(value)
        return value
    except (RecursionError, OverflowError):
        raise ValueError("malformed official JSON") from None


def read_fd(fd):
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= LIMIT)
    with os.fdopen(os.dup(fd), "rb") as stream:
        raw = stream.read(LIMIT + 1)
    require(len(raw) == info.st_size and len(raw) <= LIMIT)
    return raw


def read_path(path):
    require(path.absolute() == path.resolve(), "symlink input")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return read_fd(fd)
    finally:
        os.close(fd)


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=5)
    require(result.returncode == 0, "Git provenance check failed")
    return result.stdout


def preflight(root=ROOT, ancestry=None):
    # The approved byte anchors also reject every unknown/missing critical field,
    # including client policies and S0/S2 definitions, without a second mutable schema.
    raw = read_path(root / "bench/registration.lock.json")
    require(digest(raw) == LOCK_SHA, "registration lock hash mismatch")
    lock = decoded(raw)
    documents = []
    for name in YAMLS:
        raw = read_path(root / name)
        require(digest(raw) == lock["yaml_sha256"][name], "frozen YAML hash mismatch")
        documents.append(yaml.safe_load(raw))
    t, s = documents
    require(all(d["status"] == "FROZEN" and d["frozen"] is True for d in documents))
    names = SOURCES + ("requirements-run.lock", "requirements-dev.lock", ".github/workflows/ci.yml")
    for name in names:
        require(digest(read_path(root / name)) == t["metadata"]["proposed_source_sha256"][name], "implementation hash mismatch")
    require({n: t["metadata"]["proposed_source_sha256"][n] for n in SOURCES} == lock["implementation_sha256"])
    if ancestry is None:
        git(root, "merge-base", "--is-ancestor", FREEZE_COMMIT, "HEAD")
    else:
        require(ancestry(FREEZE_COMMIT) is True, "wrong frozen Git ancestry")
    cells = [(a, b) for a in harness.SCENARIOS for b in s["scenarios"][a]["configurations"]]
    expected, rng = [], random.Random(72026)
    for number in range(1, 4):
        shuffled = cells.copy()
        rng.shuffle(shuffled)
        expected.extend(dict(ordinal=(number-1)*17+i+1, scenario=a, config=b, round=number,
            run_slot=number, workload_seed=12000+number, fault_seed=12000+number) for i, (a, b) in enumerate(shuffled))
    require(len(set(cells)) == 17 and len(s["order"]) == 51 and s["order"] == expected)
    require(digest(encoded(s["order"])) == lock["order_sha256"])
    require(lock["health_limits"] == dict(sample_interval_seconds=.01, loop_lag_p99_seconds=.035, launch_lag_p99_seconds=.05))
    require(t["invalid_handling"]["max_total_attempts_per_slot"] == 3)
    for scenario, row in s["scenarios"].items():
        require(len(harness.generate_workload(scenario, 12001)) == row["jobs"])
    pins = dict(line.split("==") for n in ("requirements-run.lock", "requirements-dev.lock")
                for line in read_path(root / n).decode().splitlines() if "==" in line)
    require(all(runner.metadata.version(n) == v for n, v in pins.items()), "dependency version mismatch")
    return dict(thresholds=t, scenarios=s, lock=lock, dependencies=pins,
        fingerprint=dict(lock_sha256=LOCK_SHA, yaml_sha256=lock["yaml_sha256"], implementation_sha256=lock["implementation_sha256"]))


def identity(root):
    git(root, "diff", "HEAD", "--exit-code")
    raw = read_path(root / "bench/run_official.py")
    require(git(root, "show", "HEAD:bench/run_official.py") == raw, "official runner must be committed")
    return dict(version=1, sha256=digest(raw), git_commit=git(root, "rev-parse", "HEAD").decode().strip())


def private_check(value, token=None):
    raw = encoded(value)
    bounded_json(raw)
    pattern = rb'(?:^|[\s"=:])/(?:[^\s"/]+/)+|[A-Za-z]:\\\\|-----BEGIN .*PRIVATE KEY-----|\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,}|sk-(?:proj-)?[A-Za-z0-9_-]{32,})\b|"(?:admin_token|password|api_key|access_token)"\s*:'
    require(not re.search(pattern, raw, re.I), "unsafe evidence; value withheld")
    require(token is None or token.encode() not in raw, "unsafe evidence; value withheld")
    return raw


def config(slot):
    return provider.DEFAULTS | dict(scenario=slot["scenario"], seed=slot["fault_seed"], idempotency=slot["scenario"] == "S3")


@lru_cache(maxsize=12)  # Four frozen scenarios times three seeds; immutable bytes only.
def expected_schedule(scenario, seed):
    return encoded([asdict(j) for j in harness.generate_workload(scenario, seed)])


def assess(document, registration, who):
    require(type(document) is dict)
    require(set(document) == set("schema_version official slot attempt registration runner environment run ledger metadata wall_seconds validity".split()))
    require(type(document["schema_version"]) is int and document["schema_version"] == 1 and document["official"] is True)
    slot, attempt = document["slot"], document["attempt"]
    require(type(slot) is dict and type(slot.get("ordinal")) is int and 1 <= slot["ordinal"] <= 51)
    require(encoded(slot) == encoded(registration["scenarios"]["order"][slot["ordinal"]-1]), "slot/seed mismatch")
    require(type(attempt) is int and 1 <= attempt <= 3)
    require(encoded(document["registration"]) == encoded(registration["fingerprint"]) and encoded(document["runner"]) == encoded(who), "provenance mismatch")
    private_check(document)
    reasons = []
    try:
        env, run, meta = document["environment"], document["run"], document["metadata"]
        require(set(env) == set("os os_release kernel python cpu_count dependencies git_commit".split()))
        require(all(type(env[k]) is str and 1 <= len(env[k]) <= 256 for k in ("os", "os_release", "kernel", "python", "git_commit")))
        require(type(env["cpu_count"]) is int and env["cpu_count"] > 0 and env["dependencies"] == registration["dependencies"])
        require(env["git_commit"] == who["git_commit"] and harness.finite(document["wall_seconds"], 0, 750))
        require(set(meta) == {"health", "expected_faults", "applied_faults"})
        health = meta["health"]
        require(set(health) == {"measured", "context", "loop_lag_healthy", "launch_lag_healthy"})
        m, context = health["measured"], health["context"]
        require(set(context) == set("environment provider_counters provider_config lifecycle errors registration t0 provider_t0 sampling".split()))
        require(context["environment"] == env and context["registration"] == registration["fingerprint"])
        require(context["provider_config"] == config(slot))
        require(m["limits"] == dict(loop_lag_p99=.035, launch_lag_p99=.05, frozen=True) and m["limits"]["frozen"] is True)
        require(set(context["lifecycle"]) == {"pid", "returncode", "reaped", "forced_kill", "unexpected_exit"})
        require(type(context["lifecycle"]["pid"]) is int and context["lifecycle"]["pid"] > 0)
        require(m["sample_interval"] == .01 and context["sampling"]["method"] == "fixed-anchor-catch-up")
        require(encoded(run["schedule"]) == expected_schedule(slot["scenario"], slot["workload_seed"]))
        require(all(set(r) in (LEDGER_FIELDS, LEDGER_FIELDS | {"aborted"}) for r in document["ledger"]))
        require(all(r["idempotency_key"] == (digest(r["job_id"].encode()) if slot["config"] == "C4" else None) for r in document["ledger"]))
        policy = registration["thresholds"]["clients"][slot["config"]]
        require(all((r["attempts"] or policy["circuit_breaker"]) and all(a["timeout"] <= policy["attempt_timeout_seconds"] for a in r["attempts"]) for r in run["records"]))
        faults = {k: config(slot)[k] for k in harness.FAULT_FIELDS}
        require(meta["expected_faults"] == meta["applied_faults"] == faults)
        require(context["sampling"]["origin"] <= context["t0"] + min(r["launch_offset"] for r in run["records"]))
        require(context["sampling"]["stop_requested_at"] >= context["t0"] + max(r["completion_offset"] for r in run["records"]))
        # Only adapt the legacy validator's marker in a private copy. Stored
        # measurements remain frozen; all official limits/provenance were checked above.
        compatible = deepcopy(meta)
        compatible["health"]["measured"]["limits"]["frozen"] = False
        reasons.extend(harness.validate_run(run, document["ledger"], compatible)["reasons"])
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        reasons.append("missing, malformed or inconsistent official evidence")
    return dict(status="INVALID" if reasons else "VALID", reasons=list(dict.fromkeys(reasons)))


async def reap(process):
    cleanup, interrupted = asyncio.create_task(runner.stop_provider(process)), False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    result = cleanup.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


async def collect(slot, attempt, registration, who, clock=time.monotonic):
    jobs = harness.generate_workload(slot["scenario"], slot["workload_seed"])
    env = runner.environment()
    context = dict(environment=env, provider_counters={}, provider_config={}, lifecycle={}, errors=[],
        registration=registration["fingerprint"], t0=None, provider_t0=None, sampling={})
    evidence = dict(run=dict(schedule=[asdict(j) for j in jobs], records=[]), ledger=[], samples=[], cpu_seconds=0.0)
    process, token, started = None, None, clock()
    try:
        async with asyncio.timeout(730):
            process, ports, token = await runner.start_provider()
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=0), trust_env=False) as session:
                await runner.exercise(session, ports, token, slot["config"], config(slot), jobs, context, evidence)
    except Exception as error:
        context["errors"].append(type(error).__name__)
    finally:
        if process is not None:
            context["lifecycle"] = await reap(process)
    health = runner.measured_health(evidence["samples"], evidence["run"]["records"], evidence["cpu_seconds"], context)
    health["measured"]["limits"] = dict(loop_lag_p99=.035, launch_lag_p99=.05, frozen=True)
    value = health["measured"]["loop_lag_p99"]
    health["loop_lag_healthy"] = value is not None and value <= .035
    expected = {k: config(slot)[k] for k in harness.FAULT_FIELDS}
    meta = dict(health=health, expected_faults=expected,
        applied_faults={k: context["provider_config"].get(k) for k in harness.FAULT_FIELDS})
    document = dict(schema_version=1, official=True, slot=deepcopy(slot), attempt=attempt,
        registration=registration["fingerprint"], runner=who, environment=env, run=evidence["run"],
        ledger=evidence["ledger"], metadata=meta, wall_seconds=clock()-started, validity=None)
    private_check(document, token)
    document["validity"] = assess(document, registration, who)
    return document


def child(fd, name, create=False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=fd)
            os.fsync(fd)
        except FileExistsError:
            pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)


@contextmanager
def official_root(root):
    require(root.absolute() == root.resolve(), "symlink root")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in ("results", "official"):
            following = child(fd, name, True)
            os.close(fd)
            fd = following
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


@contextmanager
def reservation(fd, slot):
    require(slot["scenario"] in harness.SCENARIOS and slot["config"] in (["C0", "C4"] if slot["scenario"] == "S0" else ["C0", "C1", "C2", "C3", "C4"]))
    require(type(slot["run_slot"]) is int and 1 <= slot["run_slot"] <= 3)
    scenario = child(fd, slot["scenario"], True)
    try:
        configuration = child(scenario, slot["config"], True)
        try:
            name = f"run-{slot['run_slot']}-{uuid.uuid4().hex}.json"
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=configuration)
            try:
                os.fsync(configuration)  # Persist the reservation before starting a provider.
                yield output, configuration, f"{slot['scenario']}/{slot['config']}/{name}"
            finally:
                os.close(output)
        finally:
            os.close(configuration)
    finally:
        os.close(scenario)


def finish(fd, directory, value):
    raw = private_check(value)
    require(os.fstat(fd).st_size == 0, "refuse existing result contents")
    with os.fdopen(os.dup(fd), "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(directory)
    return digest(raw)


def read_at(fd, name):
    incoming = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        raw = read_fd(incoming)
        return decoded(raw), digest(raw)
    finally:
        os.close(incoming)


def manifest(attempts, registration, who):
    require(len(registration["scenarios"]["order"]) == 51 and set(attempts) == set(range(1, 52)))
    slots = [dict(slot=s, attempts=attempts[s["ordinal"]], status="VALID" if attempts[s["ordinal"]] and
        attempts[s["ordinal"]][-1]["status"] == "VALID" else "INCOMPLETE") for s in registration["scenarios"]["order"]]
    return dict(schema_version=1, official=True, registration=registration["fingerprint"], runner=who,
        slots=slots, status="COMPLETE" if all(s["status"] == "VALID" for s in slots) else "INCOMPLETE")


def discover(fd, registration, who):
    attempts, manifests = {i: [] for i in range(1, 52)}, []
    cells = {(s["scenario"], s["config"]) for s in registration["scenarios"]["order"]}
    entries = os.listdir(fd)
    require(len(entries) <= 158, "official directory limit")
    for name in sorted(entries):
        if re.fullmatch(r"manifest-[a-f0-9]{32}\.json", name):
            value, _ = read_at(fd, name)
            manifests.append(value)
            continue
        require(name in harness.SCENARIOS, "unexpected official path")
        scenario = child(fd, name)
        try:
            for cfg in os.listdir(scenario):
                require((name, cfg) in cells, "unexpected official cell")
                directory = child(scenario, cfg)
                try:
                    filenames = os.listdir(directory)
                    require(len(filenames) <= 9, "cell attempt limit")
                    for filename in filenames:
                        match = re.fullmatch(r"run-([1-3])-[a-f0-9]{32}\.json", filename)
                        require(match is not None, "unexpected official result")
                        doc, sha = read_at(directory, filename)
                        require(doc["slot"]["scenario"] == name and doc["slot"]["config"] == cfg and doc["slot"]["run_slot"] == int(match[1]))
                        require(doc["validity"] == assess(doc, registration, who), "corrupted resume validity")
                        attempts[doc["slot"]["ordinal"]].append(dict(path=f"{name}/{cfg}/{filename}", sha256=sha,
                            status=doc["validity"]["status"], attempt=doc["attempt"]))
                finally:
                    os.close(directory)
        finally:
            os.close(scenario)
    blocked = False
    for values in attempts.values():
        values.sort(key=lambda row: row["attempt"])
        require(len(values) <= 3 and [v["attempt"] for v in values] == list(range(1, len(values)+1)), "ambiguous attempt sequence")
        require(all(v["status"] == "INVALID" for v in values[:-1]), "attempt after VALID")
        require(not blocked or not values, "resume violates registered order")
        blocked |= not values or (len(values) < 3 and values[-1]["status"] == "INVALID")
    for previous in manifests:
        require(type(previous) is dict and type(previous.get("slots")) is list and len(previous["slots"]) == 51)
        subset = {}
        for i, row in enumerate(previous["slots"], 1):
            require(type(row) is dict and type(row.get("attempts")) is list)
            subset[i] = row["attempts"]
            require(encoded(subset[i]) == encoded(attempts[i][:len(subset[i])]), "manifest hides or changes attempts")
        flat = [v for rows in attempts.values() for v in rows]
        earlier = [v for rows in subset.values() for v in rows]
        require(encoded(earlier) == encoded(flat[:len(earlier)]), "manifest omits earlier attempts")
        require(encoded(previous) == encoded(manifest(subset, registration, who)), "corrupted official manifest")
    return attempts, manifests


async def execute(registration, root=ROOT, capture=None):
    who, capture = identity(root), capture or collect
    count = 0
    with official_root(root) as fd:
        attempts, previous = discover(fd, registration, who)
        async with asyncio.timeout(51*3*750):
            for slot in registration["scenarios"]["order"]:
                values = attempts[slot["ordinal"]]
                while len(values) < 3 and not (values and values[-1]["status"] == "VALID"):
                    require(identity(root) == who, "runner provenance changed during execution")
                    with reservation(fd, slot) as (output, directory, path):
                        count += 1
                        doc = await capture(deepcopy(slot), len(values)+1, registration, who)
                        require(doc["slot"] == slot and doc["attempt"] == len(values)+1)
                        require(doc["validity"] == assess(doc, registration, who))
                        sha = finish(output, directory, doc)
                    values.append(dict(path=path, sha256=sha, status=doc["validity"]["status"], attempt=doc["attempt"]))
        reread, _ = discover(fd, registration, who)
        require(reread == attempts, "official files changed during execution")
        final = manifest(attempts, registration, who)
        if final not in previous:
            name = f"manifest-{uuid.uuid4().hex}.json"
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                finish(output, fd, final)
            finally:
                os.close(output)
        return dict(status=final["status"], valid_slots=sum(v["status"] == "VALID" for v in final["slots"]), official_runs_executed=count)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute", action="store_true", help="requires separate authorization; starts a local provider")
    args = parser.parse_args(argv)
    try:
        registration = preflight()
        result = (dict(status="PREFLIGHT_OK", cells=17, slots=51, lock_sha256=LOCK_SHA,
            order_sha256=registration["lock"]["order_sha256"], official_runs_executed=0) if args.preflight
            else asyncio.run(execute(registration)))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] != "INCOMPLETE" else 2
    except (OSError, ValueError, KeyError, TypeError, asyncio.CancelledError):
        print("BLOCKED: official registration, provenance or evidence check failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
