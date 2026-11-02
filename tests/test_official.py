"""Synthetic official evidence; no provider, network or real-time sleeps."""
import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import itertools
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from bench import run_official as official
from outage_test import client, faults, harness, runner

ROOT = Path(__file__).resolve().parents[1]
WHO = dict(version=1, sha256="d"*64, git_commit="a"*40)
REAL_IDENTITY = official.identity


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("provider forbidden in offline tests"))
    monkeypatch.setattr(runner, "start_provider", forbidden)
    yield
    forbidden.assert_not_called()


@pytest.fixture
def registered(tmp_path, monkeypatch):
    names = official.YAMLS + SOURCES + ("bench/registration.lock.json", "requirements-run.lock",
        "requirements-dev.lock", ".github/workflows/ci.yml", "bench/run_official.py")
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    numbers = itertools.count(1)
    monkeypatch.setattr(official.uuid, "uuid4", lambda: SimpleNamespace(hex=f"{next(numbers):032x}"))
    monkeypatch.setattr(official, "identity", lambda root: deepcopy(WHO))
    return official.preflight(tmp_path, ancestry=lambda commit: commit == official.FREEZE_COMMIT)


SOURCES = official.SOURCES


def document(reg, slot=None, attempt=1, complete=True):
    slot = deepcopy(slot or reg["scenarios"]["order"][0])
    env = dict(os="Linux", os_release="synthetic", kernel="synthetic", python="3.14.4", cpu_count=4,
               dependencies=reg["dependencies"], git_commit=WHO["git_commit"])
    cfg, records, ledger, buckets = official.config(slot), [], [], {}
    jobs = harness.generate_workload(slot["scenario"], slot["workload_seed"])
    for i, job in enumerate(jobs if complete else ()):
        duration = faults.service_time(slot["fault_seed"], slot["scenario"], job.job_id, 1)
        score = int.from_bytes(bytes.fromhex(official.digest(job.text.encode())), "big") / (2**256-1)
        response = dict(request_id=str(i+1), job_id=job.job_id, label="POSITIVE" if score >= .5 else "NEGATIVE", score=score)
        wire = asdict(client.Attempt(f"wire-{i}", 1, 100+job.offset, 100+job.offset+duration, 200, None,
                                     5.0 if slot["config"] == "C0" else 2.0))
        records.append(dict(job_id=job.job_id, intended_offset=job.offset, launch_offset=job.offset, launch_lag=0,
            completion_offset=job.offset+duration, latency=duration, outcome="SUCCESS", success=True,
            response=response, attempts=[wire], error=None))
        ledger.append(dict(attempt_id=str(i+1), job_id=job.job_id, arrival_time=job.offset,
            idempotency_key=official.digest(job.job_id.encode()) if slot["config"] == "C4" else None,
            correlation_id=wire["attempt_id"], decision="SERVED", executed=True, in_flight_at_arrival=0,
            service_time=duration, execution_ordinal=1, resource_overflow=False, finished=True,
            completion_time=job.offset+duration))
        bucket = buckets.setdefault(str(math.floor(job.offset)), Counter())
        for key, value in (("receipts",1), ("SERVED",1), ("executed",1), ("resource_overflow",0)):
            bucket[key] += value
    context = dict(environment=env, provider_counters=dict(buckets=buckets, rejected=0), provider_config=cfg if complete else {},
        lifecycle=dict(pid=123, returncode=0, reaped=True, forced_kill=False, unexpected_exit=False) if complete else {},
        errors=[] if complete else ["SyntheticFailure"], registration=reg["fingerprint"], t0=100, provider_t0=100,
        sampling=dict(method="fixed-anchor-catch-up", origin=100, stop_requested_at=160, finished_at=160,
                      sample_count=6000, scheduled_ticks=6000, unobserved_tail_ticks=0))
    health = runner.measured_health([0]*6000, records, .1, context)
    health["measured"]["limits"] = dict(loop_lag_p99=.035, launch_lag_p99=.05, frozen=True)
    expected = {k: cfg[k] for k in harness.FAULT_FIELDS}
    doc = dict(schema_version=1, official=True, slot=slot, attempt=attempt, registration=reg["fingerprint"],
        runner=deepcopy(WHO), environment=env, run=dict(schedule=[asdict(j) for j in jobs], records=records), ledger=ledger,
        metadata=dict(health=health, expected_faults=expected, applied_faults=deepcopy(expected)), wall_seconds=60.5, validity=None)
    doc["validity"] = official.assess(doc, reg, WHO)
    assert doc["validity"]["status"] == ("VALID" if complete else "INVALID")
    return doc


def save(fd, doc):
    with official.reservation(fd, doc["slot"]) as (output, directory, path):
        official.finish(output, directory, doc)
    return path


def test_preflight_valid_and_wrong_ancestry(registered, tmp_path):
    assert len(registered["scenarios"]["order"]) == 51
    with pytest.raises(ValueError, match="ancestry"):
        official.preflight(tmp_path, ancestry=lambda _: False)


def test_dependency_drift_rejected(registered, tmp_path, monkeypatch):
    monkeypatch.setattr(runner.metadata,'version',lambda name:'unregistered')
    with pytest.raises(ValueError): official.preflight(tmp_path,ancestry=lambda _:True)


@pytest.mark.parametrize("damage", ["yaml", "lock", "implementation", "missing", "duplicate", "seed", "configuration"])
def test_preflight_corruption(registered, tmp_path, damage):
    if damage in ("yaml", "missing", "duplicate", "seed", "configuration"):
        path = tmp_path / official.YAMLS[1]
        data = yaml.safe_load(path.read_bytes())
        if damage == "missing": data["order"].pop()
        elif damage == "duplicate": data["order"][1] = data["order"][0]
        elif damage == "seed": data["order"][0]["fault_seed"] = 9001
        elif damage == "configuration": data["order"][0]["config"] = "C9"
        path.write_text(yaml.safe_dump(data)+"\n")
    else:
        path = tmp_path / ("bench/registration.lock.json" if damage == "lock" else SOURCES[0])
        path.write_bytes(path.read_bytes()+b"\n")
    with pytest.raises(ValueError): official.preflight(tmp_path, ancestry=lambda _: True)


@pytest.mark.parametrize("damage", ["health", "health_missing", "receipt", "fault", "config", "records", "sampling", "environment", "lifecycle", "timeout", "key"])
def test_invalid_evidence_fails_closed(registered, damage):
    doc = document(registered)
    h = doc["metadata"]["health"]
    if damage == "health": h["measured"]["limits"]["loop_lag_p99"] = .04
    elif damage == "health_missing": del h["measured"]
    elif damage == "receipt": doc["ledger"][0]["correlation_id"] = "foreign"
    elif damage == "fault": doc["metadata"]["applied_faults"]["seed"] = 9001
    elif damage == "config": h["context"]["provider_config"]["idempotency"] = True
    elif damage == "records": doc["run"]["records"].pop()
    elif damage == "sampling": del h["context"]["sampling"]
    elif damage == "environment": doc["environment"]["dependencies"] = {}
    elif damage == "lifecycle": h["context"]["lifecycle"]["reaped"] = False
    elif damage == "timeout": doc["run"]["records"][0]["attempts"][0]["timeout"] = 5
    else: doc["ledger"][0]["idempotency_key"] = "wrong"
    assert official.assess(doc, registered, WHO)["status"] == "INVALID"


@pytest.mark.parametrize("field,value", [("fault_seed",9001), ("config","C9"), ("ordinal",True)])
def test_wrong_slot_rejected(registered, field, value):
    doc = document(registered)
    doc["slot"][field] = value
    with pytest.raises(ValueError): official.assess(doc, registered, WHO)


def test_preflight_cli_is_read_only(registered, tmp_path, monkeypatch, capsys):
    original, opening = official.preflight, official.os.open
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    def readonly(path, flags, *args, **kwargs):
        assert not flags & (official.os.O_WRONLY | official.os.O_RDWR | official.os.O_CREAT | official.os.O_TRUNC)
        return opening(path, flags, *args, **kwargs)
    monkeypatch.setattr(official.os, "open", readonly)
    monkeypatch.setattr(official, "preflight", lambda: original(tmp_path, ancestry=lambda _: True))
    monkeypatch.setattr(official, "execute", Mock(side_effect=AssertionError("execution forbidden")))
    assert official.main(["--preflight"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert (output["cells"], output["slots"], output["official_runs_executed"]) == (17,51,0)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_committed_identity_required(registered, tmp_path, monkeypatch):
    # Exercise the real guard, not the fixture's identity injection.
    monkeypatch.setattr(official, "git", lambda root,*args: b"old code" if args[0] == "show" else b"")
    with pytest.raises(ValueError, match="committed"): REAL_IDENTITY(tmp_path)


def test_exclusive_and_never_overwrite(registered, tmp_path, monkeypatch):
    doc = document(registered, complete=False)
    monkeypatch.setattr(official.uuid, "uuid4", lambda: SimpleNamespace(hex="1"*32))
    with official.official_root(tmp_path) as fd:
        with official.reservation(fd, doc["slot"]) as (output, directory, path):
            official.finish(output, directory, doc)
            with pytest.raises(ValueError): official.finish(output, directory, doc)
        original = (tmp_path / 'results/official' / path).read_bytes()
        with pytest.raises(FileExistsError):
            with official.reservation(fd, doc["slot"]): pass
        assert original == (tmp_path / 'results/official' / path).read_bytes()


@pytest.mark.parametrize("level", ["results", "official", "cell", "result"])
def test_symlinks_rejected(registered, tmp_path, level):
    outside = tmp_path / 'outside'
    outside.mkdir()
    slot = registered["scenarios"]["order"][0]
    if level == "results": (tmp_path / 'results').symlink_to(outside, target_is_directory=True)
    elif level == "official":
        (tmp_path / 'results').mkdir()
        (tmp_path / 'results/official').symlink_to(outside, target_is_directory=True)
    else:
        with official.official_root(tmp_path) as fd:
            scenario = tmp_path / 'results/official' / slot['scenario']
            scenario.mkdir()
            if level == "cell":
                (scenario / slot['config']).symlink_to(outside, target_is_directory=True)
                with pytest.raises(OSError):
                    with official.reservation(fd, slot): pass
            else:
                directory = scenario / slot['config']
                directory.mkdir()
                (directory / 'run-1-00000000000000000000000000000001.json').symlink_to(outside / 'absent')
                with pytest.raises(OSError): official.discover(fd, registered, WHO)
        return
    with pytest.raises(OSError):
        with official.official_root(tmp_path): pass


def test_full_order_invalid_preservation_and_completed_resume(registered, tmp_path):
    calls = []
    async def capture(slot, attempt, reg, who):
        calls.append(deepcopy(slot))
        return document(reg, slot, attempt, complete=not(slot['ordinal'] == 1 and attempt == 1))
    result = asyncio.run(official.execute(registered, tmp_path, capture))
    assert result == dict(status="COMPLETE", valid_slots=51, official_runs_executed=52)
    assert calls == [registered['scenarios']['order'][0]] + registered['scenarios']['order']
    directory = tmp_path / 'results/official'
    snapshots = {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*.json')}
    manifests = [json.loads(v) for p,v in snapshots.items() if p.name.startswith('manifest-')]
    assert [a['status'] for a in manifests[0]['slots'][0]['attempts']] == ['INVALID','VALID']
    assert sum(len(s['attempts']) for s in manifests[0]['slots']) == 52
    assert asyncio.run(official.execute(registered,tmp_path,capture))['official_runs_executed'] == 0
    assert snapshots == {p.relative_to(directory): p.read_bytes() for p in directory.rglob('*.json')}


@pytest.mark.parametrize('ordinal', [1,3])
def test_three_attempt_cap_across_restart(registered, tmp_path, ordinal):
    slot = registered['scenarios']['order'][ordinal-1]
    first = document(registered, slot, attempt=1, complete=False)
    with official.official_root(tmp_path) as fd:
        for earlier in registered['scenarios']['order'][:ordinal-1]:
            save(fd,document(registered,earlier))
        saved = save(fd,first)
    original = (tmp_path / 'results/official' / saved).read_bytes()
    directory = tmp_path / 'results/official'
    earlier_bytes = {p: p.read_bytes() for p in directory.rglob('*.json')}
    calls = []
    async def invalid(slot, attempt, reg, who):
        calls.append((deepcopy(slot),attempt))
        return document(reg,slot,attempt,complete=False)
    result = asyncio.run(official.execute(registered,tmp_path,invalid))
    assert result == dict(status="INCOMPLETE",valid_slots=ordinal-1,official_runs_executed=2)
    assert [a for s,a in calls if s['ordinal'] == ordinal] == [2,3]
    assert all(s == slot for s,a in calls)
    assert all(s == registered['scenarios']['order'][s['ordinal']-1] for s,a in calls)
    manifests = list(directory.glob('manifest-*.json'))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_bytes())
    assert manifest['status'] == 'INCOMPLETE' and len(manifest['slots']) == 51
    assert all(row['status'] == 'VALID' for row in manifest['slots'][:ordinal-1])
    exhausted = manifest['slots'][ordinal-1]
    assert exhausted['status'] == 'INCOMPLETE'
    assert [a['attempt'] for a in exhausted['attempts']] == [1,2,3]
    assert all(a['status'] == 'INVALID' for a in exhausted['attempts'])
    assert all(not row['attempts'] for row in manifest['slots'][ordinal:])  # Later slots NOT RUN.
    for row in manifest['slots']:
        for attempt in row['attempts']:
            assert official.digest((directory / attempt['path']).read_bytes()) == attempt['sha256']
    snapshots = {p: p.read_bytes() for p in directory.rglob('*.json')}
    forbidden = AsyncMock(side_effect=AssertionError('exhausted batch must stay blocked'))
    assert asyncio.run(official.execute(registered,tmp_path,forbidden)) == dict(
        status='INCOMPLETE',valid_slots=ordinal-1,official_runs_executed=0)
    forbidden.assert_not_awaited()
    assert snapshots == {p: p.read_bytes() for p in directory.rglob('*.json')}
    assert all(p.read_bytes() == raw for p,raw in earlier_bytes.items())
    assert original == (tmp_path / 'results/official' / saved).read_bytes()


def test_first_slot_exhausted_from_empty_batch(registered, tmp_path):
    calls = []
    async def invalid(slot, attempt, reg, who):
        calls.append((deepcopy(slot),attempt))
        return document(reg,slot,attempt,complete=False)
    assert asyncio.run(official.execute(registered,tmp_path,invalid)) == dict(
        status='INCOMPLETE',valid_slots=0,official_runs_executed=3)
    assert calls == [(registered['scenarios']['order'][0],n) for n in (1,2,3)]


def test_resume_rejects_attempts_after_exhaustion(registered, tmp_path):
    order = registered['scenarios']['order']
    with official.official_root(tmp_path) as fd:
        for attempt in (1,2,3): save(fd,document(registered,order[0],attempt,complete=False))
        save(fd,document(registered,order[1],complete=False))
    forbidden = AsyncMock()
    with pytest.raises(ValueError,match='registered order'):
        asyncio.run(official.execute(registered,tmp_path,forbidden))
    forbidden.assert_not_awaited()


def test_incomplete_result_has_nonzero_exit(registered, monkeypatch, capsys):
    monkeypatch.setattr(official.argparse.ArgumentParser,'parse_args',lambda self,argv:SimpleNamespace(preflight=False))
    monkeypatch.setattr(official,'preflight',lambda:registered)
    execution = AsyncMock(return_value=dict(status='INCOMPLETE',valid_slots=0,official_runs_executed=3))
    monkeypatch.setattr(official,'execute',execution)
    assert official.main([]) == 2
    execution.assert_awaited_once_with(registered)
    assert json.loads(capsys.readouterr().out)['status'] == 'INCOMPLETE'


@pytest.mark.parametrize("damage", ['bytes','decision','after_valid','gap','out_of_order','manifest'])
def test_resume_rejects_corruption(registered, tmp_path, damage):
    doc = document(registered, complete=damage == 'after_valid')
    with official.official_root(tmp_path) as fd:
        if damage == 'gap': doc['attempt'] = 2
        if damage == 'out_of_order': doc = document(registered,registered['scenarios']['order'][1],complete=False)
        path = save(fd,doc)
        if damage == 'after_valid': save(fd,document(registered,attempt=2,complete=False))
        if damage == 'bytes': (tmp_path / 'results/official' / path).write_bytes(b'{broken')
        if damage == 'decision':
            doc['validity'] = dict(status='VALID',reasons=[])
            (tmp_path / 'results/official' / path).write_bytes(official.encoded(doc))
        if damage == 'manifest':
            values = {i:[] for i in range(1,52)}
            bad = official.manifest(values,registered,WHO)
            bad['status'] = 'COMPLETE'
            (tmp_path / 'results/official/manifest-00000000000000000000000000000000.json').write_bytes(official.encoded(bad))
        with pytest.raises((ValueError,KeyError)): official.discover(fd,registered,WHO)


def test_interruption_reserves_attempt_and_blocks_resume(registered, tmp_path):
    capture = AsyncMock(side_effect=RuntimeError('synthetic interruption'))
    with pytest.raises(RuntimeError): asyncio.run(official.execute(registered,tmp_path,capture))
    files = list((tmp_path / 'results/official').rglob('run-*.json'))
    assert len(files) == 1 and files[0].read_bytes() == b''
    with pytest.raises(ValueError): asyncio.run(official.execute(registered,tmp_path,capture))
    capture.assert_awaited_once()


def test_concurrent_engine_rejected(registered, tmp_path):
    capture = AsyncMock()
    with official.official_root(tmp_path):
        with pytest.raises(BlockingIOError): asyncio.run(official.execute(registered,tmp_path,capture))
    capture.assert_not_called()


def test_provenance_drift_stops_before_capture(registered, tmp_path, monkeypatch):
    values = iter([WHO, WHO | dict(git_commit='b'*40)])
    monkeypatch.setattr(official,'identity',lambda root:next(values))
    capture = AsyncMock()
    with pytest.raises(ValueError): asyncio.run(official.execute(registered,tmp_path,capture))
    capture.assert_not_called()


@pytest.mark.parametrize('kind', ['home','tmp','password'])
def test_unsafe_data_is_never_persisted(registered, tmp_path, kind):
    doc = document(registered,complete=False)
    if kind == 'password': doc['environment']['password'] = 'synthetic-value'
    else: doc['environment']['kernel'] = '/'.join(('',kind,'synthetic','private'))
    with official.official_root(tmp_path) as fd:
        with official.reservation(fd,doc['slot']) as (output,directory,path):
            with pytest.raises(ValueError): official.finish(output,directory,doc)
    assert (tmp_path / 'results/official' / path).read_bytes() == b''


def test_malformed_deep_json_is_rejected():
    with pytest.raises(ValueError): official.decoded(b'['*2000+b']'*2000)


def test_json_depth_boundary_and_quoted_delimiters():
    value = 0
    for _ in range(official.MAX_JSON_DEPTH-1): value = [value]
    assert official.MAX_JSON_DEPTH == 16
    assert official.decoded(official.encoded(dict(value=value))) == dict(value=value)
    with pytest.raises(ValueError, match='nesting'):
        official.decoded(official.encoded(dict(value=[value])))
    quoted = dict(text='brackets [{]} and escaped quote " and backslash \\')
    assert official.decoded(official.encoded(quoted)) == quoted
    with pytest.raises(ValueError): official.private_check(dict(value=[value]))


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
    b'{"a":1e999}', b'{"a":', b'[]', b'{"a":]'])
def test_bad_json_fails_closed(raw):
    with pytest.raises(ValueError): official.decoded(raw)


def test_strict_envelope_and_compatibility_copy(registered):
    doc = document(registered)
    original = deepcopy(doc)
    assert official.assess(doc,registered,WHO)['status'] == 'VALID' and doc == original
    assert doc['metadata']['health']['measured']['limits']['frozen'] is True
    doc['unknown'] = True
    with pytest.raises(ValueError): official.assess(doc,registered,WHO)
    del doc['unknown'], doc['metadata']
    with pytest.raises(ValueError): official.assess(doc,registered,WHO)


def test_cancellation_waits_for_cleanup(monkeypatch):
    async def run():
        started, release, finished = asyncio.Event(), asyncio.Event(), []
        async def stop(process):
            started.set()
            await release.wait()
            finished.append(True)
            return {}
        monkeypatch.setattr(runner,'stop_provider',stop)
        task = asyncio.create_task(official.reap(object()))
        await started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        assert finished == [True]
    asyncio.run(run())


@pytest.mark.parametrize('failure', [None,'exception','cancel','secret'])
def test_capture_cleanup_and_private_data(registered, monkeypatch, failure):
    base = document(registered)
    environment = deepcopy(base['environment'])
    stop = AsyncMock(return_value=base['metadata']['health']['context']['lifecycle'])
    monkeypatch.setattr(runner,'environment',lambda:environment)
    monkeypatch.setattr(runner,'start_provider',AsyncMock(return_value=(object(),{},'synthetic-admin-value')))
    monkeypatch.setattr(runner,'stop_provider',stop)
    monkeypatch.setattr(official.aiohttp,'TCPConnector',lambda **kwargs:None)
    class Session:
        def __init__(self,**kwargs): assert kwargs['trust_env'] is False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): return False
    monkeypatch.setattr(official.aiohttp,'ClientSession',Session)
    async def exercise(session,ports,token,cfg,config,jobs,context,evidence):
        if failure == 'cancel': raise asyncio.CancelledError
        if failure == 'exception': raise ValueError('/'.join(('', 'home', 'synthetic', 'private')))
        context.update(deepcopy(base['metadata']['health']['context']))
        evidence.update(run=deepcopy(base['run']),ledger=deepcopy(base['ledger']),samples=[0]*6000,cpu_seconds=.1)
        if failure == 'secret': environment['kernel'] = token
    monkeypatch.setattr(runner,'exercise',exercise)
    call = official.collect(base['slot'],1,registered,WHO,clock=iter([0,60.5]).__next__)
    if failure in ('cancel','secret'):
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else ValueError): asyncio.run(call)
    else:
        captured = asyncio.run(call)
        assert captured['validity']['status'] == ('INVALID' if failure else 'VALID')
        assert b'synthetic-admin-value' not in official.encoded(captured)
    stop.assert_awaited_once()
