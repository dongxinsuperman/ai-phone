# a6-a2 independent cause and stop-semantics review

Scope: read-only review of the sealed personal a6-a2 evidence, two exact SDK process crash reports, current product source, and an isolated replay of the actual config writer on a new temporary file. No emulator/ADB command was executed and no sealed AVD file was modified.

Baseline: `f263af3a0b8ed6aea556c63354747a79e52a4f86`; Android manager SHA-256 `a9fc524cf2206baa1295557e936a05781d4d584e96e334e685f3e01c84e1a5db`. Independent replay evidence: `/tmp/a6-config-cause-independent.json`.

## Confirmed configuration defect

The two native launch argument lists are byte-for-byte equivalent. First-stop `config.ini` records `disk.dataPartition.size=6442450944`; second-start records `4096M`. After parsing whitespace, this is the only differing config key. The generated hardware value correspondingly changes from `6g` to `4g`; the first-boot `disk.dataPartition.initPath` also disappears.

Replaying the **actual** `_write_avd_screen_config` function AST against a temporary copy of the first-stop configuration, with the unchanged request values, reproduces that exact sole semantic difference. The product unconditionally writes the requested internal-storage size on every existing-AVD start. This establishes the product mutation; it does not establish that the on-disk userdata image was physically truncated.

The second-start SDK log, lines 212–217, explicitly rejects the snapshot hardware/configuration and starts from scratch. The captured boot ID changes, and the old marker PID now belongs to a different process. Therefore the observed attempt did not restore QuickBoot RAM state. This is a product/configuration defect, not an empty-snapshot or PID-only fixture false positive.

The [official snapshot documentation](https://developer.android.com/studio/run/emulator-snapshots) describes configuration changes as invalidating saved state. The [AOSP hardware comparator at fixed revision aca144a9](https://android.googlesource.com/platform/external/qemu/+/aca144a9e9264b11c2d729096af90d695d01455d/android/android-emu/android/snapshot/Snapshot.cpp) ignores the first-boot data init path but compares the data-partition size. That public revision supports the mechanism; it is not presented as a byte-identical source release of the installed SDK. A new controlled run after the correction must still prove actual snapshot restoration.

## Smallest correction boundary

- For an existing AVD, parse a provable current capacity before any mutation. Preserve its original representation when the request is equal or smaller; do not shrink it or rewrite SDK-normalized bytes to a smaller requested value.
- Permit a provably larger explicit request through the existing growth path. Such a real hardware change does not promise compatibility with a prior snapshot.
- Unknown or ambiguous existing capacity should fail before writes and preserve old data/process ownership. It must not enter a generic startup-failure cleanup that stops an unrelated pre-existing runtime.
- Leave new-instance creation, snapshot flags, Server/API/DB contracts and real-device handling unchanged. Do not hard-code the observed 6 GiB as a universal SDK minimum.
- Describe retained existing capacity accurately: a smaller requested value is not proof that the actual disk became smaller. Avoid claiming same-ID image builds or every persistent byte are verified by this change.

## The two `-6` exits are a separate SDK behavior

Exact crash reports for PID 71919 and 72067 both identify SIGABRT in `abort <- skin_winsys_quit_request+572`; both use SDK image UUID `4c4c4415-5555-3144-a1d3-56bd0670f55d`. Their timestamps match the recorded waits. The first exit happens before the second native launch, so the second-start configuration rewrite cannot explain the first abort.

The public headless implementation contains an intentional ARM snapshot exit path that calls `abort()` because this macOS path lacks its intended quick-exit primitive. The control-health reviewer additionally matched this path to the installed binary instructions. The completion message/flag is not sufficient proof that a snapshot was successfully saved or is loadable.

Recommendation: do **not** change ordinary stop to fail solely because these two observed exit codes are `-6`, and do not introduce a general `-6` success whitelist. This run has separate evidence that processes exited and ports were released; reporting the VM as stopped is supported. The existing exit-code evidence should remain visible. Neither `stop succeeded`, a completion log, nor snapshot-file presence proves successful future restoration. Keep that acceptance in the real snapshot matrix. No new exit policy, recovery loop, or SDK workaround is justified by this run alone.

## Corrective candidate review

The subsequent manager candidate SHA-256 `4008ca8b5533712e2af251db8ae48b091b7fec3f03ee8816e339aeedc5cc78fc` implements the above limited rule. The protection flag is passed only for an existing AVD; creation keeps its previous sizing path. Capacity rejection uses the private prelaunch configuration-error path, which does not stop/delete an old VM and removes only this attempt's placeholder after a read-only exit check. Stop logic and snapshot flags are unchanged.

Independent execution of `test_android_vm_storage_size.py` and `test_android_vm_existing_image.py`: **104 passed in 3.99s**; XML result `/tmp/android-storage-independent.xml`. The SDK-compatible positive bytes/K/M/G forms are handled conservatively; zero, ambiguous syntax and signed-64-bit overflow are rejected rather than inheriting the SDK parser's permissive prefix/truncation behavior.

An additional actual-writer AST replay used only a temporary copy of the captured first-stop config. Requests 0, 4096 and 6144 MiB retained identical bytes, inode and modification time; 8192 MiB used the existing `8192M` write contract. A separate userdata witness remained unchanged. Unknown/zero/unsupported/overflow values failed before writing, and 11 unit/int64 boundary checks passed. Results: `/tmp/android-storage-independent-after.json`. The sealed original evidence remained byte-identical.

No P1/P2 was found in this candidate's reviewed scope. This is approval for regression/freeze and a separately authorized new native test; actual QuickBoot restoration is still unverified for the candidate.
