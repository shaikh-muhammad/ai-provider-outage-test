"""Security checks through application/state interfaces; no sockets."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import web

from outage_test.provider import HOST, main
from outage_test.faults import fault
from test_provider import configure, decoded, request, simulator


@pytest.mark.parametrize("raw", [b"{", b"[]", b"null", b"1", b'"x"',
    b'{"job_id":"a","text":"b","extra":1}', b'{"job_id":"a"}',
    b'{"job_id":true,"text":"b"}', b'{"job_id":"a","text":42}',
    b'{"job_id":"","text":"b"}', b'{"job_id":"a","text":""}',
    b'\xff', b'{"job_id":null,"text":"b"}', b'[' * 1000 + b']' * 1000,
    b'{"job_id":"a","text":"\\ud800"}'])
def test_bad_bodies(raw):
    async def run():
        p, _, _ = simulator()
        with pytest.raises(web.HTTPBadRequest):
            await p.complete(request(raw=raw))
        assert not p.ledger and p.rejected == 1
    asyncio.run(run())

@pytest.mark.parametrize("field,value", [("job_id", "a" * 257), ("text", "b" * 513)])
def test_string_bounds(field, value):
    async def run():
        p, _, _ = simulator()
        with pytest.raises(web.HTTPBadRequest):
            await p.complete(request(dict(job_id="a", text="b") | {field: value}))
        assert not p.ledger
    asyncio.run(run())

def test_body_limit_and_maximum_valid_strings():
    async def run():
        p, _, _ = simulator()
        with pytest.raises(web.HTTPRequestEntityTooLarge):
            await p.complete(request(raw=b" " * 2049))
        assert not p.ledger
        r = await p.complete(request(dict(job_id="a" * 256, text="b" * 512)))
        assert r.status == 200
    asyncio.run(run())

@pytest.mark.parametrize("header", ["Idempotency-Key", "X-Attempt-Id"])
@pytest.mark.parametrize("value", ["", "x" * 129, "nonascii-é", "line\nbreak", "\x7f"])
def test_header_bounds(header, value):
    async def run():
        p, _, _ = simulator()
        with pytest.raises(web.HTTPBadRequest):
            await p.complete(request(dict(job_id="a", text="b"), {header: value}))
        assert not p.ledger
    asyncio.run(run())

@pytest.mark.parametrize("route", ["reset", "configure", "start", "ledger", "counters"])
def test_every_admin_route_requires_token(route):
    async def run():
        p, now, _ = simulator()
        for headers in ({}, {"X-Admin-Token": "wrong"}, {"X-Admin-Token": "é"}):
            with pytest.raises(web.HTTPUnauthorized):
                await p.control(request({}, headers, "/admin/" + route))
        data = {"t0": now[0]} if route == "start" else {}
        r = await p.control(request(data, {"X-Admin-Token": p.token}, "/admin/" + route))
        assert r.status == 200
        client_paths = {r.resource.canonical for r in p.client.router.routes()}
        assert client_paths == {"/health", "/v1/complete"}
        assert {r.resource.canonical for r in p.admin.router.routes()} == {
            "/admin/" + name for name in ("reset", "configure", "start", "ledger", "counters")}
        assert decoded(await p.health(request())) == {"status": "ok"}
    asyncio.run(run())

@pytest.mark.parametrize("config", [dict(unknown=1), dict(seed=-1), dict(seed=2**64),
    dict(seed=True), dict(scenario="S4"), dict(scenario=[]), dict(capacity=0),
    dict(capacity=129), dict(capacity=1.5), dict(max_pending=257),
    dict(max_pending=1), dict(rate=float("nan")), dict(rate=float("inf")),
    dict(rate=-1), dict(bucket_capacity=0), dict(loss_fraction=1.1),
    dict(loss_fraction=-.1), dict(outage_start=40, outage_end=20),
    dict(idempotency=1)])
def test_invalid_configuration(config):
    async def run():
        p, _, _ = simulator()
        original = p.config.copy()
        with pytest.raises(web.HTTPBadRequest):
            await configure(p, **config)
        assert p.config == original
    asyncio.run(run())

def test_admin_schema_start_and_reset():
    async def run():
        p, now, _ = simulator()
        headers = {"X-Admin-Token": p.token}
        for route, data in (("reset", {"extra": 1}), ("start", {}),
                            ("start", {"t0": float("nan")}), ("start", {"t0": True}),
                            ("start", {"t0": now[0] + 1}), ("start", {"t0": 10**1000}),
                            ("configure", [])):
            with pytest.raises(web.HTTPBadRequest):
                await p.control(request(data, headers, "/admin/" + route))
        await configure(p, scenario="S3")
        assert p.config["idempotency"]
        await p.control(request({"t0": 90}, headers, "/admin/start"))
        assert p.t0 == 90
        await p.complete(request(dict(job_id="a", text="b")))
        assert p.ledger[0]["arrival_time"] == 10
        ledger = decoded(await p.control(request({}, headers, "/admin/ledger")))
        assert ledger["config"]["outage_start"] == 20 and ledger["t0"] == 90
        assert ledger["attempts"] == p.ledger
        await p.control(request({}, headers, "/admin/reset"))
        assert not p.ledger and not p.ordinals and not p.cache
    asyncio.run(run())


def test_active_controls_conflicting_keys_and_cancellation():
    async def run():
        p, _, _ = simulator()
        await configure(p, idempotency=True)
        entered, release = asyncio.Event(), asyncio.Event()
        async def sleep(duration):
            entered.set()
            await release.wait()
        p.sleep = sleep
        headers = {"X-Admin-Token": p.token}
        task = asyncio.create_task(p.complete(request(dict(job_id="a", text="x"),
                                                      {"Idempotency-Key": "k"})))
        await entered.wait()
        for route, data in (("reset", {}), ("configure", {}), ("start", {"t0": 100})):
            with pytest.raises(web.HTTPConflict):
                await p.control(request(data, headers, "/admin/" + route))
        for job, text in (("b", "x"), ("a", "different")):
            with pytest.raises(web.HTTPBadRequest):
                await p.complete(request(dict(job_id=job, text=text), {"Idempotency-Key": "k"}))
        assert len(p.ledger) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert p.pending == 0 and not p.cache and not p.ordinals and not p.bindings
        assert p.ledger[0]["aborted"] and not p.ledger[0]["executed"]
        release.set()
        await p.complete(request(dict(job_id="a", text="x"), {"Idempotency-Key": "k"}))
        with pytest.raises(web.HTTPBadRequest):
            await p.complete(request(dict(job_id="b", text="x"), {"Idempotency-Key": "k"}))
    asyncio.run(run())


def test_entrypoint_loopback_readiness_and_cleanup(capsys):
    async def run():
        token_line = b"synthetic-control-token-for-tests\n"
        reader = Mock(readline=AsyncMock(return_value=token_line))
        transport, loop = Mock(), asyncio.get_running_loop()
        runners = [Mock(setup=AsyncMock(), cleanup=AsyncMock(), addresses=[(HOST, port)])
                   for port in (1234, 5678)]
        with patch("outage_test.provider.asyncio.StreamReader", return_value=reader), \
             patch.object(loop, "connect_read_pipe", AsyncMock(return_value=(transport, None))), \
             patch.object(loop, "add_signal_handler", side_effect=lambda sig, cb: cb()), \
             patch.object(loop, "remove_signal_handler"), \
             patch("outage_test.provider.web.AppRunner", side_effect=runners), \
             patch("outage_test.provider.web.TCPSite") as site:
            site.return_value.start = AsyncMock()
            await main()
        assert [call.args[1:] for call in site.call_args_list] == [("127.0.0.1", 0)] * 2
        assert all(r.cleanup.await_count == 1 for r in runners)
        transport.close.assert_called_once()
    asyncio.run(run())
    assert capsys.readouterr().out == '{"client_port": 1234, "admin_port": 5678}\n'


def test_bounded_work_timeout_cleans_pending_and_cache():
    async def run():
        p, _, _ = simulator()
        await configure(p, idempotency=True)
        async def timed_out(duration):
            raise TimeoutError
        p.sleep = timed_out
        response = await p.complete(request(dict(job_id="a", text="x"), {"Idempotency-Key": "k"}))
        assert response.status == 503 and p.pending == 0
        assert not p.cache and not p.bindings and not p.ordinals
        assert p.ledger[0]["aborted"] and p.ledger[0]["decision"] == "ERROR_503"
        assert not p.ledger[0]["executed"] and p.ledger[0]["finished"]
        await configure(p, scenario="S2")
        job = next(str(i) for i in range(100) if fault(0, "S2", str(i), 1, 20) == "HUNG")
        await p.control(request({"t0": 80}, {"X-Admin-Token": p.token}, "/admin/start"))
        assert (await p.complete(request(dict(job_id=job, text="x")))).status == 503
        assert p.ledger[0]["decision"] == "HUNG" and not p.ledger[0]["executed"]
    asyncio.run(run())


def test_hung_capacity_queue_and_overflow():
    async def run():
        p, now, _ = simulator()
        await configure(p, scenario="S2", capacity=1, max_pending=2)
        job = next(str(i) for i in range(100) if fault(0, "S2", str(i), 1, 20) == "HUNG")
        now[0] = p.t0 + 20
        entered, release = asyncio.Queue(), asyncio.Queue()
        async def sleep(duration):
            entered.put_nowait(duration)
            await release.get()
        p.sleep = sleep
        tasks = []
        try:
            for i in range(3):
                tasks.append(asyncio.create_task(p.complete(request(dict(job_id=job, text="x")))))
                if i == 0:
                    assert await entered.get() == 6
            assert (await tasks[2]).status == 503
            assert len(p.ledger) == 3 and entered.empty() and p.pending == 2
            assert p.ledger[2]["resource_overflow"]
            release.put_nowait(None)
            await tasks[0]
            assert await entered.get() == 6
            release.put_nowait(None)
            await tasks[1]
            assert all(r["finished"] and not r["executed"] for r in p.ledger)
            assert [r["decision"] for r in p.ledger[:2]] == ["HUNG", "HUNG"]
            assert p.pending == 0 and not p.ordinals and not p.cache
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())
