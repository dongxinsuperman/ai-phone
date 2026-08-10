from typing import Any

import pytest

from ai_phone.server import db as db_module
from ai_phone.server.hub import Hub
from ai_phone.server.lockstore import DeviceLockStore
from ai_phone.server.models import Device, Submission
from ai_phone.server.scheduler.service import SubmissionScheduler
from ai_phone.server.submissions import NullPublisher
from ai_phone.server.submissions.paths import absolute_report_url


def test_absolute_report_url_uses_current_deployment_host() -> None:
    assert absolute_report_url(
        "https://ai-phone.example.com/",
        "/files/reports/sub-1/_summary.html",
    ) == "https://ai-phone.example.com/files/reports/sub-1/_summary.html"
    assert absolute_report_url(
        "http://127.0.0.1:8000/",
        "/files/reports/sub-1/_summary.html",
    ) == "http://127.0.0.1:8000/files/reports/sub-1/_summary.html"


@pytest.mark.asyncio
async def test_public_submission_passes_its_request_host_to_scheduler(client, app) -> None:
    captured: dict[str, Any] = {}

    class _Scheduler:
        async def submit(self, body, *, origin, public_base_url):  # noqa: ANN001
            captured.update(
                body=body,
                origin=origin,
                public_base_url=public_base_url,
            )
            return {"submissionId": "sub-1"}

    app.state.scheduler = _Scheduler()
    response = await client.post("/api/submissions", json={"items": []})

    assert response.status_code == 201
    assert captured["origin"] == "external"
    assert captured["public_base_url"] == "http://test/"


@pytest.mark.asyncio
async def test_scheduler_persists_request_host_without_a_new_database_column(
    _test_engine,
) -> None:
    factory = db_module.get_session_factory()
    async with factory() as session:
        session.add(Device(serial="S1", platform="android", status="online"))
        await session.commit()

    scheduler = SubmissionScheduler(
        hub=Hub(),
        lock_store=DeviceLockStore(),
        session_factory=factory,
        publisher=NullPublisher(),
    )
    payload = await scheduler.submit(
        {
            "submissionName": "public",
            "items": [
                {
                    "caseId": "C1",
                    "runContent": "run",
                    "platforms": ["android"],
                }
            ],
        },
        origin="external",
        public_base_url="https://enterprise.example/",
    )

    async with factory() as session:
        submission = await session.get(Submission, payload["submissionId"])
        assert submission is not None
        assert submission.raw_body["_aiPhonePublicBaseUrl"] == (
            "https://enterprise.example"
        )
