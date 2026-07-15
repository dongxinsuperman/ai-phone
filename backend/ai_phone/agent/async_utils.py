"""Agent 异步执行辅助。

设备 Driver 是同步接口，Run 通过线程池调用。普通 ``asyncio.to_thread`` 在外层
Task 被取消时只会停止等待，已经在线程里执行的设备命令仍可能继续；如果此时上报
Run 已结束并释放设备锁，下一条 Case 就可能和旧命令同时操作设备。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar


T = TypeVar("T")


async def run_blocking(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """在线程中执行同步调用；取消时等当前调用退出后再向上传递取消。

    这不会让被取消的 Run 继续跑后续步骤，只保证已经开始的那一个 Driver 调用先
    收口。上层收到 ``CancelledError`` 后即可安全上报终态并释放设备。
    """

    work = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # task.cancel() 不会终止正在运行的 Python 线程。即使重复收到 cancel，也要
        # 等这一个同步调用离开，避免锁释放后旧线程继续碰设备。
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - 取消优先，底层异常不覆盖取消语义
                break
        if work.done() and not work.cancelled():
            # work 恰好在进入 while 前失败时也要取走异常，避免后台 Task 留下
            # "exception was never retrieved"；对外仍以取消为最终语义。
            work.exception()
        raise


__all__ = ["run_blocking"]
