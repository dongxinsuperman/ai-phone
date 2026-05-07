import asyncio

import pytest

from ai_phone.agent.main import _RunSupervisor


class _Bridge:
    pass


@pytest.mark.asyncio
async def test_run_supervisor_drops_task_that_crashes_before_runner_cleanup():
    supervisor = _RunSupervisor()

    async def _boom():
        raise NameError("settings")

    task = asyncio.create_task(_boom())
    supervisor.register("R1", "S1", task, _Bridge())

    await asyncio.sleep(0)

    assert supervisor.get("R1") is None
    assert not supervisor.is_busy("S1")
