# Android a4 independent prepare review

Status: **no blocking finding in frozen candidate; root may authorize one native run**. This reviewer did not execute the harness or any native device command.

Frozen Python SHA: `0c156d1ec8783a975bce3626a3db248b2ee4dcb012074690692b120ebdf605ba`. The Java/JS remain the reviewed a3 hashes (`9219cf05...a906d`, `7a2bd86e...80cf`). All candidate hashes were checked against `a4-prepare/review-manifest.json` after review.

The visual gate is called only after the original restarted-page receipt/token/count1 assertion. Independent AST comparison confirmed the case prefix through the original confirmation assertion and the target/XML/receipt/readiness/configuration/build/API functions are unchanged. The original HTML is unchanged.

The gate rejects the actual a3 white frame and empty XML. It checks the saved result inside the green region, excludes EditText/input/invisible nodes, rejects extended tokens and counts such as 10, and requires substantial blue/green markers in the real frame. The selected original PNG/XML pair is preserved without replacing it with a later screenshot. Root pixel inspection remains required; semantic XML and color markers alone do not verify every glyph.

The added reads use the existing fresh driver's private client, exact 127.0.0.1:15037/emulator-5666, and a scoped direct connection factory. The installed adbutils source was inspected: the subclass bypasses its automatic start-server branch; XML uses the same guarded transport rather than CLI. Timeouts close only the observation's socket objects. The synchronous action is awaited to exit before another sample or cleanup, and the factory is restored in finally. The installed screenshot method runs screencap-p and can return a black image on invalid capture; it does not install/start a helper, and the gate rejects that black image.

Budgets are 8 seconds for one XML observation (dump+read), 5 seconds for screenshot, and 30 seconds overall. Valid not-yet-drawn samples may be read again within the overall limit; read/connection/identity errors fail immediately. There are no clicks, refreshes, restarts, force-stops, app-storage writes or synchronization added to induce rendering. Failed visual gates do not invoke the old additional failure-screenshot path.

Independently reran all **39 pure guards**, passed in 4.154 seconds. The copied positive/white PNG fixture hashes match the actual a3 originals. Evidence: `android-a4-independent-checks.json`. No product file or retained a2 disk was modified.

This review does not change a2's unresolved persistence failure, a3's incomplete visual acceptance, or the independent five-versus-twenty-second native shutdown-window finding. The a4 result must be judged on its own actual data and selected original pixels.
