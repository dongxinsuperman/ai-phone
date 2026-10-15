# a3: data diagnosis passed, restart visual evidence incomplete

One authorized native run completed in **71.794237 s** on product `90342046a932273e54d7f96cb2947d753476477b`. No product changes or automatic native reruns occurred. All 33 product hashes and the reviewed Python/Java/JS hashes match at start and finish.

Run root: `/Users/dongxin/.codex/personal-vm-followup/android-app-install-9034204-20261016-a3`.

| Check | Observed result |
| --- | --- |
| Real Server package/task → WS handler → NativeOperation → APK install | One real installation, persisted task/item success |
| Durable app-private witness | Initial file FD sync and parent fsync succeeded; restart only read; same 92 bytes, SHA-256, inode `132026` and mtime |
| Original HTML receipt | Initial empty/count 0 → confirmed `personal804c654f28f4`/count 1 → restart same token/count 1 |
| Read-only raw storage | Exact same origin/key; before and after restart raw JSON contains the same token/count; no read or parse error |
| Actual WebView provider | Both boots: `com.google.android.webview`, `109.0.5414.123`, versionCode `541412334`; actual UA reports the same version |
| Before/confirmed pixels | Independently inspected by fleet_inventory: empty/count 0 and the exact confirmed token/count 1 |
| Restart pixels | **White image**; subsequent native XML also contains no token. Visible saved text is not established |
| Native stop | Both `emu kill` calls succeeded; recorded process wrappers invoked no terminate/kill fallback |
| Final deletion and cleanup | Immediate Server ledger absence, owned AVD removed, owned native/ADB processes absent, ports free, protected default metadata unchanged |

The summary status is `diagnostic_data_passed_visual_incomplete`; the native harness stopped at `native_checks_passed_visual_review_pending`. The third image must not be labeled visually passed. A complete page DOM/read receipt does not substitute for rendered pixels, and the available evidence does not establish why this image is white.

Exact original images are `a3-fixture-before.png`, `a3-fixture-confirmed.png`, and `a3-fixture-persisted.png` in this directory. Their original files and `visual-review.json` are also in the run root. Root review is pending; no native instance was recreated to obtain another picture.

## Timing and scope

Host monotonic observations measured confirmation click request → first stop request at `5.168531709 s`, confirmation HTTP receipt → first stop request at `4.944583625 s`, and completed fixture-confirmed stage → stop request at `2.487360 s`. These observations are preserved with native argv/return codes and both AVD identity snapshots in `android-app-install-a3-results.json`. They do not by themselves establish the cause of a2.

The a3 observer adds asynchronous diagnostic work and a private witness before the first WebView. Its successful file/raw-storage checks prove this instrumented run's data observations. They do **not** prove that the a2 fault was fixed, that buffered browser writes are always durable, or that the product now satisfies every visual lifecycle condition. The product source did not change between those runs.

The failed a2 result and its stopped AVD remain preserved. Its cause remains unresolved. The a3 native hardware slot has been released; independent post-cleanup checks found no owned process and successful IPv4/IPv6 native-compatible binds on `15037`, `5666` and `5667`.

Primary structured evidence: `android-app-install-a3-results.json`, `android-app-install-a3.evidence.json`, the original run's `post-cleanup-readback.json`, and `diagnostic-sources/`.
