"""Called only at the V3 entry; owns the one locator for this replay."""

from .client import JevConfig


async def create_locator(*, settings, driver, main_vlm_backend, emit, run_id):
    if not getattr(settings, "jev_enabled", False):
        return None
    config = JevConfig.from_settings(settings)
    from .adapters.android import AndroidAdapter
    from .adapters.harmony import HarmonyAdapter
    from .adapters.ios import IosAdapter
    from .locator import JevClickLocator

    adapters = {
        "android": AndroidAdapter,
        "harmony": HarmonyAdapter,
        "ios": IosAdapter,
        "ios_sim": IosAdapter,
    }
    platform = str(driver.platform)
    if platform not in adapters:
        raise ValueError(f"Jev does not support platform {platform}")
    locator = JevClickLocator(
        adapter=adapters[platform](driver),
        config=config,
        settings=settings,
        main_vlm_backend=main_vlm_backend,
        emit=emit,
        run_id=run_id,
    )
    locator.adapter.visual_enabled = True
    try:
        await locator.initialize()
        return locator
    except BaseException:
        await locator.aclose()
        raise
