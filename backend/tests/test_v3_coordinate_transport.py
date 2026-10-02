"""Run-local Doubao coordinate transport; no main/aux/foreign protocol changes."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from ai_phone.config import Settings
from ai_phone.agent.trajectory_cache import v3_replay as m

REAL_CLIENT = httpx.AsyncClient


def settings():
    return Settings(
        _env_file=None,
        vlm_backend="doubao_responses",
        trajectory_cache_v3_coord_use_recovery_vlm_config=False,
        trajectory_cache_v3_coord_backend="doubao_responses",
        trajectory_cache_v3_coord_api_url="https://unit.invalid/responses",
        trajectory_cache_v3_coord_api_key="unit-key",
        trajectory_cache_v3_coord_model="unit-model",
    )


def response(request, text="<point>100 200</point>", status=200, **kwargs):
    return httpx.Response(status, request=request, json={"output_text": text}, **kwargs)


def install(monkeypatch, handler):
    clients, requests, options = [], [], []

    def factory(**kwargs):
        number = len(clients)

        async def dispatch(request):
            requests.append(request)
            return await handler(number, request)

        client = REAL_CLIENT(**kwargs, transport=httpx.MockTransport(dispatch))
        clients.append(client)
        options.append(kwargs)
        return client

    monkeypatch.setattr(m.httpx, "AsyncClient", factory)
    return clients, requests, options


async def call(locator, **overrides):
    return await locator._responses_single_image(
        prompt="点击目标",
        image_bytes=b"image",
        api_url="https://unit.invalid/responses",
        api_key="unit-key",
        model="unit-model",
        timeout_sec=300,
        **overrides,
    )


@pytest.mark.asyncio
async def test_reuses_one_client_but_preserves_payload_and_no_cookie_or_response_history(monkeypatch):
    async def handler(number, request):
        assert "cookie" not in request.headers
        return response(request, headers={"set-cookie": "session=must-not-be-carried"})

    clients, requests, options = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    for _ in range(3):
        assert await call(locator) == "<point>100 200</point>"
    assert len(clients) == 1 and not clients[0].is_closed
    assert options[0]["limits"].keepalive_expiry == 60
    bodies = [json.loads(r.content) for r in requests]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["store"] is True and bodies[0]["caching"] == {"type": "enabled"}
    assert "previous_response_id" not in bodies[0]
    assert requests[0].extensions["timeout"] == {"connect": 10.0, "read": 300, "write": 300, "pool": 300}
    await locator.aclose()
    await locator.aclose()
    assert clients[0].is_closed and locator._coordinate_client is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError, httpx.RemoteProtocolError]
)
async def test_transport_failure_closes_bad_client_and_retries_identical_request_once(monkeypatch, error):
    async def handler(number, request):
        if number == 0:
            raise error("transport failure", request=request)
        return response(request)

    clients, requests, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    assert await call(locator) == "<point>100 200</point>"
    assert len(clients) == 2 and clients[0].is_closed
    assert requests[0].content == requests[1].content
    await locator.aclose()
    assert all(c.is_closed for c in clients)


@pytest.mark.asyncio
async def test_two_transport_failures_propagate_original_error_and_do_not_loop(monkeypatch):
    async def handler(number, request):
        raise httpx.ReadTimeout("still unavailable", request=request)

    clients, requests, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    with pytest.raises(httpx.ReadTimeout, match="still unavailable"):
        await call(locator)
    assert len(requests) == 2 and all(c.is_closed for c in clients)
    assert locator._coordinate_client is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_status_errors_do_not_gain_new_retries(monkeypatch, status):
    async def handler(number, request):
        return response(request, status=status)

    clients, requests, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    with pytest.raises(RuntimeError, match=f"status={status}"):
        await call(locator)
    assert len(requests) == 1
    await locator.aclose()


@pytest.mark.asyncio
async def test_missing_target_is_semantic_miss_not_transport_retry(monkeypatch):
    async def handler(number, request):
        return response(request, text="无")

    clients, requests, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    with pytest.raises(m.V3LocatorMiss):
        await locator.locate_action(
            goal="任务",
            trajectory={},
            action={"type": "click", "plan_intent": "点击目标"},
            screenshot_bytes=b"image",
            image_size=(1000, 1000),
            window_size=(1000, 1000),
        )
    assert len(requests) == 1
    await locator.aclose()


@pytest.mark.asyncio
async def test_changing_credentials_model_or_endpoint_replaces_client(monkeypatch):
    async def handler(number, request):
        return response(request)

    clients, requests, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    for url, key, model in [
        ("https://unit.invalid/responses", "a", "m"),
        ("https://unit.invalid/responses", "b", "m"),
        ("https://unit.invalid/responses", "b", "n"),
        ("https://other.invalid/responses", "b", "n"),
    ]:
        await locator._responses_single_image(
            prompt="目标", image_bytes=b"image", api_url=url, api_key=key, model=model, timeout_sec=300
        )
    assert len(clients) == 4 and all(c.is_closed for c in clients[:-1])
    assert [r.headers["authorization"] for r in requests] == ["Bearer a", "Bearer b", "Bearer b", "Bearer b"]
    await locator.aclose()


@pytest.mark.asyncio
async def test_cancelled_request_closes_client_without_retry(monkeypatch):
    entered = asyncio.Event()

    async def handler(number, request):
        entered.set()
        await asyncio.Event().wait()

    clients, requests, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    task = asyncio.create_task(call(locator))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(requests) == 1 and clients[0].is_closed


@pytest.mark.asyncio
async def test_original_outer_timeout_is_total_budget_and_closes_pending_client(monkeypatch):
    async def handler(number, request):
        await asyncio.sleep(1)
        return response(request)

    clients, requests, _ = install(monkeypatch, handler)
    cfg = settings()
    cfg.trajectory_cache_v3_coord_timeout_sec = 0.03
    locator = m.V3PlanLocator(settings=cfg)
    with pytest.raises(TimeoutError):
        await locator.locate_action(
            goal="任务",
            trajectory={},
            action={"type": "click", "plan_intent": "点击目标"},
            screenshot_bytes=b"image",
            image_size=(1000, 1000),
            window_size=(1000, 1000),
        )
    assert len(requests) == 1 and clients[0].is_closed


@pytest.mark.asyncio
async def test_retry_does_not_receive_a_second_full_outer_timeout_budget(monkeypatch):
    async def handler(number, request):
        if number == 0:
            raise httpx.ConnectError("connection lost", request=request)
        await asyncio.sleep(1)
        return response(request)

    clients, requests, _ = install(monkeypatch, handler)
    cfg = settings()
    cfg.trajectory_cache_v3_coord_timeout_sec = 0.03
    locator = m.V3PlanLocator(settings=cfg)
    with pytest.raises(TimeoutError):
        await locator.locate_action(
            goal="任务",
            trajectory={},
            action={"type": "click", "plan_intent": "点击目标"},
            screenshot_bytes=b"image",
            image_size=(1000, 1000),
            window_size=(1000, 1000),
        )
    assert len(requests) == 2 and all(c.is_closed for c in clients)


@pytest.mark.asyncio
async def test_separate_locators_never_share_clients(monkeypatch):
    async def handler(number, request):
        return response(request)

    clients, requests, _ = install(monkeypatch, handler)
    a, b = m.V3PlanLocator(settings=settings()), m.V3PlanLocator(settings=settings())
    await asyncio.gather(call(a), call(b))
    assert len(clients) == 2 and a._coordinate_client is not b._coordinate_client
    await a.aclose()
    assert clients[0].is_closed and not clients[1].is_closed
    await b.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "restart", "error", "cancel"])
async def test_runner_releases_owned_locator_on_every_exit(monkeypatch, outcome):
    async def handler(number, request):
        return response(request)

    clients, _, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    monkeypatch.setattr(m, "V3PlanLocator", lambda **kwargs: locator)
    runner = m.V3ReplayRunner(driver=SimpleNamespace(), trajectory={"actions": []})
    entered = asyncio.Event()

    async def actions():
        await call(locator)
        entered.set()
        if outcome == "error":
            raise RuntimeError("execution error")
        if outcome == "cancel":
            await asyncio.Event().wait()
        if outcome == "restart":
            return m.V3ReplayResult(success=False, actions_total=1, actions_executed=0, restart_required=True)
        return m.ReplayResult(success=True, actions_total=0, actions_executed=0)

    monkeypatch.setattr(runner, "_run_actions", actions)
    task = asyncio.create_task(runner.run())
    await entered.wait()
    if outcome == "cancel":
        task.cancel()
    if outcome in {"error", "cancel"}:
        with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.success is (outcome == "success")
    assert clients[0].is_closed and locator._coordinate_client is None


@pytest.mark.asyncio
async def test_runner_does_not_close_caller_owned_locator(monkeypatch):
    async def handler(number, request):
        return response(request)

    clients, _, _ = install(monkeypatch, handler)
    locator = m.V3PlanLocator(settings=settings())
    runner = m.V3ReplayRunner(driver=SimpleNamespace(), trajectory={"actions": []}, locator=locator)

    async def actions():
        await call(locator)
        return m.ReplayResult(success=True, actions_total=0, actions_executed=0)

    monkeypatch.setattr(runner, "_run_actions", actions)
    await runner.run()
    assert not clients[0].is_closed
    await locator.aclose()
