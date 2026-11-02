"""P12 offline foundations only; every persisted run is exploratory.

Schedules use integer seconds/rates, <=600 seconds, <=1000 jobs/s and <=10000
jobs. S1 segments are [0,20), [20,30), [30,duration); each starts an arrival
at its left boundary. Count, if supplied, must equal the derived count.
Scheduler records retain absolute physical times and relative logical times.
Health booleans are supplied evidence, not measurements made here; no health
tolerance is frozen. P13 supplies sampling, HTTP and provider lifecycle.
"""

import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
import re
from pathlib import Path
import time
import uuid

from .client import LogicalResult, payload, validated_response

SCENARIOS = ("S0", "S1", "S2", "S3")
OUTCOMES = ("SUCCESS", "FAILED_FAST", "FAILED_AFTER_RETRIES", "DEADLINE_EXCEEDED", "INVALID_RESPONSE")
FAULT_FIELDS = {"scenario", "seed", "outage_start", "outage_end", "rate", "bucket_capacity", "loss_fraction"}


@dataclass(frozen=True)
class PlannedJob:
    job_id: str
    text: str
    offset: float


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("integer outside safety bounds")
    return value


def finite(value, low=0, high=math.inf):
    try:
        return type(value) in (int, float) and low <= value <= high and math.isfinite(value)
    except OverflowError:
        return False


def generate_workload(scenario, seed, *, duration=60, rate=None, burst_rate=14, count=None):
    if type(scenario) is not str or scenario not in SCENARIOS:
        raise ValueError("invalid scenario")
    integer(seed, 0, 2**64 - 1)
    integer(duration, 1, 600)
    rate = (6 if scenario == "S1" else 10) if rate is None else rate
    integer(rate, 1, 1000)
    integer(burst_rate, 1, 1000)
    if scenario != "S1" and burst_rate != 14:
        raise ValueError("burst_rate applies only to S1")
    segments = [(0, duration, rate)] if scenario != "S1" else [
        (0, min(20, duration), rate), (min(20, duration), min(30, duration), burst_rate),
        (min(30, duration), duration, rate)]
    total = sum((end - start) * hz for start, end, hz in segments)
    integer(total, 1, 10000)
    if count is not None and integer(count, 1, 10000) != total:
        raise ValueError("count does not match the fixed schedule")
    jobs = []
    for start, end, hz in segments:
        for i in range((end - start) * hz):
            digest = hashlib.sha256(json.dumps([scenario, seed, len(jobs)]).encode()).hexdigest()
            jobs.append(PlannedJob(digest, "synthetic " + digest, start + i / hz))
    return tuple(jobs)


def checked_jobs(jobs):
    if type(jobs) not in (list, tuple) or not 1 <= len(jobs) <= 10000:
        raise ValueError("invalid scheduled job count")
    previous, ids = -1, set()
    for job in jobs:
        if type(job) is not PlannedJob or not finite(job.offset, 0, 600):
            raise ValueError("invalid planned job")
        payload(job.job_id, job.text)
        if job.offset < previous or job.job_id in ids:
            raise ValueError("schedule must be ordered with unique jobs")
        previous = job.offset
        ids.add(job.job_id)
    return tuple(jobs)


async def run_schedule(jobs, submit, t0, *, clock=time.monotonic, sleep=asyncio.sleep):
    """One independent task/job, no service semaphore; 690s hard run limit.

    t0 may lead the current clock by at most 60s. A submit has a 30s safety
    limit (above C0's 21.5s policy maximum). Exceptions remain explicit errors
    with no invented logical outcome; cancellation of the run cancels all jobs.
    """
    jobs = checked_jobs(jobs)
    if not finite(t0) or t0 > clock() + 60:
        raise ValueError("invalid shared t0")
    async def one(job):
        await sleep(max(0, t0 + job.offset - clock()))
        launch = clock() - t0
        row = dict(job_id=job.job_id, intended_offset=job.offset, launch_offset=launch,
                   launch_lag=launch - job.offset, completion_offset=None, latency=None,
                   outcome=None, success=False, response=None, attempts=[], error=None)
        try:
            async with asyncio.timeout(30):
                result = await submit(job.job_id, job.text)
            if type(result) is not LogicalResult or result.job_id != job.job_id:
                raise ValueError("submit returned a mismatched result")
            row.update(outcome=result.outcome, success=result.outcome == "SUCCESS",
                       response=result.response, attempts=[asdict(a) for a in result.attempts])
        except (Exception, asyncio.CancelledError) as error:
            row["error"] = type(error).__name__
        row["completion_offset"] = clock() - t0
        row["latency"] = row["completion_offset"] - job.offset
        return row
    tasks = [asyncio.create_task(one(job)) for job in jobs]
    try:
        async with asyncio.timeout(690):
            records = await asyncio.gather(*tasks)
        return dict(schedule=[asdict(job) for job in jobs], records=records)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def metadata_reasons(metadata):
    if type(metadata) is not dict or set(metadata) != {"health", "expected_faults", "applied_faults"}:
        return ["missing or invalid required metadata"]
    reasons = []
    health = metadata["health"]
    reasons.extend(health_reasons(health))
    for name in ("expected_faults", "applied_faults"):
        fields = metadata[name]
        if type(fields) is not dict or set(fields) != FAULT_FIELDS:
            reasons.append("missing fault metadata: " + name)
            continue
        if (type(fields["scenario"]) is not str or fields["scenario"] not in SCENARIOS or
            type(fields["seed"]) is not int or not 0 <= fields["seed"] < 2**64 or
            not finite(fields["outage_start"], 0, 86400) or
            not finite(fields["outage_end"], 0, 86400) or
            fields["outage_end"] < fields["outage_start"] or
            not finite(fields["rate"], 0, 10000) or
            not finite(fields["bucket_capacity"], 1, 10000) or
            not finite(fields["loss_fraction"], 0, 1)):
            reasons.append("invalid fault metadata: " + name)
    if metadata["expected_faults"] != metadata["applied_faults"]:
        reasons.append("expected/applied fault fields mismatch")
    return reasons


def p99(samples):
    if not samples or any(not finite(v) for v in samples):
        raise ValueError("missing or invalid samples")
    return sorted(samples)[math.ceil(.99 * len(samples)) - 1]


def health_reasons(health):
    flags = {"loop_lag_healthy", "launch_lag_healthy"}
    if type(health) is not dict or set(health) not in (flags, flags | {"measured", "context"}):
        return ["missing health metadata"]
    reasons = []
    if any(type(health[k]) is not bool or not health[k] for k in flags):
        reasons.append("unhealthy loop-lag or launch-lag evidence")
    if "measured" not in health:
        return reasons  # P12 offline fixtures; real runner always supplies measurements.
    try:
        m, context = health["measured"], health["context"]
        if set(m) != {"loop_samples", "launch_samples", "loop_lag_p99", "launch_lag_p99",
                      "cpu_seconds", "loop_sample_count", "launch_sample_count", "sample_interval", "limits"}:
            raise ValueError()
        limits = m["limits"]
        if set(limits) != {"loop_lag_p99", "launch_lag_p99", "frozen"} or limits["frozen"] is not False:
            raise ValueError()
        if not finite(m["cpu_seconds"]) or m["sample_interval"] != .01:
            raise ValueError()
        for prefix in ("loop", "launch"):
            samples, count = m[prefix + "_samples"], m[prefix + "_sample_count"]
            metric = prefix + "_lag_p99"
            if (type(samples) is not list or not 1 <= len(samples) <= 70000 or
                type(count) is not int or count != len(samples) or not finite(limits[metric])):
                raise ValueError()
            if not finite(m[metric]) or m[metric] != p99(samples) or health[prefix + "_lag_healthy"] != (m[metric] <= limits[metric]):
                raise ValueError()
        fields = {"environment", "provider_counters", "provider_config", "lifecycle", "errors", "registration", "t0", "provider_t0"}
        if not fields <= set(context) or set(context) - fields - {"sampling", "pilot_policy"}:
            raise ValueError()
        if "pilot_policy" in context:
            p = context["pilot_policy"]
            if (set(p) != {"name", "calibration_file", "calibration_sha256", "heldout_file", "heldout_sha256",
                           "training_p99_ms", "maximum_idle_p99_ms", "selected_loop_lag_ms", "heldout_p99_ms", "predeclaration_sha256"} or
                p["name"] != "prospective-pilot-v2" or type(p["training_p99_ms"]) is not list or
                len(p["training_p99_ms"]) != 3 or any(not finite(v) for v in p["training_p99_ms"])):
                raise ValueError()
            maximum, candidate = max(p["training_p99_ms"]), p["selected_loop_lag_ms"]
            if (type(candidate) is not int or not 20 <= candidate <= 40 or
                candidate != 5 * math.ceil((maximum + 5) / 5) or p["maximum_idle_p99_ms"] != maximum or
                not finite(p["heldout_p99_ms"], 0, candidate) or
                limits["loop_lag_p99"] != candidate / 1000 or limits["launch_lag_p99"] != .050):
                raise ValueError()
            for kind in ("calibration", "heldout"):
                if not re.fullmatch(r"exploratory-pilot-v2-" + kind + r"-[a-f0-9]{32}\.json", p[kind + "_file"]):
                    raise ValueError()
            for name in ("calibration_sha256", "heldout_sha256", "predeclaration_sha256"):
                if not re.fullmatch(r"[a-f0-9]{64}", p[name]):
                    raise ValueError()
        if "sampling" in context:
            s = context["sampling"]
            if (set(s) != {"method", "origin", "stop_requested_at", "finished_at", "sample_count", "scheduled_ticks", "unobserved_tail_ticks"} or
                s["method"] != "fixed-anchor-catch-up" or not finite(s["origin"]) or
                not finite(s["stop_requested_at"], s["origin"]) or not finite(s["finished_at"], s["stop_requested_at"])):
                raise ValueError()
            ticks = int((s["stop_requested_at"] - s["origin"]) / .01)
            if (any(type(s[k]) is not int for k in ("sample_count", "scheduled_ticks", "unobserved_tail_ticks")) or
                s["sample_count"] != m["loop_sample_count"] or s["scheduled_ticks"] != ticks or
                s["sample_count"] > ticks or s["unobserved_tail_ticks"] != ticks - s["sample_count"]):
                raise ValueError()
        if not finite(context["t0"]) or context["provider_t0"] != context["t0"]:
            raise ValueError()
        lifecycle = context["lifecycle"]
        if (context["errors"] != [] or lifecycle.get("reaped") is not True or
            lifecycle.get("returncode") != 0 or lifecycle.get("forced_kill") is not False or
            lifecycle.get("unexpected_exit") is not False):
            reasons.append("provider lifecycle or runner error")
    except (TypeError, KeyError, ValueError, AttributeError):
        reasons.append("missing or inconsistent measured health")
    return reasons


def record_reasons(row, job):
    fields = {"job_id", "intended_offset", "launch_offset", "launch_lag", "completion_offset",
              "latency", "outcome", "success", "response", "attempts", "error"}
    if set(row) != fields:
        return ["invalid logical record schema"]
    reasons = []
    if row["outcome"] not in OUTCOMES or row["error"] is not None:
        reasons.append("missing/unknown outcome or submit error")
    if type(row["success"]) is not bool or row["success"] != (row["outcome"] == "SUCCESS"):
        reasons.append("inconsistent success flag")
    if (not all(finite(row[k]) for k in ("intended_offset", "launch_offset", "launch_lag", "completion_offset", "latency")) or
        row["intended_offset"] != job.offset or row["launch_offset"] < job.offset or
        row["completion_offset"] < row["launch_offset"] or
        not math.isclose(row["launch_lag"], row["launch_offset"] - job.offset, abs_tol=1e-9) or
        not math.isclose(row["latency"], row["completion_offset"] - job.offset, abs_tol=1e-9)):
        reasons.append("inconsistent logical timing")
    if row["outcome"] == "SUCCESS":
        try:
            response = validated_response(json.dumps(row["response"], allow_nan=False).encode(), job.job_id, job.text)
        except (ValueError, TypeError):
            response = None
        if response is None:
            reasons.append("missing or invalid successful response")
    return reasons


def validate_run(run, ledger, metadata):
    """Fail closed; no missing-receipt exception is permitted in P12.

    Ledger server attempt_id must be unique; correlation_id must map one-to-one
    to client attempt_id and job_id. A timeout is not pre-arrival evidence.
    """
    reasons = metadata_reasons(metadata)
    try:
        if type(run) is not dict or set(run) != {"schedule", "records"}:
            raise ValueError("invalid run schema")
        if type(run["schedule"]) is not list or not 1 <= len(run["schedule"]) <= 10000:
            raise ValueError("invalid schedule")
        jobs = checked_jobs([PlannedJob(**j) for j in run["schedule"]])
        planned = {j.job_id: j for j in jobs}
        if type(run["records"]) is not list or len(run["records"]) > 10000:
            raise ValueError("invalid records")
        counts = Counter(r["job_id"] for r in run["records"])
        if counts != Counter(planned.keys()):
            reasons.append("missing, duplicate or extra logical records")
        if "measured" in metadata.get("health", {}):
            if metadata["health"]["measured"]["launch_samples"] != [r["launch_lag"] for r in run["records"]]:
                reasons.append("measured launch samples do not match records")
        physical = {}
        for row in run["records"]:
            if row["job_id"] not in planned:
                continue
            reasons.extend(record_reasons(row, planned[row["job_id"]]))
            if type(row["attempts"]) is not list or len(row["attempts"]) > 4:
                raise ValueError("invalid physical records")
            if row["outcome"] == "SUCCESS" and not row["attempts"]:
                reasons.append("successful request has no physical attempt")
            for ordinal, a in enumerate(row["attempts"], 1):
                if set(a) != {"attempt_id", "ordinal", "start", "end", "status", "failure", "timeout", "retry_delay", "retry_after"}:
                    raise ValueError("invalid physical schema")
                identifier = a["attempt_id"]
                if (type(identifier) is not str or not 1 <= len(identifier) <= 128 or
                    any(not 32 <= ord(c) <= 126 for c in identifier)):
                    raise ValueError("invalid correlation ID")
                if (type(a["ordinal"]) is not int or a["ordinal"] != ordinal or
                    not finite(a["start"]) or not finite(a["end"], a["start"]) or
                    not finite(a["timeout"], 0, 5) or a["timeout"] == 0 or
                    not finite(a["retry_delay"], 0, 6) or
                    (a["status"] is not None and (type(a["status"]) is not int or not 100 <= a["status"] <= 599)) or
                    a["failure"] not in (None, "TIMEOUT", "CONNECTION_FAILURE", "CANCELLED", "PROGRAMMING_ERROR") or
                    (a["status"] is None and a["failure"] is None) or
                    (a["retry_after"] is not None and (type(a["retry_after"]) is not str or len(a["retry_after"]) > 8190))):
                    raise ValueError("invalid physical values")
                if identifier in physical:
                    reasons.append("duplicate physical correlation ID")
                physical[identifier] = row["job_id"]
        reasons.extend(ledger_reasons(ledger, physical, metadata.get("health", {}).get("context")))
    except (ValueError, TypeError, KeyError, AttributeError):
        reasons.append("malformed run or ledger evidence")
    return dict(status="INVALID" if reasons else "VALID", reasons=list(dict.fromkeys(reasons)))


def ledger_reasons(ledger, physical, context=None):
    if type(ledger) is not list or len(ledger) > 40000:
        return ["invalid ledger"]
    seen, receipts, reasons, ordinals, buckets = set(), set(), [], {}, {}
    full = any("decision" in row for row in ledger)  # P12 fixtures contain correlation receipts only.
    for row in ledger:
        server_id, correlation = row["attempt_id"], row["correlation_id"]
        if type(server_id) is not str or not server_id or server_id in receipts:
            reasons.append("duplicate or invalid provider receipt ID")
        if correlation in seen or physical.get(correlation) != row["job_id"]:
            reasons.append("provider receipt/attempt correlation mismatch or extra attempt")
        if row.get("resource_overflow") or row.get("aborted") or row.get("finished") is not True:
            reasons.append("abnormal or unfinished provider attempt")
        if full:
            decision, executed, ordinal = row["decision"], row["executed"], row["execution_ordinal"]
            if (decision not in ("SERVED", "LOST_REPLY", "DEDUPED", "RATE_LIMITED", "ERROR_503", "HUNG") or
                type(executed) is not bool or executed != (decision in ("SERVED", "LOST_REPLY")) or
                not finite(row["arrival_time"]) or not finite(row["completion_time"], row["arrival_time"]) or
                not finite(row["service_time"]) or type(row["resource_overflow"]) is not bool or
                type(row["in_flight_at_arrival"]) is not int or not 0 <= row["in_flight_at_arrival"] <= 40000 or
                (executed and (type(ordinal) is not int or ordinal < 1)) or
                (not executed and (ordinal is not None or row["service_time"] != 0))):
                reasons.append("inconsistent provider execution receipt")
            if executed:
                ordinals.setdefault(row["job_id"], []).append(ordinal)
            bucket = buckets.setdefault(str(math.floor(row["arrival_time"])), Counter())
            for name, value in (("receipts", 1), (decision, 1), ("executed", int(executed)),
                                ("resource_overflow", int(row["resource_overflow"]))):
                bucket[name] += value
        receipts.add(server_id)
        seen.add(correlation)
    if seen != set(physical):
        reasons.append("client physical attempt missing from ledger or extra provider attempt")
    if any(sorted(values) != list(range(1, len(values) + 1)) for values in ordinals.values()):
        reasons.append("inconsistent actual execution ordinals")
    if full and context is not None and context.get("provider_counters") != dict(buckets=buckets, rejected=0):
        reasons.append("provider counters disagree with receipts")
    return reasons


def write_pilot(directory, scenario, configuration, run_slot, run, ledger, metadata):
    """Existing fixed results/pilot directory only; slots 1..100, create-only.

    Pytest may supply its fixture's results/pilot directory. No directories are
    created. JSON is encoded before opening, with a 16 MiB safety ceiling.
    """
    if type(scenario) is not str or scenario not in SCENARIOS:
        raise ValueError("invalid scenario")
    if type(configuration) is not str or configuration not in ("C0", "C1", "C2", "C3", "C4"):
        raise ValueError("invalid configuration")
    integer(run_slot, 1, 100)
    directory = Path(directory)
    if (".." in directory.parts or "official" in directory.parts or
        directory.parts[-2:] != ("results", "pilot") or directory.absolute() != directory.resolve(strict=True)):
        raise ValueError("require a fixed nonsymlink results/pilot directory")
    document = dict(exploratory=True, scenario=scenario, configuration=configuration,
                    run_slot=run_slot, run=run, ledger=ledger, metadata=metadata,
                    validity=validate_run(run, ledger, metadata))
    data = json.dumps(document, allow_nan=False, sort_keys=True).encode()
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("result too large")
    name = f"exploratory-{scenario}-{configuration}-{run_slot}-{uuid.uuid4().hex}.json"
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(output, "wb") as stream:
            stream.write(data)
    finally:
        os.close(fd)
    return directory / name
