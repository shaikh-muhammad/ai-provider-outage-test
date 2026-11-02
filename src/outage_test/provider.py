"""Local simulator; all state is synthetic and in memory.

Separate admin app: POST /admin/{reset,configure,start}, GET /admin/{ledger,
counters}; every route requires X-Admin-Token. Start accepts {"t0": monotonic
host timestamp}; configure accepts the keys in DEFAULTS (partial updates).
Counters count receipts in one-second arrival buckets, including overflow.
Malformed/conflicting inputs are pre-admission rejections, outside the ledger.
Normal work has a 12-second total deadline including capacity waits. HUNG
acquires a service-capacity slot before starting its six-second hold and never
records an execution. Timeout/cancellation records are abnormal and make
a run invalid. All configuration defaults are provisional until G7.
Launch with a private stdin pipe containing one ASCII token line (32..128
characters); readiness contains only two ports. SIGINT/SIGTERM clean up sites.
"""

import asyncio
import hashlib
import hmac
import json
import math
import signal
import sys
import time

from aiohttp import web

from .faults import TokenBucket, fault, service_time

HOST = "127.0.0.1"
DEFAULTS = dict(scenario="S0", seed=0, rate=10.0, bucket_capacity=10.0,
                outage_start=20.0, outage_end=35.0, loss_fraction=0.15,
                idempotency=False, capacity=128, max_pending=256)


def string(value, limit, ascii_only=False):
    if type(value) is not str or not 1 <= len(value) <= limit:
        raise web.HTTPBadRequest()
    if ascii_only and any(not 32 <= ord(c) <= 126 for c in value):
        raise web.HTTPBadRequest()
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise web.HTTPBadRequest()
    return value


async def body(request, allowed, required=()):
    data = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.content.iter_chunked(2049):
                data.extend(chunk)
                if len(data) > 2048:
                    raise web.HTTPRequestEntityTooLarge(max_size=2048, actual_size=len(data))
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError, TimeoutError):
        raise web.HTTPBadRequest() from None
    if type(value) is not dict or value.keys() - allowed or set(required) - value.keys():
        raise web.HTTPBadRequest()
    return value


def settings(previous, patch):
    if patch.keys() - DEFAULTS.keys():
        raise web.HTTPBadRequest()
    result = previous | patch
    if "scenario" in patch and "idempotency" not in patch:
        result["idempotency"] = patch["scenario"] == "S3"
    if type(result["scenario"]) is not str or result["scenario"] not in ("S0", "S1", "S2", "S3"):
        raise web.HTTPBadRequest()
    if type(result["idempotency"]) is not bool:
        raise web.HTTPBadRequest()
    for key, low, high, integer in (
        ("seed", 0, 2**64 - 1, True), ("capacity", 1, 128, True),
        ("max_pending", 1, 256, True), ("rate", 0, 10000, False),
        ("bucket_capacity", 1, 10000, False), ("outage_start", 0, 86400, False),
        ("outage_end", 0, 86400, False), ("loss_fraction", 0, 1, False)):
        value = result[key]
        if type(value) not in ((int,) if integer else (int, float)):
            raise web.HTTPBadRequest()
        if not low <= value <= high or not math.isfinite(value):
            raise web.HTTPBadRequest()
    if result["outage_end"] < result["outage_start"] or result["max_pending"] < result["capacity"]:
        raise web.HTTPBadRequest()
    return result


class Provider:
    """One event loop owns state: updates without awaits are atomic.

    Per-job locks serialize actual ordinals and same-key executions. Different
    jobs share only a capacity semaphore, never a lock across service sleeps.
    Key bindings reserve active payloads; only executed results enter cache.
    """

    def __init__(self, token, clock=time.monotonic, sleep=asyncio.sleep):
        string(token, 128, True)
        if len(token) < 32:
            raise web.HTTPBadRequest()
        self.token, self.clock, self.sleep = token, clock, sleep
        self.config = DEFAULTS.copy()
        self.pending = self.sequence = self.rejected = 0
        self.reset()
        self.client = web.Application(client_max_size=2048)
        self.admin = web.Application(client_max_size=2048)
        self.client.add_routes([web.post("/v1/complete", self.complete),
                                web.get("/health", self.health)])
        for route in ("reset", "configure", "start", "ledger", "counters"):
            self.admin.router.add_route("GET" if route in ("ledger", "counters") else "POST",
                                        "/admin/" + route, self.control)

    def reset(self):
        if self.pending:
            raise web.HTTPConflict()
        self.t0 = self.clock()
        self.ledger, self.cache, self.bindings, self.ordinals, self.locks = [], {}, {}, {}, {}
        self.rejected = 0
        self.slots = asyncio.Semaphore(self.config["capacity"])
        self.bucket = TokenBucket(self.config["rate"], self.config["bucket_capacity"])

    async def health(self, request):
        return web.json_response({"status": "ok"})

    async def control(self, request):
        supplied = request.headers.get("X-Admin-Token", "")
        if len(supplied) > 128 or not supplied.isascii() or not hmac.compare_digest(supplied, self.token):
            raise web.HTTPUnauthorized()
        route = request.path.rsplit("/", 1)[1]
        if route == "ledger":
            return web.json_response(dict(config=self.config, t0=self.t0,
                                          rejected=self.rejected, attempts=self.ledger))
        if route == "counters":
            return web.json_response(self.counters())
        value = await body(request, set(DEFAULTS) if route == "configure" else
                           {"t0"} if route == "start" else set(),
                           ("t0",) if route == "start" else ())
        if self.pending:
            raise web.HTTPConflict()
        if route == "configure":
            self.config = settings(self.config, value)
            self.reset()
        elif route == "start":
            t0 = value["t0"]
            if type(t0) not in (int, float) or not 0 <= t0 <= self.clock() or not math.isfinite(t0):
                raise web.HTTPBadRequest()
            self.reset()
            self.t0 = t0
            self.bucket.last = self.clock() - t0
        else:
            self.reset()
        return web.json_response({"status": "ok"})

    def counters(self):
        buckets = {}
        for row in self.ledger:
            bucket = buckets.setdefault(str(math.floor(row["arrival_time"])), {})
            for name in ("receipts", row["decision"]):
                bucket[name] = bucket.get(name, 0) + 1
            for name in ("executed", "resource_overflow"):
                bucket[name] = bucket.get(name, 0) + int(row[name])
        return dict(buckets=buckets, rejected=self.rejected)

    async def complete(self, request):
        try:
            value = await body(request, {"job_id", "text"}, ("job_id", "text"))
            job, text = string(value["job_id"], 256), string(value["text"], 512)
            key = request.headers.get("Idempotency-Key")
            correlation = request.headers.get("X-Attempt-Id")
            for header in (key, correlation):
                if header is not None:
                    string(header, 128, True)
            key = key if self.config["idempotency"] else None
            if key in self.bindings and self.bindings[key][0] != (job, text):
                raise web.HTTPBadRequest()
        except web.HTTPException:
            self.rejected += 1
            raise
        row = self.receipt(job, request.headers.get("Idempotency-Key"), correlation)
        if self.pending >= self.config["max_pending"]:
            row.update(resource_overflow=True, finished=True, completion_time=row["arrival_time"])
            return web.Response(status=503)
        self.pending += 1
        if key is not None:
            self.bindings.setdefault(key, [(job, text), 0])[1] += 1
        try:
            async with asyncio.timeout(12):
                return await self.process(request, row, job, text, key)
        except (TimeoutError, asyncio.CancelledError) as error:
            row.update(aborted=True)
            if not row["executed"]:
                row["decision"] = "ERROR_503"
            if isinstance(error, asyncio.CancelledError):
                raise
            return web.Response(status=503)
        finally:
            row.update(finished=True, completion_time=self.clock() - self.t0)
            self.pending -= 1
            if key is not None:
                self.bindings[key][1] -= 1
                if not self.bindings[key][1] and key not in self.cache:
                    del self.bindings[key]

    def receipt(self, job, key, correlation):
        self.sequence += 1
        row = dict(attempt_id=str(self.sequence), job_id=job,
                   arrival_time=self.clock() - self.t0, idempotency_key=key,
                   correlation_id=correlation, decision="ERROR_503", executed=False,
                   in_flight_at_arrival=self.pending, service_time=0.0,
                   execution_ordinal=None, resource_overflow=False, finished=False)
        self.ledger.append(row)
        return row

    def choose(self, row, job, ordinal):
        c = self.config
        decision = fault(c["seed"], c["scenario"], job, ordinal, row["arrival_time"],
                         bucket=self.bucket, outage_start=c["outage_start"],
                         outage_end=c["outage_end"], loss_fraction=c["loss_fraction"])
        row["decision"] = "SERVED" if decision == "HEALTHY" else decision

    async def process(self, request, row, job, text, key):
        if key is not None and key in self.cache:
            row["decision"] = "DEDUPED"
            return web.json_response(self.cache[key])
        if self.config["scenario"] in ("S1", "S2"):
            self.choose(row, job, self.ordinals.get(job, 0) + 1)
            if row["decision"] == "RATE_LIMITED":
                return web.Response(status=429, headers={"Retry-After": "1"})
            if row["decision"] == "ERROR_503":
                return web.Response(status=503)
            if row["decision"] == "HUNG":
                async with self.slots:
                    try:
                        async with asyncio.timeout(6):
                            await self.sleep(6)
                    except TimeoutError:
                        pass
                return web.Response(status=503)
        async with self.locks.setdefault(job, asyncio.Lock()):
            if key is not None and key in self.cache:
                row["decision"] = "DEDUPED"
                return web.json_response(self.cache[key])
            ordinal = self.ordinals.get(job, 0) + 1
            if self.config["scenario"] in ("S0", "S3"):
                self.choose(row, job, ordinal)
            async with self.slots:
                return await self.execute(request, row, job, text, key, ordinal)

    async def execute(self, request, row, job, text, key, ordinal):
        duration = service_time(self.config["seed"], self.config["scenario"], job, ordinal)
        row["service_time"] = duration
        await self.sleep(duration)
        digest = hashlib.sha256(text.encode()).digest()
        score = int.from_bytes(digest, "big") / (2**256 - 1)
        result = dict(request_id=row["attempt_id"], job_id=job,
                      label="POSITIVE" if score >= 0.5 else "NEGATIVE", score=score)
        self.ordinals[job] = ordinal
        row.update(executed=True, execution_ordinal=ordinal)
        if key is not None:
            self.cache[key] = result
        if row["decision"] == "LOST_REPLY":
            if request.transport is not None:
                request.transport.abort()
            return web.Response(status=503)
        return web.json_response(result)


async def main():
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=129)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    try:
        async with asyncio.timeout(10):
            token = (await reader.readline()).decode("ascii").removesuffix("\n")
        provider = Provider(token)
    finally:
        transport.close()
    runners = [web.AppRunner(app, access_log=None, shutdown_timeout=12)
               for app in (provider.client, provider.admin)]
    stopped = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        for runner in runners:
            await runner.setup()
            await web.TCPSite(runner, HOST, 0).start()
        print(json.dumps(dict(client_port=runners[0].addresses[0][1],
                              admin_port=runners[1].addresses[0][1])), flush=True)
        await stopped.wait()
    finally:
        for runner in runners:
            await runner.cleanup()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ValueError, UnicodeError, TimeoutError, web.HTTPException):
        sys.exit(1)
