# Android native-exit window and operation budget — implementation handoff

Status: implementation and independent review passed; not committed or run natively by the Android owner.

Only three product files changed: Android manager, Android stop payload, and the existing WS dispatcher’s Android stop/delete default budget. No database/schema, external API, lifecycle state or retry mechanism was added.

- Normal stop forwards the Server’s existing 120-second operation budget. Old Android stop/delete messages also enter the existing WS queue with one 120-second budget. Explicit remaining budgets are preserved; other platforms/probes are unchanged.
- The Agent captures one local deadline before progress send/executor/VM-lock waiting and carries it through identity query, native command, process wait, exit verification and nested deletion. Expiration returns failure and retains ownership evidence; it never starts another force signal or deletion after the deadline.
- Accepted `emu kill` keeps the existing shutdown strategy. An exactly matched process with no override gets the SDK’s 20-second window; a positive decimal integer override is honored within the operation budget. Unknown/unreadable/noninteger/invalid process configuration conservatively uses the remaining bounded budget. The new Agent’s own environment is not used for an adopted process, and whole process environments are never logged.
- One VM’s process handles share a grace deadline covering its last accepted native request. Duplicate PID handles are waited/signaled once. Fast/already-finished processes return promptly. Existing exact AVD/process/port confirmation is unchanged.
- Explicit deletion has one deadline through stop and AVD removal. Failed startup performs only one separate bounded cleanup attempt, so an exhausted startup budget does not prevent reclaiming newly created resources. Existing-instance data is retained; only a proved first-bind creation may be deleted. No startup retry or background continuation was added.

Validation: **307 passed, 2 skipped** across `test_android_vm_stop_budget.py`, existing Android manager reliability, Android VM/API, Agent WS and three-platform lifecycle contracts. Both skips are existing Android cases that do not use the other platforms’ asynchronous Server probe. The independent reviewer reran the stop-budget/manager/WS subset: **186 passed, 0 skipped**, matching frozen hashes. `git diff --check` passed. Full hashes and logs are in `android-stop-grace-implementation.json`, `android-stop-grace-tests.log`, and `android-stop-independent-final*`.

The existing a4 observation wrapper must transparently forward the new internal `deadline`/`grace_deadline` keyword arguments and be rechecked/refrozen before root authorizes native execution. Historical a2/a3 artifacts must remain unchanged.

This repairs the independently established premature-intervention window. It does not establish or repair the cause of a2’s missing localStorage record. It does not introduce `svc power shutdown` or change the meaning of app-level persistence acknowledgments.
