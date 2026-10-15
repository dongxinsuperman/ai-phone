# a3 diagnostic preparation — not executed

This is the historical preparation record. After explicit root authorization, one a3 native run completed; its separate result is in `a3-RESULT.md`. The preparation and reviewed source hashes remain unchanged.

The a2 persistence failure remains failed and its AVD remains untouched. This is a new diagnostic harness, prepared for review only. Product commit `90342046a932273e54d7f96cb2947d753476477b` and all 33 product hashes remain unchanged.

Entry: `android_app_install_diagnostic_a3.py`; default invocation prints a plan. Its future `--execute` requires a fresh run root and explicit hardware authorization from root. No native run, ADB daemon, Server, package upload or installation was started while preparing a3. Only local Java compilation, pure guard tests and dry-run were performed.

Review files:

- `a3-prepare/a2-to-a3.diff`: Python harness diff against the exact source used by failed a2.
- `a3_MainActivity.java`: personal wrapper only; the package ID, Manifest and original shared HTML remain unchanged.
- `a3_storage_diagnostic.js`: strictly read-only storage observation, injected with `evaluateJavascript`.
- `a3-prepare/review-manifest.json`: frozen diagnostic hashes, original HTML hash and validation results.
- `android-a3-readonly-js-review.json`: Android owner's independent JS behavior check.

## Two independent observations

Before creating the first WebView, Java exclusively creates one file named `personal-witness-<run-id>.txt` directly in the already existing app `getDataDir()`. It writes the unique payload, calls the file descriptor's `sync()`, opens the parent read-only, verifies that descriptor is a directory, and calls `Os.fsync()`. Every error is explicit. An existing witness is rejected, not truncated. The Java page is not started if this first-write precondition fails.

The restart intent selects a separate `restart-read` branch. It performs no directory creation, file creation, output-stream open, fsync, repair or retry. Missing content is reported as an error and the original page still runs, allowing the independent localStorage result to be observed. Both witness observations include path, SHA-256, length, inode, mtime and operation timestamps.

The shared HTML bytes and its count=1 assertion remain unchanged. The Java WebViewClient observes the original `/fixture/receipt` request and schedules a read-only evaluation on the view thread; `onPageFinished` also observes the load. The probe reads the actual URL/origin/storage key, raw `getItem` value, missing flag, separate read/parse errors, parsed value and user agent. It does not set/remove/clear storage, change DOM, add click handlers or alter the request. Diagnostics use a separate asynchronous localhost POST and are never retried.

After constructing each WebView, Java records the actual loaded provider's package/version/versionCode. Python reports both identities and explicitly states whether they match; SDK metadata does not substitute for runtime provider version.

## Original timing and native evidence

There is no new wait, guest command, app force-stop, reinstall or synchronization after the confirmation click. The original receipt, screenshots, reconnect/reconstruction, stop/start and count=1 checks run as before. Host timing marks are appended only in memory. Diagnostic HTTP events are saved by existing evidence flushes instead of adding a flush to that path. Java asynchronous observation still adds some observation work; its trigger/evaluation/delivery timestamps are recorded, so a later a3 success cannot erase a2 or by itself prove a product repair.

The Manager is observed through a process-local delegating proxy. It calls the same native functions once and records actual launch argv, native create/delete and `emu kill` return codes. A proxy around the existing owned-process shutdown records calls to wait/poll/terminate/kill without adding any such calls. It returns the original values and rethrows original exceptions. Only first-boot and restart readiness take small read-only AVD metadata snapshots (including UUID, data-image inode and native launch record); neither reconnection readiness stage adds this file I/O to confirmation-to-stop.

The exact Python script, Java template, read-only JS, original HTML and generated Java are retained under the new run's `diagnostic-sources/`. All source hashes are checked again at finish. The preserved a2 root is not used by the new harness.

## Result rules

The real package API → persisted task → WebSocket handler → actual NativeOperation → native installation remains required. The original page must still retain the exact token/count 1. Witness preservation and raw-storage preservation are reported separately, even if the original page assertion fails.

Missing asynchronous evidence is labeled **diagnostic evidence incomplete**. It is not converted to a missing file or empty localStorage. Read errors, parse errors, missing keys, changed contents and changed provider identity remain separate observations. `raw_storage_preserved:false` means preservation was not established; its detailed evidence is required before assigning a cause.

The existing exact-ID/AVD/private-ADB/localhost package guards remain in force. On a failed existing-instance stop/restart, the new test's data is preserved; there is no automatic second run. The old a2 AVD remains preserved regardless of the new result.

Validation completed: **26 Python guard tests passed**, original HTML SHA unchanged, original a2 source SHA unchanged, 33/33 product hashes match, dry-run created no native run directory, and generated Java compiled successfully with the installed API 36 jar/JDK 17. Independent JS tests covered valid content, missing key, JSON `null`, malformed JSON and storage-read exception with zero writes. No hardware acceptance is claimed.
