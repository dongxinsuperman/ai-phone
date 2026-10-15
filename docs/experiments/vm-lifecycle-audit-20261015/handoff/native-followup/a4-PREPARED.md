# a4 visual calibration — prepared, not executed

Entry: `android_app_install_visual_a4.py`. It is a separate copy of frozen a3; a2/a3 sources and evidence remain unchanged. The final reviewed product is now `8a2a3be1bb60f719282349e6135be3c226c3a7fe`, with all 33 product hashes matching the new root freeze. The Java wrapper, read-only JS and original HTML retain their reviewed hashes.

The new production stop helper accepts a total deadline and a shared native-exit grace deadline. The a4 observer now records and transparently forwards those supplied kwargs without recalculating or extending them. Fourteen offline comparisons against the current actual helper passed, including already-expired total budgets, remaining shared grace, normal native exit and no further kill after budget exhaustion. This adapts observation only; the independently fixed SDK-exit-window issue does not establish a cause for a2.

The new gate runs **only after the original restart load receipt has the exact token/count 1, before saving the restart acceptance image**. Nothing is inserted into the first confirmation-to-stop path. AST comparisons in `a4-prepare/review-manifest.json` confirm the original case prefix through its data assertions and the initial target/XML/receipt/readiness/API functions remain unchanged.

## What the gate requires

Each sample contains native `uiautomator` XML and a real `AndroidDriver.screenshot_png()` frame from the same private device. A sample is eligible only when:

- The native result region exposes `已保存标记：<exact token>` and `确认次数：1`; input/EditText nodes, invisible nodes, nodes outside the result area, prefix-matching tokens and counts such as 10 do not qualify.
- The actual 540×960 PNG has the original page's substantial solid blue button and green result border in their expected vertical order. White/invalid frames or isolated color pixels fail.
- Both observations belong to the same sample and arrive within the gate budget.

The selected original PNG/XML pair is saved as the final restart evidence without taking another screenshot. All sample paths, assessments, hashes, timestamps, sample count and total duration remain recorded. Root must still inspect the selected original pixels: native semantics plus page markers do not automatically verify every rendered glyph.

## Bounded observation, no recovery

The total gate budget is 30 seconds. Each native XML observation has an 8-second connection deadline; each real screenshot has a 5-second deadline. Valid but not-yet-drawn samples are observed again within that budget. Native read, identity or connection errors fail immediately. Timeout fails explicitly. The code performs no click, refresh, restart, force-stop, application-storage write or extra synchronization to induce rendering.

Each XML capture uses a unique per-run/per-sample diagnostic path, avoiding an old dump as false evidence. `uiautomator` writes that capture file as part of its normal observation; it is not an application-data operation. A failed gate reuses saved observations and does not invoke the old additional failure-screenshot path.

The existing adbutils 2.12.0 client otherwise permits a 600-second shell timeout and implicit `start-server` on a failed connection. This gate scopes a direct, non-recovering connection factory to the fresh private client's existing device (`127.0.0.1:15037`, `emulator-5666`). It never invokes adb CLI or the library's auto-start branch. A timer closes only this observation's socket objects; the worker is awaited before another sample or cleanup. The factory is restored in `finally`. This does not modify the global ADB singleton or product code.

The actual installed screenshot source was inspected and copied to `a4-prepare/`: it runs native `screencap -p`; invalid image handling may read window size and return a black image, which the gate rejects. It does not install/start a helper. The library source hashes are checked before native execution, recorded with the run and checked again at finish.

## Verification and release status

**39 pure guard tests passed**. They include the actual a3 white restart PNG, actual empty restart XML, actual successful first-phase PNG/XML, input-only token rejection, exact count/token matching, undersized color-marker rejection, bounded fake-socket closure, no connection retry/autostart, exact client/serial checks and factory restoration.

The full 39-test run was repeated after the new product freeze and passed. The temporary 38-test run while the freeze was pending remains recorded separately; its one deferred freeze assertion is now covered. Observer equality evidence is in `a4-prepare/observer-budget-equivalence.json`, and the observer-only source change is in `a4-prepare/stop-budget-observer-only.diff`.

Dry-run created no run directory. No a4 hardware, Server, ADB daemon, package upload or task was started. No a2 AVD operation occurred.

Review package: `a4-prepare/a3-to-a4.diff`, `a4-prepare/review-manifest.json`, `a4-prepare/guard-tests.log`, and `a4-prepare/adbutils-read-path.json`. Independent review and a new explicit single-run hardware release remain required. The a2 persistence failure and a3 incomplete visual result are not superseded by this preparation.
