"""Prepare-only tests: no backend import, network, APK build or native command."""
from __future__ import annotations

import runpy
import copy
import json
import threading
import time
import types
from unittest.mock import patch
from io import BytesIO
import xml.etree.ElementTree as ET
import ast
import subprocess
import asyncio
import sys
import unittest
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).with_name("android_app_install_sync_a5.py")))
CHECK = MODULE["validate_install_message"]
CHECK_TERMINAL = MODULE["validate_terminal_install_task"]
CHECK_ABSENT = MODULE["validate_fixture_package_absent"]
BASE = "http://127.0.0.1:18765"
PATH = "/api/app-install/packages/personal-package/download"
ARGS = dict(serial="emulator-5666", package_id="personal-package", filename="personal-lifecycle.apk", server_base=BASE)
MESSAGE = {"task_id": "own-task", "item_id": "own-item", "serial": "emulator-5666", "platform": "android",
           "filename": "personal-lifecycle.apk", "package_url": PATH, "timeout_sec": 600}
TASK_ARGS = {"task_id": "own-task", "item_id": "own-item", "package_id": "personal-package", "serial": "emulator-5666"}
TASK = {"id": "own-task", "package_id": "personal-package", "state": "done", "finished_at": "2026-10-16T00:00:00Z",
        "summary": {"total": 1, "pending": 0, "running": 0, "success": 1, "failed": 0, "timeout": 0, "unknown": 0},
        "items": [{"id": "own-item", "task_id": "own-task", "serial": "emulator-5666", "platform": "android",
                   "state": "success", "finished_at": "2026-10-16T00:00:00Z"}]}


class InstallScopeTests(unittest.TestCase):
    def test_exact_relative_or_absolute_package_only(self):
        for url in (PATH, PATH.lstrip("/"), BASE + PATH):
            msg = {**MESSAGE, "package_url": url}
            self.assertEqual(CHECK(msg, **ARGS), BASE + PATH)

    def test_reject_external_or_other_loopback_origin(self):
        for url in ("https://example.invalid/package.apk", "//example.invalid" + PATH,
                    "http://127.0.0.1:18766" + PATH, "https://127.0.0.1:18765" + PATH):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                CHECK({**MESSAGE, "package_url": url}, **ARGS)

    def test_reject_unknown_package_and_url_extras(self):
        for url in (PATH.replace("personal-package", "other-package"), PATH + "?key=irrelevant", PATH + "#fragment",
                    "http://user:unused@127.0.0.1:18765" + PATH):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                CHECK({**MESSAGE, "package_url": url}, **ARGS)

    def test_reject_other_device_platform_or_filename(self):
        for key, value in (("serial", "emulator-5554"), ("platform", "ios"), ("filename", "other.apk")):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                CHECK({**MESSAGE, key: value}, **ARGS)

    def test_reject_missing_or_path_like_task_identity(self):
        for key, value in (("task_id", ""), ("item_id", "../../other")):
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                CHECK({**MESSAGE, key: value}, **ARGS)

    def test_reject_unbounded_native_timeout(self):
        for value in (None, "600", 10, 601):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                CHECK({**MESSAGE, "timeout_sec": value}, **ARGS)

    def test_accept_only_one_owned_terminal_success(self):
        CHECK_TERMINAL(TASK, **TASK_ARGS)

    def test_done_task_with_failed_or_inconsistent_summary_is_not_success(self):
        cases = [
            {**TASK, "items": [{**TASK["items"][0], "state": "failed"}]},
            {**TASK, "summary": {**TASK["summary"], "failed": 1}},
            {**TASK, "summary": None},
            {**TASK, "finished_at": None},
            {**TASK, "items": [{**TASK["items"][0], "finished_at": None}]},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                CHECK_TERMINAL(value, **TASK_ARGS)

    def test_terminal_success_for_other_task_or_device_is_rejected(self):
        cases = [
            {**TASK, "id": "other-task"}, {**TASK, "package_id": "other-package"},
            {**TASK, "items": [*TASK["items"], *TASK["items"]]},
            *[{**TASK, "items": [{**TASK["items"][0], key: value}]} for key, value in
              (("id", "other-item"), ("task_id", "other-task"), ("serial", "emulator-5554"), ("platform", "ios"))],
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                CHECK_TERMINAL(value, **TASK_ARGS)

    def test_absence_requires_successful_empty_package_list(self):
        self.assertTrue(CHECK_ABSENT(0, "", "")["absent"])

    def test_existing_exact_package_is_not_absent(self):
        with self.assertRaises(RuntimeError):
            CHECK_ABSENT(0, "package:org.personal.aiphone.lifecycle\n", "")

    def test_package_prefix_is_not_exact_identity(self):
        result = CHECK_ABSENT(0, "package:org.personal.aiphone.lifecycle.other\n", "")
        self.assertTrue(result["absent"])

    def test_nonzero_probe_is_not_converted_to_empty_success(self):
        for code in (1, 2, -9):
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                CHECK_ABSENT(code, "", "")

    def test_unknown_probe_output_or_stderr_is_rejected(self):
        for stdout, stderr in (("Error: package service unavailable", ""), ("package:", ""),
                               ("", "device offline"), ("Success", "")):
            with self.subTest(stdout=stdout, stderr=stderr), self.assertRaises(RuntimeError):
                CHECK_ABSENT(0, stdout, stderr)

    def test_import_and_plan_do_not_import_product_or_create_run_root(self):
        self.assertFalse(any(name == "ai_phone" or name.startswith("ai_phone.") for name in sys.modules))
        value = MODULE["plan"]("android-app-install-prepare-only")
        self.assertEqual(value["mode"], "dry-run")
        self.assertEqual(value["frozen_products"]["count"], 33)
        self.assertFalse(value["frozen_products"]["mismatches"])
        self.assertFalse(Path(value["paths"]["root"]).exists())


class DiagnosticProofTests(unittest.TestCase):
    def setUp(self):
        self.args = {"witness_name": "personal-witness-android-prepare-test.txt", "witness_sha256": "a" * 64,
                     "witness_bytes": 101, "fixture_url": "http://127.0.0.1:19080/fixture/?run=android-prepare-test&platform=android",
                     "storage_key": "personal-vm-e2e:android-prepare-test", "token": "personal-test-token"}
        saved = {"token": self.args["token"], "count": 1}
        self.records = []
        for phase in ("first-write", "restart-read"):
            writing = phase == "first-write"
            witness = {"ok": True, "mode": phase, "sha256": self.args["witness_sha256"], "bytes": 101,
                       "path": "/data/user/0/org.personal.aiphone.lifecycle/" + self.args["witness_name"],
                       "parent": "/data/user/0/org.personal.aiphone.lifecycle", "existedBefore": not writing,
                       **{key: writing for key in ("created", "openedForWrite", "fileFsync", "parentFsync")}}
            storage = {"probeOk": True, "href": self.args["fixture_url"], "origin": "http://127.0.0.1:19080",
                       "storageKey": self.args["storage_key"], "rawValue": json.dumps(saved), "parsedValue": saved,
                       "missing": False, "readError": None, "parseError": None, "trigger": "receipt-request"}
            provider = {"ok": True, "packageName": "personal.test.webview", "versionName": "test-1", "longVersionCode": 1}
            self.records.extend({"phase": phase, "kind": kind, "data": data} for kind, data in
                                (("witness", witness), ("storage", storage), ("webview-provider", provider)))

    def summary(self, records=None):
        return MODULE["diagnostic_summary"](self.records if records is None else records, **self.args)

    def data(self, phase, kind):
        return next(row["data"] for row in self.records if row["phase"] == phase and row["kind"] == kind)

    def test_both_tracks_require_their_own_evidence(self):
        result = self.summary()
        for field in ("evidence_complete", "durable_witness_preserved", "raw_storage_preserved",
                      "actual_webview_versions_observed", "webview_provider_same"):
            self.assertTrue(result[field], field)

    def test_missing_observation_is_incomplete_not_proven_storage_loss(self):
        result = self.summary([])
        self.assertFalse(result["evidence_complete"])
        self.assertIn("never inferred", result["missing_evidence_means"])
        self.assertIsNone(result["webview_provider_same"])

    def test_each_required_fsync_must_succeed(self):
        for field in ("fileFsync", "parentFsync"):
            records = copy.deepcopy(self.records)
            next(row["data"] for row in records if row["phase"] == "first-write" and row["kind"] == "witness")[field] = False
            result = self.summary(records)
            self.assertFalse(result["durable_witness_preserved"])
            self.assertTrue(result["raw_storage_preserved"])

    def test_existing_first_file_or_restart_repair_cannot_pass(self):
        for phase, field, value in (("first-write", "existedBefore", True), ("restart-read", "openedForWrite", True),
                                    ("restart-read", "created", True), ("restart-read", "existedBefore", False)):
            records = copy.deepcopy(self.records)
            next(row["data"] for row in records if row["phase"] == phase and row["kind"] == "witness")[field] = value
            self.assertFalse(self.summary(records)["durable_witness_preserved"])

    def test_wrong_witness_bytes_or_path_cannot_pass(self):
        for field, value in (("sha256", "b" * 64), ("bytes", 0), ("parent", "/sdcard"), ("path", "/data/user/0/org.personal.aiphone.lifecycle/other.txt")):
            records = copy.deepcopy(self.records)
            next(row["data"] for row in records if row["phase"] == "restart-read" and row["kind"] == "witness")[field] = value
            self.assertFalse(self.summary(records)["durable_witness_preserved"])

    def test_explicit_read_exception_stays_distinct_from_missing_key(self):
        self.data("restart-read", "storage").update(readError="SecurityError", missing=None, rawValue=None, parsedValue=None)
        result = self.summary()
        self.assertTrue(result["evidence_complete"])
        self.assertTrue(result["durable_witness_preserved"])
        self.assertFalse(result["raw_storage_preserved"])
        observation = result["phases"]["restart-read"]["storage_observations"][0]
        self.assertEqual(observation["read_error"], "SecurityError")
        self.assertIsNone(observation["missing"])

    def test_missing_key_and_bad_json_are_reported_separately(self):
        self.data("restart-read", "storage").update(missing=True, rawValue=None, parsedValue=None)
        self.assertTrue(self.summary()["phases"]["restart-read"]["storage_observations"][0]["missing"])
        self.data("restart-read", "storage").update(missing=False, rawValue="{", parseError="SyntaxError", parsedValue=None)
        result = self.summary()
        self.assertFalse(result["raw_storage_preserved"])
        self.assertEqual(result["phases"]["restart-read"]["storage_observations"][0]["parse_error"], "SyntaxError")

    def test_parsed_object_alone_cannot_forge_success(self):
        self.data("restart-read", "storage")["rawValue"] = "{}"
        self.assertFalse(self.summary()["raw_storage_preserved"])

    def test_wrong_page_or_key_is_incomplete_diagnostic_identity(self):
        for field in ("href", "origin", "storageKey"):
            records = copy.deepcopy(self.records)
            next(row["data"] for row in records if row["phase"] == "restart-read" and row["kind"] == "storage")[field] = "other"
            result = self.summary(records)
            self.assertFalse(result["evidence_complete"])
            self.assertFalse(result["raw_storage_preserved"])

    def test_actual_provider_change_is_explicit(self):
        self.data("restart-read", "webview-provider").update(versionName="test-2", longVersionCode=2)
        result = self.summary()
        self.assertTrue(result["actual_webview_versions_observed"])
        self.assertFalse(result["webview_provider_same"])

    def test_original_html_bytes_are_unchanged(self):
        self.assertEqual(MODULE["digest"](MODULE["FIXTURE_HTML"]), MODULE["EXPECTED_FIXTURE_SHA256"])


class VisualGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = Path(__file__).parent
        cls.png = (folder / "a3-fixture-confirmed.png").read_bytes()
        cls.white_png = (folder / "a3-fixture-persisted.png").read_bytes()
        cls.xml = (folder / "a4-prepare/fixtures/a3-fixture-confirmed.xml").read_text()
        cls.empty_xml = (folder / "a4-prepare/fixtures/a3-fixture-persisted.xml").read_text()
        cls.token = "personal804c654f28f4"

    def inspect(self, text=None, png=None):
        return MODULE["inspect_visual_sample"](self.xml if text is None else text, self.png if png is None else png, self.token)

    def modified_xml(self, change):
        tree = ET.fromstring(self.xml[self.xml.index("<?xml"):])
        for node in tree.iter("node"):
            change(node)
        return '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(tree, encoding="unicode")

    def test_real_a3_confirmed_native_xml_and_frame_pass(self):
        value = self.inspect()
        self.assertTrue(value["ready"])
        self.assertTrue(value["native_saved_token"])
        self.assertTrue(value["native_count_one"])

    def test_actual_white_restart_frame_cannot_pass_even_with_correct_xml(self):
        self.assertFalse(self.inspect(png=self.white_png)["ready"])

    def test_drawn_frame_without_native_saved_text_is_not_enough(self):
        self.assertFalse(self.inspect(text=self.empty_xml)["ready"])

    def test_input_value_cannot_substitute_for_saved_result(self):
        text = self.modified_xml(lambda node: node.set("text", "") if node.get("text") == self.token and node.get("class") != "android.widget.EditText" else None)
        self.assertFalse(self.inspect(text=text)["ready"])

    def test_wrong_or_prefix_extended_saved_token_is_rejected(self):
        for value in ("another-token", self.token + "x"):
            text = self.modified_xml(lambda node: node.set("text", value) if node.get("text") == self.token and node.get("class") != "android.widget.EditText" else None)
            self.assertFalse(self.inspect(text=text)["ready"])

    def test_count_must_be_exactly_one(self):
        for value in ("0", "10", "11", "2"):
            text = self.modified_xml(lambda node: node.set("text", value) if node.get("text") == "1" else None)
            self.assertFalse(self.inspect(text=text)["ready"])

    def test_result_token_outside_result_region_is_rejected(self):
        text = self.modified_xml(lambda node: node.set("bounds", "[0,0][1,1]") if node.get("text") == self.token else None)
        self.assertFalse(self.inspect(text=text)["ready"])

    def test_single_color_pixels_are_not_visual_markers(self):
        from PIL import Image
        picture = Image.new("RGB", (540, 960), "white")
        picture.putpixel((50, 400), (21, 90, 182))
        picture.putpixel((50, 600), (10, 121, 91))
        output = BytesIO(); picture.save(output, format="PNG")
        self.assertFalse(self.inspect(png=output.getvalue())["ready"])

    def test_invalid_xml_or_png_fails_closed(self):
        self.assertFalse(self.inspect(text="broken XML")["ready"])
        self.assertFalse(self.inspect(png=b"not a PNG")["ready"])


class BoundedPrivateReadTests(unittest.TestCase):
    def make_driver(self):
        class Client:
            host = "127.0.0.1"
            port = 15037
            def make_connection(self, timeout=None):
                raise AssertionError("Original factory must not run auto-start recovery")
        client = Client()
        return types.SimpleNamespace(_device=types.SimpleNamespace(_client=client, serial="emulator-5666"))

    def make_socket(self):
        class Socket:
            def __init__(self): self.closed = threading.Event()
            def settimeout(self, value): self.timeout = value
            def shutdown(self, how): self.closed.set()
            def close(self): self.closed.set()
        return Socket()

    def test_success_returns_original_bytes_and_restores_private_factory(self):
        driver = self.make_driver(); client = driver._device._client; original = client.make_connection; connection = self.make_socket()
        calls = []
        def read():
            client.make_connection(timeout=600); calls.append("read"); return b"original native bytes"
        with patch.object(MODULE["socket"], "create_connection", return_value=connection) as dial, patch("subprocess.run", side_effect=AssertionError("no server start")):
            self.assertEqual(MODULE["bounded_private_observation"](driver, read, 1), b"original native bytes")
        self.assertEqual(dial.call_args.args[0], ("127.0.0.1", 15037))
        self.assertEqual(calls, ["read"])
        self.assertEqual(client.make_connection, original)
        self.assertTrue(connection.closed.is_set())

    def test_deadline_closes_only_owned_connection_and_waits_for_worker_exit(self):
        driver = self.make_driver(); client = driver._device._client; original = client.make_connection; connection = self.make_socket(); exited = []
        def read():
            client.make_connection(timeout=600)
            if not connection.closed.wait(1): raise AssertionError("deadline did not close private socket")
            exited.append(True); return b"late bytes"
        started = time.monotonic()
        with patch.object(MODULE["socket"], "create_connection", return_value=connection), self.assertRaises(TimeoutError):
            MODULE["bounded_private_observation"](driver, read, 0.03)
        self.assertEqual(exited, [True])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(client.make_connection, original)

    def test_connection_failure_is_not_retried_and_never_starts_adb(self):
        driver = self.make_driver(); client = driver._device._client; original = client.make_connection
        with patch.object(MODULE["socket"], "create_connection", side_effect=ConnectionRefusedError("absent")) as dial, \
                patch("subprocess.run", side_effect=AssertionError("no server start")), self.assertRaises(ConnectionRefusedError):
            MODULE["bounded_private_observation"](driver, lambda: client.make_connection(), 1)
        self.assertEqual(dial.call_count, 1)
        self.assertEqual(client.make_connection, original)

    def test_unknown_client_or_serial_is_rejected_before_connect(self):
        for field, value in (("host", "example.invalid"), ("port", 5037), ("serial", "emulator-5554")):
            driver = self.make_driver()
            setattr(driver._device if field == "serial" else driver._device._client, field, value)
            with patch.object(MODULE["socket"], "create_connection") as dial, self.assertRaises(RuntimeError):
                MODULE["bounded_private_observation"](driver, lambda: None, 1)
            dial.assert_not_called()


class GuestSyncScopeTests(unittest.TestCase):
    def setUp(self):
        self.adb = "/personal-test-sdk/platform-tools/adb"
        self.serial = "emulator-5666"
        self.marker = "0123456789abcdef0123456789abcdef"
        self.args = [self.adb, "-s", self.serial, "shell", "/system/bin/sync && printf '%s\\n' '" + self.marker + "'"]
        self.env = {"ADB_SERVER_SOCKET": "tcp:127.0.0.1:15037", "ANDROID_ADB_SERVER_HOST": "127.0.0.1",
                    "ANDROID_ADB_SERVER_PORT": "15037", "ADB_SERVER_PORT": "15037"}

    def validate(self, args=None, env=None):
        return MODULE["validate_guest_sync_argv"](self.args if args is None else args,
            self.env if env is None else env, adb_path=self.adb, serial=self.serial)

    def test_exact_private_guest_sync_is_recognized(self):
        self.assertEqual(self.validate(), {"ack_marker": self.marker, "expected_stdout": self.marker + "\n"})

    def test_other_serial_binary_or_power_command_is_rejected(self):
        cases = [["/unknown/adb", *self.args[1:]], [self.adb, "-s", "emulator-5554", *self.args[3:]],
                 [self.adb, "-s", self.serial, "reboot"], [self.adb, "-s", self.serial, "shell", "svc power shutdown"],
                 [self.adb, "-s", self.serial, "shell", "sync"],
                 [*self.args[:4], self.args[4].replace(self.marker, "wrong")],
                 [*self.args, "--extra"]]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(RuntimeError): self.validate(args=args)

    def test_missing_empty_or_default_route_is_rejected(self):
        for env in ({}, {**self.env, "ADB_SERVER_PORT": "5037"}, {**self.env, "ADB_SERVER_SOCKET": "tcp:127.0.0.1:5037"},
                    {key:value for key,value in self.env.items() if key != "ANDROID_ADB_SERVER_HOST"}):
            with self.subTest(env=env), self.assertRaises(RuntimeError): self.validate(env=env)

    def test_existing_nonpower_native_commands_remain_unclassified(self):
        for args in ([self.adb, "-s", self.serial, "emu", "kill"], ["avdmanager", "list", "avd"],
                     [self.adb, "-s", self.serial, "shell", "svc", "power", "stayon", "true"]):
            self.assertFalse(self.validate(args=args))


class StopCleanupBoundaryTests(unittest.TestCase):
    def manager(self, responses):
        class Manager:
            def __init__(self): self.calls = []
            def stop_sync(self, vm_id, *args, **kwargs):
                self.calls.append((vm_id, args, kwargs))
                value = responses.pop(0)
                if isinstance(value, BaseException): raise value
                return value
        return Manager()

    def test_failed_normal_stop_is_not_repeated_by_cleanup(self):
        attempts = []; manager = self.manager([{"ok": False, "reason": "stop_unconfirmed", "details": {"sync_completed": False}}])
        MODULE["observe_manager_stops"](manager, lambda vm: "pid:boot1", attempts)
        manager.stop_sync("vm", deadline=123.0)
        decision = MODULE["cleanup_stop_decision"]("vm", "pid:boot1", attempts)
        if decision["allowed"]: manager.stop_sync("vm")
        self.assertFalse(decision["allowed"])
        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(attempts[0]["forwarded_kwargs"], {"deadline": 123.0})

    def test_new_generation_allows_one_protective_stop_then_blocks_repeat(self):
        attempts = []; generation = ["same-pid:first-launch"]; manager = self.manager([{"ok": True}, {"ok": False, "reason": "operation_timeout"}])
        MODULE["observe_manager_stops"](manager, lambda vm: generation[0], attempts)
        manager.stop_sync("vm")
        generation[0] = "same-pid:second-launch"
        self.assertTrue(MODULE["cleanup_stop_decision"]("vm", generation[0], attempts)["allowed"])
        manager.stop_sync("vm")
        self.assertFalse(MODULE["cleanup_stop_decision"]("vm", generation[0], attempts)["allowed"])
        self.assertEqual(len(manager.calls), 2)

    def test_unknown_http_or_inflight_stop_blocks_cleanup(self):
        for outcome in ("requested", "running", "failed"):
            attempts = [{"vm_id": "vm", "generation_id": "g1", "kind": "http-stop", "outcome": outcome}]
            self.assertFalse(MODULE["cleanup_stop_decision"]("vm", "g1", attempts)["allowed"])
            self.assertTrue(MODULE["cleanup_stop_decision"]("vm", "g2", attempts)["allowed"])

    def test_manager_exception_and_result_are_transparently_preserved(self):
        error = TimeoutError("original budget"); result = {"ok": True, "details": {"exit_evidence": "actual"}}
        attempts = []; manager = self.manager([error, result])
        MODULE["observe_manager_stops"](manager, lambda vm: "g1", attempts)
        with self.assertRaises(TimeoutError) as caught: manager.stop_sync("vm", "emulator-5666", deadline=42.5)
        self.assertIs(caught.exception, error)
        self.assertIs(manager.stop_sync("vm", deadline=43.5), result)
        self.assertEqual(manager.calls[0], ("vm", ("emulator-5666",), {"deadline": 42.5}))
        self.assertEqual([row["outcome"] for row in attempts], ["failed", "succeeded"])
        self.assertFalse(MODULE["cleanup_stop_decision"]("vm", "g1", attempts)["allowed"])


class HttpStopCleanupBoundaryTests(unittest.TestCase):
    def functions(self, post_status, final_state, *, action="stop", final_outcome="succeeded", post_transform=None, read_transform=None, script_path=None):
        source = ast.parse(Path(script_path or MODULE["__file__"]).read_text())
        names = ("api", "wait_vm", "finish_api_stop", "latest_api_operation", "finish_api_operation")
        found = {node.name:node for node in ast.walk(source) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names}
        nodes = list(found.values())
        def instance(state, outcome):
            return {"id": "vm", "state": state, "runtime": {"operation": {"action": action,
                "request_id": "current-request", "pending": outcome == "pending", "outcome": outcome}}}
        class HTTP:
            async def request(self, method, path, **kwargs):
                status = post_status if method == "POST" else 200
                if method == "POST":
                    payload = {"sent": True, "instance": instance("stopping" if action == "stop" else "starting", "pending")}
                    if post_transform: payload = post_transform(payload)
                else:
                    payload = instance(final_state, final_outcome)
                    if read_transform: payload = read_transform(payload)
                return types.SimpleNamespace(status_code=status, text="synthetic transport response", json=lambda: payload)
        state = {"api_stop_attempts": [], "api_lifecycle_attempts": []}
        scope = {"http": HTTP(), "auth": {}, "timing": lambda *args, **kwargs: None,
                 "native_generation": lambda vm: "g1", "diagnostic_state": state, "utc_now": lambda: "fake-clock",
                 "time": time, "eventually": MODULE["eventually"], "prefix": "/api/internal/vm/instances",
                 "require_operation": MODULE["require_operation"], "operation_snapshot_ready": MODULE["operation_snapshot_ready"]}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "actual-http-stop-wrapper", "exec"), scope)
        return scope, state

    def test_failed_http_dispatch_blocks_cleanup_even_without_manager_attempt(self):
        scope, state = self.functions(409, "running")
        with self.assertRaises(RuntimeError): asyncio.run(scope["api"]("POST", "/api/internal/vm/instances/vm/stop"))
        self.assertFalse(MODULE["cleanup_stop_decision"]("vm", "g1", state["api_stop_attempts"])["allowed"])

    def test_accepted_stop_stays_unresolved_until_terminal_readback(self):
        scope, state = self.functions(200, "stopped")
        asyncio.run(scope["api"]("POST", "/api/internal/vm/instances/vm/stop"))
        self.assertFalse(MODULE["cleanup_stop_decision"]("vm", "g1", state["api_stop_attempts"])["allowed"])
        asyncio.run(scope["wait_vm"]("vm", "stopped", timeout=1))
        self.assertTrue(MODULE["cleanup_stop_decision"]("vm", "g1", state["api_stop_attempts"])["allowed"])

    def test_failed_stop_physical_stopped_never_resumes_or_retries(self):
        for outcome in ("failed", "timeout", "timed_out"):
            scope, state = self.functions(200, "stopped", final_outcome=outcome)
            advanced = []
            async def flow():
                await scope["api"]("POST", "/api/internal/vm/instances/vm/stop")
                await scope["wait_vm"]("vm", "stopped", timeout=1)
                advanced.append("start")
            with self.subTest(outcome=outcome), self.assertRaises(RuntimeError): asyncio.run(flow())
            self.assertEqual(advanced, [])
            native_success = {"vm_id": "vm", "generation_id": "g1", "outcome": "succeeded"}
            self.assertFalse(MODULE["cleanup_stop_decision"]("vm", "g1", [native_success, *state["api_stop_attempts"]])["allowed"])
            self.assertEqual(state["api_stop_attempts"][0]["request_id"], "current-request")

    def test_missing_or_changed_identity_metadata_cannot_pass(self):
        transforms = [lambda value:{**value, "id":"other-vm"}, lambda value:{k:v for k,v in value.items() if k!="id"},
                      lambda value:{**value, "runtime":{}},
                      lambda value:{**value,"runtime":{"operation":{**value["runtime"]["operation"],"request_id":"other-request"}}},
                      lambda value:{**value,"runtime":{"operation":{**value["runtime"]["operation"],"request_id":""}}},
                      lambda value:{**value,"runtime":{"operation":{**value["runtime"]["operation"],"action":"start"}}}]
        for transform in transforms:
            scope,state=self.functions(200,"stopped",read_transform=transform)
            asyncio.run(scope["api"]("POST","/api/internal/vm/instances/vm/stop"))
            with self.assertRaises(RuntimeError):asyncio.run(scope["wait_vm"]("vm","stopped",timeout=1))
            self.assertFalse(MODULE["cleanup_stop_decision"]("vm","g1",state["api_stop_attempts"])["allowed"])
        for transform in (lambda body:{"sent":True,"instance":{"state":"stopped"}}, lambda body:{**body,"sent":False}):
            scope,state=self.functions(200,"stopped",post_transform=transform)
            with self.assertRaises(RuntimeError):asyncio.run(scope["api"]("POST","/api/internal/vm/instances/vm/stop"))
            self.assertEqual(state["api_stop_attempts"][0]["outcome"],"failed")

    def test_failed_or_wrong_start_dispatch_cannot_enter_case(self):
        for endpoint in ("start", "dispatch"):
            for outcome in ("failed", "timeout"):
                scope,state=self.functions(200,"running",action="start",final_outcome=outcome)
                entered=[]
                async def flow():
                    await scope["api"]("POST","/api/internal/vm/instances/vm/"+endpoint)
                    await scope["wait_vm"]("vm","running",timeout=1)
                    entered.append("case")
                with self.assertRaises(RuntimeError):asyncio.run(flow())
                self.assertEqual(entered,[])
                self.assertEqual(state["api_lifecycle_attempts"][0]["outcome"],"failed")
            scope,state=self.functions(200,"running",action="start")
            asyncio.run(scope["api"]("POST","/api/internal/vm/instances/vm/"+endpoint))
            asyncio.run(scope["wait_vm"]("vm","running",timeout=1))
            self.assertEqual(state["api_lifecycle_attempts"][0]["verified_request_id"],"current-request")
            scope,state=self.functions(200,"running",action="start",read_transform=lambda value:{**value,
                "runtime":{"operation":{**value["runtime"]["operation"],"request_id":"another-start"}}})
            asyncio.run(scope["api"]("POST","/api/internal/vm/instances/vm/"+endpoint))
            with self.assertRaises(RuntimeError):asyncio.run(scope["wait_vm"]("vm","running",timeout=1))
            self.assertEqual(state["api_lifecycle_attempts"][0]["outcome"],"failed")

    def test_pending_or_incomplete_operation_is_not_physical_success(self):
        for action,target in (("stop","stopped"),("start","running")):
            snapshot={"id":"vm","state":target,"runtime":{"operation":{"request_id":"r","action":action,"pending":True,"outcome":"pending"}}}
            self.assertFalse(MODULE["operation_snapshot_ready"](snapshot,vm_id="vm",action=action,request_id="r",desired_state=target))
            for operation in ({"request_id":"r","action":action,"outcome":"succeeded"},
                              {"request_id":"r","action":action,"pending":False},
                              {"request_id":"r","action":action,"pending":True,"outcome":"succeeded"}):
                snapshot["runtime"]["operation"]=operation
                with self.assertRaises(RuntimeError):MODULE["operation_snapshot_ready"](snapshot,vm_id="vm",action=action,request_id="r",desired_state=target)

    def test_failed_http_attempt_cannot_be_promoted_by_finish(self):
        scope,state=self.functions(200,"stopped")
        asyncio.run(scope["api"]("POST","/api/internal/vm/instances/vm/stop"))
        scope["finish_api_stop"]("vm","failed")
        with self.assertRaises(RuntimeError):scope["finish_api_stop"]("vm","succeeded")
        self.assertEqual(state["api_stop_attempts"][0]["outcome"],"failed")


class SyncCommandObserverTests(unittest.TestCase):
    def setup_observer(self, result=None, error=None):
        scope_case = GuestSyncScopeTests(); scope_case.setUp()
        class Backend:
            def __init__(self): self.calls = []
            def run(self, args, *extra, **kwargs):
                self.calls.append((args, extra, kwargs))
                if error is not None: raise error
                return result
        backend = Backend(); state = {"native_calls": []}
        source = ast.parse(Path(MODULE["__file__"]).read_text())
        cls = next(node for node in ast.walk(source) if isinstance(node, ast.ClassDef) and node.name == "ManagerSubprocessObserver")
        namespace = {"original_manager_subprocess": backend, "validate_guest_sync_argv": MODULE["validate_guest_sync_argv"],
                     "adb": scope_case.adb, "EMULATOR_PORT": 5666, "os": types.SimpleNamespace(environ=scope_case.env),
                     "manager": types.SimpleNamespace(_runtimes={"vm": types.SimpleNamespace(vm_id="vm", adb_serial="emulator-5666")}),
                     "owned": {"vm"}, "diagnostic_state": state, "native_generation": lambda vm: "generation1",
                     "utc_now": lambda: "fake-clock", "time": time}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "observer-only", "exec"), namespace)
        return namespace["ManagerSubprocessObserver"](), backend, state, scope_case

    def test_zero_and_nonzero_results_are_returned_without_completing_vm(self):
        for code in (0, 1):
            result = subprocess.CompletedProcess([], code, stdout="ADB closed", stderr="")
            observer, backend, state, case = self.setup_observer(result=result)
            actual = observer.run(case.args, check=False, capture_output=True, text=True, timeout=4.75)
            self.assertIs(actual, result)
            self.assertEqual(len(backend.calls), 1)
            self.assertEqual(backend.calls[0][2]["timeout"], 4.75)
            record = state["native_calls"][0]
            self.assertEqual(record["returncode"], code)
            self.assertTrue(record["returncode_is_not_completion_evidence"])
            self.assertNotIn("stopped", record)
            self.assertEqual(record["ack_marker"], case.marker)

    def test_uncertain_native_exception_is_preserved_without_retry(self):
        error = subprocess.TimeoutExpired("sync", 2.5)
        observer, backend, state, case = self.setup_observer(error=error)
        with self.assertRaises(subprocess.TimeoutExpired) as caught: observer.run(case.args, timeout=2.5)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(len(state["native_calls"]), 1)

    def test_empty_environment_never_reaches_native_backend(self):
        observer, backend, state, case = self.setup_observer(result=subprocess.CompletedProcess([], 0))
        with self.assertRaises(RuntimeError): observer.run(case.args, env={})
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
