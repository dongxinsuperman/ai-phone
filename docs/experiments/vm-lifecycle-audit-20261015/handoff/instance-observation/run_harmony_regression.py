"""Run Harmony fixtures with the unchanged unified I/O guard, separate evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

evidence_dir = Path(__file__).resolve().parent
handoff = evidence_dir.parent
repo = handoff.parents[3]
label = sys.argv[1] if len(sys.argv) > 1 else "r2"
assert label.isalnum()
selected = sorted(str(path.relative_to(repo)) for path in (repo / "backend/tests").glob("test_harmony_vm*.py"))
if len(sys.argv) > 2 and sys.argv[2] == "agent":
    selected = [
        "backend/tests/test_harmony_vm_instance_observation.py",
        "backend/tests/test_harmony_vm_agent_hardening.py",
        "backend/tests/test_harmony_vm_stop_identity.py",
    ]
runner = handoff / "run_combined_isolated.py"
source = runner.read_text()
print(f"Inherited guard SHA256: {hashlib.sha256(source.encode()).hexdigest()}", flush=True)
selection = 'paths = json.loads((HERE / "combined-test-files.json").read_text())'
assert source.count(selection) == 1
source = source.replace(selection, "paths = SELECTED_TEST_FILES")
source = source.replace('"combined-regression', f'"instance-observation/harmony-regression-{label}')
source = source.replace('"--maxfail=5"', '"--maxfail=20"')
exec(compile(source, str(runner), "exec"), {
    "__name__": "__main__", "__file__": str(runner), "SELECTED_TEST_FILES": selected,
})
