"""``ResultPublisher`` 抽象 + 两个 v1 实现。

设计目标：

- **调用方只看抽象**：scheduler 只拿 ``publish_terminal(event)``，不知道背后是
  stdout 还是 kafka；broker 换血时 scheduler 零改动。
- **stdout 默认，kafka 占位**：按 ``AI_PHONE_BROADCAST_BACKEND`` 切。broker
  地址/topic 尚未到位，所以 ``KafkaPublisher`` 先实现成"打结构化 loguru 日志"
  的 mock，把真正发到 Kafka 的动作留成 ``TODO``，broker 到手后原地替换为
  ``aiokafka`` / ``kafka-python``。
- **永不抛异常到调用侧**：广播是副作用，广播挂不能把已经成功终态的 item
  拖回失败状态。publisher 内部捕获所有异常打 WARN 即可。
"""

from __future__ import annotations

import abc
import json
from typing import Any, Dict, Optional

from loguru import logger

from ai_phone.config import Settings, get_settings

# v1 对外终态 topic 名；分区键是 ``submissionId``（保证同一批次终态有序）
DEFAULT_KAFKA_TOPIC = "ai-phone.submission.result"


class ResultPublisher(abc.ABC):
    """submission item 终态广播抽象。scheduler 只依赖这个接口。"""

    name: str = "abstract"

    @abc.abstractmethod
    async def publish_terminal(self, event: Dict[str, Any]) -> None:
        """把一条终态事件广播出去；实现必须吞掉所有异常，绝不向上抛。"""

    async def close(self) -> None:
        """生命周期收尾。stdout 不需要；kafka 真接入后要 flush producer。"""
        return None


class NullPublisher(ResultPublisher):
    """什么都不做。只在"broadcast 完全禁用"场景用（比如单测）。"""

    name = "null"

    async def publish_terminal(self, event: Dict[str, Any]) -> None:  # noqa: D401
        return None


class StdoutPublisher(ResultPublisher):
    """v1 默认实现：把事件当成一行 JSON 打到 loguru，级别 INFO。

    选 loguru 而不是 ``print``：
      1. 和 server 其他日志走同一输出通道（单 tail 一份日志就能看广播流）
      2. 结构化字段保留在 ``extra`` 里，未来切 kafka 时 payload 字节级等价
    """

    name = "stdout"

    async def publish_terminal(self, event: Dict[str, Any]) -> None:
        try:
            line = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[broadcast:stdout] 事件 JSON 化失败（跳过）: {}", exc)
            return
        logger.bind(broadcast=True, topic=DEFAULT_KAFKA_TOPIC).info(
            "[broadcast:stdout] {}",
            line,
        )


class KafkaPublisher(ResultPublisher):
    """v1 的 Kafka **占位实现**。

    公司 broker 地址 / topic 名 / ACL 凭证尚未到位（见计划表『待确认问题』）。
    这里先实现一个"mock producer"——打结构化 loguru 日志、带 topic / 分区键 /
    payload 字节长度，外观和真接入时一致。broker 到手后替换 ``_send_async``
    为真 aiokafka 调用，其它保持不动。

    关键契约（见计划表『P0. 广播通道技术选型』）：
      - topic = ``ai-phone.submission.result``
      - 分区键 = ``submissionId``（保证同一批次事件顺序）
      - value = UTF-8 编码的 JSON
      - v1 **只发终态**，不广播 queued/running 中间态

    真接入示例（broker 到位后替换 ``_send_async``）::

        from aiokafka import AIOKafkaProducer
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._brokers,
            sasl_mechanism="PLAIN", security_protocol="SASL_PLAINTEXT",
            sasl_plain_username=..., sasl_plain_password=...,
            linger_ms=20, acks="all",
        )
        await self._producer.start()
        await self._producer.send_and_wait(self._topic, value=payload, key=key.encode())
    """

    name = "kafka"

    def __init__(
        self,
        *,
        brokers: Optional[str] = None,
        topic: str = DEFAULT_KAFKA_TOPIC,
    ) -> None:
        self._brokers = brokers or ""
        self._topic = topic
        self._started = False
        if not self._brokers:
            logger.warning(
                "[broadcast:kafka] AI_PHONE_KAFKA_BROKERS 未配置，"
                "KafkaPublisher 进入 mock 模式（只打日志，不真发 Kafka）"
            )

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        # TODO(broker-ready): 替换为 AIOKafkaProducer.start() / 客户端鉴权。
        logger.info(
            "[broadcast:kafka] producer 启动（mock）| brokers={} topic={}",
            self._brokers or "<unset>",
            self._topic,
        )

    async def _send_async(self, key: str, payload: bytes) -> None:
        """真接入点——现在是 mock，broker 到位后替换这个方法的实现。"""
        logger.bind(
            broadcast=True,
            topic=self._topic,
            kafka_key=key,
            kafka_mock=True,
        ).info(
            "[broadcast:kafka-mock] topic={} key={} bytes={} payload={}",
            self._topic,
            key,
            len(payload),
            payload.decode("utf-8", errors="replace"),
        )

    async def publish_terminal(self, event: Dict[str, Any]) -> None:
        try:
            await self._ensure_started()
            key = str(event.get("submissionId") or "")
            payload = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            await self._send_async(key, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[broadcast:kafka] 发送失败（吞异常，不影响终态落位）: {}",
                exc,
            )

    async def close(self) -> None:
        if not self._started:
            return
        # TODO(broker-ready): AIOKafkaProducer.stop()
        logger.info("[broadcast:kafka] producer 关闭（mock）")


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def make_publisher(settings: Optional[Settings] = None) -> ResultPublisher:
    """按 ``AI_PHONE_BROADCAST_BACKEND`` 选择实现，不认的值回落到 stdout。"""
    s = settings or get_settings()
    backend = (s.broadcast_backend or "stdout").strip().lower()
    if backend == "kafka":
        brokers = getattr(s, "kafka_brokers", "") or ""
        topic = getattr(s, "kafka_topic", "") or DEFAULT_KAFKA_TOPIC
        return KafkaPublisher(brokers=brokers, topic=topic)
    if backend in ("null", "none", "off", "disable"):
        return NullPublisher()
    if backend != "stdout":
        logger.warning(
            "[broadcast] 未知 broadcast_backend={!r}，回落到 stdout",
            backend,
        )
    return StdoutPublisher()


__all__ = [
    "ResultPublisher",
    "NullPublisher",
    "StdoutPublisher",
    "KafkaPublisher",
    "make_publisher",
    "DEFAULT_KAFKA_TOPIC",
]
