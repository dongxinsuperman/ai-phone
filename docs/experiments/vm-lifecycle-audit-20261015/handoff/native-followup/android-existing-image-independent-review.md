# Android existing AVD image guard — independent review

Reviewed on 2026-10-16 against the uncommitted candidate following `e26e3d77e88b6a769ce80ccd86807e92ec7c881f`.

- Product: `backend/ai_phone/agent/android_vm/manager.py`, SHA-256 `a9fc524cf2206baa1295557e936a05781d4d584e96e334e685f3e01c84e1a5db`.
- Tests: `backend/tests/test_android_vm_existing_image.py`, SHA-256 `0e3baa011705f76b55cf92d320999cbea88bf2f85c8ed21b730a96b5ffe571d5`.
- Independent execution: `.venv/bin/python -m pytest -q tests/test_android_vm_existing_image.py --junitxml=/tmp/android-existing-image-independent.xml`, from the personal worktree's `backend` directory: **52 passed in 1.84s**. Test fixtures use temporary SDK/AVD files and fake subprocess/process/port operations. No native command, device, external network or company resource was used.
- `git diff --check` passed. Product/test SHA-256 values were unchanged after the run.

## Result

No reproducible P1/P2 found in this change. The source candidate can proceed to the parent's final regression and artifact freeze; this review is not native acceptance and does not by itself authorize running a5 against an obsolete freeze.

The existing-AVD path checks actual list `Name`/`Path`, the configuration that would be written, unique `image.sysdir.1`, and the resolved image's unique SDK `localPackage` identity **before** hardware/screen writes (`manager.py:1092–1114`, `1548–1597`). Ambiguous or missing metadata fails instead of guessing or rebuilding. Relative SDK paths, absolute paths, Windows path syntax, symlinks and `..` are covered in the isolated tests.

The ready/reclaimed runtime fast path also checks image identity through the native AVD index before replacing its prior operation deadline/request ID (`manager.py:313–337`). A mismatch or unprovable path preserves the original runtime and disk, and does not enter stop/delete cleanup.

The prelaunch mismatch branch only removes the placeholder created by this attempt. It keeps positive inventory evidence, reports `created_in_attempt=False`, and sets `cleanup_confirmed` from the existing read-only process/port exit check rather than assuming a missing Popen means the old VM is stopped (`manager.py:502–507`). Tests cover both a confirmed exit and an incomplete process inventory, and restoration of the correct effective package can start the original disk.

First-bind name conflicts still fail before image adoption; new AVD creation still passes the package ID to `avdmanager`. The diff does not change snapshot flags, normal-stop sync behavior, Server PATCH/API/schema, or real-device handling.

## Exact limits

- This establishes SDK **package ID** agreement, not binary/build/revision equivalence under the same ID.
- Missing metadata, an index without one absolute path, or a custom native AVD directory differing from the existing managed writer path is deliberately rejected with existing data retained; no directory migration is attempted.
- Windows syntax is covered offline. This review did not run Windows, an emulator, or the a5 native case.
- The a5 harness remains subject to the new final product freeze and guard rerun. Its prior `e26e3d7`/`c609f1b6` manifest is not the reviewed image-guard candidate.
