"""Execute unchanged manager AST against only owned fake time/process/ADB data."""
import ast
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import psutil

SOURCE = Path('/Users/dongxin/.codex/worktrees/vm-lifecycle-hardening/ai-phone/backend/ai_phone/agent/android_vm/manager.py')
raw = SOURCE.read_text()
tree = ast.parse(raw)
original_class = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'AndroidVmManager')
method_names = {'stop_sync', '_stop_sync_locked', '_cleanup_failure', '_sync_guest', '_emulator_avd_name',
                '_kill_emulator', '_runtime_exit_confirmed', '_forget_runtime', 'stop_all', 'delete_sync', '_delete_sync_locked'}
function_names = {'_stop_owned_process', '_native_shutdown_grace', '_startup_timeout', '_startup_lock',
                  '_safe_avd_name', '_emulator_port', '_opt_port'}
functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in function_names]
methods = [n for n in original_class.body if isinstance(n, ast.FunctionDef) and n.name in method_names]
compiled = compile(ast.fix_missing_locations(ast.Module(body=functions + [ast.ClassDef(
    name='Manager', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])), str(SOURCE), 'exec')


def run_case(case):
    clock = [100.0]
    calls = []
    marker = 'a' * 32
    marker_calls = []
    vm_id = "a'; echo injected #" if case == 'no-untrusted-text-in-shell' else 'a'
    request_id = "stop'; touch forbidden #"

    class Process:
        def __init__(self, pid, port):
            self.pid, self.port, self.code = pid, port, None

        def environ(self):
            return {}

        def poll(self):
            return self.code

        def is_running(self):
            return self.code is None

        def wait(self, timeout):
            if self.code is not None:
                return self.code
            clock[0] += timeout
            raise subprocess.TimeoutExpired('fake-process', timeout)

        def terminate(self):
            calls.append({'kind': 'terminate', 'pid': self.pid})
            self.code = -15

        def kill(self):
            calls.append({'kind': 'kill', 'pid': self.pid})
            self.code = -9

    processes = [Process(111, 5554)]
    if case == 'ambiguous-second-serial':
        processes.append(Process(222, 5556))
    if case == 'unknown-second-process-port':
        processes.append(Process(222, 0))
    if case == 'already-exited':
        processes[0].code = 0

    identity_queries = [0]

    def run(args, **kwargs):
        assert args[:2] == ['/isolated/adb', '-s'], args
        serial = args[2]
        assert serial == 'emulator-5554', args
        assert 0 < kwargs['timeout'] <= 120.0
        if args[3:] == ['emu', 'avd', 'name']:
            identity_queries[0] += 1
            mismatch = case == 'wrong-identity' or (case == 'identity-changed-after-sync' and identity_queries[0] > 1)
            value = 'aiphone_vm_other' if mismatch else ns['_safe_avd_name'](vm_id)
            calls.append({'kind': 'identity', 'value': value})
            return SimpleNamespace(returncode=0, stdout=value + '\n', stderr='')
        if args[3] == 'shell':
            assert len(args) == 5
            expected_command = "/system/bin/sync && printf '%s" + chr(92) + "n' '" + marker + "'"
            assert args[4] == expected_command, (args[4], expected_command)
            assert re.fullmatch('[0-9a-f]{32}', marker), (marker, len(marker))
            assert vm_id not in args[4] if case == 'no-untrusted-text-in-shell' else True
            assert request_id not in args[4]
            calls.append({'kind': 'sync', 'command': args[4]})
            if case == 'sync-timeout':
                raise subprocess.TimeoutExpired('fake-adb', kwargs['timeout'])
            if case == 'late-marker':
                clock[0] = 121.0
            text = marker + '\n'
            if case in {'rc0-without-marker', 'stop-all-preserves-on-sync-failure'}:
                text = ''
            if case == 'stale-marker':
                text = 'f' * 32 + '\n'
            if case == 'extra-stdout':
                text += 'unexpected\n'
            return SimpleNamespace(returncode=1 if case == 'nonzero-with-marker' else 0, stdout=text, stderr='')
        if args[3:] == ['emu', 'kill']:
            calls.append({'kind': 'emu-kill', 'serial': serial})
            if case == 'native-request-failed':
                return SimpleNamespace(returncode=1, stdout='', stderr='not confirmed')
            for process in processes:
                process.code = 0
            return SimpleNamespace(returncode=0, stdout='OK', stderr='')
        raise AssertionError(args)

    ns = dict(math=math, re=re, subprocess=SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
              psutil=psutil, threading=threading, contextmanager=contextmanager, Any=Any, Dict=Dict, List=List, Optional=Optional,
              AndroidVmTools=object, VmRuntime=object,
              time=SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda x: clock.__setitem__(0, clock[0] + x)),
              uuid=SimpleNamespace(uuid4=lambda: marker_calls.append(True) or SimpleNamespace(hex=marker)),
              _CLEANUP_TIMEOUT_SEC=120.0, _NATIVE_GRACE_TIMEOUT_SEC=20.0,
              _NATIVE_GRACE_ENV='ANDROID_EMULATOR_WAIT_TIME_BEFORE_KILL', _STOP_VERIFY_TIMEOUT_SEC=5.0,
              _AVD_NAME_PREFIX='aiphone_vm_', _is_port_free=lambda _: True,
              _find_avd_processes=lambda _: ([(p, p.port) for p in processes if p.code is None], True),
              find_android_tools=lambda: (SimpleNamespace(adb='/isolated/adb'), []),
              logger=SimpleNamespace(warning=lambda *_a, **_k: calls.append({'kind': 'diagnostic-error', 'message': repr(_a)})))
    exec(compiled, ns)
    manager = ns['Manager']()
    runtime = SimpleNamespace(vm_id=vm_id, adb_serial='emulator-5554', port=5554,
                              process=None if case == 'reclaimed-process' else processes[0],
                              avd_name=ns['_safe_avd_name'](vm_id), observed_request_id='',
                              transport_unavailable=False, exit_evidence=None, log_file=None)
    manager._runtimes = {vm_id: runtime}
    manager._created_vm_ids = {vm_id}
    manager._last_reclaimed_ids = set()
    manager._avd_inventory_proofs = {}
    manager._vm_lock = lambda _: SimpleNamespace(acquire=lambda **_k: True, release=lambda: None)
    manager._delete_avd = lambda *_a, **_k: calls.append({'kind': 'delete-avd'}) or True
    if case == 'explicit-delete-skips-sync':
        result = manager.delete_sync(vm_id, deadline=120.0)
    elif case == 'stop-all-preserves-on-sync-failure':
        result = manager.stop_all()
    else:
        result = manager.stop_sync(vm_id, request_id=request_id, deadline=120.0)
    kinds = [x['kind'] for x in calls]
    assert 'terminate' not in kinds and 'kill' not in kinds, (case, calls)
    if case == 'explicit-delete-skips-sync':
        assert result['ok'] and kinds.count('emu-kill') == 1 and kinds.count('delete-avd') == 1 and 'sync' not in kinds
    elif case in {'complete-marker', 'no-untrusted-text-in-shell', 'reclaimed-process'}:
        assert result['ok'] and result['details']['sync_completed'] is True, (case, result, calls)
        assert kinds.count('sync') == kinds.count('emu-kill') == 1
        assert kinds.index('sync') < kinds.index('emu-kill')
    elif case == 'already-exited':
        assert result['ok'] and not calls
    else:
        assert result == 0 if isinstance(result, int) else result['ok'] is False
        assert manager._runtimes[vm_id] is runtime and vm_id in manager._created_vm_ids
        assert 'emu-kill' not in kinds if case != 'native-request-failed' else kinds.count('emu-kill') == 1
        assert 'delete-avd' not in kinds
    if case in {'ambiguous-second-serial', 'unknown-second-process-port', 'wrong-identity'}:
        assert 'sync' not in kinds
    assert len(marker_calls) <= 1
    return {'case': case, 'result': result, 'calls': calls, 'clock_after': clock[0]}


names = ['complete-marker', 'rc0-without-marker', 'stale-marker', 'nonzero-with-marker', 'extra-stdout',
         'sync-timeout', 'late-marker', 'wrong-identity', 'identity-changed-after-sync', 'native-request-failed',
         'ambiguous-second-serial', 'unknown-second-process-port', 'already-exited', 'reclaimed-process',
         'explicit-delete-skips-sync', 'stop-all-preserves-on-sync-failure', 'no-untrusted-text-in-shell']
results = [run_case(name) for name in names]
evidence = {'source_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'cases': results, 'passed': len(results),
            'scope': 'Unchanged production manager methods/helper AST; fake time/process/ADB only; no native commands, network or product modification'}
output = Path('/tmp/android-sync-independent-replay.json')
output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + '\n')
print(json.dumps({'passed': len(results), 'source_sha256': evidence['source_sha256'], 'evidence': str(output)}))
