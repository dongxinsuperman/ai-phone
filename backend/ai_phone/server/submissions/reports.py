"""Submission item / 批次的自包含 HTML 报告（v1.2 时间线流式版）。

版本演进
--------
* v1.0：单 case = 顶部元信息 + 步骤区 + 底部一大块黑色日志。截图与日志分离，
  需要在两个区域间来回滚动比对，体验差。批次汇总 = 表格。
* v1.1：单 case = 步骤折叠卡片，截图嵌在卡内；批次 = SPA 左列表 + 右复用单
  case 渲染。问题：仍然按 step "卡片化"，与运行时的"日志流"观感不一致；术语
  全英文（``steps`` / ``elapsed`` / ``finished``）。
* v1.2（本文件）：
  - 单 case = **时间线流式视图**：step 与 RunLog 合并按时间正序排列，文字日志
    一行一条，step 事件展开为「思考 + 动作 + 操作前/后双图」，整体像运行时的
    日志面板从上往下流。无折叠、无侧边步骤导航。
  - 批次汇总 = **嵌套**：左侧执行单元（用 ``case_name`` 显示，``case_id`` 退到
    tooltip / 副标），右侧切到所选执行单元的"流式视图"（与单 case 同模板）。
  - 顶栏所有标签全部中文化（用例 / 平台 / 设备 / 步数 / 耗时 / 开始 / 结束 /
    Run ID / 状态原因 / 来源 ...）。

设计要点
--------
1. **图文一体、时间线驱动**：每条 step 卡的位置由 ``RunStep.created_at`` 决定，
   每条日志由 ``RunLog.ts`` 决定，统一排序后按时间从上往下渲染。
2. **零外部依赖**：所有 CSS/JS inline；图片走 ``/files/...`` 绝对路径。
3. **批次 SPA**：每条 item 的"完整 case 报告片段"全部 inline 进汇总页，左侧
   导航点击后纯 JS 切换，无需 iframe / 跳转；点击 case 时滚回顶部。
4. **case_name 优先**：标题、侧栏列表、modal 提示一律用 ``case_name``，
   ``case_id`` 仅出现在 chip / tooltip 里。

落盘规则
--------
* 单 case：``<storage_dir>/reports/<sub_id>/<case_id>__<platform>.html``
  对外 URL ``/files/reports/<sub_id>/<case_id>__<platform>.html``
* 批次汇总：``<storage_dir>/reports/<sub_id>/_summary.html``
  对外 URL ``/files/reports/<sub_id>/_summary.html``
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_phone.config import get_settings

from ..models import DeviceAlias, Run, RunLog, RunStep, Submission, SubmissionItem
from .paths import (
    SUMMARY_FILENAME,
    item_report_rel_path,
    item_report_url,
    submission_summary_rel_path,
    submission_summary_url,
)


REPORT_VERSION = "1.2"


# ---------------------------------------------------------------------------
# 路径工具 / URL helper
# ---------------------------------------------------------------------------


def _reports_root() -> Path:
    root = Path(get_settings().storage_dir).resolve() / "reports"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _item_rel_path(item: SubmissionItem) -> str:
    return item_report_rel_path(item.submission_id, item.case_id, item.platform)


def report_url_for_item(item: SubmissionItem) -> str:
    """对外可访问 URL；与 :func:`build_item_report_html` 生成路径一致。"""
    return item_report_url(item.submission_id, item.case_id, item.platform)


def summary_url_for_submission(submission_id: str) -> str:
    return submission_summary_url(submission_id)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _esc(v: Any) -> str:
    if v is None:
        return "—"
    return _html.escape(str(v), quote=False)


def _esc_attr(v: Any) -> str:
    """属性安全转义（含双引号）。"""
    if v is None:
        return ""
    return _html.escape(str(v), quote=True)


def _fmt_ts(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value)


def _fmt_ts_ms(value: Any) -> str:
    """时分秒 + 毫秒，用于时间线左列。"""
    if not value:
        return "--:--:--"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    return str(value)


def _fmt_elapsed(ms: Optional[int]) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms}ms"
    sec = ms / 1000.0
    if sec < 60:
        return f"{sec:.2f}s"
    m, s = divmod(int(sec), 60)
    return f"{m}m{s}s"


def _row_elapsed_ms(it: SubmissionItem) -> Optional[int]:
    if not it.started_at or not it.finished_at:
        return None
    try:
        return max(0, int((it.finished_at - it.started_at).total_seconds() * 1000))
    except Exception:  # noqa: BLE001
        return None


def _case_display_name(item: SubmissionItem) -> str:
    """用例展示名：优先 case_name，回落 case_id。"""
    return (item.case_name or "").strip() or item.case_id


_LEVEL_CLASS = {1: "lvl-info", 2: "lvl-warn", 3: "lvl-error"}
_LEVEL_LABEL = {1: "INFO", 2: "WARN", 3: "ERROR"}


_STATE_BADGE_LABEL = {
    "success": ("badge-success", "成功"),
    "failed": ("badge-failed", "失败"),
    "cancelled": ("badge-cancelled", "已取消"),
    "running": ("badge-running", "运行中"),
    "queued": ("badge-queued", "排队中"),
    "expired": ("badge-cancelled", "已过期"),
    "done": ("badge-success", "已完成"),
    "accepted": ("badge-running", "已受理"),
}


def _state_badge(state: str, *, size: str = "") -> str:
    cls, label = _STATE_BADGE_LABEL.get(
        state, ("badge-other", (state or "?"))
    )
    extra = f" {size}" if size else ""
    return f'<span class="badge {cls}{extra}">{_esc(label)}</span>'


_STATUS_REASON_ZH = {
    "completed": "正常完成",
    "agent_finished": "Agent 主动结束",
    "max_steps_reached": "达到最大步数",
    "step_failed": "步骤失败",
    "vlm_error": "VLM 异常",
    "device_offline": "设备离线",
    "timeout_run": "Run 超时",
    "timeout_step": "单步超时",
    "submission_timeout": "批次超时未派发",
    "cancelled_by_request": "外部取消",
    "cancelled_by_batch": "随批次取消",
    "internal_error": "内部异常",
}


def _status_reason_zh(code: str) -> str:
    if not code:
        return "—"
    return _STATUS_REASON_ZH.get(code, code)


_PLATFORM_ZH = {"android": "Android", "ios": "iOS", "harmony": "HarmonyOS"}


def _platform_label(platform: str) -> str:
    return _PLATFORM_ZH.get((platform or "").lower(), platform or "—")


# ---------------------------------------------------------------------------
# 公共 CSS / JS（深色主题，单 case 与批次 SPA 共用）
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif;
  background: #0f172a;
  color: #e2e8f0;
  line-height: 1.55;
}
a { color: #60a5fa; }
code { font-family: ui-monospace, Menlo, monospace; color: #a5f3fc; }

/* ---------- 顶栏 ---------- */
.hd {
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 12px;
  padding: 18px 22px;
  margin-bottom: 16px;
}
.hd-title {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.hd-title h1, .hd-title h2 {
  font-size: 20px;
  color: #f8fafc;
  font-weight: 600;
}
.hd-title .sub {
  color: #64748b;
  font-size: 13px;
  font-weight: 400;
}
.hd-subtitle {
  color: #94a3b8;
  font-size: 13px;
  margin: -4px 0 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.hd-subtitle .hd-sub-id {
  color: #64748b;
  font-family: ui-monospace, Menlo, monospace;
  font-size: 12px;
}
.hd-meta { display: flex; gap: 8px; flex-wrap: wrap; font-size: 12px; color: #94a3b8; align-items: center; }
.hd-meta .chip {
  background: #334155;
  padding: 4px 12px;
  border-radius: 6px;
  font-family: ui-monospace, Menlo, monospace;
  color: #cbd5e1;
}
.hd-meta .chip b { color: #94a3b8; font-weight: 500; margin-right: 6px; }
/* 设备 chip：别名 · serial 双保险，别名加粗提示，serial 仍用等宽以便复制 */
.hd-meta .chip .dev-alias {
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  font-weight: 700;
  color: #e2e8f0;
}
.hd-meta .chip .dev-sep { color: #64748b; margin: 0 2px; }
.hd-meta .chip .dev-serial { color: #cbd5e1; }

/* ---------- 状态徽章 ---------- */
.badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
  white-space: nowrap;
}
.badge.lg { font-size: 12px; padding: 4px 14px; }
.badge-success   { background: #064e3b; color: #6ee7b7; border: 1px solid #047857; }
.badge-failed    { background: #7f1d1d; color: #fca5a5; border: 1px solid #b91c1c; }
.badge-cancelled { background: #374151; color: #d1d5db; border: 1px solid #4b5563; }
.badge-running   { background: #1e3a8a; color: #93c5fd; border: 1px solid #2563eb; }
.badge-queued    { background: #1e293b; color: #93c5fd; border: 1px solid #334155; }
.badge-other     { background: #78350f; color: #fcd34d; border: 1px solid #b45309; }

/* ---------- 大节标题 ---------- */
.section { margin-top: 18px; }
.section-title {
  font-size: 13px;
  color: #cbd5e1;
  font-weight: 600;
  letter-spacing: 0.4px;
  margin-bottom: 8px;
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.section-title .hint { font-size: 11px; color: #64748b; font-weight: 400; }

/* ---------- runContent / token 卡 ---------- */
.runcontent {
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  padding: 12px 14px;
  font-size: 13px;
  color: #cbd5e1;
  white-space: pre-wrap;
  word-break: break-word;
}
/* ---------- 时间线 ---------- */
.timeline {
  background: #0a1120;
  border: 1px solid #1e293b;
  border-radius: 10px;
  padding: 8px 0;
}
.tl-row {
  display: grid;
  grid-template-columns: 92px 1fr;
  gap: 10px;
  padding: 4px 16px;
  align-items: start;
}
.tl-row + .tl-row { border-top: 1px solid #111c30; }
.tl-time {
  color: #64748b;
  font-family: ui-monospace, Menlo, monospace;
  font-size: 11.5px;
  padding-top: 2px;
}

/* ---------- 时间线 — 日志 ---------- */
.tl-log .tl-body {
  display: grid;
  grid-template-columns: 56px 1fr;
  gap: 10px;
  font-family: ui-monospace, Menlo, monospace;
  font-size: 12px;
  padding: 1px 0;
}
.tl-log .lvl { font-weight: 700; }
.tl-log.lvl-info  .lvl { color: #60a5fa; }
.tl-log.lvl-warn  .lvl { color: #fbbf24; }
.tl-log.lvl-error .lvl { color: #f87171; }
.tl-log .msg { color: #cbd5e1; white-space: pre-wrap; word-break: break-word; }

/* ---------- 时间线 — 步骤 ---------- */
.tl-step { background: #101a30; }
.tl-step .tl-body {
  background: #1e293b;
  border: 1px solid #334155;
  border-left: 3px solid #60a5fa;
  border-radius: 8px;
  padding: 12px 14px;
  margin: 4px 0;
}
.tl-step.t-finished .tl-body { border-left-color: #34d399; }
.tl-step.t-failed   .tl-body { border-left-color: #f87171; }
.tl-step.t-unknown  .tl-body { border-left-color: #fbbf24; }

.tl-step-hd {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #cbd5e1;
  margin-bottom: 8px;
  flex-wrap: wrap;
}
.tl-step-idx {
  color: #60a5fa;
  font-weight: 700;
  font-family: ui-monospace, Menlo, monospace;
}
.tl-step-tag {
  background: #1e3a8a;
  color: #bfdbfe;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-family: ui-monospace, Menlo, monospace;
  font-weight: 500;
}
.tl-step.t-finished .tl-step-tag { background: #064e3b; color: #6ee7b7; }
.tl-step.t-failed   .tl-step-tag { background: #7f1d1d; color: #fca5a5; }
.tl-step.t-unknown  .tl-step-tag { background: #78350f; color: #fcd34d; }
.tl-step-elapsed { color: #fbbf24; font-family: ui-monospace, Menlo, monospace; font-size: 11px; }

.tl-step-thought {
  background: #0f172a;
  border-left: 3px solid #fbbf24;
  border-radius: 4px;
  padding: 8px 12px;
  font-size: 13px;
  color: #e2e8f0;
  white-space: pre-wrap;
  word-break: break-word;
  margin-bottom: 8px;
}
.tl-step-action {
  background: #0f172a;
  border-left: 3px solid #60a5fa;
  border-radius: 4px;
  padding: 8px 12px;
  font-size: 12px;
  color: #a5f3fc;
  font-family: ui-monospace, Menlo, monospace;
  white-space: pre-wrap;
  word-break: break-all;
  margin-bottom: 10px;
}
.tl-step-shots {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
.tl-shot { display: flex; flex-direction: column; }
.tl-shot h5 {
  font-size: 11px;
  color: #94a3b8;
  margin-bottom: 4px;
  font-weight: 500;
}
.tl-shot img {
  width: 100%;
  border-radius: 6px;
  border: 1px solid #334155;
  cursor: zoom-in;
  background: #000;
  transition: opacity 0.12s;
  max-height: 520px;
  object-fit: contain;
  object-position: top;
}
.tl-shot img:hover { opacity: 0.92; }
.tl-shot .no-img {
  background: #0f172a;
  border: 1px dashed #334155;
  border-radius: 6px;
  padding: 28px;
  text-align: center;
  color: #475569;
  font-size: 12px;
  font-style: italic;
}

/* 没有数据 */
.empty {
  background: #0f172a;
  border: 1px dashed #334155;
  border-radius: 8px;
  padding: 18px;
  text-align: center;
  color: #64748b;
  font-size: 12px;
}

/* 截图大图 modal */
.modal {
  display: none;
  position: fixed;
  z-index: 9999;
  inset: 0;
  background: rgba(0, 0, 0, 0.92);
  justify-content: center;
  align-items: center;
}
.modal.on { display: flex; }
.modal img { max-width: 95%; max-height: 95%; border-radius: 6px; box-shadow: 0 0 60px rgba(0,0,0,0.5); }
.modal-tip {
  position: absolute;
  bottom: 24px;
  color: #94a3b8;
  font-size: 12px;
  background: rgba(0,0,0,0.6);
  padding: 6px 14px;
  border-radius: 4px;
}

/* 底部 */
.foot {
  text-align: center;
  color: #475569;
  font-size: 11px;
  margin-top: 28px;
  padding-bottom: 16px;
}

/* ---------- 单 case 容器 ---------- */
.single-container { max-width: 1200px; margin: 0 auto; padding: 24px; }

/* ---------- 批次 SPA 布局 ---------- */
.spa-layout { display: grid; grid-template-columns: 320px 1fr; min-height: 100vh; }
.sidebar {
  background: #0a1120;
  border-right: 1px solid #1e293b;
  padding: 20px 16px;
  overflow-y: auto;
  position: sticky;
  top: 0;
  max-height: 100vh;
}
.sb-title {
  font-size: 16px;
  color: #f8fafc;
  font-weight: 600;
  margin-bottom: 6px;
  letter-spacing: 0.2px;
  word-break: break-word;
  line-height: 1.35;
}
.sb-sub-id {
  font-family: ui-monospace, Menlo, monospace;
  font-size: 11px;
  color: #64748b;
  word-break: break-all;
  margin-bottom: 14px;
}
.sb-counts {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-bottom: 16px;
}
.sb-count-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 4px;
  padding: 3px 8px;
  font-size: 11px;
  color: #cbd5e1;
  font-family: ui-monospace, Menlo, monospace;
}
.sb-count-chip b { font-weight: 700; color: #f8fafc; }

.sb-section-title {
  font-size: 11px;
  color: #475569;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin: 14px 0 6px;
}

.sb-list { display: flex; flex-direction: column; gap: 4px; }
.sb-item {
  background: #1e293b;
  border: 1px solid #1e293b;
  border-left: 3px solid transparent;
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
  transition: all 0.12s;
}
.sb-item:hover { background: #273449; border-color: #334155; }
.sb-item.active {
  background: #1e3a8a;
  border-color: #2563eb;
  border-left-color: #60a5fa;
}
.sb-item.active .sb-item-case { color: #f8fafc; }

.sb-item-row1 {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.sb-item-idx { color: #64748b; font-size: 11px; font-family: ui-monospace, Menlo, monospace; min-width: 22px; }
.sb-item-case {
  color: #cbd5e1;
  font-size: 12px;
  font-weight: 500;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sb-item-row2 {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: #94a3b8;
  font-family: ui-monospace, Menlo, monospace;
}
.sb-item-platform {
  background: #334155;
  color: #cbd5e1;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
}
.sb-item-elapsed { color: #fbbf24; }
.sb-item-id {
  color: #475569;
  font-size: 10px;
  font-family: ui-monospace, Menlo, monospace;
}

.main {
  padding: 24px 28px;
  max-width: 1280px;
}

.case-pane { display: none; }
.case-pane.active { display: block; }

@media (max-width: 900px) {
  .spa-layout { grid-template-columns: 1fr; }
  .sidebar { position: static; max-height: none; border-right: 0; border-bottom: 1px solid #1e293b; }
  .main { padding: 16px; }
  .tl-row { grid-template-columns: 76px 1fr; padding: 4px 10px; }
  .tl-step-shots { grid-template-columns: 1fr; }
}
""".strip()


_JS = """
function _aiPhoneShowImg(src) {
  var m = document.getElementById('AI_PHONE_MODAL');
  if (!m) return;
  m.querySelector('img').src = src;
  m.classList.add('on');
}
function _aiPhoneCloseModal() {
  var m = document.getElementById('AI_PHONE_MODAL');
  if (m) m.classList.remove('on');
}
function _aiPhoneSelectCase(key) {
  document.querySelectorAll('.case-pane').forEach(function(p) {
    p.classList.toggle('active', p.getAttribute('data-case-key') === key);
  });
  document.querySelectorAll('.sb-item').forEach(function(it) {
    it.classList.toggle('active', it.getAttribute('data-case-key') === key);
  });
  var main = document.querySelector('.main');
  if (main) main.scrollTop = 0;
  window.scrollTo({top: 0, behavior: 'instant'});
}
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') _aiPhoneCloseModal();
});
""".strip()


_MODAL_HTML = (
    '<div class="modal" id="AI_PHONE_MODAL" onclick="_aiPhoneCloseModal()">'
    '<img alt="screenshot zoom"/>'
    '<div class="modal-tip">点击任意位置 / Esc 关闭</div>'
    '</div>'
)


# ---------------------------------------------------------------------------
# 时间线渲染（核心：合并 RunStep + RunLog 为按时间序的事件流）
# ---------------------------------------------------------------------------


def _classify_step_tag(action_type: str, unknown: int) -> str:
    if unknown:
        return "t-unknown"
    a = (action_type or "").lower()
    if a in ("finished", "done", "complete"):
        return "t-finished"
    if a in ("failed", "fail", "abort"):
        return "t-failed"
    return ""


def _shot_html(label: str, url: str) -> str:
    if not url:
        return (
            f'<div class="tl-shot"><h5>{_esc(label)}</h5>'
            f'<div class="no-img">无截图</div></div>'
        )
    return (
        f'<div class="tl-shot"><h5>{_esc(label)}</h5>'
        f'<img src="{_esc(url)}" alt="{_esc(label)}" '
        f'onclick="_aiPhoneShowImg(this.src)"/></div>'
    )


def _render_log_row(r: RunLog) -> str:
    lvl = int(r.level or 1)
    css = _LEVEL_CLASS.get(lvl, "lvl-info")
    label = _LEVEL_LABEL.get(lvl, "INFO")
    title = (r.title or "").strip()
    body = (r.content or "").strip()
    if title and body:
        msg = f"{title} — {body}"
    else:
        msg = title or body
    return (
        f'<div class="tl-row tl-log {css}">'
        f'  <span class="tl-time">{_esc(_fmt_ts_ms(r.ts))}</span>'
        f'  <div class="tl-body">'
        f'    <span class="lvl">{_esc(label)}</span>'
        f'    <span class="msg">{_esc(msg) if msg else "—"}</span>'
        f'  </div>'
        f'</div>'
    )


def _render_step_row(s: RunStep) -> str:
    tag_cls = _classify_step_tag(s.action_type, s.unknown)
    thought = (s.thought or "").strip()
    action_text = (s.action or "").strip()
    if not action_text and s.action_type:
        action_text = f"({s.action_type})"

    elapsed_label = (
        f"耗时 {_fmt_elapsed(int(s.elapsed_ms))}" if s.elapsed_ms else ""
    )

    head = (
        '<div class="tl-step-hd">'
        f'  <span class="tl-step-idx">步骤 #{_esc(s.step)}</span>'
        f'  <span class="tl-step-tag">{_esc(s.action_type or "?")}</span>'
        f'  <span class="tl-step-elapsed">{_esc(elapsed_label)}</span>'
        '</div>'
    )

    blocks: List[str] = [head]
    if thought:
        blocks.append(
            f'<div class="tl-step-thought"><b style="color:#fbbf24;'
            f'font-weight:600;margin-right:6px;">思考</b>{_esc(thought)}</div>'
        )
    if action_text:
        blocks.append(
            f'<div class="tl-step-action"><b style="color:#60a5fa;'
            f'font-weight:600;margin-right:6px;">动作</b>{_esc(action_text)}</div>'
        )
    blocks.append(
        '<div class="tl-step-shots">'
        f'{_shot_html("操作前", s.screenshot_before)}'
        f'{_shot_html("操作后", s.screenshot_after)}'
        '</div>'
    )

    return (
        f'<div class="tl-row tl-step {tag_cls}">'
        f'  <span class="tl-time">{_esc(_fmt_ts_ms(s.created_at))}</span>'
        f'  <div class="tl-body">{"".join(blocks)}</div>'
        f'</div>'
    )


@dataclass
class _TLEntry:
    ts: Optional[datetime]
    seq: int  # tie-breaker，保持稳定排序
    kind: str  # 'log' | 'step'
    html: str


def _build_timeline(steps: List[RunStep], logs: List[RunLog]) -> str:
    """合并 step / log 按时间正序输出，渲染为时间线 HTML。

    排序规则（同时间戳时）：
    - log 排在 step 前面（同一时刻通常先有日志、再有 step commit）；
    - 同类按出现顺序。
    """
    entries: List[_TLEntry] = []
    seq = 0
    for r in logs:
        entries.append(_TLEntry(ts=r.ts, seq=seq, kind="log", html=_render_log_row(r)))
        seq += 1
    for s in steps:
        entries.append(
            _TLEntry(ts=s.created_at, seq=seq, kind="step", html=_render_step_row(s))
        )
        seq += 1

    if not entries:
        return '<div class="empty">无运行记录</div>'

    def _key(e: _TLEntry) -> Tuple[int, int, int]:
        # ts None 排到最后；step 排在同时刻 log 之后（kind 'log' < 'step'）
        bucket = 1 if e.ts is None else 0
        ts_int = int(e.ts.timestamp() * 1000) if e.ts else 0
        kind_rank = 0 if e.kind == "log" else 1
        return (bucket, ts_int, kind_rank * 10_000_000 + e.seq)

    entries.sort(key=_key)
    return f'<div class="timeline">{"".join(e.html for e in entries)}</div>'


# ---------------------------------------------------------------------------
# 单 case 渲染（被单 case 文件 & 批次 SPA 复用）
# ---------------------------------------------------------------------------


def _format_device_label(serial: Optional[str], alias_map: Optional[Dict[str, str]]) -> str:
    """给 HTML 报告 chip 用的设备标签：``别名 · serial`` 组合形式。

    - 有别名：`别名 · serial`，别名加粗与 serial 视觉上拆开
    - 无别名 / 未绑定：只显 serial
    - 无 serial：显示 ``—``

    所有 HTML 片段在返回前都已经转义，调用方直接塞进 chip 即可。
    """
    if not serial:
        return "—"
    alias = (alias_map or {}).get(serial, "")
    if alias:
        return (
            f'<span class="dev-alias">{_esc(alias)}</span>'
            f'<span class="dev-sep"> · </span>'
            f'<span class="dev-serial">{_esc(serial)}</span>'
        )
    return f'<span class="dev-serial">{_esc(serial)}</span>'


async def _load_alias_map_for_serials(
    session: AsyncSession, serials: List[str]
) -> Dict[str, str]:
    """一次 SQL 拉出用到的 serial 的别名映射；没用到就返回空 dict，零开销。"""
    uniq = sorted({s for s in serials if s})
    if not uniq:
        return {}
    res = await session.execute(
        select(DeviceAlias.serial, DeviceAlias.alias).where(
            DeviceAlias.serial.in_(uniq)
        )
    )
    return {row.serial: row.alias for row in res.all()}


def _render_case_inner(
    *,
    item: SubmissionItem,
    run: Optional[Run],
    steps: List[RunStep],
    logs: List[RunLog],
    heading_level: int = 1,
    alias_map: Optional[Dict[str, str]] = None,
) -> str:
    """渲染从顶部元信息到底部 token 的整段 HTML 内容片段。

    单 case 文件 = framework + 这块片段；批次 SPA 也直接复用这块片段，每条
    item 一份，按 ``data-case-key`` 隐藏切换。``heading_level`` 控制最外层
    标题层级（单 case 用 ``<h1>``，SPA 内用 ``<h2>``）。
    """
    h_tag = f"h{max(1, min(heading_level, 6))}"

    elapsed_ms = int((run.elapsed_ms if run else 0) or 0)
    if not elapsed_ms:
        em = _row_elapsed_ms(item)
        elapsed_ms = em or 0

    case_name = _case_display_name(item)

    # ---- 顶栏 chips（全部中文标签）----
    chips = [
        f'<span class="chip"><b>用例 ID</b>{_esc(item.case_id)}</span>',
        f'<span class="chip"><b>平台</b>{_esc(_platform_label(item.platform))}</span>',
        f'<span class="chip"><b>设备</b>{_format_device_label(item.device_serial, alias_map)}</span>',
        f'<span class="chip"><b>步数</b>{_esc(run.steps if run else 0)}</span>',
        f'<span class="chip"><b>耗时</b>{_esc(_fmt_elapsed(elapsed_ms) if elapsed_ms else "—")}</span>',
        f'<span class="chip"><b>开始</b>{_esc(_fmt_ts(item.started_at))}</span>',
        f'<span class="chip"><b>结束</b>{_esc(_fmt_ts(item.finished_at))}</span>',
    ]
    if item.run_id:
        chips.append(f'<span class="chip"><b>Run ID</b>{_esc(item.run_id)}</span>')
    if item.status_reason:
        chips.append(
            f'<span class="chip"><b>状态原因</b>'
            f'{_esc(_status_reason_zh(item.status_reason))}</span>'
        )

    title_html = (
        f'<div class="hd-title">'
        f'  <{h_tag}>{_esc(case_name)}</{h_tag}>'
        f'  <span class="sub">· {_esc(_platform_label(item.platform))}</span>'
        f'  {_state_badge(item.state, size="lg")}'
        f'</div>'
    )
    head_html = (
        f'<div class="hd">'
        f'  {title_html}'
        f'  <div class="hd-meta">{"".join(chips)}</div>'
        f'</div>'
    )

    # ---- runContent ----
    rc_html = (
        '<div class="section">'
        '<div class="section-title">执行指令<span class="hint">runContent</span></div>'
        f'<div class="runcontent">{_esc(item.run_content) if item.run_content else "—"}</div>'
        '</div>'
    )

    # ---- 时间线（步骤 + 日志混排）----
    if not steps and not logs and not run:
        timeline_html = (
            '<div class="empty">本条目尚未真正执行（已在排队阶段被取消或批次超时），无运行记录。</div>'
        )
    else:
        timeline_html = _build_timeline(steps, logs)

    timeline_section = (
        '<div class="section">'
        f'<div class="section-title">运行时间线'
        f'<span class="hint">共 {len(steps)} 步 · {len(logs)} 条日志，按时间正序混排</span></div>'
        f'{timeline_html}'
        '</div>'
    )

    return head_html + rc_html + timeline_section


# ---------------------------------------------------------------------------
# 单 case：build_item_report_html
# ---------------------------------------------------------------------------


async def _load_run_bundle(
    session: AsyncSession,
    item: SubmissionItem,
    *,
    run: Optional[Run] = None,
) -> Tuple[Optional[Run], List[RunStep], List[RunLog]]:
    """同时拉 Run + steps + logs（按时间正序）。"""
    if not item.run_id:
        return None, [], []
    run_obj = run
    if run_obj is None or run_obj.id != item.run_id:
        run_obj = await session.get(Run, item.run_id)
    if run_obj is None:
        return None, [], []
    steps_res = await session.execute(
        select(RunStep)
        .where(RunStep.run_id == item.run_id)
        .order_by(RunStep.step.asc(), RunStep.id.asc())
    )
    steps: List[RunStep] = list(steps_res.scalars().all())
    logs_res = await session.execute(
        select(RunLog)
        .where(RunLog.run_id == item.run_id)
        .order_by(RunLog.ts.asc(), RunLog.id.asc())
    )
    logs: List[RunLog] = list(logs_res.scalars().all())
    return run_obj, steps, logs


async def build_item_report_html(
    session: AsyncSession,
    item: SubmissionItem,
    *,
    run: Optional[Run] = None,
) -> Optional[str]:
    """为一条终态 item 生成 HTML 报告，返回对外 URL。

    失败返回 ``None``；调用方据此决定是否把 ``reportUrl`` 置空。
    典型失败场景：``item`` 还没关联到 Run（即 queued 时被取消）。
    """
    if not item.run_id:
        return None

    try:
        run_obj, steps, logs = await _load_run_bundle(session, item, run=run)
        if run_obj is None:
            return None

        alias_map = await _load_alias_map_for_serials(session, [item.device_serial or ""])
        inner = _render_case_inner(
            item=item, run=run_obj, steps=steps, logs=logs, heading_level=1,
            alias_map=alias_map,
        )
        html_doc = _wrap_single_html(item=item, inner=inner)
        rel = _item_rel_path(item)
        out_path = _reports_root() / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_doc, encoding="utf-8")

        logger.debug(
            "[report] 生成 submission={} case={} platform={} run={} steps={} logs={} -> {}",
            item.submission_id, item.case_id, item.platform,
            item.run_id, len(steps), len(logs), out_path.name,
        )
        return f"/files/reports/{rel}"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[report] 生成失败 submission={} case={} platform={} err={}",
            item.submission_id, item.case_id, item.platform, exc,
        )
        return None


def _wrap_single_html(*, item: SubmissionItem, inner: str) -> str:
    title = f"{_case_display_name(item)} · {_platform_label(item.platform)}"
    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f'<title>{_esc(title)}</title>\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n<body>\n'
        '<div class="single-container">\n'
        f'{inner}\n'
        f'<div class="foot">ai-phone 报告 v{_esc(REPORT_VERSION)} · 生成于 {_esc(datetime.now(timezone.utc).isoformat())}</div>\n'
        '</div>\n'
        f'{_MODAL_HTML}\n'
        f'<script>{_JS}</script>\n'
        '</body>\n</html>\n'
    )


# ---------------------------------------------------------------------------
# 批次汇总：build_submission_summary_html
# ---------------------------------------------------------------------------


_PRIORITY_ORDER = {"failed": 0, "cancelled": 1, "success": 2}


def _pick_default_case_key(items: List[SubmissionItem], keys: List[str]) -> str:
    """挑默认选中的 case：优先 failed → cancelled → success → 列表第一。"""
    if not items:
        return ""
    pairs = list(zip(items, keys))
    pairs.sort(key=lambda x: (_PRIORITY_ORDER.get(x[0].state, 9), 0))
    for it, k in pairs:
        if it.state in ("failed", "cancelled"):
            return k
    return keys[0]


def _case_key(item: SubmissionItem) -> str:
    """SPA 内每条 case 的稳定 DOM key（avoid 特殊字符）。"""
    return f"{item.case_id}__{item.platform}__{item.id[-8:]}"


def _submission_overview_label(sub: Submission, counts: Dict[str, int]) -> str:
    total = sum(counts.values()) or 1
    succ = counts.get("success", 0)
    fail = counts.get("failed", 0)
    canc = counts.get("cancelled", 0)
    if sub.state == "expired":
        return "批次过期"
    if succ == total:
        return "全部成功"
    if fail == total:
        return "全部失败"
    if canc == total:
        return "全部取消"
    if fail and succ:
        return f"部分成功（成功 {succ} / 总数 {total}）"
    if fail:
        return f"失败为主（成功 {succ} / 失败 {fail} / 取消 {canc}）"
    return f"已结束（成功 {succ} / 失败 {fail} / 取消 {canc}）"


async def build_submission_summary_html(
    session: AsyncSession,
    submission: Submission,
    *,
    items: Optional[List[SubmissionItem]] = None,
) -> Optional[str]:
    """为整个 submission 生成一份 SPA 风格的汇总 HTML 报告。

    - 左侧侧边栏：批次概览 + 执行单元列表（用 ``case_name`` 显示，副标含
      ``case_id`` / 平台 / 耗时）
    - 右侧主区：对应执行单元的"运行时间线"（步骤 + 日志混排）
    - 默认选中第一条 ``failed`` / ``cancelled``，全成功时选第一条
    - 单文件：所有 case 数据 inline，无外部依赖

    生成失败返回 ``None``，调用方据此决定 ``summaryReportUrl`` 是否置空。
    """
    try:
        if items is None:
            res = await session.execute(
                select(SubmissionItem)
                .where(SubmissionItem.submission_id == submission.id)
                .order_by(SubmissionItem.enqueued_at.asc())
            )
            items = list(res.scalars().all())

        alias_map = await _load_alias_map_for_serials(
            session, [it.device_serial or "" for it in items]
        )
        bundles: List[Tuple[SubmissionItem, str]] = []
        for it in items:
            run_obj, steps, logs = await _load_run_bundle(session, it)
            inner = _render_case_inner(
                item=it, run=run_obj, steps=steps, logs=logs, heading_level=2,
                alias_map=alias_map,
            )
            bundles.append((it, inner))

        html_doc = _render_summary_spa_html(submission, bundles)
        rel = submission_summary_rel_path(submission.id)
        out_path = _reports_root() / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_doc, encoding="utf-8")

        logger.info(
            "[report:summary] 生成 submission={} items={} -> {}",
            submission.id, len(items), out_path.name,
        )
        return f"/files/reports/{rel}"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[report:summary] 生成失败 submission={} err={}",
            submission.id, exc,
        )
        return None


def _render_summary_spa_html(
    submission: Submission, bundles: List[Tuple[SubmissionItem, str]]
) -> str:
    items = [it for it, _ in bundles]
    keys = [_case_key(it) for it in items]
    default_key = _pick_default_case_key(items, keys)

    state_counts: Dict[str, int] = {}
    plat_counts: Dict[str, int] = {}
    total_elapsed = 0
    for it in items:
        state_counts[it.state] = state_counts.get(it.state, 0) + 1
        plat_counts[it.platform] = plat_counts.get(it.platform, 0) + 1
        em = _row_elapsed_ms(it)
        if em is not None:
            total_elapsed += em

    overview_label = _submission_overview_label(submission, state_counts)

    # ----- 侧边栏 -----
    count_chips: List[str] = [
        f'<span class="sb-count-chip"><b>{n}</b>{_esc(_STATE_BADGE_LABEL.get(st, ("","?"))[1])}</span>'
        for st, n in sorted(state_counts.items(), key=lambda x: x[0])
    ]
    if not count_chips:
        count_chips.append('<span class="sb-count-chip">无执行单元</span>')

    plat_chips = "".join(
        f'<span class="sb-count-chip">{_esc(_platform_label(p))} ×{n}</span>'
        for p, n in sorted(plat_counts.items())
    )
    plat_chips_html = plat_chips or '<span class="sb-count-chip">无</span>'

    sb_items_html: List[str] = []
    for idx, (it, _inner) in enumerate(bundles, start=1):
        k = keys[idx - 1]
        em = _row_elapsed_ms(it)
        em_label = f"⏱ {_fmt_elapsed(em)}" if em else "—"
        active_attr = ' active' if k == default_key else ''
        case_name = _case_display_name(it)
        tooltip = f"用例名: {case_name}\n用例 ID: {it.case_id}\n平台: {_platform_label(it.platform)}"
        sb_items_html.append(
            f'<div class="sb-item{active_attr}" data-case-key="{_esc_attr(k)}" '
            f'onclick="_aiPhoneSelectCase(\'{_esc_attr(k)}\')" title="{_esc_attr(tooltip)}">'
            f'  <div class="sb-item-row1">'
            f'    <span class="sb-item-idx">#{idx}</span>'
            f'    <span class="sb-item-case">{_esc(case_name)}</span>'
            f'    {_state_badge(it.state)}'
            f'  </div>'
            f'  <div class="sb-item-row2">'
            f'    <span class="sb-item-platform">{_esc(_platform_label(it.platform))}</span>'
            f'    <span class="sb-item-elapsed">{_esc(em_label)}</span>'
            f'  </div>'
            f'  <div class="sb-item-row2">'
            f'    <span class="sb-item-id" title="case_id">{_esc(it.case_id)}</span>'
            f'  </div>'
            f'</div>'
        )

    sb_list_html = "".join(sb_items_html) or '<div class="empty">无执行单元</div>'
    count_chips_html = "".join(count_chips)

    submission_name = (submission.submission_name or "").strip() or submission.id
    sidebar_html = (
        '<aside class="sidebar">'
        f'  <div class="sb-title" title="{_esc_attr(submission_name)}">{_esc(submission_name)}</div>'
        f'  <div class="sb-sub-id" title="submission_id: {_esc_attr(submission.id)}">批次 ID · {_esc(submission.id)}</div>'
        '  <div class="sb-section-title">状态分布</div>'
        '  <div class="sb-counts">'
        f'    {count_chips_html}'
        '  </div>'
        '  <div class="sb-section-title">平台分布</div>'
        '  <div class="sb-counts">'
        f'    {plat_chips_html}'
        '  </div>'
        f'  <div class="sb-section-title">执行单元（{len(items)}）</div>'
        '  <div class="sb-list">'
        f'    {sb_list_html}'
        '  </div>'
        '</aside>'
    )

    # ----- 主区：批次顶栏 + 每条 case pane -----
    overview_chips = [
        f'<span class="chip"><b>来源</b>{_esc(submission.origin)}</span>',
        f'<span class="chip"><b>状态</b>{_esc(_STATE_BADGE_LABEL.get(submission.state, ("","?"))[1])}</span>',
        f'<span class="chip"><b>受理</b>{_esc(_fmt_ts(submission.accepted_at))}</span>',
        f'<span class="chip"><b>结束</b>{_esc(_fmt_ts(submission.finished_at))}</span>',
        f'<span class="chip"><b>总数</b>{len(items)}</span>',
        f'<span class="chip"><b>总耗时</b>{_esc(_fmt_elapsed(total_elapsed) if total_elapsed else "—")}</span>',
    ]
    overview_html = (
        '<div class="hd">'
        f'  <div class="hd-title">'
        f'    <h1 title="批次 ID: {_esc_attr(submission.id)}">{_esc(submission_name)}</h1>'
        f'    {_state_badge(submission.state, size="lg")}'
        f'  </div>'
        f'  <div class="hd-subtitle">{_esc(overview_label)}'
        f'    <span class="hd-sub-id"> · 批次 ID {_esc(submission.id)}</span>'
        f'  </div>'
        f'  <div class="hd-meta">{"".join(overview_chips)}</div>'
        '</div>'
    )

    panes_html: List[str] = []
    for idx, ((_it, inner), k) in enumerate(zip(bundles, keys)):
        active_attr = ' active' if k == default_key else ''
        panes_html.append(
            f'<div class="case-pane{active_attr}" data-case-key="{_esc_attr(k)}">{inner}</div>'
        )

    if not panes_html:
        panes_html.append('<div class="empty">本批次没有任何执行单元记录</div>')

    main_html = (
        '<main class="main">'
        f'  {overview_html}'
        f'  {"".join(panes_html)}'
        f'  <div class="foot">ai-phone 批次汇总 v{_esc(REPORT_VERSION)} · '
        f'生成于 {_esc(datetime.now(timezone.utc).isoformat())}</div>'
        '</main>'
    )

    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f'<title>批次汇总 · {_esc(submission_name)}</title>\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n<body>\n'
        '<div class="spa-layout">\n'
        f'{sidebar_html}\n'
        f'{main_html}\n'
        '</div>\n'
        f'{_MODAL_HTML}\n'
        f'<script>{_JS}</script>\n'
        '</body>\n</html>\n'
    )


__all__ = [
    "build_item_report_html",
    "build_submission_summary_html",
    "report_url_for_item",
    "summary_url_for_submission",
    "REPORT_VERSION",
]
