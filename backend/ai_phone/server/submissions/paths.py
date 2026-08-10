"""Submission 报告 URL 计算——单独抽出来让 models.py 能不依赖 reports.py 就用。

路径契约与 :mod:`reports` 必须完全一致：

- 落盘：``<storage_dir>/reports/<submission_id>/<case_id>__<platform>.html``
- 对外 URL：``/files/reports/<submission_id>/<case_id>__<platform>.html``

非法字符（路径分隔符 / 空格 / 中文等）统一替换成下划线，避免挂载到静态文件
服务器时的路径歧义和注入风险。
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit


def safe_name(raw: str) -> str:
    """把任意字符串压成文件名安全形式；与 reports._safe_name 等价。"""
    if not raw:
        return "unnamed"
    out = []
    for ch in raw:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "unnamed"


def item_report_rel_path(submission_id: str, case_id: str, platform: str) -> str:
    """返回 ``<submission_id>/<case_id>__<platform>.html`` 形式的相对路径。"""
    return f"{safe_name(submission_id)}/{safe_name(case_id)}__{safe_name(platform)}.html"


def item_report_url(submission_id: str, case_id: str, platform: str) -> str:
    """返回 ``/files/reports/...`` 形式的对外可访问 URL。"""
    return f"/files/reports/{item_report_rel_path(submission_id, case_id, platform)}"


# Submission 级汇总报告。文件名以下划线开头，避免和真实 caseId 撞车
# （caseId 走 safe_name 后理论上不会以下划线开头，但显式区隔更稳）。
SUMMARY_FILENAME = "_summary.html"


def submission_summary_rel_path(submission_id: str) -> str:
    return f"{safe_name(submission_id)}/{SUMMARY_FILENAME}"


def submission_summary_url(submission_id: str) -> str:
    return f"/files/reports/{submission_summary_rel_path(submission_id)}"


def absolute_report_url(
    public_base_url: Optional[str], report_url: Optional[str]
) -> Optional[str]:
    """把报告相对路径补成外部可直接打开的 URL。

    ``public_base_url`` 来自提交请求自己的 scheme + host，因此企业版、
    个人版和本地环境共用一套逻辑，不硬编码任何部署域名。
    """
    if not report_url:
        return None
    if urlsplit(report_url).scheme in ("http", "https"):
        return report_url
    base = str(public_base_url or "").strip().rstrip("/")
    if not base:
        return report_url
    return f"{base}/{report_url.lstrip('/')}"


__all__ = [
    "safe_name",
    "item_report_rel_path",
    "item_report_url",
    "submission_summary_rel_path",
    "submission_summary_url",
    "absolute_report_url",
    "SUMMARY_FILENAME",
]
