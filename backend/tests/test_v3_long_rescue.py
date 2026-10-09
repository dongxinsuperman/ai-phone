"""Independent rescue handoff: current scene, factual progress, one terminal event."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from ai_phone.agent.runner import long_rescue, vlm_loop
from ai_phone.agent.runner.long_rescue_prompts import build_system_prompt_for_backend
from ai_phone.agent.runner.events import EVT_RUN_FINISH, EVT_STEP_START, make_event
from ai_phone.agent.trajectory_cache import orchestrate, v3_replay
from ai_phone.agent.trajectory_cache.restart import V3TakeoverRequest, restart_report_event
from ai_phone.config import Settings
from tests.test_v3_restart import Bridge, SNAPSHOT
from tests.test_vlm_runner import FakeDriver, ScriptedStep, ScriptedVLMClient

GOAL = '测试标题：完成流程\n前置条件：关闭并重新打开「洋葱学园」\n操作步骤：领取奖励；打开记录。\n预期结果：显示记录页'
MAP = '完整Map：领取按钮只可使用一次；记录入口在页面右上方。'
HISTORY = [
    {'index': 3, 'source': 'cache', 'runtime_status': 'completed_without_exception',
     'action': {'type': 'click', 'plan_intent': '领取奖励', 'point': {'x': 400, 'y': 500}}},
    {'index': 4, 'source': 'rescue_repair', 'runtime_status': 'completed_without_exception',
     'action': {'type': 'press_back'}, 'reason': '退出遮挡层'},
    {'index': 4, 'source': 'rescue_continue', 'runtime_status': 'skipped',
     'action': {'type': 'click', 'plan_intent': '旧计划未执行'}, 'reason': '当时认为可以继续'},
]


def handoff():
    return V3TakeoverRequest(reason='v3_rescue_limit_exceeded limit=5', step_offset=4, elapsed_ms=500,
                             failed_action={'index': 4, 'plan_intent': '打开记录入口'},
                             next_action={'index': 5, 'plan_intent': '查看记录详情'},
                             execution_history=deepcopy(HISTORY))


@pytest.mark.parametrize('backend', ['doubao_responses', 'claude_cu', 'gpt_cu'])
@pytest.mark.parametrize('zh', [False, True])
def test_independent_prompts_keep_task_protocol_and_live_start(backend, zh):
    text = build_system_prompt_for_backend(GOAL, backend=backend, zh_readable=zh,
                                         substeps_text='1. 领取奖励\n2. 打开记录', function_map_context=MAP)
    assert '独立长程救援执行器' in text and GOAL in text and '2. 打开记录' in text
    assert '首轮固定为子步骤 1' not in text
    assert "first turn is fixed at substep 1" not in text
    assert 'seed:tool_call' in text if backend=='doubao_responses' else 'computer' in text


@pytest.mark.asyncio
async def test_orchestrator_passes_current_failure_and_actual_history_without_terminal(monkeypatch):
    class Replay:
        def __init__(self, **kwargs): self.emit = kwargs['emit']
        @property
        def execution_history(self): return deepcopy(HISTORY)
        async def run(self):
            self.emit(make_event(EVT_STEP_START, 'unit', step=4))
            return v3_replay.V3ReplayResult(success=False, actions_total=5, actions_executed=3,
                                           failed_index=4, error='limit=5', restart_required=True,
                                           takeover_required=True)
    monkeypatch.setattr(v3_replay, 'V3ReplayRunner', Replay)
    snapshot = deepcopy(SNAPSHOT)
    snapshot['actions'] = [{'index':4,'plan_intent':'当前卡点'}, {'index':5,'plan_intent':'后续计划'}]
    bridge=Bridge()
    request=await orchestrate.run_v3_replay(run_id='unit',serial='fake',goal=GOAL,attempt=1,
        driver=object(),bridge=bridge,snapshot=snapshot,settings=SimpleNamespace(vlm_backend='doubao_responses'),
        function_map_context=MAP,restart_on_rescue_failure=True)
    assert isinstance(request,V3TakeoverRequest)
    assert request.failed_action['plan_intent']=='当前卡点'
    assert request.next_action['plan_intent']=='后续计划'
    assert request.execution_history==HISTORY
    assert bridge.done==[] and len(bridge.suspects)==1
    context=request.prompt_context()
    assert '未确认完成' in context and '不是业务子步骤编号' in context
    assert 'skipped' in context and 'rescue_repair' in context


def test_takeover_report_does_not_double_offset_steps():
    event=make_event(EVT_RUN_FINISH,'unit',step=6,steps=6,elapsed_ms=50,ok=True)
    out=restart_report_event(event,handoff())
    assert out['step']==out['steps']==6 and out['elapsed_ms']==550
    assert event['elapsed_ms']==50


@pytest.mark.asyncio
async def test_independent_rescue_runs_from_live_scene_with_whole_case_assertion(monkeypatch):
    settings=Settings(_env_file=None,vlm_page_stable_enabled=False,transient_ui_enabled=False)
    monkeypatch.setattr(long_rescue,'get_settings',lambda:settings)
    driver=FakeDriver()
    model=ScriptedVLMClient([ScriptedStep('打开尚未完成的记录入口', "click(point='<point>800 200</point>')"),
                             ScriptedStep('所有剩余目标完成', "finished(content='记录页已显示')")])
    events=[]; assertions=[]
    runner=long_rescue.LongRescueRunner('unit',driver,GOAL,takeover=handoff(),
                                      vlm_client=model,assistant=object(),emit=events.append,function_map_context=MAP)
    async def substeps(): return '1. 领取奖励\n2. 打开记录'
    async def forbidden(*args,**kwargs): pytest.fail('普通主循环或冷启动不应进入接管路径')
    async def verify(**kwargs):
        assertions.append(runner._format_finished_assertion_history(100))
        return 'PASS','完整任务验收通过'
    monkeypatch.setattr(runner,'_extract_struct_substeps',substeps)
    monkeypatch.setattr(runner,'_run_app_lifecycle_prelude',forbidden)
    monkeypatch.setattr(runner,'_verify_finished_assertion',verify)
    monkeypatch.setattr(vlm_loop.VLMRunner,'run',forbidden)
    result=await runner.run()
    assert result.ok and result.steps==6
    assert [e['step'] for e in events if e['type']==EVT_STEP_START]==[5,6]
    assert len(model.received_screenshots)==2
    assert not any(c[0] in {'open_app','close_app'} for c in driver.calls)
    assert '领取奖励' in assertions[0] and 'press_back' in assertions[0] and '旧计划未执行' in assertions[0]
    assert '缓存阶段记录' in assertions[0] and 'skipped' in assertions[0]
    assert GOAL in model.system_prompt and '2. 打开记录' in model.system_prompt
    assert '当前截图是接管后的最新现场' in model.system_prompt
    assert '打开记录入口' in model.system_prompt
    assert runner._function_map_context==MAP
    assert not issubclass(long_rescue.LongRescueRunner,vlm_loop.VLMRunner)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',[None,'init','cancel'])
async def test_agent_selects_separate_rescue_and_never_archives_suffix(monkeypatch,failure):
    import asyncio
    from ai_phone.agent import main as agent_main
    bridge=Bridge();scheduled=[];seen=[];started=asyncio.Event()
    class Client:
        server_http_base='http://unit.invalid'
        async def send(self,message):return True
    async def replay(**kwargs):return handoff()
    class Rescue:
        def __init__(self,**kwargs):
            seen.append(kwargs)
            if failure=='init':raise RuntimeError('init test')
            self.emit=kwargs['emit']
        async def run(self):
            self.emit(make_event(EVT_STEP_START,'unit',step=5))
            if failure=='cancel':
                started.set();await asyncio.Event().wait()
            self.emit(make_event(EVT_RUN_FINISH,'unit',steps=5,elapsed_ms=50,ok=True,reason='完成'))
    monkeypatch.setattr(orchestrate,'run_v3_replay',replay)
    monkeypatch.setattr(long_rescue,'LongRescueRunner',Rescue)
    monkeypatch.setattr(agent_main,'build_runner',lambda **kw:pytest.fail('不能调用普通主执行工厂'))
    monkeypatch.setattr(agent_main,'RunnerBridge',lambda **kw:bridge)
    monkeypatch.setattr(agent_main,'has_runtime_override',lambda:True)
    monkeypatch.setattr(agent_main,'get_settings',lambda:SimpleNamespace(android_wake_before_run=False))
    monkeypatch.setattr(agent_main,'_get_or_open_driver',lambda serial:SimpleNamespace(platform='android'))
    monkeypatch.setattr(agent_main,'_schedule_cache_archive',lambda **kw:scheduled.append(kw))
    supervisor=agent_main._RunSupervisor()
    await agent_main._handle_start_run(Client(),supervisor,dict(run_id='unit',device_serial='fake',goal=GOAL,
        function_map_context=MAP,cache_mode='v3',cache_snapshot=deepcopy(SNAPSHOT),should_sleep_after_run=False))
    task=supervisor.get('unit')['task']
    if failure=='cancel':
        await asyncio.wait_for(started.wait(),1);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    else:await task
    assert len(seen)==1 and seen[0]['goal']==GOAL and seen[0]['function_map_context']==MAP
    assert len(bridge.done)==1 and scheduled==[]
    assert bridge.done[0]['steps']==(4 if failure=='init' else 5)
    assert bridge.done[0]['elapsed_ms']>=500
    assert bridge.closed and supervisor.get('unit') is None
