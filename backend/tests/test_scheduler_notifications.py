from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import pytest

from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Submission, SubmissionItem
from ai_phone.server.scheduler.service import SubmissionScheduler
from ai_phone.server.submissions import ResultPublisher


class _BlockingPublisher(ResultPublisher):
    name = "blocking"

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def publish_terminal(self, event: Dict[str, Any]) -> None:
        self.events.append(dict(event))
        self.started.set()
        await self.release.wait()


class _CollectingPublisher(ResultPublisher):
    name = "collecting"

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    async def publish_terminal(self, event: Dict[str, Any]) -> None:
        self.events.append(dict(event))


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.01)


async def _seed_done_item(factory, *, callback_url: str = "http://case-flow/callback") -> None:
    async with factory() as s:
        now = datetime.now(timezone.utc)
        s.add(
            Submission(
                id="sub1",
                state="accepted",
                callback_url=callback_url,
                submission_name="smoke",
            )
        )
        s.add(
            SubmissionItem(
                id="item1",
                submission_id="sub1",
                case_id="C1",
                case_name="case one",
                platform="android",
                run_content="rc",
                state="success",
                status_reason="completed",
                enqueued_at=now,
                started_at=now,
                finished_at=now,
            )
        )
        await s.commit()


async def _seed_two_items(factory, *, callback_url: str = "http://case-flow/callback") -> None:
    async with factory() as s:
        now = datetime.now(timezone.utc)
        s.add(
            Submission(
                id="sub1",
                state="accepted",
                callback_url=callback_url,
                submission_name="two-items",
            )
        )
        s.add_all(
            [
                SubmissionItem(
                    id="item1",
                    submission_id="sub1",
                    case_id="C1",
                    case_name="case one",
                    platform="android",
                    run_content="rc1",
                    state="success",
                    status_reason="completed",
                    enqueued_at=now,
                    started_at=now,
                    finished_at=now,
                ),
                SubmissionItem(
                    id="item2",
                    submission_id="sub1",
                    case_id="C2",
                    case_name="case two",
                    platform="android",
                    run_content="rc2",
                    state="queued",
                    status_reason=None,
                    enqueued_at=now,
                ),
            ]
        )
        await s.commit()


def _make_scheduler(factory, publisher: ResultPublisher) -> SubmissionScheduler:
    return SubmissionScheduler(
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=factory,
        publisher=publisher,
    )


@pytest.mark.asyncio
async def test_kafka_blocking_does_not_block_submission_close_or_webhook(
    _test_engine,
    monkeypatch,
):
    """Kafka 是副作用：卡住时不能挡 submission 收口，也不能挡 webhook."""
    factory = db_module.get_session_factory()
    await _seed_done_item(factory)

    webhook_events: List[Tuple[str, Dict[str, Any]]] = []

    class _FakeWebhook:
        def __init__(self, *, url: str, timeout_sec: float = 5.0) -> None:  # noqa: ARG002
            self.url = url

        async def publish_terminal(self, event: Dict[str, Any]) -> None:
            webhook_events.append((self.url, dict(event)))

    monkeypatch.setattr(
        "ai_phone.server.scheduler.service.WebhookPublisher",
        _FakeWebhook,
    )

    publisher = _BlockingPublisher()
    sched = _make_scheduler(factory, publisher)
    sched._start_notification_workers()  # noqa: SLF001
    try:
        async with factory() as s:
            item = await s.get(SubmissionItem, "item1")
            assert item is not None
            await asyncio.wait_for(sched._finalize_and_publish(s, item), timeout=0.5)  # noqa: SLF001

        async with factory() as s:
            sub = await s.get(Submission, "sub1")
            assert sub is not None
            assert sub.state == "done"
            assert sub.finished_at is not None

        await asyncio.wait_for(publisher.started.wait(), timeout=0.5)
        await _wait_until(lambda: len(webhook_events) == 2)

        assert [event["event"] for _url, event in webhook_events] == [
            "submission.item.terminal",
            "submission.terminal",
        ]
        assert {url for url, _event in webhook_events} == {"http://case-flow/callback"}
    finally:
        publisher.release.set()
        try:
            await asyncio.wait_for(sched._publisher_queue.join(), timeout=1.0)  # noqa: SLF001
            await asyncio.wait_for(sched._webhook_queue.join(), timeout=1.0)  # noqa: SLF001
        finally:
            await sched._stop_notification_workers()  # noqa: SLF001


@pytest.mark.asyncio
async def test_item_terminal_is_sent_before_batch_terminal(
    _test_engine,
    monkeypatch,
):
    """批次未全部终态时只发 item；最后一条结束后再发 submission.terminal."""
    factory = db_module.get_session_factory()
    await _seed_two_items(factory)

    webhook_events: List[Dict[str, Any]] = []

    class _FakeWebhook:
        def __init__(self, *, url: str, timeout_sec: float = 5.0) -> None:  # noqa: ARG002
            self.url = url

        async def publish_terminal(self, event: Dict[str, Any]) -> None:
            webhook_events.append(dict(event))

    monkeypatch.setattr(
        "ai_phone.server.scheduler.service.WebhookPublisher",
        _FakeWebhook,
    )

    publisher = _CollectingPublisher()
    sched = _make_scheduler(factory, publisher)
    sched._start_notification_workers()  # noqa: SLF001
    try:
        async with factory() as s:
            item1 = await s.get(SubmissionItem, "item1")
            assert item1 is not None
            await sched._finalize_and_publish(s, item1)  # noqa: SLF001

        await asyncio.wait_for(sched._publisher_queue.join(), timeout=1.0)  # noqa: SLF001
        await asyncio.wait_for(sched._webhook_queue.join(), timeout=1.0)  # noqa: SLF001
        assert [event["event"] for event in publisher.events] == ["submission.item.terminal"]
        assert [event["event"] for event in webhook_events] == ["submission.item.terminal"]

        async with factory() as s:
            item2 = await s.get(SubmissionItem, "item2")
            assert item2 is not None
            now = datetime.now(timezone.utc)
            item2.state = "success"
            item2.status_reason = "completed"
            item2.started_at = now
            item2.finished_at = now
            await s.commit()
            await s.refresh(item2)
            await sched._finalize_and_publish(s, item2)  # noqa: SLF001

        await asyncio.wait_for(sched._publisher_queue.join(), timeout=1.0)  # noqa: SLF001
        await asyncio.wait_for(sched._webhook_queue.join(), timeout=1.0)  # noqa: SLF001
        assert [event["event"] for event in publisher.events] == [
            "submission.item.terminal",
            "submission.item.terminal",
            "submission.terminal",
        ]
        assert [event["event"] for event in webhook_events] == [
            "submission.item.terminal",
            "submission.item.terminal",
            "submission.terminal",
        ]
    finally:
        await sched._stop_notification_workers()  # noqa: SLF001


@pytest.mark.asyncio
async def test_webhook_blocking_does_not_block_submission_close_or_kafka(
    _test_engine,
    monkeypatch,
):
    """Webhook 是副作用：接收方卡住时不能挡主流程，也不能挡 Kafka."""
    factory = db_module.get_session_factory()
    await _seed_done_item(factory)

    webhook_started = asyncio.Event()
    webhook_release = asyncio.Event()

    class _BlockingWebhook:
        def __init__(self, *, url: str, timeout_sec: float = 5.0) -> None:  # noqa: ARG002
            self.url = url

        async def publish_terminal(self, event: Dict[str, Any]) -> None:  # noqa: ARG002
            webhook_started.set()
            await webhook_release.wait()

    monkeypatch.setattr(
        "ai_phone.server.scheduler.service.WebhookPublisher",
        _BlockingWebhook,
    )

    publisher = _CollectingPublisher()
    sched = _make_scheduler(factory, publisher)
    sched._start_notification_workers()  # noqa: SLF001
    try:
        async with factory() as s:
            item = await s.get(SubmissionItem, "item1")
            assert item is not None
            await asyncio.wait_for(sched._finalize_and_publish(s, item), timeout=0.5)  # noqa: SLF001

        async with factory() as s:
            sub = await s.get(Submission, "sub1")
            assert sub is not None
            assert sub.state == "done"
            assert sub.finished_at is not None

        await asyncio.wait_for(webhook_started.wait(), timeout=0.5)
        await asyncio.wait_for(sched._publisher_queue.join(), timeout=1.0)  # noqa: SLF001
        assert [event["event"] for event in publisher.events] == [
            "submission.item.terminal",
            "submission.terminal",
        ]
    finally:
        webhook_release.set()
        try:
            await asyncio.wait_for(sched._webhook_queue.join(), timeout=1.0)  # noqa: SLF001
        finally:
            await sched._stop_notification_workers()  # noqa: SLF001
