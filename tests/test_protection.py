"""Deterministic breaker and idempotency checks; no sockets or real sleeps."""

import asyncio
import hashlib

import aiohttp
import pytest

from outage_test.client import Attempt, Breaker
from test_client import Fake, reply, success


def observe(breaker, status=200, failure=None, successful=True):
    probe = breaker.admit()
    assert probe is not None
    breaker.observe(probe, Attempt("physical", 1, 0, 0, status, failure, 2), successful)


def trip(breaker):
    for _ in range(10):
        observe(breaker, 503, successful=False)
    assert breaker.state == "OPEN"


def test_closed_minimum_and_exact_half_threshold():
    fake = Fake([])
    b = Breaker(fake.clock)
    assert b.state == "CLOSED" and not b.history
    for _ in range(9):
        observe(b, 503, successful=False)
    assert b.state == "CLOSED" and len(b.history) == 9
    observe(b, 503, successful=False)
    assert b.state == "OPEN"
    b = Breaker(fake.clock)
    for status in [503] * 5 + [200] * 4:
        observe(b, status, successful=status == 200)
    assert b.state == "CLOSED"
    observe(b)
    assert b.state == "OPEN" and sum(b.history) == 5 and len(b.history) == 10


def test_bounded_rolling_window_and_neutral_statuses():
    b = Breaker(lambda: 0)
    for status in [200, 429, 400, 401, 403, 422] * 10:
        observe(b, status, successful=status == 200)
        assert b.state == "CLOSED" and len(b.history) <= 20 and not any(b.history)
    for _ in range(9):
        observe(b, 503, successful=False)
    assert b.state == "CLOSED" and len(b.history) == 20 and sum(b.history) == 9
    for _ in range(20):
        observe(b)
    assert b.state == "CLOSED" and not any(b.history)


def test_physical_observations_open_guard_boundary_and_fresh_client():
    async def run():
        fake = Fake([reply(503)] * 10 + [reply()])
        client = fake.client("C3")
        for _ in range(2):
            assert (await client.submit("job", "synthetic")).outcome == "FAILED_AFTER_RETRIES"
        result = await client.submit("job", "synthetic")
        assert result.outcome == "FAILED_FAST" and len(result.attempts) == 2
        b = client.breaker
        assert len(b.history) == len(fake.calls) == 10
        fake.now = b.opened_at + 4.999
        blocked = await client.submit("job", "synthetic")
        assert blocked.outcome == "FAILED_FAST" and not blocked.attempts
        assert len(b.history) == len(fake.calls) == 10 and b.state == "OPEN"
        fresh = fake.client("C3")
        assert fresh.breaker.state == "CLOSED" and not fresh.breaker.history
        fake.now = b.opened_at + 5
        assert (await client.submit("job", "synthetic")).outcome == "SUCCESS"
        assert b.state == "CLOSED" and not b.history and not b.probing
    asyncio.run(run())


def test_concurrent_half_open_allows_one_probe():
    async def run():
        fake = Fake([])
        client = fake.client("C3")
        trip(client.breaker)
        fake.now = 5
        entered, release, calls = asyncio.Event(), asyncio.Event(), []
        async def transport(data, headers, timeout):
            calls.append(headers)
            entered.set()
            await release.wait()
            return reply()
        client.transport = transport
        task = asyncio.create_task(client.submit("job", "synthetic"))
        try:
            await entered.wait()
            blocked = await client.submit("other", "synthetic")
            assert blocked.outcome == "FAILED_FAST" and not blocked.attempts and len(calls) == 1
            assert client.breaker.state == "HALF_OPEN" and client.breaker.probing
            release.set()
            assert (await task).outcome == "SUCCESS"
            assert client.breaker.state == "CLOSED" and not client.breaker.history
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("failure", [reply(500), reply(599), TimeoutError(), aiohttp.ClientConnectionError()])
def test_failed_probe_reopens_for_five_seconds(failure):
    async def run():
        fake = Fake([failure])
        client = fake.client("C3")
        trip(client.breaker)
        fake.now = 5
        result = await client.submit("job", "synthetic")
        assert result.outcome == "FAILED_FAST" and len(result.attempts) == 1
        assert client.breaker.state == "OPEN" and client.breaker.opened_at == 5
        fake.now = 9.999
        assert client.breaker.admit() is None
        fake.now = 10
        assert client.breaker.admit() is True
    asyncio.run(run())


@pytest.mark.parametrize("response", [reply(429), reply(400), reply(401), reply(403), reply(422),
    reply(value=success() | {"score": -1}), asyncio.CancelledError(), RuntimeError("synthetic")])
def test_neutral_probe_releases_permit_without_closing(response):
    async def run():
        fake = Fake([response] * 4)
        client = fake.client("C3")
        trip(client.breaker)
        fake.now = 5
        result = await client.submit("job", "synthetic")
        assert result.outcome != "SUCCESS"
        assert client.breaker.state == "HALF_OPEN" and not client.breaker.probing
        assert sum(client.breaker.history) == 10
        fake.responses = [reply()]
        assert (await client.submit("job", "synthetic")).outcome == "SUCCESS"
        assert client.breaker.state == "CLOSED" and not client.breaker.history
    asyncio.run(run())


def test_late_closed_completion_cannot_finish_half_open_probe():
    fake = Fake([])
    b = Breaker(fake.clock)
    late = b.admit()
    trip(b)
    fake.now = 5
    probe = b.admit()
    b.observe(late, Attempt("late", 1, 0, 5, 200, None, 2), True)
    assert b.state == "HALF_OPEN" and b.probing
    b.observe(probe, Attempt("probe", 1, 5, 5, 503, None, 2), False)
    assert b.state == "OPEN" and b.opened_at == 5


def test_deadline_attempt_is_published_before_logical_outcome():
    async def run():
        fake = Fake([])
        client = fake.client("C3")
        for _ in range(9):
            observe(client.breaker, 503, successful=False)
        async def expired(data, headers, timeout):
            fake.now = 6
            raise TimeoutError
        client.transport = expired
        result = await client.submit("job", "synthetic")
        assert result.outcome == "DEADLINE_EXCEEDED" and len(result.attempts) == 1
        assert client.breaker.state == "OPEN" and len(client.breaker.history) == 10
    asyncio.run(run())


@pytest.mark.parametrize("durations", [[], [2, 2, 2]])
def test_c3_c4_inherit_c2_jitter_deadlines_and_retry_after(durations):
    results = []
    for configuration in ("C2", "C3", "C4"):
        fake = Fake([reply(503)] * 4, durations)
        result = asyncio.run(fake.client(configuration).submit("job", "synthetic"))
        results.append((result.outcome, result.elapsed, fake.waits, [a.timeout for a in result.attempts]))
        fake = Fake([reply(429, {"Retry-After": "1"}), reply()])
        assert asyncio.run(fake.client(configuration).submit("job", "synthetic")).outcome == "SUCCESS"
        assert fake.waits == [1]
        fake = Fake([reply(429, {"Retry-After": "6"})])
        assert asyncio.run(fake.client(configuration).submit("job", "synthetic")).outcome == "FAILED_FAST"
        assert not fake.waits
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize("configuration", ["C0", "C1", "C2", "C3"])
def test_no_idempotency_header_before_c4(configuration):
    fake = Fake([reply(429)] * 4)
    asyncio.run(fake.client(configuration).submit("job", "synthetic"))
    assert len(fake.calls) == 4 and all(set(call[1]) == {"X-Attempt-Id"} for call in fake.calls)


def test_c4_keys_stable_across_physical_retries_jobs_and_seeds():
    keys = {}
    for job, seed in (("job", 7), ("other", 7), ("job", 99)):
        fake = Fake([aiohttp.ClientConnectionError()] * 3 + [reply(value=success(job))])
        result = asyncio.run(fake.client("C4", seed).submit(job, "synthetic"))
        assert result.outcome == "SUCCESS" and len(result.attempts) == len(fake.calls) == 4
        headers = [call[1] for call in fake.calls]
        expected = hashlib.sha256(job.encode()).hexdigest()
        assert all(h == {"X-Attempt-Id": a.attempt_id, "Idempotency-Key": expected}
                   for h, a in zip(headers, result.attempts))
        assert len({h["X-Attempt-Id"] for h in headers}) == 4
        assert 1 <= len(expected) <= 128 and all(32 <= ord(c) <= 126 for c in expected)
        keys[job, seed] = expected
    assert keys["job", 7] == keys["job", 99] != keys["other", 7]
