"""PERSONAL localhost UI fixture; no business Server/Agent imports or native commands.

Serve the already-built frontend with deterministic in-memory VM records. The
fixture is deliberately not evidence of native VM lifecycle correctness.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import socket
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

OUTPUT = Path(__file__).resolve().parent
REPO = OUTPUT.parents[4]
DIST = REPO / 'web' / 'dist'
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
platform_paths = {'vm': 'android', 'harmony-vm': 'harmony', 'ios-sim': 'ios'}
rows: dict[str, list[dict]] = {}
next_list_delay: dict[str, float] = {}
events: list[dict] = []
started = monotonic()


def log(event: str, **data) -> None:
    item = {'event': event, 'seconds': round(monotonic() - started, 4), **data}
    events.append(item)
    with (OUTPUT / 'http-events.ndjson').open('a', encoding='utf8') as output:
        output.write(json.dumps(item, ensure_ascii=False) + '\n')


def details(vm: dict, platform: str) -> dict:
    if platform == 'ios':
        return vm['runtime_state']['details']
    return vm['runtime']['last_status']['details']


def runtime(vm: dict, platform: str) -> dict:
    return vm['runtime_state'] if platform == 'ios' else vm['runtime']


def reset() -> None:
    for platform in platform_paths.values():
        records = []
        for key, state, alias in [
            ('running', 'running', '运行示例'),
            ('starting', 'starting', '启动示例'),
            ('error', 'error', '停止失败示例'),
            ('stopping', 'stopping', '停止中示例'),
            ('bind-failed', 'error', '首次绑定失败示例'),
            ('offline', 'agent_offline', '离线删除示例'),
            ('switch-source', 'agent_offline', '离线换机示例'),
            ('stopped', 'stopped', '已停止示例'),
            ('probe-a', 'draft', '探查 A'),
            ('probe-b', 'draft', '探查 B'),
        ]:
            vm = {
                'id': f'{platform}-{key}', 'alias': f'{platform.upper()} · {alias}',
                'name': alias, 'state': state,
                'assigned_agent_id': '' if state == 'draft' or key in {'starting', 'bind-failed'} else 'personal-fixture-host',
                'screen_width': 1080, 'screen_height': 2340, 'density': 420,
                'error_message': '',
            }
            initial_details = {'cleanup_confirmed': state in {'stopped', 'draft'}}
            if platform == 'ios':
                vm.update(device_type='com.apple.CoreSimulator.SimDeviceType.iPhone-17',
                          device_type_name='iPhone 17', runtime='com.apple.CoreSimulator.SimRuntime.iOS-26-0',
                          runtime_name='iOS 26.0', udid='' if state == 'draft' else f'FIXTURE-{key.upper()}',
                          wda_port=None, mjpeg_port=None, runtime_state={'details': initial_details})
            else:
                vm['runtime'] = {'last_status': {'details': initial_details}}
            if platform == 'android':
                vm.update(profile_name='Pixel UI 验收机', api_level=33, abi='arm64',
                          system_type='google_apis', adb_serial='emulator-15554' if state == 'running' else '',
                          config_json={'performance': {'ram_mb': 2048, 'gpu_mode': 'auto'}})
            if platform == 'harmony':
                vm.update(device_type='Phone', screen_profile='Mate UI', os_version='6.0.31',
                          api_version='23', abi='arm64', memory_gb=4, storage_gb=8,
                          hdc_serial='127.0.0.1:19991' if state == 'running' else '',
                          hdc_port=19991 if state == 'running' else None, config_json={})
            if state == 'error':
                vm['error_message'] = '测试宿主未确认实例停止，请重试停止。'
            if key == 'bind-failed':
                vm['error_message'] = '首次启动失败，配置尚未绑定 Agent。'
            records.append(vm)
        rows[platform] = records


reset()


@app.middleware('http')
async def record_requests(request: Request, call_next):
    log('request', method=request.method, path=request.url.path)
    response = await call_next(request)
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:"
    log('response', method=request.method, path=request.url.path, status=response.status_code)
    return response


@app.get('/api/healthz')
async def health():
    return {'ok': True, 'version': 'personal-ui-fixture'}


@app.get('/api/internal/vm/device-profiles')
async def android_profiles():
    profile = {'id': 'fixture-pixel', 'marketing_name': 'Pixel UI 验收机', 'brand': 'Google',
               'manufacturer': 'Google', 'model_code': 'fixture', 'form_factor': 'Phone',
               'sdk_versions': ['33'], 'abis': ['arm64-v8a'], 'screen_width': 1080,
               'screen_height': 2340, 'densities': [420], 'ram_mb': 4096,
               'verification_status': 'verified', 'source_type': 'personal_fixture'}
    return {'items': [profile], 'page': {'total': 1, 'offset': 0, 'limit': 60, 'has_more': False},
            'stats': {'dispatchable_template_total': 1}}


@app.get('/api/internal/vm/device-facets')
async def facets():
    return {'form_factor': [{'id': 'Phone', 'count': 1}], 'brand': [{'id': 'Google', 'count': 1}],
            'sdk': [{'id': '33', 'count': 1}], 'resolution': [], 'ram': [], 'matched_total': 1}


@app.get('/api/internal/harmony-vm/catalog')
async def harmony_catalog():
    return {'ok': True, 'device_types': ['Phone'],
            'images': [{'id': 'fixture-harmony', 'device_type': 'Phone', 'os_version': 'HarmonyOS 6.0.31(23)',
                        'api_version': '23', 'abi': 'auto', 'creatable': True}],
            'screen_profiles': [{'name': 'Mate UI', 'device_type': 'Phone', 'width': 1080,
                                 'height': 2340, 'density': 420, 'size_in': '6.5',
                                 'supported_image_ids': ['fixture-harmony'],
                                 'create_methods': {'fixture-harmony': 'model'}}],
            'source': 'personal fixture', 'stats': {}}


@app.get('/api/internal/ios-sim/catalog')
async def ios_catalog():
    return {'device_types': [{'identifier': 'com.apple.CoreSimulator.SimDeviceType.iPhone-17',
                             'name': 'iPhone 17', 'product_family': 'iPhone', 'model_identifier': 'iPhone18,1',
                             'min_runtime_version': 1703936, 'max_runtime_version': 4294967295,
                             'min_runtime_version_string': '26.0', 'max_runtime_version_string': '65535.255.255'}],
            'official_runtimes': [{'identifier': 'com.apple.CoreSimulator.SimRuntime.iOS-26-0',
                                   'name': 'iOS 26.0', 'version': '26.0'}],
            'source': 'personal fixture', 'xcode_version': '26.0.1'}


@app.get('/api/internal/{domain}/instances')
async def list_instances(domain: str):
    platform = platform_paths[domain]
    snapshot = deepcopy(rows[platform])
    delay = next_list_delay.pop(platform, 0)
    log('list_snapshot', platform=platform, delay=delay,
        states={vm['id']: vm['state'] for vm in snapshot})
    if delay:
        await asyncio.sleep(delay)
    return JSONResponse(snapshot, headers={'X-Fixture-Delayed': '1' if delay else '0'})


def get_vm(domain: str, vm_id: str):
    platform = platform_paths[domain]
    vm = next((item for item in rows[platform] if item['id'] == vm_id), None)
    if vm is None:
        raise HTTPException(404, 'fixture VM not found')
    return platform, vm


@app.post('/api/internal/{domain}/instances/{vm_id}/dispatch-candidates')
async def candidates(domain: str, vm_id: str):
    get_vm(domain, vm_id)
    marker = 'A' if vm_id.endswith('probe-a') else 'B' if vm_id.endswith('probe-b') else '目标'
    delay = 0.6 if marker == 'A' else 1.4 if marker == 'B' else 0
    log('probe_begin', vm_id=vm_id, marker=marker, delay=delay)
    await asyncio.sleep(delay)
    log('probe_end', vm_id=vm_id, marker=marker)
    return {'agents': [{'agent_id': f'personal-candidate-{marker}', 'agent_name': f'个人候选 {marker}',
                        'ok': True, 'reason': '本地隔离验收候选', 'host_os': 'macOS',
                        'details': {'host_abi': 'arm64'}}]}


@app.post('/api/internal/{domain}/instances/{vm_id}/{action}')
async def action(domain: str, vm_id: str, action: str, request: Request):
    platform, vm = get_vm(domain, vm_id)
    if action == 'start':
        vm['state'] = 'starting'
        details(vm, platform)['cleanup_confirmed'] = False
    elif action == 'stop':
        vm['state'] = 'stopped'
        vm['error_message'] = ''
        details(vm, platform)['cleanup_confirmed'] = True
        if platform == 'harmony':
            vm['hdc_port'], vm['hdc_serial'] = None, ''
    elif action == 'dispatch':
        payload = await request.json()
        old_id = vm['id']
        switched = bool(vm['assigned_agent_id'] and payload['agent_id'] != vm['assigned_agent_id'])
        if switched:
            rows[platform].remove(vm)
            vm = deepcopy(vm)
            vm['id'] = f'{platform}-switched-1'
            vm['alias'] = f'{platform.upper()} · 新空白实例'
            vm['name'] = '新空白实例'
            rows[platform].insert(0, vm)
        vm['state'] = 'starting'
        vm['assigned_agent_id'] = ''
        vm['error_message'] = ''
        runtime(vm, platform)['operation'] = {'action': 'start', 'target_agent_id': payload['agent_id'], 'request_id': 'fixture-start'}
        details(vm, platform)['cleanup_confirmed'] = False
        log('vm_action', platform=platform, vm_id=vm['id'], old_vm_id=old_id, action='dispatch', switched=switched, state='starting', target=payload['agent_id'])
        return {'sent': True, 'switched': switched, 'old_vm_id': old_id if switched else None, 'instance': deepcopy(vm)}
    else:
        raise HTTPException(404, 'Unsupported fixture action')
    log('vm_action', platform=platform, vm_id=vm_id, action=action, state=vm['state'])
    return {'sent': True, 'instance': deepcopy(vm)}


@app.delete('/api/internal/{domain}/instances/{vm_id}')
async def remove(domain: str, vm_id: str):
    platform, vm = get_vm(domain, vm_id)
    rows[platform].remove(vm)
    log('delete_ledger', platform=platform, vm_id=vm_id, previous_state=vm['state'])
    return {'id': vm_id, 'deleted': True, 'cleanup_sent': vm['state'] != 'agent_offline'}


@app.post('/fixture/complete-bind')
async def complete_bind(request: Request):
    body = await request.json()
    platform, vm = get_vm(body['domain'], body['vm_id'])
    operation = runtime(vm, platform).get('operation', {})
    state = body['state']
    vm['state'] = state
    vm['assigned_agent_id'] = operation.get('target_agent_id', '') if state == 'running' else ''
    vm['error_message'] = '' if state == 'running' else '首次启动失败，配置尚未绑定 Agent。'
    details(vm, platform)['cleanup_confirmed'] = state != 'running'
    log('bind_ack', platform=platform, vm_id=vm['id'], state=state, assigned_agent_id=vm['assigned_agent_id'])
    return {'ok': True}


@app.post('/fixture/arm-list-delay')
async def arm_delay(request: Request):
    body = await request.json()
    next_list_delay[body['platform']] = float(body.get('seconds', 2.5))
    log('arm_list_delay', **body)
    return {'ok': True}


@app.get('/fixture/events')
async def read_events():
    return events


@app.get('/fixture/state')
async def read_state():
    return rows


@app.post('/fixture/reset')
async def reset_all():
    reset()
    next_list_delay.clear()
    log('reset')
    return {'ok': True}


app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')


@app.get('/favicon.svg')
async def favicon():
    return FileResponse(DIST / 'favicon.svg')


@app.get('/virtual-machines')
async def page():
    return FileResponse(DIST / 'index.html')


if __name__ == '__main__':
    (OUTPUT / 'http-events.ndjson').write_text('', encoding='utf8')
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    (OUTPUT / 'fixture-url.txt').write_text(url + '\n', encoding='utf8')
    print(f'PERSONAL_UI_FIXTURE={url}', flush=True)
    uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')).run(sockets=[listener])
