# Android native graceful-exit window: independent gap

Read-only review; product source remains unchanged. This is not an attribution of a2.

## Finding

The actual SDK 36.6.11 logs in a3 announce a 20-second graceful shutdown window and the `ANDROID_EMULATOR_WAIT_TIME_BEFORE_KILL` override. Current `backend/ai_phone/agent/android_vm/manager.py` `_stop_owned_process` at line 1392 waits only five seconds after an accepted `emu kill`, then sends terminate and eventually kill. A normally exiting process taking between five and twenty seconds can therefore be interrupted by the Agent before the SDK's own window expires. Snapshot save and disk cleanup are plausible affected work, but no such interruption was measured in a2.

`android-stop-grace-counterexample.json` is an isolated fake-process demonstration using the actual production helper: a process modeled to exit normally at second eight receives terminate at second five. Changing only the grace wait to twenty in a temporary AST (not a product edit) allows normal exit at second eight without a signal. No real time delay or native process was used.

In actual a3 both `emu kill` requests returned zero and both process waits returned zero in about 0.852/1.157 seconds, with no terminate/kill fallback. Those facts do not reconstruct a2's unrecorded branch.

## Smallest justified change

Keep the current exact-AVD ownership checks, `emu kill`, exit/port confirmation, terminate/kill fallback and status schema. Correct only the grace interval after the native request was accepted; an already exited process returns immediately. When several exact-owned process handles are found, use one shared grace deadline for the VM rather than restarting a full window for every handle.

The effective SDK override must be considered. The SDK reports the default and override in its real log; Android's own Gradle emulator launcher also passes that variable when it needs a longer shutdown window: https://android.googlesource.com/platform/tools/base/+/HEAD/build-system/gradle-core/src/main/java/com/android/build/gradle/internal/AvdSnapshotHandler.kt . A validated finite nonnegative/positive override policy must be explicit; malformed or excessively large values must not make shutdown unbounded. The exact SDK parser behavior for invalid values was not established here. An already-running adopted emulator may have inherited a different environment from the newly restarted Agent, so current Agent environment alone is not proof of that process's original override. Do not add new privilege requirements just to read arbitrary process environments.

## Budget boundary that cannot be hidden

The Server's normal stop operation timeout is 120 seconds (`server/vm_lifecycle.py:328`), but Android stop payload currently omits `operation_timeout_sec` (`server/android_vm/api.py:748`), and `handle_stop`/`handle_delete` do not propagate a native deadline (`manager.py:160`, `:177`). The start-failure cleanup call at `manager.py:497` likewise does not pass the start deadline to stop/delete.

Consequently a default grace increase from 5 to 20 seconds leaves the normal single-process path well inside 120 seconds, but it does not establish a strict end-to-end timeout guarantee. Existing per-phase limits include identity/command waits, terminate 5 seconds, kill 5 seconds, exit confirmation 5 seconds, and AVD delete 30 seconds. Custom large SDK windows or multiple processes cannot simply be added on top.

If strict overall budgeting is required for this change, reuse the existing optional operation timeout contract: capture one local deadline before executor/lock waiting, carry it through stop/delete/failed-start cleanup, and bound the grace phase by the remaining budget while retaining room for existing cleanup/verification. This is deadline plumbing, not a new lifecycle state or retry system. If budget or ownership prevents confirmation, retain runtime/AVD evidence and report failure; never release the slot or claim deletion early. A full native grace window and a smaller already-exhausted total deadline cannot both be promised.

First-binding rollback must still delete only the AVD actually created in that attempt and only after confirmed exit. An existing-instance restart failure must still preserve the AVD. Neither path gets a new background retry. A longer graceful wait changes latency, not those ownership/deletion rules.

## Minimal isolated regression set

1. Accepted native stop, normal exit at 8 seconds: no terminate/kill before the effective native window.
2. Already exited or fast exit: returns immediately without sleeping out the window.
3. Native command rejected/unavailable: existing exact-owned fallback behavior remains unchanged.
4. Deadline/override cases: default, supported explicit override, malformed/negative/nonfinite/huge input, and less remaining time than the native window; no timeout reset while moving between process handles.
5. Native grace expires: preserve the existing bounded terminate/kill/verify order; unconfirmed resources remain tracked.
6. First-created rollback versus existing-instance restart failure: only the former may delete, exactly once after confirmed stop; timeout does not turn unknown cleanup into success.

No live run or product implementation was made for this finding.
