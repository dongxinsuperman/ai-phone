"""Choose a channel once before V3 starts, not per click or by a model."""

from ai_phone.agent.async_utils import run_blocking


async def create_source(driver, *, borrowed_device=None):
    if getattr(driver, "platform", "") == "ios":
        from ai_phone.config import get_settings
        if getattr(get_settings(), "v3_ios_fast_enabled", False):
            from .ios_demand import IosDemandSource
            source = IosDemandSource(driver)
            try:
                await run_blocking(source.initialize)
                return source, "iOS WDA按需截图（无推流，启动检查通过）"
            except BaseException as exc:
                try:
                    await run_blocking(source.close)
                except Exception as cleanup_error:
                    from loguru import logger
                    logger.warning("iOS快速通道清理失败：{}", type(cleanup_error).__name__)
                if not isinstance(exc, Exception):
                    raise
                return None, f"原截图通道（iOS快速检查失败：{type(exc).__name__}）"
    if getattr(driver, "platform", "") == "harmony":
        from ai_phone.config import get_settings
        if not getattr(get_settings(), "v3_harmony_fast_enabled", False):
            return None, "原截图通道（鸿蒙快速分支关闭）"
        from .harmony import HarmonyFastChannel

        source = HarmonyFastChannel(driver)
        try:
            await run_blocking(source.initialize)
            return source, "鸿蒙原生JPEG及UITest直返树（启动检查通过）"
        except Exception as exc:
            source.close()
            return None, f"原截图通道（鸿蒙快速通道检查失败：{type(exc).__name__}）"
    # Other platforms keep their original capture/session logic.
    if getattr(driver, "platform", "") != "android":
        return None, "原截图通道（当前平台未接快速适配）"
    from .android import AndroidJpegSource

    source = AndroidJpegSource(driver, borrowed_device)
    try:
        await run_blocking(source.initialize)
        return source, "UiAutomator2原生JPEG（启动检查通过，复用连接）"
    except Exception as exc:
        await run_blocking(source.close)
        return None, f"原截图通道（快速通道启动检查未通过：{type(exc).__name__}）"
    except BaseException:
        await run_blocking(source.close)
        raise
