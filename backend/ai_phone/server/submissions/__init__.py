"""v1 第 3 梯队：submission 终态广播 + HTML 报告 + 对外查询路由。

本包的职责边界（严格按 codex后续计划表.md v1 契约）：

* ``publisher`` —— ``ResultPublisher`` 抽象 + StdoutPublisher（默认）+
  KafkaPublisher（mock 占位，真实 broker 接入时只改这一块）。scheduler 在
  item 进入终态（success / failed / cancelled）时调一次 ``publish_terminal``。
* ``events``   —— 把 ``SubmissionItem`` + 关联 ``Run`` 序列化成"广播事件"字典。
  包含 reportUrl（如已生成）/ elapsedMs / tokenStats / statusReason / state 等
  供外部平台消费的最小字段集。
* ``reports``  —— 根据 Run/RunStep/RunLog 同步生成自包含 HTML 报告落盘到
  ``storage_dir/reports/<submission_id>/<case_id>__<platform>.html``。

本包**不** 触碰：三端 agent 执行流程、WS start_run/stop_run 协议、Run 现有字段。
对 scheduler 的侵入仅限于：构造时多接一个 publisher、on_run_done + cancel +
submission_timeout 三处各多发一次 publish_terminal 调用。
"""

from __future__ import annotations

from .events import (
    SUBMISSION_EVENT,
    build_submission_terminal_event,
    build_terminal_event,
)
from .paths import submission_summary_url
from .publisher import (
    KafkaPublisher,
    NullPublisher,
    ResultPublisher,
    StdoutPublisher,
    make_publisher,
)
from .reports import (
    build_item_report_html,
    build_submission_summary_html,
    report_url_for_item,
    summary_url_for_submission,
)

__all__ = [
    "ResultPublisher",
    "StdoutPublisher",
    "KafkaPublisher",
    "NullPublisher",
    "make_publisher",
    "build_terminal_event",
    "build_submission_terminal_event",
    "SUBMISSION_EVENT",
    "build_item_report_html",
    "build_submission_summary_html",
    "report_url_for_item",
    "summary_url_for_submission",
    "submission_summary_url",
]
