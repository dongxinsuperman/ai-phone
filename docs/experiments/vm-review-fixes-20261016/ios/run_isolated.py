import argparse, json, os, sys
from pathlib import Path
root = Path(os.environ["AIPHONE_AUDIT_SOURCE_ROOT"])
sys.path.insert(0, str(root / "docs/experiments/vm-lifecycle-audit-20261015/handoff"))
import run_main_flow_isolated as runner
runner.HANDOFF = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--prefix", default="ios-stop-budget")
parser.add_argument("--node", action="append")
args = parser.parse_args()
files = ["test_ios_sim_stop_budget.py", "test_ios_sim_manager.py", "test_ios_sim_instance_observation.py", "test_ios_simulator_wda.py", "test_ios_sim_agent_wiring.py"]
selected = args.node or [str(root / "backend/tests" / name) for name in files]
code = runner.run(only_tests=selected, prefix=args.prefix)
p = runner.HANDOFF / (args.prefix + "-isolation.json")
data = json.loads(p.read_text())
data["harness_default_base_commit"] = data["base_commit"]
data["base_commit"] = "578fb711f0d26b1ba7aa29aeefe6b8a208ff9847"
p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
raise SystemExit(code)
