"""Read-only product review: loopback API, private SQLite, fake Agent, real CLI."""
import os, sys, tempfile, pathlib, asyncio, socket, json, hashlib, re, shutil

repo = pathlib.Path('/Users/dongxin/.codex/worktrees/vm-lifecycle-hardening/ai-phone')
cli = pathlib.Path('/Users/dongxin/.codex/worktrees/cli-vm-lifecycle-compat/aiphone-cli')
node = shutil.which('node')
assert node
scratch = tempfile.TemporaryDirectory(prefix='vm-cli-independent-')
os.chdir(scratch.name)
for key in list(os.environ):
    if key.startswith(('AI_PHONE_', 'AIPHONE_')) or key.lower().endswith('_proxy') or key == 'NODE_OPTIONS':
        os.environ.pop(key)
os.environ.update(AI_PHONE_DB_URL='sqlite+aiosqlite:///:memory:', AI_PHONE_STORAGE_DIR=str(pathlib.Path(scratch.name)/'storage'), AI_PHONE_SUBMISSION_INTERNAL_TOKEN='personal-local-test', AI_PHONE_AGENT_TOKEN='personal-local-test')
sys.path.insert(0, str(repo/'backend'))
from fastapi import FastAPI
import uvicorn
from ai_phone.server import db
from ai_phone.server.db import Base
from ai_phone.server.api import include_routers  # initialize existing package import order
from ai_phone.server.android_vm import api as android
from ai_phone.server.ios_sim import api as ios
from ai_phone.server.harmony_vm import api as harmony
from ai_phone.server.models import AndroidVmInstance
from ai_phone.server.ios_sim.models import IosSimVmInstance
from ai_phone.server.harmony_vm.models import HarmonyVmInstance
from ai_phone.server.vm_lifecycle import update_runtime
from tests.test_vm_lifecycle_contract import ContractHub
from types import SimpleNamespace


class FakeHub(ContractHub):
    async def send_to_agent(self, agent_id, payload):
        self.sent.append((agent_id, dict(payload)))
        return False


async def available(**kwargs):
    return {'ok': True}


async def candidates(**kwargs):
    return {'agents': [{'agent_id': 'new', 'ok': True}]}


waiter = SimpleNamespace(probe=candidates, probe_agent=available, excluded_ports_for=lambda _: [])
for module in (android, ios, harmony):
    module.get_capability_waiter = lambda: waiter


async def main():
    db.init_engine(db_url='sqlite+aiosqlite:///:memory:')
    async with db.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    hub = FakeHub()
    app = FastAPI()
    app.state.hub = hub
    app.state.lock_store = hub.vm_lock_store
    for module in (android, ios, harmony):
        app.include_router(module.router)
    trace = []

    @app.middleware('http')
    async def capture(request, call_next):
        response = await call_next(request)
        trace.append({'method': request.method, 'path': request.url.path, 'status': response.status_code})
        return response

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', lifespan='off'))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    results = []
    try:
        for platform, model in [('android', AndroidVmInstance), ('ios', IosSimVmInstance), ('harmony', HarmonyVmInstance)]:
            for action in ('start', 'stop', 'dispatch', 'delete'):
                hub.online = {'old', 'new'} if action != 'delete' else set()
                hub.sent.clear()
                trace.clear()
                async with db.get_session_factory()() as session:
                    fields = dict(name=f'cli-{platform}-{action}', alias=f'cli-{platform}-{action}', state='running' if action == 'stop' else 'stopped', assigned_agent_id='old')
                    if platform == 'android':
                        fields.update(api_level=33, abi='arm64')
                    if platform == 'ios':
                        fields['udid'] = f'OWN-FAKE-UDID-{action}'
                    vm = model(**fields)
                    session.add(vm)
                    await session.flush()
                    update_runtime(vm, owner_host_id='host-one', instance_created=True)
                    await session.commit()
                    old_id = vm.id
                env = dict(os.environ)
                env.update(AIPHONE_BASE_URL=f'http://127.0.0.1:{port}', AIPHONE_INTERNAL_TOKEN='personal-local-test', VM_TEST_PORT=str(port), NODE_PATH='/Users/dongxin/aiphonecli开源/node_modules', NODE_OPTIONS=f'--require {cli}/test/helpers/vm-network-guard.cjs', NO_PROXY='127.0.0.1,localhost')
                args = [node, str(cli/'bin/aiphone.js'), 'vm', platform, action, old_id]
                if action == 'dispatch':
                    args += ['--agent', 'new']
                process = await asyncio.create_subprocess_exec(*args, env=env, cwd=scratch.name, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
                data = json.loads(stdout)
                guarded = re.findall(r'^VM_TEST_GUARD (.+)$', stderr.decode(), re.M)
                assert len(guarded) == 1, stderr.decode()
                guard = json.loads(guarded[0])
                assert guard['profileReads'] == 0 and guard['loopbackOnly'] and guard['requests'] > 0
                if action == 'delete':
                    assert process.returncode == 0 and data['deleted'] is True and data['cleanup_sent'] is False, data
                    assert data['host_cleanup_confirmed'] is False
                    async with db.get_session_factory()() as session:
                        assert await session.get(model, old_id) is None
                    result = {'platform':platform, 'action':action, 'exit':process.returncode, 'deleted':data['deleted'], 'cleanup_sent':data['cleanup_sent']}
                else:
                    expected_http = 409 if action == 'dispatch' or platform != 'ios' else 502
                    assert process.returncode == 1 and data['ok'] is False, data
                    error = data['error']
                    assert error['http_status'] == expected_http, data
                    assert error['vm_id'] == error['instance']['id'] and error['request_id'], data
                    field = 'runtime_state' if platform == 'ios' else 'runtime'
                    op = error['instance'][field]['operation']
                    assert op['request_id'] == error['request_id'] and op['outcome'] == 'failed' and op['pending'] is False, data
                    starts = [msg for _,msg in hub.sent if msg['type'].endswith('vm_start')]
                    stops = [msg for _,msg in hub.sent if msg['type'].endswith('vm_stop')]
                    if action == 'dispatch':
                        assert error['vm_id'] != old_id and error['retired_vm_id'] == old_id and error['switched'] is True, data
                        assert error['vm_id'] in error['next_commands']['status']
                        async with db.get_session_factory()() as session:
                            assert await session.get(model, old_id) is None
                            new = await session.get(model, error['vm_id'])
                            assert new is not None and new.assigned_agent_id is None
                        assert len(starts) == 1 and not stops, hub.sent
                    else:
                        assert error['vm_id'] == old_id
                        assert (len(starts),len(stops)) == ((1,0) if action == 'start' else (0,1)), hub.sent
                    result = {'platform':platform, 'action':action, 'exit':process.returncode, 'http_status':expected_http, 'id_promoted':True, 'request_id_promoted':True, 'new_id_preserved':action == 'dispatch', 'operation_outcome':op['outcome']}
                result.update(guard=guard, http_trace=list(trace), fake_messages=[message['type'] for _,message in hub.sent])
                results.append(result)
                print(json.dumps(result), flush=True)
        paths = [repo/'backend/ai_phone/server/vm_lifecycle.py', repo/'backend/ai_phone/server/android_vm/api.py', repo/'backend/ai_phone/server/ios_sim/api.py', repo/'backend/ai_phone/server/harmony_vm/api.py']
        evidence = {'cli_commit':'e65dad549d2d667972c44718962d6c6f87bf8eb9', 'scope':'actual current FastAPI VM routers, isolated SQLite, fake Agents, actual CLI child; no native', 'passed':len(results), 'results':results, 'server_source_sha256':{str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
        output = pathlib.Path('/tmp/vm-cli-independent-current-server.json')
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
        print(f'EVIDENCE {output}')
    finally:
        server.should_exit = True
        await serving
        await db.dispose_engine()
        sock.close()


asyncio.run(main())
scratch.cleanup()
