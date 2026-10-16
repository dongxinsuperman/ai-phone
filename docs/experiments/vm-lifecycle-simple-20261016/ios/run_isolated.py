import json,os,sys
from pathlib import Path
sys.path.insert(0,os.environ["REVIEW_HELPER_DIR"])
import run_main_flow_isolated as runner
runner.HANDOFF=Path(os.environ["REVIEW_OUTPUT_DIR"])
root=Path(os.environ["AIPHONE_AUDIT_SOURCE_ROOT"])
files=["test_ios_sim_manager.py","test_ios_sim_instance_observation.py","test_ios_simulator_wda.py","test_ios_sim_agent_wiring.py"]
code=runner.run(only_tests=[str(root/"backend/tests"/name) for name in files],prefix="ios-observation")
p=runner.HANDOFF/"ios-observation-isolation.json"
d=json.loads(p.read_text());d["harness_default_base_commit"]=d["base_commit"];d["base_commit"]=os.environ["REVIEW_BASE_COMMIT"]
p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n")
raise SystemExit(code)
