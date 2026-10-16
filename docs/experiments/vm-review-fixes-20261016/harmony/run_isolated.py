"""Reuse the review I/O guard for explicitly selected nodeids only."""
from pathlib import Path
import sys

here = Path(__file__).resolve().parent
label = sys.argv[1] if len(sys.argv) > 1 else ""
tests = sys.argv[2:]
if not label.isalnum() or not tests:
    raise SystemExit("Usage: run_isolated.py LABEL EXACT_NODEID [EXACT_NODEID ...]")
runner = here.parent / "run_regression.py"
source = runner.read_text()
start = source.index("    paths = ")
end = source.index("\n", start)
source = source[:start] + "    paths = SELECTED_TESTS" + source[end:]
source = source.replace('HERE = Path(__file__).resolve().parent', 'HERE = Path(__file__).resolve().parent / "harmony"')
source = source.replace('REPO = HERE.parents[2]', 'REPO = HERE.parents[3]')
source = source.replace('"regression.xml"', f'"regression-{label}.xml"')
source = source.replace('"regression-evidence.json"', f'"regression-{label}-evidence.json"')
sys.argv = [str(runner), *tests]
exec(compile(source, str(runner), "exec"), {"__name__": "__main__", "__file__": str(runner), "SELECTED_TESTS": tests})
