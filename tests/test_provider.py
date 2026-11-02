"""Offline deterministic fault-engine specification; no provider exists yet."""

import pytest

from outage_test.faults import TokenBucket, fault, service_time


@pytest.mark.parametrize("now", [0, 20, 34.999, 35, 100])
def test_s0_healthy(now):
    assert fault(7, "S0", "job", 1, now) == "HEALTHY"


@pytest.mark.parametrize("scenario", ["S0", "S2", "S3"])
def test_repeatable_and_independent_of_unrelated_calls(scenario):
    expected = [fault(42, scenario, str(i), 1, 25) for i in range(100)]
    for i in reversed(range(100)):
        fault(99, scenario, "unrelated", 3, 25)
        assert fault(42, scenario, str(i), 1, 25) == expected[i]


@pytest.mark.parametrize("now,inside", [(19.999, False), (20, True),
                                         (34.999, True), (35, False)])
def test_s2_boundaries(now, inside):
    choices = {fault(42, "S2", str(i), 1, now) for i in range(200)}
    assert choices == ({"ERROR_503", "HUNG"} if inside else {"HEALTHY"})


def test_s2_supplied_interval_and_seeded_split():
    assert fault(1, "S2", "job", 1, 20, outage_start=2, outage_end=3) == "HEALTHY"
    choices = [fault(7, "S2", str(i), 1, 2, outage_start=2, outage_end=3)
               for i in range(2000)]
    assert set(choices) == {"ERROR_503", "HUNG"}
    assert 800 < choices.count("HUNG") < 1200
    assert choices != [fault(8, "S2", str(i), 1, 2, outage_start=2, outage_end=3)
                       for i in range(2000)]


def test_s3_first_execution_only_and_retry_order_independence():
    jobs = [str(i) for i in range(200)]
    first = {job: fault(42, "S3", job, 1, 0) for job in jobs}
    assert set(first.values()) == {"HEALTHY", "LOST_REPLY"}
    for job in reversed(jobs):
        for ordinal in (3, 2, 4):
            assert fault(42, "S3", job, ordinal, 10) == "HEALTHY"
        assert fault(42, "S3", job, 1, 15) == first[job]


@pytest.mark.parametrize("fraction,expected", [(0, "HEALTHY"), (1, "LOST_REPLY")])
def test_s3_supplied_fraction(fraction, expected):
    for i in range(20):
        assert fault(7, "S3", str(i), 1, 0, loss_fraction=fraction) == expected
        assert fault(7, "S3", str(i), 2, 0, loss_fraction=fraction) == "HEALTHY"


def test_bucket_capacity_depletion_and_refill_boundaries():
    bucket = TokenBucket(rate=4, capacity=2, now=10)
    assert bucket.allow(10)
    assert bucket.allow(10)
    assert not bucket.allow(10)
    assert not bucket.allow(10.125)
    assert not bucket.allow(10.125)
    assert bucket.allow(10.25)
    assert not bucket.allow(10.25)
    assert bucket.allow(20)
    assert bucket.allow(20)
    assert not bucket.allow(20)


def test_bucket_defaults_and_s1_integration():
    bucket = TokenBucket()
    for i in range(10):
        assert fault(7, "S1", str(i), 1, 0, bucket=bucket) == "HEALTHY"
    assert fault(7, "S1", "x", 1, 0.05, bucket=bucket) == "RATE_LIMITED"
    assert fault(7, "S1", "x", 1, 0.1, bucket=bucket) == "HEALTHY"
    assert fault(7, "S1", "x", 1, 0.1, bucket=bucket) == "RATE_LIMITED"


def test_service_duration_bounds_and_determinism():
    values = [service_time(7, "S3", str(i), 1) for i in range(100)]
    assert len(set(values)) > 1
    for i in reversed(range(100)):
        service_time(99, "S2", "other", 3)
        assert service_time(7, "S3", str(i), 1) == values[i]
        for base in (0, 0.01, 2):
            duration = service_time(7, "S3", str(i), 1, base)
            assert 0.9 * base <= duration <= 1.1 * base
        assert 0.072 <= values[i] <= 0.088
    assert values != [service_time(7, "S3", str(i), 2) for i in range(100)]


@pytest.mark.parametrize("kwargs", [dict(seed=True), dict(seed=-1),
    dict(scenario="S4"), dict(job_id=""), dict(job_id="x" * 257),
    dict(execution_ordinal=0), dict(execution_ordinal=True), dict(now=-1),
    dict(now=float("nan")), dict(outage_start=30, outage_end=20),
    dict(loss_fraction=1.1), dict(loss_fraction=-0.1)])
def test_invalid_fault_inputs(kwargs):
    inputs = dict(seed=7, scenario="S0", job_id="job", execution_ordinal=1, now=0)
    inputs.update(kwargs)
    with pytest.raises(ValueError):
        fault(**inputs)


def test_invalid_bucket_and_service_inputs():
    with pytest.raises(ValueError):
        fault(7, "S1", "job", 1, 0)
    for kwargs in (dict(rate=-1), dict(capacity=0), dict(now=float("inf"))):
        with pytest.raises(ValueError):
            TokenBucket(**kwargs)
    bucket = TokenBucket(now=1)
    with pytest.raises(ValueError):
        bucket.allow(0)
    assert bucket.allow(1)
    for base in (-1, float("nan"), float("inf"), True):
        with pytest.raises(ValueError):
            service_time(7, "S0", "job", 1, base)


import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

from outage_test.provider import Provider


def request(value=None, headers=None, path="/v1/complete", raw=None):
    data = json.dumps(value).encode() if raw is None else raw
    async def chunks(size):
        for offset in range(0, len(data), size):
            yield data[offset:offset + size]
    return SimpleNamespace(headers=headers or {}, path=path,
                           content=SimpleNamespace(iter_chunked=chunks), transport=Mock())


def simulator():
    now, waits = [100.0], []
    async def sleep(duration):
        waits.append(duration)
        now[0] += duration
    return Provider("synthetic-control-token-for-tests", lambda: now[0], sleep), now, waits


async def configure(p, **values):
    return await p.control(request(values, {"X-Admin-Token": p.token}, "/admin/configure"))


def decoded(response):
    return json.loads(response.text)


def test_success_ledger_ordinals_and_counters():
    async def run():
        p, now, _ = simulator()
        responses = [await p.complete(request(dict(job_id=job, text="same"),
                     {"X-Attempt-Id": str(i)})) for i, job in enumerate(("a", "b", "a"))]
        results = [decoded(r) for r in responses]
        score = int.from_bytes(hashlib.sha256(b"same").digest(), "big") / (2**256 - 1)
        assert all(r.status == 200 for r in responses)
        assert all(set(r) == {"request_id", "job_id", "label", "score"} for r in results)
        assert all(r["score"] == score and r["label"] ==
                   ("POSITIVE" if score >= .5 else "NEGATIVE") for r in results)
        assert [r["job_id"] for r in results] == ["a", "b", "a"]
        assert len({r["request_id"] for r in results}) == 3
        assert [r["execution_ordinal"] for r in p.ledger] == [1, 1, 2]
        assert all(r["executed"] and r["finished"] and r["service_time"] > 0 for r in p.ledger)
        assert p.ledger[0]["arrival_time"] == 0 and p.ledger[0]["correlation_id"] == "0"
        assert p.ledger[0]["idempotency_key"] is None
        assert sum(b["receipts"] for b in p.counters()["buckets"].values()) == 3
        assert sum(b["executed"] for b in p.counters()["buckets"].values()) == 3
        now[0] += 2
        await configure(p, scenario="S1", rate=0, bucket_capacity=1)
        assert (await p.complete(request(dict(job_id="a", text="x")))).status == 200
        r = await p.complete(request(dict(job_id="a", text="x")))
        assert r.status == 429 and r.headers["Retry-After"] == "1"
        assert not p.ledger[-1]["executed"] and p.ordinals == {"a": 1}
    asyncio.run(run())


def test_simultaneous_hangs_do_not_serialize_per_job():
    async def run():
        p, now, _ = simulator()
        await configure(p, scenario="S2")
        job = next(str(i) for i in range(100) if fault(0, "S2", str(i), 1, 20) == "HUNG")
        now[0] = p.t0 + 20
        entered, release = asyncio.Queue(), asyncio.Event()
        async def sleep(duration):
            entered.put_nowait(duration)
            await release.wait()
        p.sleep = sleep
        tasks = [asyncio.create_task(p.complete(request(dict(job_id=job, text="x")))) for _ in range(2)]
        assert await entered.get() == 6
        assert await entered.get() == 6
        release.set()
        await asyncio.gather(*tasks)
        assert not p.ordinals and all(not r["executed"] for r in p.ledger)
    asyncio.run(run())


def test_s2_boundaries_errors_hangs_and_no_cache_poisoning():
    async def run():
        p, now, waits = simulator()
        await configure(p, scenario="S2", idempotency=True)
        jobs = {fault(0, "S2", str(i), 1, 20): str(i) for i in range(100)}
        for elapsed in (19.999, 20, 34.999, 35):
            for decision, job in jobs.items():
                await configure(p, scenario="S2", idempotency=True)
                now[0] = p.t0 + elapsed
                r = await p.complete(request(dict(job_id=job, text="x")))
                row = p.ledger[-1]
                assert row["arrival_time"] == pytest.approx(elapsed)
                assert row["decision"] == (decision if 20 <= elapsed < 35 else "SERVED")
                assert row["executed"] == (r.status == 200)
                if row["decision"] == "HUNG":
                    assert waits[-1] == 6 and row["service_time"] == 0
        await configure(p, scenario="S2", idempotency=True)
        now[0] = p.t0 + 20
        req = request(dict(job_id=jobs["ERROR_503"], text="x"), {"Idempotency-Key": "k"})
        assert (await p.complete(req)).status == 503 and not p.cache
        now[0] = p.t0 + 35
        assert (await p.complete(req)).status == 200
        assert p.ledger[-1]["execution_ordinal"] == 1
    asyncio.run(run())


def test_lost_reply_replay_and_disabled_idempotency():
    async def run():
        p, _, _ = simulator()
        await configure(p, scenario="S3", loss_fraction=1)
        req = request(dict(job_id="a", text="x"), {"Idempotency-Key": "stable"})
        assert (await p.complete(req)).status != 200
        req.transport.abort.assert_called_once()
        first = p.cache["stable"].copy()
        assert decoded(await p.complete(req)) == first
        assert [r["decision"] for r in p.ledger] == ["LOST_REPLY", "DEDUPED"]
        assert [r["executed"] for r in p.ledger] == [True, False]
        assert p.ordinals == {"a": 1}
        await configure(p, scenario="S3", idempotency=False, loss_fraction=1)
        await p.complete(req)
        assert (await p.complete(req)).status == 200
        assert [r["execution_ordinal"] for r in p.ledger] == [1, 2]
        assert not p.cache
    asyncio.run(run())


def test_concurrency_queue_overflow_and_duplicate_serialization():
    async def run():
        p, now, _ = simulator()
        await configure(p, idempotency=True, capacity=2, max_pending=3)
        entered, release = asyncio.Queue(), asyncio.Event()
        async def sleep(duration):
            entered.put_nowait(duration)
            await release.wait()
        p.sleep = sleep
        a = request(dict(job_id="a", text="x"), {"Idempotency-Key": "k"})
        first = asyncio.create_task(p.complete(a))
        await entered.get()
        second = asyncio.create_task(p.complete(a))
        other = asyncio.create_task(p.complete(request(dict(job_id="b", text="y"))))
        await entered.get()  # Another job starts before a is released.
        overflow = await p.complete(request(dict(job_id="c", text="z")))
        assert overflow.status == 503 and p.ledger[-1]["resource_overflow"]
        assert p.pending == 3 and len(p.ledger) == 4
        assert p.ledger[1]["in_flight_at_arrival"] == 1
        now[0] += 1
        release.set()
        results = await asyncio.gather(first, second, other)
        assert decoded(results[0]) == decoded(results[1])
        assert sum(r["executed"] for r in p.ledger) == 2 and p.pending == 0
        assert p.counters()["buckets"]["0"]["resource_overflow"] == 1
        await configure(p, capacity=1, max_pending=2)
        entered, release = asyncio.Queue(), asyncio.Event()
        first = asyncio.create_task(p.complete(request(dict(job_id="a", text="x"))))
        await entered.get()
        second = asyncio.create_task(p.complete(request(dict(job_id="b", text="y"))))
        # Yield through a ready callback, without waiting on real time.
        ready = asyncio.Event()
        asyncio.get_running_loop().call_soon(ready.set)
        await ready.wait()
        assert entered.empty() and len(p.ledger) == 2
        assert p.ledger[1]["arrival_time"] == 0
        now[0] += 2
        release.set()
        await asyncio.gather(first, second)
        assert p.ledger[1]["arrival_time"] == 0
    asyncio.run(run())
