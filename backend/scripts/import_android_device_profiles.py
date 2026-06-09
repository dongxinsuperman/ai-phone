#!/usr/bin/env python3
"""Import Android VM device profiles from internal catalog sources.

Examples:
  backend/.venv/bin/python backend/scripts/import_android_device_profiles.py \
    --source google-public \
    --url https://storage.googleapis.com/play_public/supported_devices.csv

  backend/.venv/bin/python backend/scripts/import_android_device_profiles.py \
    --source play-console \
    --csv /path/to/play-console-device-catalog.csv
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from ai_phone.server.android_vm.catalog import (
    clean_and_dedupe_profiles,
    parse_google_supported_devices_csv,
    parse_play_device_catalog_csv,
)
from ai_phone.server.db import get_session_factory, init_db, init_engine
from ai_phone.server.models import AndroidDeviceProfile


async def main() -> None:
    args = _parse_args()
    csv_text = _read_text(path=args.csv, url=args.url)
    collected_at = _parse_datetime(args.collected_at)
    if args.source == "google-public":
        rows = parse_google_supported_devices_csv(
            csv_text,
            source_url=args.source_url or args.url or str(args.csv or ""),
            collected_at=collected_at,
        )
    else:
        rows = parse_play_device_catalog_csv(
            csv_text,
            source_url=args.source_url or args.url or str(args.csv or ""),
            collected_at=collected_at,
        )
        # 与 Web 导入端点一致：预清洗（form factor/品牌归一、去重、多值拆分、
        # 派生 resolution_bucket / sdk_index、按列长度截断），否则筛选会异常。
        rows = clean_and_dedupe_profiles(rows)

    if args.dry_run:
        print(_summary(rows) | {"dry_run": True})
        return

    if args.db_url:
        init_engine(args.db_url)
    await init_db()

    factory = get_session_factory()
    imported = 0
    updated = 0
    async with factory() as session:
        for data in rows:
            existing = await _find_existing_profile(session, data)
            if existing is None:
                session.add(AndroidDeviceProfile(**data))
                imported += 1
            else:
                for key, value in data.items():
                    setattr(existing, key, value)
                updated += 1
        if rows:
            await session.commit()
    print(_summary(rows) | {"imported": imported, "updated": updated})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=("google-public", "play-console"),
        required=True,
        help="google-public imports identity-only rows as candidate_pending; play-console imports verified specs.",
    )
    parser.add_argument("--csv", type=Path, help="Local CSV path.")
    parser.add_argument("--url", help="CSV URL to download.")
    parser.add_argument("--source-url", default="", help="Source URL stored on imported rows.")
    parser.add_argument("--collected-at", default="", help="ISO datetime stored on imported rows.")
    parser.add_argument("--db-url", default="", help="Override AI_PHONE_DB_URL/db_url.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and summarize without DB writes.")
    args = parser.parse_args()
    if not args.csv and not args.url:
        parser.error("one of --csv or --url is required")
    if args.csv and args.url:
        parser.error("--csv and --url are mutually exclusive")
    return args


def _read_text(*, path: Path | None, url: str | None) -> str:
    if path is not None:
        data = path.read_bytes()
    else:
        assert url is not None
        with urlopen(url, timeout=60) as resp:
            data = resp.read()
    return _decode_csv_bytes(data)


def _decode_csv_bytes(data: bytes) -> str:
    head = data[:200]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in head:
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def _parse_datetime(value: str) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


async def _find_existing_profile(session: Any, data: dict[str, Any]) -> AndroidDeviceProfile | None:
    res = await session.execute(
        select(AndroidDeviceProfile).where(
            AndroidDeviceProfile.source_type == data.get("source_type", ""),
            AndroidDeviceProfile.brand == data.get("brand", ""),
            AndroidDeviceProfile.device == data.get("device", ""),
            AndroidDeviceProfile.model_code == data.get("model_code", ""),
            AndroidDeviceProfile.variant_key == data.get("variant_key", ""),
        )
    )
    return res.scalar_one_or_none()


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("verification_status") or "")
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(rows), "verification_counts": counts}


if __name__ == "__main__":
    asyncio.run(main())
