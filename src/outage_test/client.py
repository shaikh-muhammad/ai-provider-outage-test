"""Provisional C0–C4 policies; only C4 sends a job-derived idempotency key.

Deadline expiry (now >= deadline) wins over an attempt result, even success.
Nonretryable failures/cancellation are FAILED_FAST; four retryable failures
are FAILED_AFTER_RETRIES. Programming exceptions from transport are terminal
FAILED_FAST, never retried. Transport failure cannot establish nonexecution.
Job IDs identify logical operations: reuse yields the same deterministic IDs.
Invalid local inputs raise ValueError before admitting a logical operation.
The caller owns a reusable aiohttp session; POST has no aiohttp retry layer.
"""

import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
import random
import time
from urllib.parse import urlsplit

import aiohttp


@dataclass
class HTTPReply:
    status: int
    headers: dict
    body: bytes


@dataclass
class Attempt:
    attempt_id: str
    ordinal: int
    start: float
    end: float
    status: int | None
    failure: str | None
    timeout: float
    retry_delay: float = 0.0
    retry_after: str | None = None


@dataclass
class LogicalResult:
    job_id: str
    outcome: str
    elapsed: float
    attempts: list[Attempt]
    response: dict | None = None


def retryable(status=None, failure=None):
    return failure in ("TIMEOUT", "CONNECTION_FAILURE") or (
        type(status) is int and (status == 429 or 500 <= status <= 599))


def backoff(ordinal, rng):
    return rng.uniform(0, min(2.0, 0.25 * 2**min(ordinal - 1, 3)))


def retry_after(value):
    if type(value) is str and value and value.isascii() and value.isdecimal():
        # Float handles arbitrarily long integer headers without int conversion limits.
        return float(value)
    return None


def payload(job_id, text):
    for value, limit in ((job_id, 256), (text, 512)):
        if type(value) is not str or not 1 <= len(value) <= limit:
            raise ValueError("invalid synthetic request")
    try:
        data = json.dumps(dict(job_id=job_id, text=text), ensure_ascii=False).encode()
    except UnicodeError:
        raise ValueError("invalid synthetic request") from None
    if len(data) > 2048:
        raise ValueError("request exceeds 2048 bytes")
    return data


def validated_response(body, job_id, text):
    try:
        if type(body) is not bytes or len(body) > 2048:
            return None
        value = json.loads(body)
        if type(value) is not dict or set(value) != {"request_id", "job_id", "label", "score"}:
            return None
        request_id = value["request_id"]
        if type(request_id) is not str or not 1 <= len(request_id) <= 256:
            return None
        request_id.encode()
        if type(value["job_id"]) is not str or value["job_id"] != job_id:
            return None
        score = int.from_bytes(hashlib.sha256(text.encode()).digest(), "big") / (2**256 - 1)
        label = "POSITIVE" if score >= 0.5 else "NEGATIVE"
        actual = value["score"]
        if type(actual) not in (int, float) or not 0 <= actual <= 1 or not math.isfinite(actual):
            return None
        if value["label"] != label or actual != score:
            return None
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


class HTTPTransport:
    """Caller-owned shared session, fixed loopback endpoint, bounded body.

    No connector limit is imposed here. The future worker configures the pool.
    """

    def __init__(self, session, base_url):
        if type(base_url) is not str:
            raise ValueError("invalid loopback URL")
        parsed = urlsplit(base_url)
        port = parsed.port
        if port is None or not 1 <= port <= 65535 or base_url != f"http://127.0.0.1:{port}":
            raise ValueError("require http://127.0.0.1:<port>")
        self.session, self.url = session, base_url + "/v1/complete"

    async def __call__(self, data, headers, timeout):
        async with self.session.post(self.url, data=data,
                headers={"Content-Type": "application/json"} | headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=False, raise_for_status=False) as response:
            body = bytearray()
            if response.status == 200:
                async for chunk in response.content.iter_chunked(2049):
                    body.extend(chunk)
                    if len(body) > 2048:
                        body.clear()
                        break
            return HTTPReply(response.status, {"Retry-After": response.headers.get("Retry-After")}, bytes(body))


class Breaker:
    """Completion-ordered, bounded observations; one event loop owns state.

    HALF_OPEN neutral outcomes (429, other 4xx, invalid 200, cancellation or
    programming error) release the probe but remain HALF_OPEN. Only validated
    success closes/resets; a relevant failure reopens for five seconds. Late
    CLOSED attempts still contribute observations but cannot complete a probe.
    Admission and publication contain no awaits; no lock spans HTTP or sleep.
    """

    def __init__(self, clock):
        self.clock = clock
        self.history = deque(maxlen=20)
        self.state, self.opened_at, self.probing = "CLOSED", 0.0, False

    def admit(self):
        """False = CLOSED admission, True = probe, None = rejected."""
        if self.state == "OPEN":
            if self.clock() - self.opened_at < 5:
                return None
            self.state = "HALF_OPEN"
        if self.state == "HALF_OPEN":
            if self.probing:
                return None
            self.probing = True
            return True
        return False

    def observe(self, probe, record, successful):
        failed = record.failure in ("TIMEOUT", "CONNECTION_FAILURE") or (
            record.status is not None and 500 <= record.status <= 599)
        self.history.append(failed)
        if probe:
            self.probing = False
            if failed:
                self.open()
            elif successful:
                self.state, self.opened_at = "CLOSED", 0.0
                self.history.clear()
        elif self.state == "CLOSED" and len(self.history) >= 10:
            if sum(self.history) * 2 >= len(self.history):
                self.open()

    def open(self):
        self.state, self.opened_at = "OPEN", self.clock()


class Client:
    def __init__(self, configuration, transport, seed=0, clock=time.monotonic, sleep=asyncio.sleep):
        if configuration not in ("C0", "C1", "C2", "C3", "C4") or type(configuration) is not str:
            raise ValueError("invalid configuration")
        if type(seed) is not int or not 0 <= seed < 2**64:
            raise ValueError("invalid seed")
        self.configuration, self.transport, self.seed = configuration, transport, seed
        self.clock, self.sleep = clock, sleep
        self.breaker = Breaker(clock) if configuration in ("C3", "C4") else None

    async def attempt(self, data, identifier, ordinal, timeout, key=None):
        probe = self.breaker.admit() if self.breaker else False
        if probe is None:
            return None, None, None
        record = Attempt(identifier, ordinal, self.clock(), self.clock(), None, None, timeout)
        reply = None
        headers = {"X-Attempt-Id": identifier}
        if key is not None:
            headers["Idempotency-Key"] = key
        try:
            async with asyncio.timeout(timeout):
                reply = await self.transport(data, headers, timeout)
            record.status = reply.status
            record.retry_after = reply.headers.get("Retry-After")
        except TimeoutError:
            record.failure = "TIMEOUT"
        except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError):
            record.failure = "CONNECTION_FAILURE"
        except asyncio.CancelledError:
            record.failure = "CANCELLED"
        except Exception:
            record.failure = "PROGRAMMING_ERROR"
        record.end = self.clock()
        response = None
        if self.breaker:
            if record.failure is None and record.status == 200:
                request = json.loads(data)
                response = validated_response(reply.body, request["job_id"], request["text"])
            self.breaker.observe(probe, record, response is not None)
        return record, reply, response

    def delay(self, record, rng):
        if self.configuration in ("C0", "C1"):
            return 0.5, None
        delay = backoff(record.ordinal, rng)
        minimum = retry_after(record.retry_after) if record.status == 429 else None
        return max(delay, minimum or 0), minimum

    async def submit(self, job_id, text):
        data = payload(job_id, text)
        started, attempts = self.clock(), []
        deadline = math.inf if self.configuration == "C0" else started + 6
        per_attempt = 5.0 if self.configuration == "C0" else 2.0
        identity = json.dumps([self.seed, self.configuration, job_id, text]).encode()
        prefix = hashlib.sha256(identity).hexdigest()
        key = hashlib.sha256(job_id.encode()).hexdigest() if self.configuration == "C4" else None
        rng = random.Random(int.from_bytes(hashlib.sha256(
            json.dumps([self.seed, job_id]).encode()).digest(), "big"))
        def result(outcome, response=None):
            return LogicalResult(job_id, outcome, self.clock() - started, attempts, response)
        for ordinal in range(1, 5):
            if self.clock() >= deadline:
                return result("DEADLINE_EXCEEDED")
            timeout = min(per_attempt, deadline - self.clock())
            record, reply, response = await self.attempt(data, f"{prefix}-{ordinal}", ordinal, timeout, key)
            if record is None:
                return result("FAILED_FAST")
            attempts.append(record)
            if self.clock() >= deadline:
                return result("DEADLINE_EXCEEDED")
            if record.failure is None and record.status == 200:
                if not self.breaker:
                    response = validated_response(reply.body, job_id, text)
                return result("SUCCESS" if response is not None else "INVALID_RESPONSE", response)
            if not retryable(record.status, record.failure):
                return result("FAILED_FAST")
            if ordinal == 4:
                return result("FAILED_AFTER_RETRIES")
            if self.breaker and self.breaker.state == "OPEN":
                return result("FAILED_FAST")
            delay, minimum = self.delay(record, rng)
            remaining = deadline - self.clock()
            if minimum is not None and minimum >= remaining:
                return result("FAILED_FAST")
            record.retry_delay = min(delay, remaining)
            try:
                async with asyncio.timeout(None if math.isinf(remaining) else remaining):
                    await self.sleep(record.retry_delay)
            except (TimeoutError, asyncio.CancelledError):
                return result("DEADLINE_EXCEEDED" if self.clock() >= deadline else "FAILED_FAST")
