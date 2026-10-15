# a2 failed persistence: preserved observations

Run root: `/Users/dongxin/.codex/personal-vm-followup/android-app-install-9034204-20261016-a2`.

The real package upload → persisted installation task → WebSocket handler → real `NativeOperation` → Android installer → persisted task success completed once. The subsequent stop/start returned the fixture's empty record. This is a failed full acceptance; the immediate deletion stage was not reached. No third native run, force-stop, extra wait-for-flush, reinstall, guest modification, or AVD reconstruction has been performed.

## Identity, creation and backing files

| Observation | First boot | Restart |
| --- | --- | --- |
| VM ID / AVD name | `d499f403dffb` / `aiphone_vm_d499f403dffb` | same |
| Exact target serial | `emulator-5666` | same |
| Native PID | `39771` | `39992` |
| Persisted host ID | `f46e701b2b8c415291082c718acab5d9` | same; fresh Manager and Agent ID were reconstructed before first stop |
| Native readiness | `2026-10-15T19:47:07.989344Z` | `2026-10-15T19:47:43.583524Z` |
| Native create | new isolated root, first binding | Server's real status `created_in_attempt:false` |
| Native delete | none | cleanup explicitly skipped deletion to preserve failure data |

All dates in this document are UTC. Full exact event timestamps are in `evidence.json`. The Server row was read through SQLite `mode=ro` and copied to `read-only-server-vm.json`. The final Server record still belongs to the same VM/host; it is offline because the test Agent and Server have now shut down.

Private data directory: `android/avd/aiphone_vm_d499f403dffb.avd/` under the run root. Current file metadata was saved to `read-only-avd-file-manifest.json` without changing the AVD:

| File | Current inode | Birth time | Last modification |
| --- | --- | --- | --- |
| `config.ini` | `84322641` | `19:46:44.405314Z` | `19:47:27.583813Z` |
| `userdata-qemu.img` | `84322663` | `19:46:44.463946Z` | `19:46:44.465698Z` |
| `userdata-qemu.img.qcow2` | `84322683` | `19:46:44.805788Z` | `19:48:17.432311Z` |

The current `emulator-user.ini` records UUID `1792093604484`. `hardware-qemu.ini` points to that same private data-image path. Read-only `qemu-img info` reports the overlay backed by the same directory's `userdata-qemu.img`, `corrupt:false`, `dirty-flag:false`; the output is preserved as `read-only-qcow-info.json`.

The file birth times precede restart and the restarted native status says it did not create an instance. The freshly installed APK also still opens after restart. Together these support retaining the original instance rather than replacing the entire AVD. **The harness did not record inode/UUID snapshots before the first stop**, so current inode numbers must not be described as a directly measured before/after equality.

## Start flags and shutdown

The native `emu-launch-params.txt` records the actual second launch, including `-avd aiphone_vm_d499f403dffb`, `-port 5666`, `-no-snapshot`, `-no-window`, `-memory 2048`, `-cores 2`, and no `-wipe-data`. The full argv is copied into `android-app-install-a2-results.json`.

The Server's persisted configuration is `storage.wipe_data:false` and `snapshot_policy:no_snapshot`. Its second-start status confirms `snapshot_policy:no_snapshot`. Both emulator shutdowns log that snapshots were disabled. The first full native argv was overwritten by the native second-launch record and was not separately captured; its exact flags are supported by the unchanged configuration/source and native snapshot log, not a preserved first argv.

Production shutdown checks exact AVD identity, calls `adb -s emulator-5666 emu kill`, and waits for the owned process; only if necessary does its bounded fallback terminate/kill that owned process. The emulator log records shutdown around lines 104–107 and 209–212. This run did not instrument the `emu kill` return code or chosen process-signal fallback branch, so those branch outcomes cannot be retrospectively claimed. Both actual exit checks confirmed no matching processes and native-compatible free ports. First stop completed at `19:47:27.132417Z`; final protective stop completed at `19:48:20.370235Z` with native exit code 0.

`native_exit_samples` also captured plain-bind errno 48 versus native-compatible reuse-bind 0 after actual exit; this did not cause the persistence failure.

## Same page and storage receipt chain

Both launches use exactly:

`http://127.0.0.1:19080/fixture/?run=android-app-install-9034204-20261016-a2&platform=android`

The shared fixture derives storage key:

`personal-vm-e2e:android-app-install-9034204-20261016-a2`

Both are recorded in the harness/page source and `fixture-open` evidence; the exact reverse mapping points to this same run's loopback Server. Full receipt sequence:

1. `load`: token `""`, count `0`.
2. `confirm`: token `personal05598c7fa9de`, count `1`.
3. After restart, `load`: token `""`, count `0`.

`fixture-confirmed.png` visibly shows the saved token and count 1; `failure-d499f403dffb.png` visibly shows no saved token and count 0. The confirmation stage completed at `19:47:22.643904Z`. The exact localStorage commit-to-disk time and guest WebView/app process lifecycle were not instrumented. Thus whether the application write remained buffered, failed to persist, or suffered another application/storage issue remains unresolved; a successful receipt only establishes the page's in-memory/localStorage API state then.

The real installer used the unchanged `adb push` plus `pm install -r -t -g`; there was one native call and no uninstall or `pm clear`. Its temporary files and device leases had been released before UI interaction. The native APK itself remained usable after restart.

## Preserved state and release

The AVD and its `.ini` remain preserved exactly for investigation. The independent `post-cleanup-readback.json` confirms no owned emulator/private-ADB process and successful IPv4/IPv6 native-compatible bind checks for `15037`, `5666`, and `5667`. Default metadata fingerprints are unchanged. All 33 frozen product hashes match at start and finish.

The hardware slot is released for the independent iOS run. Reading these evidence files is permitted; starting, rebuilding, deleting, or modifying this AVD is not currently authorized.
