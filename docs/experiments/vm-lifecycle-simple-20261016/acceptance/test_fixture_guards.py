"""Preparation-only checks. No backend import, HTTP, browser, SDK or device."""
from __future__ import annotations

import ast
import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
M = runpy.run_path(str(HERE / "common.py"))


class PureFixtureTests(unittest.TestCase):
    def test_preparation_import_and_plan_have_no_product_or_runtime(self):
        self.assertFalse(any(name == "ai_phone" or name.startswith("ai_phone.") for name in sys.modules))
        with patch("subprocess.Popen", side_effect=AssertionError("process")), patch("socket.socket", side_effect=AssertionError("socket")):
            value = M["plan"]()
        self.assertEqual(value["mode"], "prepare-only")
        self.assertEqual(len(value["ids"]), 3)

    def test_twenty_one_distinct_twelve_hex_ids(self):
        ids = [M["vm_id"](platform, case) for platform in M["PLATFORMS"] for case, _, _ in M["CASES"]]
        self.assertEqual(len(ids), 21)
        self.assertEqual(len(set(ids)), 21)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{12}", value) for value in ids))

    def test_native_identities_are_actual_fixture_identity_not_display_alias(self):
        ident = M["vm_id"]("android", "start_failure")
        self.assertEqual(M["identities"]("android", ident, scratch="/owned")["avd_name"], "aiphone_vm_" + ident)
        ios = M["identities"]("ios", ident, scratch="/owned")
        self.assertEqual(ios["sim_name"], "aiphone_sim_" + ident)
        self.assertRegex(ios["udid"], r"^[A-F0-9-]{36}$")
        harmony = M["identities"]("harmony", ident, scratch="/owned", lease_token="own-lease", serial="127.0.0.1:10001")
        self.assertEqual(harmony, {"instance_name": "aiphone_harmony_" + ident,
            "instance_path": "/owned/fake-native", "lease_token": "own-lease", "hdc_serial": "127.0.0.1:10001"})

    def test_unsafe_or_unknown_identifiers_fail(self):
        for platform, ident in (("other", "0" * 12), ("ios", "../../device"), ("android", "ABCDEF123456")):
            with self.subTest(platform=platform, ident=ident), self.assertRaises(ValueError):
                M["identities"](platform, ident, scratch="/owned")
        with self.assertRaises(ValueError):
            M["vm_id"]("android", "foreign")

    def test_observations_separate_state_and_resource_release(self):
        ident = M["vm_id"]("harmony", "unknown_running")
        for state, released in (("running", False), ("stopped", True), ("missing", False), ("unknown", False)):
            value = M["observation"]("harmony", ident, state, scratch="/owned", lease_token="lease", serial="serial")
            self.assertEqual(value["resources_released"], released)
            self.assertEqual(value["vm_id"], ident)
            self.assertNotIn("lifecycle", value)
        with self.assertRaises(ValueError):
            M["observation"]("android", ident, "error", scratch="/owned")

    def test_actual_fake_harmony_reader_preserves_old_native_identity_after_lease_release(self):
        tree = ast.parse((HERE / "browser_fixture.py").read_text())
        reader = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "read_vm")
        observer = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "observed")
        ident = M["vm_id"]("harmony", "start_failure")
        row = types.SimpleNamespace(id=ident, assigned_agent_id="fixture-owner", lease_token="original-lease",
                                    hdc_serial="127.0.0.1:10001", to_dict=lambda: {})
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return None
            async def get(self, model, requested):
                self_requested = requested
                if self_requested != ident: raise AssertionError("unowned ID")
                return row
        scope = {"db": types.SimpleNamespace(get_session_factory=lambda: Session), "models": {"harmony": object()},
                 "lifecycle": types.SimpleNamespace(runtime=lambda vm: {}), "scratch": Path("/owned/runtime"),
                 "observation": M["observation"]}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[reader, observer], type_ignores=[])), "actual-fake-identity", "exec"), scope)
        spec = {"platform": "harmony", "vm_id": ident}
        first = asyncio.run(scope["read_vm"](spec))
        self.assertEqual(first["native_lease_token"], "original-lease")
        row.lease_token, row.hdc_serial = None, None
        released = asyncio.run(scope["read_vm"](spec))
        self.assertEqual(released["lease_token"], "")
        observed = scope["observed"](spec, "stopped", released, "fresh-passive")
        self.assertEqual(observed["identity"]["lease_token"], "original-lease")
        self.assertEqual(observed["identity"]["hdc_serial"], "127.0.0.1:10001")
        self.assertEqual(observed["identity"]["instance_path"], "/owned/runtime/fake-native")
        row.lease_token, row.hdc_serial = "new-lease", "127.0.0.1:10002"
        new = asyncio.run(scope["read_vm"](spec))
        self.assertEqual(scope["observed"](spec, "running", new)["identity"]["lease_token"], "new-lease")

    def test_only_owned_fresh_run_scope_allowed(self):
        expected = HERE / "runs/personal-simple-guard-case"
        self.assertEqual(M["own_run_dir"](expected), expected)
        for path in (HERE / "runs/other", HERE / "personal-simple-guard-case", Path("/tmp/personal-simple-guard-case")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                M["own_run_dir"](path)

    def test_symlink_run_rejected(self):
        function = M["own_run_dir"]
        with tempfile.TemporaryDirectory(prefix="personal-simple-guard-", dir=HERE) as tmp:
            before = function.__globals__["OUTPUT"]
            function.__globals__["OUTPUT"] = Path(tmp)
            try:
                runs = Path(tmp) / "runs"; runs.mkdir()
                (runs / "personal-simple-symlink-guard").symlink_to(Path(tmp) / "elsewhere")
                with self.assertRaises(ValueError):
                    function(runs / "personal-simple-symlink-guard")
            finally:
                function.__globals__["OUTPUT"] = before

    def test_freeze_rejects_missing_or_changed_sources(self):
        function = M["verify_manifest"]
        with tempfile.TemporaryDirectory(prefix="personal-simple-guard-", dir=HERE) as tmp:
            path = Path(tmp) / "manifest.json"
            expected = {"fixture/" + str(index): "sha" for index in range(20)}
            path.write_text(json.dumps({"kind": "personal-lifecycle-simple-frozen-source", "source_repo": str(M["REPO"]), "source_sha256": expected}))
            with patch.dict(function.__globals__, {"source_hashes": lambda: expected}):
                self.assertEqual(function(path)["mismatches"], [])
            with patch.dict(function.__globals__, {"source_hashes": lambda: {**expected, "fixture/0": "changed"}}):
                with self.assertRaises(RuntimeError):
                    function(path)
            path.write_text(json.dumps({"kind": "wrong", "source_repo": str(M["REPO"]), "source_sha256": expected}))
            with self.assertRaises(ValueError):
                function(path)


class ActualAuditTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((HERE / "browser_fixture.py").read_text())
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "audit")
        self.blocked = []
        scope = {"ipaddress": ipaddress, "Path": Path, "os": os, "time": type("Clock", (), {"monotonic": staticmethod(lambda: 1.0)}),
                 "started": 0.0, "port": 49321, "scratch": Path("/owned/runtime"), "blocked": self.blocked}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "actual-fixture-audit", "exec"), scope)
        self.audit = scope["audit"]

    def test_native_subprocess_and_signals_are_blocked_without_execution(self):
        for event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.kill", "os.killpg"):
            with self.subTest(event=event), self.assertRaises(PermissionError):
                self.audit(event, ())
        self.assertEqual(len(self.blocked), 6)

    def test_main_services_external_sockets_and_unix_sockets_are_blocked(self):
        for event, address in (("socket.connect", ("127.0.0.1", 8000)), ("socket.connect", ("127.0.0.1", 5180)),
                               ("socket.connect", ("192.0.2.1", 49321)), ("socket.bind", ("0.0.0.0", 0)),
                               ("socket.bind", ("127.0.0.1", 8000)), ("socket.connect", "/var/run/foreign.sock")):
            with self.subTest(event=event, address=address), self.assertRaises(PermissionError):
                self.audit(event, (None, address))

    def test_only_loopback_bind_and_own_fixture_connection_are_allowed(self):
        self.audit("socket.bind", (None, ("127.0.0.1", 0)))
        self.audit("socket.bind", (None, ("::1", 0)))
        self.audit("socket.connect", (None, ("127.0.0.1", 49321)))
        self.assertEqual(self.blocked, [])

    def test_default_env_and_nonlocal_dns_are_blocked(self):
        for path in ("/project/backend/.env", "/project/web/.env.local"):
            with self.assertRaises(PermissionError):
                self.audit("open", (path, "r", 0))
        self.audit("open", ("/owned/runtime/.env", "r", 0))
        with self.assertRaises(PermissionError):
            self.audit("socket.getaddrinfo", ("company.invalid", 80))

    def test_fixture_never_builds_a_lifecycle_response_literal_or_imports_native_manager(self):
        tree = ast.parse((HERE / "browser_fixture.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("ai_phone.agent"))
            if isinstance(node, ast.Dict):
                self.assertNotIn("lifecycle", [key.value for key in node.keys if isinstance(key, ast.Constant)])
        source = (HERE / "browser_fixture.py").read_text()
        self.assertIn('.handle_vm_status(', source)
        self.assertIn('app.include_router(module.router)', source)

    def test_server_api_package_bootstrap_precedes_domain_router_imports(self):
        tree = ast.parse((HERE / "browser_fixture.py").read_text())
        serve = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve")
        imports = [node.module for node in serve.body if isinstance(node, ast.ImportFrom)]
        bootstrap = imports.index("ai_phone.server.api")
        for module in ("ai_phone.server.android_vm", "ai_phone.server.ios_sim", "ai_phone.server.harmony_vm"):
            self.assertLess(bootstrap, imports.index(module))

    def test_actual_record_accepts_payload_kind_without_parameter_collision(self):
        tree = ast.parse((HERE / "browser_fixture.py").read_text())
        function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "record")
        events = []
        scope = {"events": events, "started": 0.0, "time": types.SimpleNamespace(monotonic=lambda: 1.0)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])), "actual-fixture-record", "exec"), scope)
        scope["record"]("agent-send", kind="vm_capability_probe", vm_id="07571cd6fe76")
        self.assertEqual(events, [{"event": "agent-send", "seconds": 1.0,
                                   "kind": "vm_capability_probe", "vm_id": "07571cd6fe76"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
