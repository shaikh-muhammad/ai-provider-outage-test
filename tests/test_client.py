"""Offline C0–C2 specification using synthetic time and transport."""

import asyncio
import hashlib
import json
import random
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import pytest

from outage_test.client import Client, HTTPReply, HTTPTransport, backoff, retryable


def success(job="job", text="synthetic"):
    score = int.from_bytes(hashlib.sha256(text.encode()).digest(), "big") / (2**256 - 1)
    return dict(request_id="provider-1", job_id=job,
                label="POSITIVE" if score >= .5 else "NEGATIVE", score=score)


def reply(status=200, headers=None, value=None):
    return HTTPReply(status, headers or {}, json.dumps(success() if value is None else value).encode())


class Fake:
    def __init__(self, responses, durations=()):
        self.responses, self.durations = list(responses), list(durations)
        self.now, self.calls, self.waits = 0.0, [], []

    def clock(self):
        return self.now

    async def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay

    async def transport(self, data, headers, timeout):
        self.calls.append((json.loads(data), headers, timeout))
        duration = self.durations.pop(0) if self.durations else 0
        self.now += min(duration, timeout)
        if duration >= timeout:
            raise TimeoutError
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def client(self, configuration, seed=7):
        return Client(configuration, self.transport, seed, self.clock, self.sleep)


def submit(configuration, responses, durations=()):
    fake = Fake(responses, durations)
    return fake, asyncio.run(fake.client(configuration).submit("job", "synthetic"))


@pytest.mark.parametrize("status,expected", [(429, True), (500, True), (503, True),
    (599, True), (400, False), (401, False), (403, False), (422, False),
    (404, False), (200, False), (302, False), (600, False)])
def test_status_classification(status, expected):
    assert retryable(status=status) is expected


@pytest.mark.parametrize("failure,expected", [("TIMEOUT", True),
    ("CONNECTION_FAILURE", True), ("PROGRAMMING_ERROR", False), ("CANCELLED", False)])
def test_failure_classification(failure, expected):
    assert retryable(failure=failure) is expected


@pytest.mark.parametrize("configuration", ["C0", "C1"])
@pytest.mark.parametrize("failure", [reply(429, {"Retry-After": "99"}), reply(500),
    reply(599), TimeoutError(), aiohttp.ClientConnectionError(), aiohttp.ClientPayloadError()])
def test_baseline_four_attempts_fixed_delay_and_records(configuration, failure):
    fake, result = submit(configuration, [failure] * 4, [.2] * 4)
    assert result.outcome == "FAILED_AFTER_RETRIES" and result.response is None
    assert fake.waits == [.5] * 3 and result.elapsed == pytest.approx(2.3)
    assert [r.ordinal for r in result.attempts] == [1, 2, 3, 4]
    assert len({r.attempt_id for r in result.attempts}) == 4
    for i, (record, call) in enumerate(zip(result.attempts, fake.calls)):
        assert call[0] == dict(job_id="job", text="synthetic")
        assert call[1] == {"X-Attempt-Id": record.attempt_id}
        assert record.timeout == call[2] == (5 if configuration == "C0" else 2)
        assert record.start == pytest.approx(i * .7)
        assert record.end == pytest.approx(i * .7 + .2)
        assert record.retry_delay == (.5 if i < 3 else 0)
        assert (record.status is not None) != (record.failure is not None)
    assert result.attempts[0].retry_after == ("99" if isinstance(failure, HTTPReply) and failure.status == 429 else None)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 302])
def test_nonretryable_statuses(status):
    fake, result = submit("C0", [reply(status), reply()])
    assert result.outcome == "FAILED_FAST" and len(fake.calls) == 1 and not fake.waits


@pytest.mark.parametrize("configuration", ["C0", "C1", "C2"])
def test_verified_success(configuration):
    fake, result = submit(configuration, [reply()], [.1])
    assert result.outcome == "SUCCESS" and result.response == success()
    assert result.job_id == "job" and result.elapsed == .1 and len(result.attempts) == 1
    assert result.attempts[0].status == 200 and not fake.waits


@pytest.mark.parametrize("change", [dict(job_id="other"), dict(label="wrong"),
    dict(score=.123), dict(score=True), dict(score=float("nan")),
    dict(score=float("inf")), dict(score="0.5"), dict(request_id=""),
    dict(request_id=3), dict(extra=1), dict(request_id="\ud800")])
def test_malformed_success_is_terminal(change):
    fake, result = submit("C2", [reply(value=success() | change), reply()])
    assert result.outcome == "INVALID_RESPONSE" and result.response is None
    assert len(fake.calls) == 1 and not fake.waits


@pytest.mark.parametrize("body", [b"{", b"[]", b"null", b"\xff", b" " * 2049,
    b'{"job_id":"job"}', b'[' * 1000 + b']' * 1000])
def test_invalid_json_schema_or_body_size(body):
    _, result = submit("C1", [HTTPReply(200, {}, body)])
    assert result.outcome == "INVALID_RESPONSE"


def test_c1_caps_timeout_and_deadline_takes_precedence():
    fake, result = submit("C1", [reply(503)] * 4, [2, 2, 2])
    assert result.outcome == "DEADLINE_EXCEEDED" and result.elapsed == 6
    assert [r.timeout for r in result.attempts] == [2, 2, 1]
    assert fake.waits == [.5, .5] and len(fake.calls) == 3
    assert result.attempts[-1].failure == "TIMEOUT"


def test_c1_deadline_before_retry_caps_sleep():
    fake, result = submit("C1", [reply(500)] * 4, [1.75, 1.75, 1.25])
    assert result.outcome == "DEADLINE_EXCEEDED" and result.elapsed == 6
    assert fake.waits == [.5, .5, .25] and len(fake.calls) == 3
    assert result.attempts[-1].end == 5.75


def test_deadline_wins_even_if_transport_returns_at_boundary():
    fake = Fake([])
    async def boundary(data, headers, timeout):
        fake.now += 6
        return reply(503)
    client = Client("C1", boundary, clock=fake.clock, sleep=fake.sleep)
    result = asyncio.run(client.submit("job", "synthetic"))
    assert result.outcome == "DEADLINE_EXCEEDED" and not fake.waits


@pytest.mark.parametrize("error", [RuntimeError("synthetic"), asyncio.CancelledError()])
def test_programming_errors_and_cancellation_are_terminal(error):
    fake, result = submit("C1", [error, reply()])
    assert result.outcome == "FAILED_FAST" and len(fake.calls) == 1
    assert result.attempts[0].failure in ("PROGRAMMING_ERROR", "CANCELLED")


def test_sleep_cancellation_returns_logical_outcome():
    fake = Fake([reply(503)])
    async def cancel(delay):
        raise asyncio.CancelledError
    client = Client("C1", fake.transport, clock=fake.clock, sleep=cancel)
    result = asyncio.run(client.submit("job", "synthetic"))
    assert result.outcome == "FAILED_FAST" and len(result.attempts) == 1


def test_c2_backoff_cap_full_jitter_and_seeded_job_order_independence():
    rng = random.Random(123)
    for ordinal in range(1, 10):
        cap = min(2, .25 * 2**(ordinal - 1))
        draws = [backoff(ordinal, rng) for _ in range(100)]
        assert all(0 <= value <= cap for value in draws) and len(set(draws)) > 1
    async def sequence(order, seed=7):
        fake = Fake([reply(503)] * 8)
        client = fake.client("C2", seed)
        results = {}
        for job in order:
            result = await client.submit(job, "synthetic")
            assert result.outcome == "FAILED_AFTER_RETRIES"
            results[job] = [a.retry_delay for a in result.attempts]
        return results
    first = asyncio.run(sequence(["a", "b"]))
    assert first == asyncio.run(sequence(["b", "a"]))
    assert first == asyncio.run(sequence(["a", "b"]))
    assert first != asyncio.run(sequence(["a", "b"], 8))
    assert all(0 <= value <= cap for value, cap in zip(first["a"], [.25, .5, 1, 0]))


@pytest.mark.parametrize("value", ["0", "1", "2", "0001"])
def test_retry_after_honored(value):
    fake, result = submit("C2", [reply(429, {"Retry-After": value}), reply()])
    assert result.outcome == "SUCCESS" and fake.waits[0] >= int(value)
    assert result.attempts[0].retry_after == value


@pytest.mark.parametrize("value", ["-1", "+1", "0.5", " 1", "", "Wed, 01 Jan 2025", "١"])
def test_invalid_retry_after_uses_seeded_jitter(value):
    fake, _ = submit("C2", [reply(429, {"Retry-After": value}), reply()])
    reference, _ = submit("C2", [reply(429), reply()])
    assert fake.waits == reference.waits


@pytest.mark.parametrize("value", ["6", "7", "9" * 5000])
def test_retry_after_exhausting_deadline_fails_fast(value):
    fake, result = submit("C2", [reply(429, {"Retry-After": value}), reply()])
    assert result.outcome == "FAILED_FAST" and not fake.waits and len(fake.calls) == 1


def test_c2_ignores_retry_after_on_5xx_and_ids_are_stable():
    fake, result = submit("C2", [reply(503, {"Retry-After": "99"}), reply()])
    other, repeated = submit("C2", [reply(503), reply()])
    assert result.outcome == repeated.outcome == "SUCCESS"
    assert fake.waits == other.waits
    assert [a.attempt_id for a in result.attempts] == [a.attempt_id for a in repeated.attempts]
    assert all(set(call[1]) == {"X-Attempt-Id"} for call in fake.calls)


@pytest.mark.parametrize("job,text", [("", "x"), ("a" * 257, "x"), ("a", ""),
    ("a", "x" * 513), (1, "x"), ("a", "\ud800"), ("😀" * 256, "😀" * 512)])
def test_invalid_local_input_is_not_sent(job, text):
    fake = Fake([])
    with pytest.raises(ValueError):
        asyncio.run(fake.client("C0").submit(job, text))
    assert not fake.calls


@pytest.mark.parametrize("url", ["http://localhost:1234", "http://0.0.0.0:1234",
    "https://127.0.0.1:1234", "http://127.0.0.1:0", "http://127.0.0.1:65536",
    "http://127.0.0.1:1234/path", "http://user@127.0.0.1:1234", "http://127.0.0.1"])
def test_transport_requires_fixed_loopback_url(url):
    with pytest.raises(ValueError):
        HTTPTransport(Mock(), url)


@pytest.mark.parametrize("oversized", [False, True])
def test_real_transport_contract_without_sockets(oversized):
    async def run():
        async def chunks(size):
            yield b" " * 2049 if oversized else json.dumps(success()).encode()
        response = SimpleNamespace(status=200, headers={}, content=SimpleNamespace(iter_chunked=chunks))
        class Context:
            async def __aenter__(self):
                return response
            async def __aexit__(self, *args):
                return False
        session = Mock()
        session.post.return_value = Context()
        client = Client("C0", HTTPTransport(session, "http://127.0.0.1:1234"))
        for _ in range(2):
            result = await client.submit("job", "synthetic")
            assert result.outcome == ("INVALID_RESPONSE" if oversized else "SUCCESS")
        assert session.post.call_count == 2
        for call in session.post.call_args_list:
            assert call.args == ("http://127.0.0.1:1234/v1/complete",)
            assert call.kwargs["allow_redirects"] is False
            assert call.kwargs["raise_for_status"] is False
            assert call.kwargs["timeout"].total == 5
            assert set(call.kwargs["headers"]) == {"X-Attempt-Id", "Content-Type"}
    asyncio.run(run())
