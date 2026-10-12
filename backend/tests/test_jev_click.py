"""V3 Jev contracts: no real devices, network, private config or paid model calls."""

import asyncio
import io
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from ai_phone.config import Settings, build_downlink_config, downlink_field_names
from ai_phone.agent.trajectory_cache.jev_click.client import DecisionsClient, JevConfig, JevError
from ai_phone.agent.trajectory_cache.jev_click.adapters.base import TreeAdapter
from ai_phone.agent.trajectory_cache.jev_click.adapters.ios import IosAdapter
from ai_phone.agent.trajectory_cache.jev_click.adapters.harmony import HarmonyAdapter
from ai_phone.agent.trajectory_cache.jev_click.locator import JevClickLocator
from ai_phone.agent.trajectory_cache.jev_click.factory import create_locator
from ai_phone.agent.trajectory_cache.jev_click.types import redact, tree_data
from ai_phone.agent.trajectory_cache.v3_replay import V3LocatorMiss, V3PlanLocator, V3ReplayRunner


XML = '<hierarchy><node text="继续" clickable="true" enabled="true" bounds="[20,30][80,90]"/></hierarchy>'


def settings(**kwargs):
    return Settings(_env_file=None, jev_api_key="test-secret", **kwargs)


class Driver:
    platform = "android"
    serial = "only-this-device"

    def __init__(self):
        self.size = (100, 200)
        self.orientation = 0
        self.screenshots = 0
        self.xml = XML
        self.clicks = []

    def window_size(self):
        return self.size

    def rotation(self):
        return self.orientation

    def screenshot_jpeg(self, *args):
        self.screenshots += 1
        out = io.BytesIO()
        Image.new("RGB", self.size, "white").save(out, "JPEG")
        return out.getvalue()

    def click(self, x, y):
        self.clicks.append((x, y))


class Adapter(TreeAdapter):
    def __init__(self, driver):
        super().__init__(driver)
        self.initializations = self.closes = self.tree_reads = 0

    def initialize(self):
        self.initializations += 1

    def close(self):
        self.closes += 1

    def tree(self, size):
        self.tree_reads += 1
        return self.driver.xml


def params(kind="click"):
    return {
        "goal": "完整任务",
        "trajectory": {},
        "action": {
            "type": kind,
            "index": 1,
            "action_id": "a-1",
            "plan_intent": "点击继续",
            "point": {"x": 1, "y": 1},
        },
        "screenshot_bytes": Driver().screenshot_jpeg(),
        "image_size": (100, 200),
        "window_size": (100, 200),
    }


def locator(*, choice="N1", mutate=None, emit=None, provider="openrouter"):
    driver = Driver()
    adapter = Adapter(driver)
    client = SimpleNamespace(aclose=AsyncMock())

    async def choose(*args):
        if mutate:
            mutate(driver)
        return {"choice": choice}

    client.choose = AsyncMock(side_effect=choose)
    s = settings(jev_provider=provider)
    loc = JevClickLocator(
        adapter=adapter,
        config=JevConfig.from_settings(s),
        settings=s,
        main_vlm_backend="doubao_responses",
        client=client,
        emit=emit,
        run_id="unit",
    )
    return loc, driver


@pytest.mark.parametrize(
    "provider,model,url",
    [
        ("openrouter", "openai/gpt-6-luna-decisions", "https://openrouter.ai/api/alpha/decisions"),
        ("openai", "gpt-6-luna", "https://api.openai.com/v1/decisions"),
    ],
)
def test_config_defaults(provider, model, url):
    config = JevConfig.from_settings(settings(jev_provider=provider))
    assert (config.url, config.model) == (url, model)
    assert "test-secret" not in repr(config)
    assert "test-secret" not in repr(settings())


@pytest.mark.parametrize(
    "change",
    [
        {"jev_provider": "chat"},
        {"jev_api_key": ""},
        {"jev_api_url": "https://api.openai.com/v1/chat/completions"},
    ],
)
def test_invalid_config_is_not_silently_another_model(change):
    s = settings().model_copy(update=change)
    with pytest.raises(ValueError):
        JevConfig.from_settings(s)


def test_config_downlink_independent_of_existing_models():
    s = settings(
        phone_vlm_provider="doubao",
        phone_vlm_model="phone",
        phone_vlm_api_key="phone-key",
        phone_vlm_base_url="https://phone.invalid/api/v3",
        aux_provider="doubao",
        aux_model="aux",
        aux_api_key="aux-key",
        aux_base_url="https://aux.invalid/api/v3",
        jev_enabled=True,
    )
    cfg = build_downlink_config(s)
    assert cfg["jev_api_key"] == "test-secret"
    assert cfg["vlm_model"] == "phone" and cfg["assistant_model"] == "aux"
    assert {"jev_enabled", "jev_provider", "jev_api_key"} <= downlink_field_names()
    assert Settings(_env_file=None).jev_enabled is False


@pytest.mark.asyncio
async def test_off_factory_touches_nothing():
    assert (
        await create_locator(
            settings=SimpleNamespace(jev_enabled=False),
            driver=object(),
            main_vlm_backend="",
            emit=None,
            run_id="unit",
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openrouter", "openai"])
async def test_transport_exact_protocol_and_choice(provider):
    seen = []

    def request(req):
        payload = json.loads(req.content)
        seen.append(payload)
        assert req.headers["Authorization"] == "Bearer test-secret"
        if provider == "openrouter":
            assert "input" not in payload and payload["questions"]["node"]["criteria"] == {"N1": "继续"}
            assert payload["state"][-1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
            return httpx.Response(200, json={"answers": {"node": {"type": "choice", "choice": "N1"}}})
        assert "state" not in payload and payload["questions"][0]["name"] == "node"
        assert payload["input"][0]["content"][-1]["type"] == "input_image"
        return httpx.Response(200, json={"answers": [{"name": "node", "type": "choice", "choice": "N1"}]})

    c = DecisionsClient(
        JevConfig.from_settings(settings(jev_provider=provider)), transport=httpx.MockTransport(request)
    )
    try:
        assert (await c.choose(["目标"], b"image", {"N1": "继续"}))["choice"] == "N1"
    finally:
        await c.aclose()
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data", [{}, {"answers": {"node": {"type": "refusal"}}}, {"answers": {"node": {"choice": "N999"}}}]
)
async def test_bad_answers_never_return_coordinates(data):
    c = DecisionsClient(
        JevConfig.from_settings(settings()),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data)),
    )
    try:
        with pytest.raises(JevError):
            await c.choose([], b"", {"N1": "继续"})
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_http_error_is_redacted_and_not_retried():
    calls = []

    def error(req):
        calls.append(req)
        return httpx.Response(401, text="test-secret sensitive upstream body")

    c = DecisionsClient(JevConfig.from_settings(settings()), transport=httpx.MockTransport(error))
    try:
        with pytest.raises(JevError, match="HTTP 401") as exc:
            await c.choose([], b"", {})
        assert "test-secret" not in str(exc.value) and len(calls) == 1
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_click_preserves_action_initializes_once_and_exact_evidence_image():
    evidence = []
    loc, driver = locator(emit=evidence.append)
    await loc.initialize()
    p = params()
    original = deepcopy(p["action"])
    for _ in range(2):
        result = await loc.locate_action(**p)
        assert result.action == {**original, "point": {"x": 50, "y": 60}}
    assert p["action"] == original and driver.clicks == []
    assert loc.adapter.initializations == 1
    assert evidence[-1]["images"]["model_input"] == loc.client.choose.call_args.args[1]
    await loc.aclose()
    await loc.aclose()
    assert loc.adapter.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,source",
    [
        ("scroll", None),
        ("type", None),
        ("double_tap", None),
        ("long_press", None),
        ("drag", None),
        ("click", "type"),
    ],
)
async def test_non_click_and_visibility_probe_use_original(monkeypatch, kind, source):
    original = AsyncMock(return_value="original")
    monkeypatch.setattr(V3PlanLocator, "locate_action", original)
    loc, driver = locator()
    p = params(kind)
    if source:
        p["action"]["locator_source_type"] = source
    assert await loc.locate_action(**p) == "original"
    original.assert_awaited_once()
    loc.client.choose.assert_not_awaited()
    assert driver.screenshots == 0
    await loc.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["UNCERTAIN", "NOT_FOUND"])
async def test_miss_never_calls_original(monkeypatch, choice):
    original = AsyncMock()
    monkeypatch.setattr(V3PlanLocator, "locate_action", original)
    loc, _ = locator(choice=choice)
    with pytest.raises(V3LocatorMiss):
        await loc.locate_action(**params())
    original.assert_not_awaited()
    await loc.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: setattr(d, "orientation", 2),
        lambda d: setattr(d, "size", (200, 100)),
        lambda d: setattr(d, "xml", XML.replace("继续", "删除")),
        lambda d: setattr(d, "xml", XML.replace("[20,30]", "[30,40]")),
    ],
)
async def test_no_reverse_read_after_model_returns(mutate):
    loc, driver = locator(mutate=mutate)

    def forbidden(*args):
        raise AssertionError("locator must reuse V3 frame/viewport, not query again")

    driver.screenshot_jpeg = driver.window_size = driver.rotation = forbidden
    result = await loc.locate_action(**params())
    assert result.action["point"] == {"x": 50, "y": 60}
    assert loc.adapter.tree_reads == 1
    await loc.aclose()


@pytest.mark.asyncio
async def test_single_step_request_has_no_history_or_post_execution_collection():
    loc, driver = locator()
    assert not hasattr(loc, "bind_history")
    assert not hasattr(loc, "record_execution")
    runner = V3ReplayRunner(driver=driver, trajectory={"actions": []}, locator=loc)
    await runner._execute_recorded_action(
        {"type": "click", "point": {"x": 50, "y": 60}}, index=1, source="cache"
    )
    assert loc.adapter.tree_reads == 0
    assert runner.execution_history[0]["runtime_status"] == "completed_without_exception"
    await loc.locate_action(**params())
    text = loc.client.choose.call_args.args[0]
    assert all("history" not in t and "feedback" not in t for t in text)
    assert not any(t.startswith("整体任务") or t.startswith("最近五轮") for t in text)
    assert loc.adapter.tree_reads == 1
    await loc.aclose()


def test_ios_reuses_client_and_accepts_accessible_false_other():
    source = (
        '<Application type="XCUIElementTypeApplication" x="0" y="0" width="50" height="100">'
        '<Other type="XCUIElementTypeOther" label="内部别名" accessible="false" visible="true" '
        'enabled="true" x="10" y="15" width="30" height="30"/></Application>'
    )
    requests = []
    wda = SimpleNamespace(
        window_size=lambda: SimpleNamespace(width=50, height=100),
        orientation=lambda: "PORTRAIT",
        _request=lambda *a: requests.append(a) or {"value": source},
    )
    adapter = IosAdapter(SimpleNamespace(_wda=wda, _get_scale=lambda: 2))
    adapter.initialize()
    candidates = tree_data(adapter.tree((100, 200)), (100, 200))[0]
    assert candidates[0]["bounds"] == [20, 30, 80, 90]
    adapter.close()
    assert requests == [("GET", "/source?format=xml")]


def test_harmony_clips_native_image_to_parent():
    tree = {
        "attributes": {"type": "Row", "bounds": "[10,10][90,90]", "clickable": "true", "enabled": "true"},
        "children": [{"attributes": {"type": "Image", "bounds": "[0,0][100,100]", "enabled": "true"}}],
    }
    nodes = tree_data(HarmonyAdapter.convert(tree), (100, 200))[0]
    assert nodes[1]["bounds"] == [10, 10, 90, 90]


def test_redacts_ui_urls_and_auth_without_truncating_target():
    data = redact({"text": "打开 https://example.org/a?token=private 然后继续", "api_key": "private"})
    assert "private" not in json.dumps(data)
    assert data["text"].endswith("然后继续")


@pytest.mark.asyncio
async def test_evidence_failure_does_not_change_click():
    def failed_emit(_):
        raise OSError("disk unavailable")

    loc, _ = locator(emit=failed_emit)
    assert (await loc.locate_action(**params())).action["point"] == {"x": 50, "y": 60}
    await loc.aclose()


@pytest.mark.asyncio
async def test_miss_enters_existing_runner_rescue(monkeypatch):
    loc, driver = locator(choice="NOT_FOUND")
    runner = V3ReplayRunner(driver=driver, trajectory={"actions": []}, locator=loc)
    rescue = AsyncMock(return_value=None)
    monkeypatch.setattr(runner, "_rescue_and_retry_locator", rescue)
    assert await runner._locate_with_retry_and_rescue(params()["action"], driver.screenshot_jpeg()) is None
    rescue.assert_awaited_once()
    await loc.aclose()


@pytest.mark.asyncio
async def test_request_cancellation_does_not_rescue_or_click():
    loc, driver = locator()
    loc.client.choose.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await loc.locate_action(**params())
    assert driver.clicks == []
    await loc.aclose()


@pytest.mark.asyncio
async def test_old_runner_viewport_never_accepts_new_orientation():
    loc, driver = locator()
    p = params()
    p["window_size"] = (200, 100)
    with pytest.raises(V3LocatorMiss, match="Viewport changed since"):
        await loc.locate_action(**p)
    loc.client.choose.assert_not_awaited()
    await loc.aclose()


@pytest.mark.asyncio
async def test_empty_tree_skips_model():
    loc, driver = locator()
    driver.xml = "<hierarchy/>"
    with pytest.raises(V3LocatorMiss, match="no visible candidates"):
        await loc.locate_action(**params())
    loc.client.choose.assert_not_awaited()
    await loc.aclose()


@pytest.mark.asyncio
async def test_init_failure_stays_jev_miss(monkeypatch):
    loc, _ = locator()

    def broken():
        raise RuntimeError("sensitive data")

    monkeypatch.setattr(loc.adapter, "initialize", broken)
    await loc.initialize()
    with pytest.raises(V3LocatorMiss, match="initialization failed: RuntimeError"):
        await loc.locate_action(**params())
    assert loc.original is None
    loc.client.choose.assert_not_awaited()
    await loc.aclose()


@pytest.mark.asyncio
async def test_orchestration_closes_injected_locator_on_success_and_cancel(monkeypatch):
    from ai_phone.agent.trajectory_cache import orchestrate, v3_replay
    from ai_phone.agent.trajectory_cache.jev_click import factory

    loc, driver = locator()
    initialize = AsyncMock(return_value=loc)
    monkeypatch.setattr(factory, "create_locator", initialize)
    for cancel in (False, True):
        loc.closed = False

        class Runner:
            execution_history = []

            def __init__(self, **kw):
                assert kw["locator"] is loc

            async def run(self):
                if cancel:
                    raise asyncio.CancelledError()
                return SimpleNamespace(success=False, error="unit failure", actions_executed=0, elapsed_ms=0)

        monkeypatch.setattr(v3_replay, "V3ReplayRunner", Runner)
        bridge = SimpleNamespace(
            emit=lambda e: None, send_cache_suspect=AsyncMock(), send_run_done=AsyncMock()
        )
        call = orchestrate.run_v3_replay(
            run_id="unit",
            serial=driver.serial,
            goal="goal",
            attempt=1,
            driver=driver,
            bridge=bridge,
            snapshot={"cache_mode": "v3"},
            settings=settings(jev_enabled=True),
        )
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            await call
        assert loc.closed
    assert loc.adapter.closes == 2


@pytest.mark.asyncio
async def test_bridge_evidence_does_not_replace_step_screenshots(monkeypatch):
    from ai_phone.agent.runner_bridge import RunnerBridge
    from ai_phone.agent.runner.events import EVT_LOCATOR_EVIDENCE, EVT_SCREENSHOT, EVT_STEP_END, make_event

    sent = []

    async def send(msg):
        sent.append(msg)
        return True

    bridge = RunnerBridge(run_id="unit", serial="serial", ws_send=send, server_http_base="http://test")
    monkeypatch.setattr(
        bridge, "_upload_with_retry", AsyncMock(side_effect=lambda data, step, phase: f"/files/{phase}.jpg")
    )
    bridge.emit(make_event(EVT_SCREENSHOT, "unit", step=1, phase="before", bytes=b"before"))
    bridge.emit(
        make_event(
            EVT_LOCATOR_EVIDENCE,
            "unit",
            step=1,
            evidence={"schema": "jev_locator_v1"},
            images={"model_input": b"model", "current": b"current"},
        )
    )
    bridge.emit(make_event(EVT_SCREENSHOT, "unit", step=1, phase="after", bytes=b"after"))
    bridge.emit(make_event(EVT_STEP_END, "unit", step=1, action_type="click"))
    await bridge.aclose()
    done = next(m for m in sent if m["type"] == "step_done")
    assert done["before_url"] == "/files/before.jpg" and done["after_url"] == "/files/after.jpg"
    evidence = json.loads(next(m for m in sent if m.get("title") == "Jev定位证据")["content"])
    assert evidence["images"]["model_input"] == "/files/jev-model_input.jpg"
    assert evidence["evidence_complete"] is True
    assert [m["phase"] for m in sent if m["type"] == "frame"] == ["before", "after"]


@pytest.mark.asyncio
async def test_upload_failure_still_reports_missing_evidence(monkeypatch):
    from ai_phone.agent.runner_bridge import RunnerBridge

    sent = []

    async def send(msg):
        sent.append(msg)
        return True

    bridge = RunnerBridge(run_id="unit", serial="serial", ws_send=send, server_http_base="http://test")
    monkeypatch.setattr(bridge, "_upload_with_retry", AsyncMock(side_effect=OSError()))
    await bridge._upload_locator_evidence(
        {"evidence": {"schema": "jev_locator_v1"}, "images": {"model_input": b"x"}}
    )
    await bridge.aclose()
    assert json.loads(sent[0]["content"])["evidence_complete"] is False
    assert sent[0]["level"] == 2


def test_report_attachments_render_safely():
    from ai_phone.server.submissions.reports import _render_log_row

    log = SimpleNamespace(
        level=1,
        title="Jev定位证据",
        ts=None,
        content=json.dumps(
            {
                "schema": "jev_locator_v1",
                "images": {"model_input": "/files/model.jpg", "current": "javascript:alert(1)"},
            }
        ),
    )
    html = _render_log_row(log)
    assert '<img src="/files/model.jpg"' in html
    assert '<img src="javascript:' not in html


def test_android_close_only_owned_u2_process():
    from ai_phone.agent.trajectory_cache.jev_click.adapters.android import AndroidAdapter

    calls = []
    adapter = AndroidAdapter(object())
    adapter.device = SimpleNamespace(stop_uiautomator=lambda **kw: calls.append(kw))
    adapter.close()
    adapter.close()
    assert calls == [{"wait": False}]


@pytest.mark.asyncio
async def test_expired_reply_cannot_produce_a_click():
    from dataclasses import replace

    loc, driver = locator()
    loc.config = replace(loc.config, timeout=0.02)

    async def delayed(*args):
        await asyncio.sleep(0.1)
        return {"choice": "N1"}

    loc.client.choose.side_effect = delayed
    with pytest.raises(V3LocatorMiss, match="Expired frame"):
        await loc.locate_action(**params())
    assert driver.clicks == [] and loc.adapter.tree_reads == 1
    await loc.aclose()


@pytest.mark.asyncio
async def test_each_invocation_uses_its_own_frame_and_candidates():
    loc, driver = locator()
    evidence = []
    loc.emit = evidence.append
    p = params()
    await loc.locate_action(**p)
    assert "current" not in evidence[0]["images"]
    driver.xml = "<hierarchy/>"
    with pytest.raises(V3LocatorMiss, match="no visible candidates"):
        await loc.locate_action(**params())
    assert loc.adapter.tree_reads == 2
    loc.client.choose.assert_awaited_once()
    assert driver.screenshots == 0
    await loc.aclose()


@pytest.mark.asyncio
async def test_v3_entry_real_runner_click_and_final_assertion_contract(monkeypatch):
    from ai_phone.agent.trajectory_cache import orchestrate, assertion, v3_replay
    from ai_phone.agent.trajectory_cache.jev_click import factory

    loc, driver = locator()
    fast_clicks = []
    loc.adapter.device = object()
    loc.adapter.click = lambda x, y: fast_clicks.append((x, y))
    monkeypatch.setattr(factory, "create_locator", AsyncMock(return_value=loc))
    verify = AsyncMock(return_value=assertion.CacheAssertionResult("PASS", "verified"))
    monkeypatch.setattr(assertion.CacheReplayAssertionVerifier, "verify", verify)
    # This test exercises entry→locator→dispatcher→runtime history→assertion. Timing
    # sleeps are irrelevant here and covered separately by existing runner tests.
    monkeypatch.setattr(v3_replay.V3ReplayRunner, "_observe_after_action", AsyncMock())

    async def screenshot(self, *args, **kw):
        return driver.screenshot_jpeg()

    monkeypatch.setattr(v3_replay.V3ReplayRunner, "_wait_stable_for_step", screenshot)
    monkeypatch.setattr(v3_replay.V3ReplayRunner, "capture_final_frame", screenshot)
    bridge = SimpleNamespace(emit=lambda e: None, send_cache_suspect=AsyncMock(), send_run_done=AsyncMock())
    await orchestrate.run_v3_replay(
        run_id="unit",
        serial=driver.serial,
        goal="original Case",
        attempt=1,
        driver=driver,
        bridge=bridge,
        snapshot={"cache_mode": "v3", "actions": [params()["action"]]},
        settings=settings(jev_enabled=True),
    )
    assert fast_clicks == [(50, 60)] and driver.clicks == []
    assert loc.closed and loc.original is None
    assert verify.call_args.kwargs["goal"] == "original Case"
    history = verify.call_args.kwargs["execution_history"]
    assert history[0]["action"]["point"] == {"x": 50, "y": 60}
    assert history[0]["runtime_status"] == "completed_without_exception"
    assert bridge.send_run_done.call_args.args[0]["result"] == "pass"


@pytest.mark.asyncio
async def test_disabled_v3_entry_does_not_construct_jev(monkeypatch):
    from ai_phone.agent.trajectory_cache import orchestrate, v3_replay
    from ai_phone.agent.trajectory_cache.jev_click import factory

    create = AsyncMock(side_effect=AssertionError("Jev must stay unloaded"))
    monkeypatch.setattr(factory, "create_locator", create)

    class Runner:
        def __init__(self, **kwargs):
            assert "locator" not in kwargs
            assert kwargs["capture_after_each_action"] is False
            assert kwargs["observe_delay_ms"] == 200

        async def run(self):
            return SimpleNamespace(success=False, error="unit", actions_executed=0, elapsed_ms=0)

    monkeypatch.setattr(v3_replay, "V3ReplayRunner", Runner)
    bridge = SimpleNamespace(emit=lambda e: None, send_cache_suspect=AsyncMock(), send_run_done=AsyncMock())
    await orchestrate.run_v3_replay(
        run_id="unit",
        serial="unit",
        goal="goal",
        attempt=1,
        driver=object(),
        bridge=bridge,
        snapshot={"cache_mode": "v3"},
        settings=Settings(_env_file=None),
    )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_v3_without_after_still_captures_each_before_and_fresh_final(monkeypatch):
    from ai_phone.agent.runner import stability
    from ai_phone.agent.trajectory_cache import v3_replay

    settings_ = Settings(_env_file=None, trajectory_cache_page_stable_enabled=False)
    monkeypatch.setattr(stability, "get_settings", lambda: settings_)
    monkeypatch.setattr(v3_replay, "get_settings", lambda: settings_)
    driver = Driver()
    events = []
    actions = [params()["action"], {**params()["action"], "index": 2, "action_id": "a-2"}]
    runner = V3ReplayRunner(
        driver=driver,
        trajectory={"actions": actions},
        capture_after_each_action=False,
        emit=events.append,
        run_id="unit",
    )

    async def materialize(action, *args, **kwargs):
        return {**action, "point": {"x": 50, "y": 60}}

    monkeypatch.setattr(runner, "_materialize_action", materialize)
    monkeypatch.setattr(runner, "_observe_after_action", AsyncMock())
    result = await runner.run()
    assert result.success and driver.clicks == [(50, 60), (50, 60)]
    assert driver.screenshots == 2
    assert [e["phase"] for e in events if e["type"] == "screenshot"] == ["before", "before"]
    assert result.final_before_bytes and result.final_after_bytes is None
    assert await runner.capture_final_frame()
    assert driver.screenshots == 3


@pytest.mark.asyncio
async def test_final_capture_cannot_fall_back_to_pre_action_frame_without_after(monkeypatch):
    from ai_phone.agent.runner import stability
    from ai_phone.agent.trajectory_cache import v3_replay

    settings_ = Settings(_env_file=None, trajectory_cache_page_stable_enabled=False)
    monkeypatch.setattr(stability, "get_settings", lambda: settings_)
    monkeypatch.setattr(v3_replay, "get_settings", lambda: settings_)
    driver = Driver()
    runner = V3ReplayRunner(driver=driver, trajectory={"actions": []}, capture_after_each_action=False)
    runner._last_frame = runner._final_after_bytes = b"old-frame"

    async def broken():
        raise RuntimeError("screenshot failed")

    monkeypatch.setattr(runner, "_screenshot_jpeg", broken)
    with pytest.raises(RuntimeError, match="screenshot failed"):
        await runner.capture_final_frame()
    assert runner._last_frame is None and runner._final_after_bytes is None


def test_partial_android_initialization_releases_owned_process(monkeypatch):
    import sys
    from ai_phone.agent.trajectory_cache.jev_click.adapters.android import AndroidAdapter

    stopped = []

    class Device:
        def __init__(self, serial):
            assert serial == "only-this-device"
            self._process = object()
            raise RuntimeError("startup failed")

        def stop_uiautomator(self, **kwargs):
            stopped.append(kwargs)

    monkeypatch.setitem(sys.modules, "uiautomator2", SimpleNamespace(Device=Device))
    adapter = AndroidAdapter(Driver())
    with pytest.raises(RuntimeError, match="startup failed"):
        adapter.initialize()
    adapter.close()
    assert stopped == [{"wait": False}]


@pytest.mark.asyncio
async def test_v3_observation_200ms_is_local_and_not_a_shared_config_change(monkeypatch):
    from ai_phone.agent.trajectory_cache import v3_replay

    monkeypatch.setattr(v3_replay.asyncio, "sleep", AsyncMock())
    runner = V3ReplayRunner(driver=Driver(), trajectory={"actions": []}, observe_delay_ms=200)
    await runner._observe_after_action()
    v3_replay.asyncio.sleep.assert_awaited_once_with(0.2)
    assert Settings(_env_file=None).trajectory_cache_observe_delay_ms == 500


def test_ios_static_text_regions_include_tabs_without_claiming_native_clickability():
    from ai_phone.agent.trajectory_cache.jev_click.types import tree_data

    xml = """<AppiumAUT><XCUIElementTypeApplication type="XCUIElementTypeApplication" x="0" y="0" width="375" height="667">
    <n type="XCUIElementTypeStaticText" label="研究员之家" visible="true" enabled="true" x="79" y="30" width="80" height="23"/>
    <n type="XCUIElementTypeStaticText" label="研究员之家" visible="true" enabled="true" x="79" y="30" width="80" height="23"/>
    <n type="XCUIElementTypeStaticText" label="正文" visible="true" enabled="true" x="10" y="100" width="100" height="20"/>
    <n type="XCUIElementTypeStaticText" label="隐藏" visible="false" enabled="true" x="10" y="100" width="100" height="20"/>
    <n type="XCUIElementTypeStaticText" label="禁用" visible="true" enabled="false" x="10" y="100" width="100" height="20"/>
    <n type="XCUIElementTypeStaticText" label="  " visible="true" enabled="true" x="10" y="100" width="100" height="20"/>
    </XCUIElementTypeApplication></AppiumAUT>"""
    nodes, _ = tree_data(IosAdapter.convert(xml, (1125, 2001)), (1125, 2001))
    assert [n["text"] for n in nodes] == ["研究员之家", "正文"]
    assert nodes[0]["bounds"] == [237, 90, 477, 159]
