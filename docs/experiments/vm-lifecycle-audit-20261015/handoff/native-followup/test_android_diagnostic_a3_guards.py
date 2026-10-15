"""Prepare-only tests: no backend import, network, APK build or native command."""
from __future__ import annotations

import runpy
import copy
import json
import sys
import unittest
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).with_name("android_app_install_diagnostic_a3.py")))
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


if __name__ == "__main__":
    unittest.main()
