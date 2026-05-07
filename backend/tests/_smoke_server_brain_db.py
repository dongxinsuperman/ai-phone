"""Phase 1-B 冒烟脚本（不进 pytest）：

验证 next/server-brain 引入的新字段 / 新表能在 SQLite 内存库走通：
1. ``Base.metadata.create_all`` 能把所有新列 / run_commands 表建出来
2. ORM 写 / 读新字段 round-trip
3. ``to_dict`` 永远输出新字段（None 时返回 None / 'agent_brain'）
4. RunCommand 的 cascade delete 关系正常

这是 PoC 阶段的快速校验脚本，等 mock RemoteDriver 进来后会被 pytest 用例替代。
跑法：``cd backend && .venv/bin/python tests/_smoke_server_brain_db.py``
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.server.db import (
    Base,
    dispose_engine,
    get_engine,
    get_session_factory,
    init_db,
    init_engine,
)
from ai_phone.server.models import Run, RunCommand, RunLog, RunStep


def _enable_sqlite_foreign_keys() -> None:
    """SQLite 默认 PRAGMA foreign_keys=OFF，需要每条连接显式开。

    PG 生产没这个问题（FK 默认就 enforce）；本 smoke 仅是为了在内存库里验证
    ondelete=CASCADE 的预期生产行为。
    """
    sync_engine = get_engine().sync_engine

    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _run_smoke() -> None:
    init_engine("sqlite+aiosqlite:///:memory:")
    _enable_sqlite_foreign_keys()
    await init_db()

    factory = get_session_factory()
    trace_id = uuid.uuid4().hex[:16]
    msg_id = uuid.uuid4().hex[:16]

    async with factory() as session:  # type: AsyncSession
        run = Run(
            id="run_smoke_001",
            device_serial="emulator-5554",
            agent_id="agt-A",
            goal="冒烟测试",
            status="running",
            execution_mode="server_brain",
            dispatch_source="api",
            trace_id=trace_id,
            agent_id_at_start="agt-A",
        )
        session.add(run)
        await session.flush()

        step = RunStep(
            run_id=run.id,
            step=1,
            thought="想点击",
            action='click(point="<point>500 500</point>")',
            action_type="click",
            elapsed_ms=1200,
            driver_method="click",
            command_id=msg_id,
            rpc_elapsed_ms=42,
        )
        session.add(step)

        log = RunLog(
            run_id=run.id,
            step=1,
            level=3,
            title="RPC 超时",
            content="driver_command 等待 driver_result 超过 30s",
            trace_id=trace_id,
            error_class="RpcTimeout",
            error_category="network",
        )
        session.add(log)

        cmd = RunCommand(
            run_id=run.id,
            step=1,
            message_id=msg_id,
            method="click",
            agent_id="agt-A",
            serial="emulator-5554",
            ok=False,
            error_class="RpcTimeout",
            error_category="network",
            error_msg="等待 driver_result 超时",
            rpc_elapsed_ms=30000,
            sent_at=_now(),
        )
        session.add(cmd)

        await session.commit()

    async with factory() as session:
        loaded = (await session.execute(select(Run).where(Run.id == "run_smoke_001"))).scalar_one()
        assert loaded.execution_mode == "server_brain"
        assert loaded.dispatch_source == "api"
        assert loaded.trace_id == trace_id
        assert loaded.agent_id_at_start == "agt-A"
        assert loaded.agent_offline_at is None

        d = loaded.to_dict()
        assert d["execution_mode"] == "server_brain"
        assert d["dispatch_source"] == "api"
        assert d["trace_id"] == trace_id
        assert d["agent_id_at_start"] == "agt-A"
        assert d["agent_offline_at"] is None
        print("[ok] Run 新字段 round-trip & to_dict 正常")

        loaded_step = (
            await session.execute(select(RunStep).where(RunStep.run_id == "run_smoke_001"))
        ).scalar_one()
        assert loaded_step.driver_method == "click"
        assert loaded_step.command_id == msg_id
        assert loaded_step.rpc_elapsed_ms == 42
        sd = loaded_step.to_dict()
        assert sd["driver_method"] == "click"
        assert sd["command_id"] == msg_id
        assert sd["rpc_elapsed_ms"] == 42
        print("[ok] RunStep 新字段 round-trip & to_dict 正常")

        loaded_log = (
            await session.execute(select(RunLog).where(RunLog.run_id == "run_smoke_001"))
        ).scalar_one()
        assert loaded_log.trace_id == trace_id
        assert loaded_log.error_class == "RpcTimeout"
        assert loaded_log.error_category == "network"
        ld = loaded_log.to_dict()
        assert ld["trace_id"] == trace_id
        assert ld["error_class"] == "RpcTimeout"
        assert ld["error_category"] == "network"
        print("[ok] RunLog 新字段 round-trip & to_dict 正常")

        loaded_cmd = (
            await session.execute(select(RunCommand).where(RunCommand.run_id == "run_smoke_001"))
        ).scalar_one()
        assert loaded_cmd.message_id == msg_id
        assert loaded_cmd.method == "click"
        assert loaded_cmd.ok is False
        assert loaded_cmd.error_category == "network"
        assert loaded_cmd.rpc_elapsed_ms == 30000
        cd = loaded_cmd.to_dict()
        assert cd["message_id"] == msg_id
        assert cd["method"] == "click"
        assert cd["ok"] is False
        assert cd["error_category"] == "network"
        print("[ok] RunCommand round-trip & to_dict 正常")

    async with factory() as session:
        run_to_delete = (
            await session.execute(select(Run).where(Run.id == "run_smoke_001"))
        ).scalar_one()
        await session.delete(run_to_delete)
        await session.commit()

    async with factory() as session:
        remaining_cmds = (
            await session.execute(select(RunCommand).where(RunCommand.run_id == "run_smoke_001"))
        ).all()
        remaining_steps = (
            await session.execute(select(RunStep).where(RunStep.run_id == "run_smoke_001"))
        ).all()
        remaining_logs = (
            await session.execute(select(RunLog).where(RunLog.run_id == "run_smoke_001"))
        ).all()
        assert remaining_cmds == [], f"RunCommand 残留: {remaining_cmds}"
        assert remaining_steps == [], f"RunStep 残留: {remaining_steps}"
        assert remaining_logs == [], f"RunLog 残留: {remaining_logs}"
        print("[ok] cascade delete：Run 删除时 RunStep / RunLog / RunCommand 同时被清掉")

    async with factory() as session:
        legacy_run = Run(
            id="run_smoke_legacy",
            device_serial="emulator-5556",
            goal="模拟老链路",
            status="success",
        )
        session.add(legacy_run)
        await session.commit()

    async with factory() as session:
        legacy = (
            await session.execute(select(Run).where(Run.id == "run_smoke_legacy"))
        ).scalar_one()
        assert legacy.execution_mode == "agent_brain", "未填 execution_mode 时应回落到 agent_brain"
        assert legacy.dispatch_source is None
        assert legacy.trace_id is None
        assert legacy.agent_id_at_start is None
        print("[ok] 不填新字段的 Run 自动回落 agent_brain（向后兼容）")

    await dispose_engine()


def main() -> None:
    asyncio.run(_run_smoke())
    print("\n[PASS] Phase 1-B DB schema 冒烟全部通过")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"\n[FAIL] 断言失败：{exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] 异常：{type(exc).__name__}: {exc}")
        sys.exit(1)
