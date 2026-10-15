"""Prepare-only tests: no backend import, network, APK build or native command."""
from __future__ import annotations

import runpy
import sys
import unittest
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).with_name("android_app_install_e2e.py")))
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


if __name__ == "__main__":
    unittest.main()
