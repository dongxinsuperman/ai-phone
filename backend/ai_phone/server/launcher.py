from __future__ import annotations

import uvicorn
from loguru import logger

from ai_phone.config import get_settings


def run(host: str, port: int, reload: bool = False) -> None:
    settings = get_settings()
    logger.info(
        "Starting ai-phone server | host={} port={} reload={} env={}",
        host,
        port,
        reload,
        settings.env,
    )
    uvicorn.run(
        "ai_phone.server.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )
