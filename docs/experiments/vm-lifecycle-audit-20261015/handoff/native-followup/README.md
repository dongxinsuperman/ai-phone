# Android personal package-install native follow-up

The initial preparation was read-only. Root subsequently authorized one a1 run and, after its harness-only correction, one a2 run on product commit `90342046a932273e54d7f96cb2947d753476477b`. Both attempts remain **failed** in their own evidence. a1 stopped before upload because its missing-package probe mishandled exit 1; a2 passed the real installation chain but failed the same-origin storage persistence check after stop/start. No third native attempt is authorized. The a2 AVD remains stopped and preserved for read-only forensics; private ADB and native emulator processes/ports are released.

This keeps the previously accepted personal lifecycle harness and its fixture geometry. The installation stage now goes through the existing Server package/task API, WebSocket handler, `NativeOperation` and Android `install_apk`. Product files and earlier acceptance evidence are unchanged.

## Entry points and isolation

- Script: `android_app_install_e2e.py`; default invocation or `--dry-run` only prints a plan using stdlib and read-only source hashes.
- Native opt-in: `--execute --run-id android-app-install-<unique-name>`; reused roots are rejected. No automatic second run or retry endpoint is present.
- New root: `/Users/dongxin/.codex/personal-vm-followup/<run-id>/`.
- Server: a new `server.sqlite3`, `storage/`, random local token, loopback `127.0.0.1` ephemeral HTTP/WS port. The script clears inherited deployment/proxy settings and changes cwd to the new root before product imports. It does not read a CLI profile.
- Agent: narrow `AgentWSClient` plus real `AndroidVmManager`; no `agent.main`, default provider or global orphan scan.
- Android: private `ANDROID_AVD_HOME`, user home and runtime folders under this root; private ADB port `15037`; `--one-device` names a nonexistent USB device; mDNS/emulator scanning disabled; emulator ports `5666/5667`. The global APK adapter is asserted to use `127.0.0.1:15037`.
- Every native target must be an ID returned by this fresh Server, recorded before dispatch, with serial `emulator-5666` and the actual native AVD name `aiphone_vm_<vm_id>`. Unknown transports, private AVDs, package messages or repeated install requests abort.
- Existing default Android/CoreSimulator/Harmony metadata is fingerprinted before and after. No existing personal/company VM is selected or stopped.
- APK: this run builds the embedded personal Java WebView source as `fixture/personal-lifecycle.apk`, package `org.personal.aiphone.lifecycle`. No downloaded or company package is accepted.
- Server upload lives under `storage/app-install/`; the Agent's real temporary download directory is forced under this run's `tmp/`.
- Guest page uses the same shared `execution/personal-fixture.html` through exact-serial `adb reverse`, guest `http://127.0.0.1:19080/fixture/?run=<run-id>&platform=android`. External guest networking remains outside this acceptance.

The script now compares all 33 reviewed product file hashes with the root-reviewed `frozen-products.json` for commit `9034204` before creating any native resources and again at teardown. Additional app-install module and harness hashes are recorded in the run evidence. The earlier 32-file preparation record remains historical; the current freeze includes `vm_ports.py`.

## Actual package/task path and proof

1. Create and bind/start the single owned VM via `/api/internal/vm/instances`; confirm native boot/package service/screenshot and Server availability.
2. Verify the fresh VM has no `org.personal.aiphone.lifecycle` package. Upload the current APK using multipart `POST /api/app-install/packages` with one `file` part. Read the actual `AppPackage` row and saved file, requiring its SHA-256 to equal the built APK and its path to remain under the new upload storage.
3. `GET /api/app-install/packages/{package_id}/eligible-devices` must return exactly the owned serial.
4. `POST /api/app-install/tasks` sends exactly `{"package_id":"<this-upload>","serials":["emulator-5666"]}`.
5. Before calling the real `handle_app_install_start`, the narrow handler checks the exact relative/absolute localhost `/api/app-install/packages/{package_id}/download` URL, filename, platform, serial and timeout. It rereads the real persisted task and checks the one task item. No mocked task or injected successful response is used.
6. A wrapper around the existing installer verifies downloaded bytes and AVD identity, then records that the real `NativeOperation._invoke` is in the current call stack with a held device activity lease. It calls the unchanged original Android `install_apk`, which performs the actual push and `pm install`. Only one handler entry and one native install call are allowed. No direct `adb install` call exists in this stage.
7. The real Agent sends `app_install_result` and the real Server persists it. `GET /api/app-install/tasks/{task_id}` must identify the exact task/package/item/serial, have task state `done`, one Android item state `success`, terminal timestamps, and summary total/success `1` with all other counts `0`. A completed task containing failure is rejected.
8. Require exactly one real HTTP 200 package download, one successful real result observed after native exit and host temporary-file removal, zero remaining handler jobs/activity users, native `pm path` for the APK, and unchanged guest `ai-phone-install-*` temporary-file inventory.
9. Continue the accepted visible case: real AndroidDriver input and click, receipt plus PNG review, connection-only reconnect without native PID change, fresh manager/Agent ID with persisted host ID, stop/start preserving the saved token/count, immediate Server ledger deletion and native AVD/process/port absence.

Evidence starts as running and ends at most `native_checks_passed_visual_review_pending`; root must independently inspect the before/confirmed/persisted images before acceptance. Native installation success alone does not prove the visible business page.

## Cleanup and limits

One failure is recorded and no new lifecycle test is started. Normal cleanup targets only this run's recorded VM. If the real native install is still active, cleanup waits once for existing work and then preserves its VM/ADB/temp resources rather than cancelling native work or falsely claiming cleanup. A stop/restart failure on the existing VM preserves its data evidence. All such cases remain failed with their exact retained resources recorded.

This is a real package/API/WS/native installation plus synthetic lifecycle test. It does not establish VLM/company business acceptance, physical-device behavior, external guest networking, OS-level Agent crash recovery, or `agent.main._get_driver` cache eviction during live serial/port reuse. The harness deliberately uses fresh direct drivers; separate isolated timing tests cover the cache contract.

## Preparation validation

`test_android_install_harness_guards.py`: **15 passed** after the a1 correction. It checks exact package origin/identity, rejects other targets and unbounded timeouts, rejects failed/inconsistent/foreign terminal tasks, and verifies plan mode creates no run root and imports no product modules. The five added package-absence checks require a successful understood `pm list packages <APP_ID>` response, match the complete package line, and reject nonzero/unknown results.

`prepare-verification.json` records AST parse, dry-run success, 32/32 frozen product hashes matching, prior `execution/personal_e2e.py` unchanged, and no created native run root. `plan.json` is the exact prepared plan; `guard-tests.log` contains the test output.

Those files describe the initial preparation, not later native acceptance. Current attempt summaries are `android-app-install-a1-results.json` and `android-app-install-a2-results.json`; a2's guard output is `guard-tests-a2.log`. Each attempted harness source is preserved separately and matched to its run's source hash. See `a2-readonly-findings.md` for the persistence failure evidence and gaps. The three a2 images are initial, confirmed, and failure-after-restart, not three passing states.

Preparation commands from the personal hardening worktree:

```sh
backend/.venv/bin/python docs/experiments/vm-lifecycle-audit-20261015/handoff/native-followup/test_android_install_harness_guards.py
backend/.venv/bin/python docs/experiments/vm-lifecycle-audit-20261015/handoff/native-followup/android_app_install_e2e.py --dry-run --run-id android-app-install-prepare-only
```

After a separate explicit hardware release, root can authorize one unique `--execute` invocation using this same personal backend venv. Preparation does not grant that release.
