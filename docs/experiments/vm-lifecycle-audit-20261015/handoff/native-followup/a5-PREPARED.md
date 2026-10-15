# a5 sync-only preparation and execution record

Entry: `android_app_install_sync_a5.py`. The unexecuted poweroff draft was renamed and replaced before review; no `svc power shutdown` experiment occurred. The frozen a4 script remains SHA-256 `a958493a2f746d209eba4b7dbd7a0982de35da157120a9f599532872d6016f9a`, and the a2/a4 AVDs are untouched.

The candidate observes the product's exact normal-stop command:

`adb -s emulator-5666 shell "/system/bin/sync && printf '%s\n' '<product-generated 32 lowercase hex marker>'"`

Only the installed private SDK adb, exact owned runtime/serial, and all private ADB routing variables are accepted. Guest power/reboot commands, extra shell syntax, another serial/binary, missing routing or an empty explicit environment are rejected. Existing authorized emulator-kill and AVD create/delete commands retain their prior observation behavior. The observer forwards the same arguments, timeouts, results and exceptions once; it does not issue sync itself or decide that a command return code means the VM stopped.

The marker acknowledges this sync command's completion and kernel writeback invocation. It does not establish application userspace commit, the absence of every underlying I/O error, or VM exit. The actual installed SDK's shell-v2 EOF handling is not being reported as an observed deployment bug. The original page/raw-storage/witness/exit assertions remain separate evidence.

## Failed-stop cleanup boundary

The harness observes actual `manager.stop_sync` return values and HTTP stop attempts. Records are tied to a native generation derived from the latest owned launch's PID plus its monotonic launch timestamp; Agent/Manager reconstruction does not create a new native generation.

- A failed or unresolved stop in the current generation blocks automatic cleanup stop/delete and preserves the private ADB daemon and VM resources for root. It also suppresses an extra failure screenshot that could otherwise reconnect after an uncertain stop.
- A successful old-generation stop followed by a new native launch allows one protective stop if the restarted page's data/visual check fails. Failure of that protective stop blocks a further retry.
- An HTTP stop failure with no native delivery is still unresolved for this purpose; cleanup does not silently turn it into another stop request.

No normal-stop failure is repaired by a hidden retry. Metadata observation adds no fixture sync, pre-stop wait, click, refresh or storage rewrite.

HTTP dispatch/start/stop acceptance now records the exact returned VM identity and operation request ID/action. Existing GET polling requires that same operation to be explicitly succeeded and no longer pending, together with the requested physical state. Failed/timeout outcomes stay failed even if a late report says running/stopped; missing or replaced IDs/operations fail closed. The prior `9c5cea...` harness was rejected by independent review and is retained only for the counterexample replay, never for execution.

## Preserved case and scope

The original HTML, Java wrapper and read-only JS retain identical bytes. AST comparisons confirm `visible_case`, original receipt/readiness, XML, target, configure/build, restart visual sampling and diagnostic-summary functions are unchanged from a4. The existing actual native XML plus original-frame gate remains in place and still requires root pixel review.

This case uses **no_snapshot**. It does not validate save/discard_changes/cold_boot behavior against an existing QuickBoot snapshot; that is a separately authorized later matrix. If this case passes, the result may establish that this specific previously failing case passed on the sync candidate. It does not by itself prove a unique cause for every a2/a4 failure.

After the operation-result correction, **57 guard tests passed** and **14 actual production-helper versus observer comparisons passed**, repeated against the final `f263af3a0b8ed6aea556c63354747a79e52a4f86` freeze. The same AST replay proves the old harness advanced on failed/timeout stop or failed start/dispatch physical facts; the corrected harness blocks all four cases. Independent review separately replayed failed/timeout/replaced ID/missing operation/pending deadline/success cases and closed this harness blocker. The final 33 product files matched both the frozen manifest and their Git blobs before the native run.

Review package: `a5-prepare/a4-to-a5.diff`, `a5-prepare/review-manifest.json`, guard logs and observer-equivalence JSON. The shared product freeze and main guides were not edited by this preparation.

Root subsequently authorized exactly one native run, `android-app-install-f263af3a-20261016-a5`. It completed in 76.820651 seconds with all native checks passing; root independently inspected all three original screenshots and accepted the result (`android-a5-root-review.json`). The original page receipts were count 0 → confirmed count 1 → restarted count 1, with `personal11a45b500f0f` preserved. Raw storage and the separate durable witness passed independently; both product sync commands returned their exact acknowledgment markers, and neither native stop used an Agent TERM/KILL fallback. The Server ledger, AVD/ini and owned native/ADB processes were removed; private ports and protected metadata were verified. See `android-app-install-a5-results.json` and the three `a5-fixture-*.png` originals copied without alteration. No a6 native execution followed this run.
