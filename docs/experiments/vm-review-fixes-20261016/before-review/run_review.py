"""Read-only product review runner: no real network/native commands or dotenv."""
import hashlib,json,os,shutil,sys,tempfile,time
from pathlib import Path
ROOT=Path("/Users/dongxin/代码文件/sonic合集/ai-phone")
OUT=Path(__file__).resolve().parent
RUNTIME=Path(tempfile.mkdtemp(prefix="runtime-",dir=OUT))
os.chdir(RUNTIME)
for key in list(os.environ):
    if key.startswith("AI_PHONE_") or key.upper().endswith("_PROXY"): os.environ.pop(key)
os.environ.update(AI_PHONE_DB_URL="sqlite+aiosqlite:///"+str(RUNTIME/"fixture.db"),
    AI_PHONE_STORAGE_DIR=str(RUNTIME/"storage"),AI_PHONE_AGENT_TOKEN="dev",
    AI_PHONE_SUBMISSION_INTERNAL_TOKEN="dev",PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    REVIEW_CASE_OUT=str(OUT),TMPDIR=str(RUNTIME))
tempfile.tempdir=str(RUNTIME)
sys.path.insert(0,str(ROOT/"backend"))
paths=json.loads((ROOT/"docs/experiments/vm-lifecycle-simple-20261016/regression-evidence.json").read_text())["source_sha256"]
hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in paths}
blocked=[]
def audit(event,args):
    if event in {"subprocess.Popen","os.system","os.posix_spawn","os.exec","os.kill","os.killpg","socket.connect","socket.connect_ex"}:
        blocked.append({"event":event})
        raise PermissionError("Review denied "+event)
    if event=="socket.bind" and isinstance(args[1],tuple):
        blocked.append({"event":event,"address":str(args[1])})
        raise OSError("Review denied network bind")
    if event=="open" and isinstance(args[0],(str,bytes,os.PathLike)):
        p=Path(os.fsdecode(args[0]))
        if p.name in {".env",".env.local",".env.defaults"}:
            blocked.append({"event":"dotenv","path":str(p)})
            raise PermissionError("Review denied dotenv")
sys.addaudithook(audit)
from ai_phone.config import Settings,get_settings
Settings.model_config["env_file"]=None
get_settings.cache_clear()
import pytest
reports=[]
class Collector:
    def pytest_runtest_logreport(self,report):
        if report.when=="call" or report.failed:
            reports.append({"test":report.nodeid,"phase":report.when,"outcome":report.outcome})
started=time.monotonic()
code=pytest.main(["-p","pytest_asyncio.plugin","-p","anyio.pytest_plugin","-p","tests.conftest",
    "-c",str(ROOT/"backend/pyproject.toml"),"-o","asyncio_default_fixture_loop_scope=function",
    "--basetemp",str(RUNTIME/"pytest"),"-q","--tb=short","--junitxml="+str(OUT/"review.xml"),
    str(OUT/"test_findings.py")],plugins=[Collector()])
unchanged=all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==v for p,v in hashes.items())
evidence={"commit":"578fb711f0d26b1ba7aa29aeefe6b8a208ff9847","pytest_exit_code":int(code),
    "source_sha256":hashes,"source_unchanged":unchanged,"blocked_calls":blocked,
    "reports":reports,"duration_seconds":round(time.monotonic()-started,3),
    "scope":"personal isolated SQLite, fake native commands; existing services and VMs untouched"}
shutil.rmtree(RUNTIME)
evidence["runtime_removed"]=not RUNTIME.exists()
(OUT/"evidence.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
print(json.dumps({"source_unchanged":unchanged,"runtime_removed":evidence["runtime_removed"],
    "counts":{x:sum(r["outcome"]==x for r in reports) for x in ("passed","failed","skipped")},
    "evidence":str(OUT/"evidence.json")}))
raise SystemExit(code)

