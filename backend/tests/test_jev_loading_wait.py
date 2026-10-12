from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from ai_phone.agent.trajectory_cache.v3_replay import V3ReplayRunner, V3LocatorMiss
from ai_phone.agent.trajectory_cache.jev_click.locator import JevWaitRequested


@pytest.mark.asyncio
async def test_wait_refreshes_frame_and_third_request_has_no_wait(monkeypatch):
    runner=object.__new__(V3ReplayRunner)
    runner.locator=SimpleNamespace(supports_loading_wait=True)
    runner._jev_wait_step=None
    runner._jev_requests_this_step=0
    runner._log_v3_stage=AsyncMock()
    runner._screenshot_jpeg=AsyncMock(side_effect=[b'fresh1',b'fresh2'])
    runner._locate_once=AsyncMock(side_effect=[JevWaitRequested(),JevWaitRequested(),{'type':'click'}])
    sleep=AsyncMock()
    monkeypatch.setattr('ai_phone.agent.trajectory_cache.v3_replay.asyncio.sleep',sleep)
    action={'type':'click','index':1,'action_id':'a1'}
    assert await runner._locate_action(action,b'initial')=={'type':'click'}
    calls=runner._locate_once.call_args_list
    assert [c.kwargs['wait_allowed'] for c in calls]==[True,True,False]
    assert [c.args[1] for c in calls]==[b'initial',b'fresh1',b'fresh2']
    assert sleep.await_count==2
    assert runner._final_before_bytes==b'fresh2'
    runner._locate_once=AsyncMock(return_value={'type':'click'})
    await runner._locate_action({'type':'click','index':2,'action_id':'a2'},b'next')
    assert runner._locate_once.call_args.kwargs['wait_allowed'] is True


@pytest.mark.asyncio
async def test_rejected_wait_cannot_loop_past_third_request(monkeypatch):
    runner=object.__new__(V3ReplayRunner)
    runner.locator=SimpleNamespace(supports_loading_wait=True)
    runner._jev_wait_step='a1'
    runner._jev_requests_this_step=2
    runner._locate_once=AsyncMock(side_effect=JevWaitRequested())
    runner._screenshot_jpeg=AsyncMock()
    with pytest.raises(V3LocatorMiss,match='budget exhausted'):
        await runner._locate_action({'type':'click','index':1,'action_id':'a1'},b'frame')
    runner._screenshot_jpeg.assert_not_called()
