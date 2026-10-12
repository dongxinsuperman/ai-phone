import asyncio
import io

from PIL import Image

from ai_phone.agent.trajectory_cache.fast_stability import FastStabilityPolicy, wait_fast_stable


def frame(color):
    stream = io.BytesIO()
    Image.new('RGB', (240,108), color).save(stream,format='JPEG')
    return stream.getvalue()


def test_waits_through_change_and_returns_exact_final_frame():
    before, after = frame('white'), frame('black')
    async def run():
        calls=0
        async def shot():
            nonlocal calls
            calls+=1
            return before if calls==1 else after
        result=await wait_fast_stable(shot,frame_a_bytes=before,policy=FastStabilityPolicy(timeout_s=.3, interval_s=.01))
        assert result.stable and result.bytes_ is after and calls==3
    asyncio.run(run())


def test_fresh_equal_pair_returns_immediately():
    data=frame('white')
    async def run():
        async def shot(): return data
        result=await wait_fast_stable(shot,frame_a_bytes=data,policy=FastStabilityPolicy(timeout_s=.2, interval_s=.01))
        assert result.stable and result.bytes_ is data and result.checks==2 and result.elapsed_ms<150
    asyncio.run(run())


def test_slow_capture_is_bounded():
    async def run():
        async def shot(): await asyncio.sleep(1)
        result=await wait_fast_stable(shot,policy=FastStabilityPolicy(timeout_s=.03))
        assert not result.stable and result.elapsed_ms<150
    asyncio.run(run())


def test_runner_uses_waiter_frame_without_fixed_sleep():
    from ai_phone.agent.trajectory_cache.v3_replay import V3ReplayRunner
    from ai_phone.agent.runner.stability import StabilityResult
    runner=object.__new__(V3ReplayRunner)
    data=frame('black')
    runner._last_frame=frame('white')
    async def unwanted_capture(): raise AssertionError('duplicate capture')
    runner._screenshot_jpeg=unwanted_capture
    async def waiter(screenshot, *, frame_a_bytes):
        assert frame_a_bytes is runner._last_frame
        return StabilityResult(data,True,900,5)
    runner.stability_waiter=waiter
    async def run():
        result=await runner._wait_stable_result()
        assert result.bytes_ is data
        await runner._observe_after_action()
    asyncio.run(run())


def test_color_change_with_similar_luminance_and_small_area():
    from ai_phone.agent.trajectory_cache.fast_stability import pixel_change_ratio
    before=Image.new("RGB", (480,216), (255,0,0))
    after=before.copy()
    after.paste((0,130,0), (20,20,40,40))
    assert pixel_change_ratio(before,after) > .001
    noise=Image.new("RGB", before.size, (250,3,3))
    assert pixel_change_ratio(before,noise)==0


def test_platform_interval_only_changes_harmony():
    from dataclasses import asdict
    from ai_phone.agent.trajectory_cache.fast_stability import policy_for_platform
    android=asdict(policy_for_platform('android'))
    harmony=asdict(policy_for_platform('harmony'))
    assert android.pop('interval_s')==.1
    assert harmony.pop('interval_s')==0
    assert android==harmony
    assert harmony['timeout_s']==4


def test_ios_has_independent_sampling_interval_and_same_limits():
    from ai_phone.agent.trajectory_cache.fast_stability import policy_for_platform
    policy=policy_for_platform('ios')
    assert policy.interval_s==.2
    assert policy.timeout_s==4 and policy.pixel_delta==12 and policy.change_ratio==.001
