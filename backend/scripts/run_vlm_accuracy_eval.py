#!/usr/bin/env python3
"""Run the deterministic AI Phone VLM accuracy suite against a configured model."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from ai_phone.evals.vlm_accuracy import default_suite, run_accuracy_suite, suite_manifest


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Model or Ark endpoint ID. Required unless --list is used.")
    parser.add_argument(
        "--api-key-env",
        default="VLM_EVAL_API_KEY",
        help="Environment variable that contains the API key (default: VLM_EVAL_API_KEY).",
    )
    parser.add_argument(
        "--api-url",
        default="https://ark.cn-beijing.volces.com/api/v3/responses",
        help="Responses API URL.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Case ID to run; repeat this option to split a long evaluation into batches.",
    )
    parser.add_argument("--list", action="store_true", help="List cases without making model requests.")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    all_cases = default_suite()
    by_id = {case.id: case for case in all_cases}
    requested_ids = args.case_ids or list(by_id)
    unknown_ids = sorted(set(requested_ids) - set(by_id))
    if unknown_ids:
        raise SystemExit("Unknown case IDs: " + ", ".join(unknown_ids))
    selected_cases = tuple(by_id[case_id] for case_id in requested_ids)
    if args.list:
        print(json.dumps(suite_manifest(selected_cases), ensure_ascii=False, indent=2))
        return 0
    if not args.model:
        raise SystemExit("--model is required unless --list is used")
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise SystemExit(f"API key is empty: set environment variable {args.api_key_env}")
    report = asyncio.run(
        run_accuracy_suite(
            model=args.model,
            api_url=args.api_url,
            api_key=api_key,
            cases=selected_cases,
        )
    )
    rendered = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
