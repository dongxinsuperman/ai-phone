"""Reuse the full handoff I/O guard with only test selection/output changed."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys


handoff = Path(__file__).resolve().parent.parent
repo = handoff.parents[3]
mode = sys.argv[1] if len(sys.argv) > 1 else "all"
assert mode in {"focused", "all"}
if mode == "focused":
    selected = [
        "backend/tests/test_harmony_vm_identity.py",
        "backend/tests/test_harmony_vm_recovery.py",
    ]
else:
    selected = sorted(str(path.relative_to(repo)) for path in (repo / "backend/tests").glob("test_harmony_vm*.py"))
runner = handoff / "run_combined_isolated.py"
source = runner.read_text()
print(f"Inherited guard SHA256: {hashlib.sha256(source.encode()).hexdigest()}", flush=True)
selection = 'paths = json.loads((HERE / "combined-test-files.json").read_text())'
assert source.count(selection) == 1
source = source.replace(selection, "paths = SELECTED_TEST_FILES")
source = source.replace('"combined-regression', f'"harmony-fixture-{mode}')
exec(compile(source, str(runner), "exec"), {
    "__name__": "__main__", "__file__": str(runner), "SELECTED_TEST_FILES": selected,
})
