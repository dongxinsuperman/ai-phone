"""Offline a6 scope/semantic guards. No product imports, sockets, ADB or VMs."""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
M = runpy.run_path(str(HERE / "android_snapshot_a6.py"))
VM = "012345abcdef"
BOOT = "11111111-2222-3333-4444-555555555555"
NEW_BOOT = "66666666-7777-8888-9999-aaaaaaaaaaaa"
MARKER = {"pid": 812, "present": True, "state": "S", "starttick": 2345,
          "cmdline_hex": b"/system/bin/sleep\0" + b"1131\0"}
MARKER["cmdline_hex"] = MARKER["cmdline_hex"].hex()


def stat_bytes(pid=812, tick=2345, state="S", cmdline=b"/system/bin/sleep\0" + b"1131\0"):
    fields = [state] + ["0"] * 18 + [str(tick)] + ["0"] * 10
    return (f"PRESENT\n{pid} (sleep) " + " ".join(fields) + "\nCMDLINE\n").encode() + cmdline


def instance(policy="save", state="stopped"):
    return {"id": VM, "state": state, "config_json": {"storage": {"snapshot_policy": policy, "wipe_data": False}}}


def failed_instance():
    return {"id": VM, "state": "error",
            "error_message": "无法核实已有虚拟机的镜像，未修改或启动：实际镜像 " + M["IMAGE_33"] + " 与请求 " + M["IMAGE_34"] + " 不一致",
            "runtime": {"operation": {"action": "start", "request_id": "this-operation", "outcome": "failed", "pending": False}}}


class ScopeTests(unittest.TestCase):
    def test_one_device_only_on_explicit_start_with_private_port(self):
        environment = {"ADB_SERVER_SOCKET": "tcp:127.0.0.1:15037", "ANDROID_ADB_SERVER_HOST": "127.0.0.1",
                       "ANDROID_ADB_SERVER_PORT": "15037", "ADB_SERVER_PORT": "15037"}
        run_id = "android-snapshot-a6-offline-argv-guards"
        build = lambda args: M["private_adb_argv"](args, run_id=run_id, environment=environment)
        prefix = [str(M["SDK"] / "platform-tools/adb"), "-P", "15037"]
        self.assertEqual(build(["start-server"]), [*prefix, "--one-device", "personal-a6-no-usb-" + run_id, "start-server"])
        for args in (["devices"], ["kill-server"], ["-s", "emulator-5666", "emu", "avd", "name"],
                     ["-s", "emulator-5666", "shell", "cat /proc/sys/kernel/random/boot_id"]):
            with self.subTest(args=args):
                argv = build(args)
                self.assertEqual(argv, prefix + args)
                self.assertNotIn("--one-device", argv)
                self.assertNotIn("5037", argv)

    def test_client_cannot_override_private_daemon_or_target(self):
        environment = {"ADB_SERVER_SOCKET": "tcp:127.0.0.1:15037", "ANDROID_ADB_SERVER_HOST": "127.0.0.1",
                       "ANDROID_ADB_SERVER_PORT": "15037", "ADB_SERVER_PORT": "15037"}
        run_id = "android-snapshot-a6-offline-argv-guards"
        for args in (["-P", "5037", "devices"], ["-s", "emulator-5554", "shell", "id"],
                     ["--one-device", "wrong", "devices"], ["kill-server", "extra"], ["start-server", "extra"], []):
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                M["private_adb_argv"](args, run_id=run_id, environment=environment)
        for key in environment:
            for bad in ("", "5037", "remote.invalid"):
                with self.subTest(key=key, bad=bad), self.assertRaises(RuntimeError):
                    M["private_adb_argv"](["kill-server"], run_id=run_id, environment={**environment, key: bad})
        with self.assertRaises(RuntimeError):
            M["private_adb_argv"](["devices"], run_id=run_id, environment={})

    def test_import_and_plan_are_read_only_without_product_or_native(self):
        self.assertFalse(any(name == "ai_phone" or name.startswith("ai_phone.") for name in sys.modules))
        run_id = "android-snapshot-a6-offline-only-guards"
        root = M["DEFAULT_PARENT"] / run_id
        self.assertFalse(root.exists())
        with patch("subprocess.run", side_effect=AssertionError("native command")), \
                patch("subprocess.Popen", side_effect=AssertionError("native process")), \
                patch("socket.socket", side_effect=AssertionError("socket")):
            value = M["plan"](run_id)
        self.assertEqual(value["mode"], "dry-run")
        self.assertFalse(root.exists())

    def test_base_a5_source_pin_and_no_source_edit(self):
        self.assertEqual(M["B"]["digest"](M["BASE_PATH"]), M["BASE_SHA256"])
        self.assertEqual(M["BASE_SHA256"], "43356f481dea68c8adcd7b5fb7344b08912f2b2e4f678075533f2a15856eaf51")

    def test_own_path_and_vm_id(self):
        with tempfile.TemporaryDirectory(prefix="personal-a6-guard-") as tmp:
            root = Path(tmp)
            self.assertEqual(M["checked_avd"](root, VM), root / "android/avd/aiphone_vm_012345abcdef.avd")
            for value in ("", "../foreign", "ABCDEF012345", "0" * 13):
                with self.subTest(value=value), self.assertRaises(RuntimeError):
                    M["checked_avd"](root, value)

    def test_symlink_parent_or_avd_rejected(self):
        with tempfile.TemporaryDirectory(prefix="personal-a6-guard-") as tmp:
            root = Path(tmp)
            (root / "android").symlink_to(root / "other")
            with self.assertRaises(RuntimeError):
                M["checked_avd"](root, VM)

    def test_launch_policy_exact_flags(self):
        for policy, flags in (("save", []), ("discard_changes", ["-no-snapshot-save"]), ("cold_boot", ["-no-snapshot-load"])):
            M["check_launch"]([str(M["SDK"] / "emulator/emulator"), "-avd", "aiphone_vm_" + VM, "-port", "5666", *flags], vm_id=VM, policy=policy)

    def test_launch_wipe_wrong_avd_port_or_policy_rejected(self):
        original = [str(M["SDK"] / "emulator/emulator"), "-avd", "aiphone_vm_" + VM, "-port", "5666"]
        for argv in (original + ["-wipe-data"], original + ["-no-snapshot"], original + ["-no-snapshot-load"],
                     ["/other/emulator", *original[1:]], [*original[:-1], "5554"],
                     [*original[:2], "aiphone_vm_other", *original[3:]], original + ["-avd", "other"]):
            with self.subTest(argv=argv), self.assertRaises(RuntimeError):
                M["check_launch"](argv, vm_id=VM, policy="save")

    def test_policy_patch_requires_stopped_and_exact_storage(self):
        M["check_policy"](instance(), VM, "save")
        for value in (instance(state="running"), {**instance(), "id": "other"}, instance(policy="cold_boot"),
                      {**instance(), "config_json": {"storage": {"snapshot_policy": "save", "wipe_data": True}}}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                M["check_policy"](value, VM, "save")

    def test_only_predefined_bounded_markers(self):
        for label, duration in M["MARKER_SECONDS"].items():
            cmd = M["marker_launch"](label)
            self.assertIn(f"/system/bin/sleep {duration}", cmd)
            self.assertGreater(duration, M["MAX_RUN_SECONDS"])
            self.assertIn("trap '' HUP", cmd)
            self.assertNotIn("sync", cmd)
        for invalid in ("C", "A; reboot", "", None):
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                M["marker_launch"](invalid)

    def test_marker_pid_validation_prevents_shell_injection(self):
        for pid in (0, 1, -1, "812; reboot", 4194304, True):
            with self.subTest(pid=pid), self.assertRaises(RuntimeError):
                M["marker_read_command"](pid)


class RamProofTests(unittest.TestCase):
    def test_exact_proc_signature_parsed(self):
        value = M["parse_marker"](stat_bytes(), 812)
        for key in MARKER:
            self.assertEqual(value[key], MARKER[key])
        self.assertTrue(M["same_marker"](value, MARKER))

    def test_absence_is_only_the_exact_success_sentinel(self):
        self.assertEqual(M["parse_marker"](b"ABSENT\n", 812), {"pid": 812, "present": False})
        for raw in (b"", b"Permission denied\n", b"ABSENT", b"ABSENT\nerror\n", b"PRESENT\n", b"PRESENT\n812 (sleep) S\nCMDLINE\nx\0"):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                M["parse_marker"](raw, 812)

    def test_pid_reuse_is_not_original_marker(self):
        for changed in ({**MARKER, "starttick": 2346}, {**MARKER, "cmdline_hex": b"other\0".hex()},
                        {**MARKER, "pid": 813}, {**MARKER, "state": "Z"}, {"pid": 812, "present": False}):
            self.assertFalse(M["same_marker"](changed, MARKER))

    def test_save_restore_requires_boot_and_marker_identity(self):
        M["check_ram"](BOOT, BOOT, {"A": MARKER}, {"A": MARKER})
        for boot, actual in ((NEW_BOOT, MARKER), (BOOT, {"pid": 812, "present": False}), (BOOT, {**MARKER, "starttick": 5})):
            with self.subTest(boot=boot, actual=actual), self.assertRaises(RuntimeError):
                M["check_ram"](boot, BOOT, {"A": actual}, {"A": MARKER})

    def test_discard_requires_A_present_and_B_signature_absent(self):
        expected = {"A": MARKER, "B": {**MARKER, "pid": 913}}
        M["check_ram"](BOOT, BOOT, {"A": MARKER, "B": {"pid": 913, "present": False}}, expected, require_b_absent=True)
        with self.assertRaises(RuntimeError):
            M["check_ram"](BOOT, BOOT, expected, expected, require_b_absent=True)

    def test_cold_requires_new_boot_and_no_original_signatures(self):
        expected = {"A": MARKER, "B": {**MARKER, "pid": 913}}
        absent = {"A": {"pid": 812, "present": False}, "B": {"pid": 913, "present": False}}
        M["check_ram"](NEW_BOOT, BOOT, absent, expected, cold=True)
        with self.assertRaises(RuntimeError):
            M["check_ram"](BOOT, BOOT, absent, expected, cold=True)
        with self.assertRaises(RuntimeError):
            M["check_ram"](NEW_BOOT, BOOT, {**absent, "A": MARKER}, expected, cold=True)


class SnapshotAndNegativeTests(unittest.TestCase):
    def test_empty_avd_cannot_count_as_saved_snapshot(self):
        for value in ({"exists": False, "files": []}, {"exists": True, "files": []},
                      {"exists": True, "files": [{"name": "snapshot.pb", "size": 1}]}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                M["require_real_snapshot"](value)
        M["require_real_snapshot"]({"exists": True, "files": [{"name": "snapshot.pb", "size": 1}, {"name": "ram.bin", "size": 1}]})

    def test_snapshot_metadata_requires_owned_no_symlink_path(self):
        with tempfile.TemporaryDirectory(prefix="personal-a6-guard-") as tmp:
            avd = Path(tmp) / "own.avd"
            target = avd / "snapshots/default_boot"
            target.mkdir(parents=True)
            (target / "snapshot.pb").write_bytes(b"metadata")
            (target / "ram.bin").write_bytes(b"ram")
            value = M["snapshot_manifest"](avd)
            M["require_real_snapshot"](value)
            self.assertTrue(all(row.get("sha256") for row in value["files"]))
            (target / "foreign").symlink_to(Path(tmp) / "other")
            with self.assertRaises(RuntimeError):
                M["snapshot_manifest"](avd)

    def test_negative_requires_failed_exact_operation_and_actual_identity_error(self):
        M["image_failure_ready"](failed_instance(), vm_id=VM, request_id="this-operation")
        value = failed_instance()
        value["runtime"]["operation"].update(outcome="pending", pending=True)
        self.assertFalse(M["image_failure_ready"](value, vm_id=VM, request_id="this-operation"))

    def test_missing_sdk_or_generic_failure_not_image_guard_proof(self):
        for reason in ("missing system image", "Agent offline", "timeout", "未修改或启动但原因未知"):
            value = failed_instance(); value["error_message"] = reason
            with self.subTest(reason=reason), self.assertRaises(RuntimeError):
                M["image_failure_ready"](value, vm_id=VM, request_id="this-operation")

    def test_success_timeout_other_id_or_missing_operation_rejected(self):
        cases = []
        for field, value in (("outcome", "succeeded"), ("outcome", "timeout"), ("request_id", "other"), ("action", "stop"), ("pending", True)):
            row = failed_instance(); row["runtime"]["operation"][field] = value; cases.append(row)
        cases += [{**failed_instance(), "state": "stopped"}, {**failed_instance(), "id": "other"}, {**failed_instance(), "runtime": {}}]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                M["image_failure_ready"](row, vm_id=VM, request_id="this-operation")

    def test_full_owned_data_bytes_and_metadata_fingerprint(self):
        with tempfile.TemporaryDirectory(prefix="personal-a6-guard-") as tmp:
            avd = Path(tmp) / "own.avd"; avd.mkdir()
            (avd / "config.ini").write_text("image.sysdir.1=system-images/android-33\n")
            (avd / "userdata-qemu.img.qcow2").write_bytes(b"owned data")
            avd.with_suffix(".ini").write_text("path=" + str(avd))
            first = M["avd_content_fingerprint"](avd)
            self.assertEqual(first, M["avd_content_fingerprint"](avd))
            (avd / "userdata-qemu.img.qcow2").write_bytes(b"changed data")
            self.assertNotEqual(first, M["avd_content_fingerprint"](avd))

    def test_fingerprint_rejects_symlink_and_missing_userdata(self):
        with tempfile.TemporaryDirectory(prefix="personal-a6-guard-") as tmp:
            avd = Path(tmp) / "own.avd"; avd.mkdir()
            (avd / "config.ini").write_text("owned")
            with self.assertRaises(RuntimeError):
                M["avd_content_fingerprint"](avd)
            (avd / "userdata-qemu.img").symlink_to(Path(tmp) / "foreign")
            with self.assertRaises(RuntimeError):
                M["avd_content_fingerprint"](avd)

    def test_api34_availability_not_guessed_from_directory(self):
        with tempfile.TemporaryDirectory(prefix="personal-a6-sdk-") as tmp:
            function = M["installed_image"]
            saved = function.__globals__["SDK"]
            function.__globals__["SDK"] = Path(tmp)
            try:
                image = Path(tmp).joinpath(*M["IMAGE_34"].split(";")); image.mkdir(parents=True)
                self.assertFalse(function(M["IMAGE_34"])["installed"])
                (image / "source.properties").write_text("AndroidVersion.ApiLevel=34\n")
                (image / "package.xml").write_text('<repository><localPackage path="' + M["IMAGE_33"] + '"/></repository>')
                self.assertFalse(function(M["IMAGE_34"])["installed"])
                (image / "package.xml").write_text('<repository><localPackage path="' + M["IMAGE_34"] + '"/></repository>')
                self.assertTrue(function(M["IMAGE_34"])["installed"])
            finally:
                function.__globals__["SDK"] = saved


class ContractAndCleanupTests(unittest.TestCase):
    def test_exact_operation_helper_reused_not_state_only(self):
        value = {"id": VM, "state": "running", "runtime": {"operation": {"action": "start", "request_id": "own", "outcome": "failed", "pending": False}}}
        with self.assertRaises(RuntimeError):
            M["B"]["operation_snapshot_ready"](value, vm_id=VM, action="start", request_id="own", desired_state="running")

    def test_failed_current_generation_stop_cannot_be_cleanup_retried(self):
        rows = [{"vm_id": VM, "generation_id": "pid:1", "outcome": "failed"}]
        self.assertFalse(M["B"]["cleanup_stop_decision"](VM, "pid:1", rows)["allowed"])
        self.assertTrue(M["B"]["cleanup_stop_decision"](VM, "pid:2", rows)["allowed"])

    def test_actual_http_operation_wrapper_rejects_late_facts_and_other_attempts(self):
        tree = ast.parse((HERE / "android_snapshot_a6.py").read_text())
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "operation")
        for name, endpoint, response_outcome, read_outcome, changed_id, missing, succeeds in (
            ("stop-failed-with-late-stopped", "stop", "failed", "failed", False, False, False),
            ("stop-pending-then-failed", "stop", "pending", "failed", False, False, False),
            ("stop-timeout-with-late-stopped", "stop", "pending", "timeout", False, False, False),
            ("stop-replaced-operation", "stop", "pending", "succeeded", True, False, False),
            ("stop-missing-operation", "stop", "pending", "succeeded", False, True, False),
            ("stop-success", "stop", "pending", "succeeded", False, False, True),
            ("dispatch-failed-with-late-running", "dispatch", "failed", "failed", False, False, False),
            ("start-failed-with-late-running", "start", "pending", "failed", False, False, False),
        ):
            with self.subTest(case=name):
                action = "stop" if endpoint == "stop" else "start"
                state = "stopped" if action == "stop" else "running"
                def make(outcome, request="own-request"):
                    return {"id": VM, "state": state, "runtime": {"operation": {"action": action, "request_id": request,
                            "outcome": outcome, "pending": outcome == "pending"}}}
                initial = make(response_outcome)
                if missing:
                    initial["runtime"] = {}
                terminal = make(read_outcome, "another-request" if changed_id else "own-request")
                async def api(method, path, **kwargs):
                    return {"sent": True, "instance": initial} if method == "POST" else terminal
                async def eventually(read, predicate, **kwargs):
                    value = await read()
                    if not predicate(value):
                        raise TimeoutError("bounded fake deadline")
                    return value
                attempts = []
                helpers = {**M["B"], "eventually": eventually}
                scope = {"B": helpers, "api": api, "prefix": "/own", "generation": lambda _: "pid:1", "attempts": attempts,
                         "time": types.SimpleNamespace(monotonic=lambda: 100.0, monotonic_ns=lambda: 100000),
                         "start_time": 100.0, "MAX_RUN_SECONDS": 900,
                         "evidence": types.SimpleNamespace(value={"vm_readbacks": []}, flush=lambda: None)}
                exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "actual-a6-operation", "exec"), scope)
                if succeeds:
                    result = asyncio.run(scope["operation"](VM, endpoint))
                    self.assertEqual(result, terminal)
                    self.assertEqual(attempts[-1]["outcome"], "succeeded")
                else:
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        asyncio.run(scope["operation"](VM, endpoint))
                    self.assertEqual(attempts[-1]["outcome"], "failed")
                if action == "stop":
                    self.assertEqual(M["B"]["cleanup_stop_decision"](VM, "pid:1", attempts)["allowed"], succeeds)

    def test_process_observer_transparently_forwards_deadlines_and_exceptions(self):
        tree = ast.parse((HERE / "android_snapshot_a6.py").read_text())
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "observed_stopper")
        calls, native_calls = [], []
        class Process:
            pid = 123
            def wait(self, timeout):
                calls.append(("wait", timeout)); return 0
            def poll(self): return None
            def terminate(self): calls.append(("terminate",))
        process = Process()
        def original(proxy, *, graceful, **kwargs):
            calls.append(("original", graceful, kwargs))
            return proxy.wait(timeout=kwargs["grace_deadline"])
        scope = {"original_stopper": original, "native_calls": native_calls, "time": M["time"]}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "observer-only", "exec"), scope)
        self.assertEqual(scope["observed_stopper"](process, graceful=True, deadline=123.0, grace_deadline=2.0), 0)
        self.assertEqual(calls, [("original", True, {"deadline": 123.0, "grace_deadline": 2.0}), ("wait", 2.0)])
        self.assertEqual(native_calls[0]["kwargs"], {"deadline": 123.0, "grace_deadline": 2.0})
        error = RuntimeError("unchanged exception")
        def fails(*args, **kwargs): raise error
        scope["original_stopper"] = fails
        with self.assertRaises(RuntimeError) as caught:
            scope["observed_stopper"](process, graceful=False, deadline=0.0, grace_deadline=1.0)
        self.assertIs(caught.exception, error)

    def test_all_fourteen_real_product_helper_paths_match_observer(self):
        reference = runpy.run_path(str(HERE / "test_android_a5_stop_observer_equivalence.py"))
        tree = ast.parse(reference["MANAGER"].read_text())
        constant = next(node for node in tree.body if isinstance(node, ast.Assign) and
                        any(isinstance(target, ast.Name) and target.id == "_CLEANUP_TIMEOUT_SEC" for target in node.targets))
        production = ast.Module(body=[constant, reference["function"](tree, "_startup_timeout"),
                                     reference["function"](tree, "_stop_owned_process")], type_ignores=[])
        observer = next(node for node in ast.walk(ast.parse((HERE / "android_snapshot_a6.py").read_text()))
                        if isinstance(node, ast.FunctionDef) and node.name == "observed_stopper")
        for scenario in reference["SCENARIOS"]:
            with self.subTest(case=scenario["name"]):
                expected, _ = reference["run"](scenario, False)
                clock = reference["Clock"](scenario.get("now", 100.0))
                scope = {"time": clock, "subprocess": reference["subprocess"], "psutil": reference["psutil"],
                         "Any": reference["Any"], "Optional": reference["Optional"], "native_calls": []}
                exec(compile(ast.fix_missing_locations(production), "actual-production-helper", "exec"), scope)
                scope["original_stopper"] = scope["_stop_owned_process"]
                exec(compile(ast.fix_missing_locations(ast.Module(body=[observer], type_ignores=[])), "actual-a6-observer", "exec"), scope)
                inner = reference["FakeProcess"](clock, scenario)
                process = reference["AdoptedProcess"](inner) if scenario.get("adopted") else inner
                kwargs = copy.deepcopy(scenario["kwargs"])
                try:
                    value = scope["observed_stopper"](process, graceful=scenario["graceful"], **kwargs)
                    outcome = {"kind": "returned", "value": value}
                except BaseException as exc:
                    outcome = {"kind": "raised", "type": type(exc).__name__, "message": str(exc)}
                actual = {"outcome": outcome, "clock_after": clock.value, "calls": inner.calls,
                          "signals": [call["method"] for call in inner.calls if call["method"] in ("terminate", "kill")]}
                self.assertEqual(actual, expected)
                self.assertEqual(kwargs, scenario["kwargs"])
                self.assertEqual(scope["native_calls"][0]["kwargs"], scenario["kwargs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
