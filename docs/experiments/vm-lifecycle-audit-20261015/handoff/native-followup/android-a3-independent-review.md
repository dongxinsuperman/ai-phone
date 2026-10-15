# Android a3 independent prepare review

Status: **approved for root's one-run authorization decision; not executed by this reviewer**.

The a2 failure remains failed and sealed. This approval is for diagnostic preparation only, not Android persistence acceptance or a product repair.

## Reviewed candidate

The four source hashes in `a3-prepare/review-manifest.json` were rechecked before and after review and matched. Python: `4ab5904ea8262a27994eae4a90dd969372caea5142ba4fd063ba52b725731fee`; Java: `9219cf05d043b476d802cc0b5e5b02b2827d013d13f73d5fab6a39ac03ba906d`; JavaScript: `7a2bd86efcb92da3a63956b34e24948e1399bc48cc4e42cb2d284981d2db80cf`. The original shared HTML remains SHA `24e20a5cf5ecaf8c3490c0ec11a8065b9974a51328a400e6e36a18ba7162cf87`. The original Manifest and a2 source are unchanged.

## Findings

No blocking finding in the frozen candidate.

- The first witness is exclusively created in existing app `getDataDir()`. File descriptor synchronization and parent-directory synchronization both precede `new WebView`. Failure prevents starting the page. Existing content is never silently truncated or repaired.
- `restart-read` does not create a directory/file, open an output stream, synchronize storage, or repair missing content. Its result is independent of the original page result.
- The original HTML and count=1/token assertion remain intact. The input/click, connection reconstruction, stop/start sequence is retained. Added confirmation-to-stop observations only append in memory; no new guest command, wait, force-stop, reinstall or global sync was added.
- Raw JavaScript only reads the current URL/origin/key and `localStorage.getItem`, then parses its returned value. Missing key, JSON null, read exception and parse exception remain distinguishable. Diagnostic delivery is asynchronous and does not replace the original receipt.
- Native observation delegates the actual calls and returns their original values/exceptions. It records startup argv, AVD create/delete, `emu kill` result and existing process wait/signal calls. AVD file/UUID snapshots occur only at first/restarted readiness, not in the confirmation-to-stop interval.
- Witness and raw-storage tracks are separately summarized in `finally`, including when the original page fails. Missing asynchronous evidence is explicitly incomplete, not proof of lost data. Provider identities from both boots are retained and compared.
- `WebView.getCurrentWebViewPackage()` is called after the WebView constructor, so it identifies the loaded provider. The Android API documents this distinction: https://developer.android.com/reference/android/webkit/WebView#getCurrentWebViewPackage(). No current/a2 provider version is inferred from the SDK revision.

## Independent checks

- `android-a3-readonly-js-review.json`: five actual Node VM executions (valid value, missing, JSON null, malformed JSON, read exception), each one getItem and zero mutations.
- `android-a3-process-observer-review.json`: seven isolated comparisons between the real production stop helper and its diagnostic wrapper, covering graceful exit, wait timeout/terminate, terminate timeout/kill, adopted process, already exited, vanished process and unexpected exception. Native-call order/arguments and outcome were identical.
- Independently reran all 26 pure Python guards: passed. Tests import no backend and execute no native command.
- Inspected the successful offline javac preparation evidence and generated-source hash. No APK installation or VM was started by this reviewer.

## Remaining evidence boundary

A3 adds asynchronous observation work, so an a3 success does not erase a2 or prove a timing-independent fix. The same unchanged production stop must be used. A durable-file success proves preservation of that synchronized file; it does not make an absent/error localStorage observation successful.

The actual provider version and user-agent must come from the authorized run. Match those exact version tags to Chromium storage commit/shutdown code before discussing implementation details. Existing a2 artifacts do not contain that version; the SDK only identifies API33 Google APIs revision17/build12342917, which is insufficient. Current Chromium main's delay is not evidence for a2.

No product file or retained a2 AVD was changed during this review.
