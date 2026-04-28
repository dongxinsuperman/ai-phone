"""Analytics 模块：单日大盘数据聚合 + 可选 AI 文本分析。

与 ``submissions/`` 子包并列，职责边界：

- 只做"读聚合"，不写库、不改 scheduler、不影响主流程
- 所有口径严格锁在 **"本地日历日"**（默认 ``Asia/Shanghai``），避免 UTC 时区
  和用户直觉偏 8 小时
- 设备健康刻意拆成"当日 / 历史" 两份：当日看短期波动；历史看设备本身好坏
- AI 分析走豆包 Chat API，**纯文本**上下文、**单日**切片、**手动触发**
"""
from .aggregator import aggregate_day, local_day_range, parse_date
from .ai import AnalyticsAIClient, AnalyticsAIError

__all__ = [
    "aggregate_day",
    "local_day_range",
    "parse_date",
    "AnalyticsAIClient",
    "AnalyticsAIError",
]
