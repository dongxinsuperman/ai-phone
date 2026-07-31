#!/usr/bin/env python3
"""Export one DevEco official catalog JSON for Server import.

This is an explicit maintenance action, analogous to exporting/importing the
Android official CSV. It is not called by Agent runtime code.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ai_phone.agent.harmony_vm.capability import find_harmony_tools
from ai_phone.server.harmony_vm.catalog import normalize_manifest


ERROR_MARKERS = (
    "can not get image data",
    "cannot get image data",
    "download failed",
    "jsondataopt failed",
    "update cloud screen profiles failed",
    "network host not found",
)


def _run(args: list[str], timeout: float = 60.0) -> str:
    proc = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    output = "\n".join(
        part for part in (proc.stdout, proc.stderr) if part
    ).strip()
    if proc.returncode != 0 or any(
        marker in output.lower() for marker in ERROR_MARKERS
    ):
        raise RuntimeError(
            f"DevEco command failed rc={proc.returncode}: {output[-2000:]}"
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("harmony-vm-official-catalog.json"),
    )
    args = parser.parse_args()
    tools, missing = find_harmony_tools()
    if tools is None:
        raise RuntimeError(f"missing tools: {', '.join(missing)}")
    raw_manifest = {
        "images": _run([tools.emulator, "-imageList"]),
        "screen_profiles": _run(
            [tools.emulator, "-screenProfileList", "-details"]
        ),
        "emulator_version": _run([tools.emulator, "-version"], timeout=15.0),
    }
    normalized = normalize_manifest(raw_manifest)
    manifest = {
        **normalized,
        "source_url": "DevEco Emulator CLI official export",
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"exported {len(normalized['images'])} images and "
        f"{len(normalized['screen_profiles'])} screen profiles to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
