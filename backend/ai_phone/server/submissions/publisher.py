"""``ResultPublisher`` 抽象 + 四个 v1 实现（Stdout / Null / Kafka / Webhook）。

设计目标：

- **调用方只看抽象**：scheduler 只拿 ``publish_terminal(event)``，不知道背后是
  stdout 还是 kafka；broker 换血时 scheduler 零改动。
- **stdout 默认 / kafka 真发**：按 ``AI_PHONE_BROADCAST_BACKEND`` 切。
  ``KafkaPublisher`` 默认走 aiokafka 真发；当 broker 未配置 / aiokafka 未装 /
  producer 启动失败时，自动回落到 "打结构化 loguru 日志" 的 mock 形态，
  保证主流程不受 broker 故障拖累。
- **WebhookPublisher 旁路**：与 backend 选择正交。投递时 callbackUrl 跟批次走，
  scheduler 会把 item 终态与 submission 终态事件都投递到该 URL（与 Kafka
  / stdout 主 publisher 并存）。失败发一次就吞，不重试不签名（v1.8 简化版）。
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
    """v1 Kafka 实现：aiokafka producer + at-least-once 语义。

    工作模式（按配置自动选）：
      1. 真发模式：``brokers`` 配置 + aiokafka 装好 + producer 启动成功 → 走真客户端
      2. mock 模式：以下任一条件触发，自动降级为打结构化日志，主流程不受影响：
           - ``AI_PHONE_KAFKA_BROKERS`` 未配置
           - ``aiokafka`` 未安装（pip install 'aiokafka>=0.11,<1'）
           - producer 启动失败（broker 不可达 / SASL 错 / 网络问题）

    关键契约：
      - topic = ``AI_PHONE_KAFKA_TOPIC``，默认 ``ai-phone.submission.result``
      - 分区键 = ``submissionId``（保证同一批次事件顺序）
      - value = UTF-8 编码的 JSON
      - acks=all + idempotent producer：at-least-once（broker 0.11+）
      - v1 只发终态，不广播 queued/running 中间态

    永不向上抛异常——广播是副作用，broker 挂不能拖死主流程。
    """

    name = "kafka"

    def __init__(
        self,
        *,
        brokers: Optional[str] = None,
        topic: str = DEFAULT_KAFKA_TOPIC,
        sasl_username: str = "",
        sasl_password: str = "",
    ) -> None:
        self._brokers = brokers or ""
        self._topic = topic
        self._sasl_username = sasl_username or ""
        self._sasl_password = sasl_password or ""
        self._started = False
        # 没 broker 直接进 mock；运行时若启动失败也会切到 mock
        self._mock = not bool(self._brokers)
        self._producer: Any = None

        if self._mock:
            logger.warning(
                "[broadcast:kafka] AI_PHONE_KAFKA_BROKERS 未配置，"
                "KafkaPublisher 进入 mock 模式（只打日志，不真发 Kafka）"
            )

    async def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True

        if self._mock:
            logger.info(
                "[broadcast:kafka] producer 启动（mock）| brokers=<unset> topic={}",
                self._topic,
            )
            return

        try:
            from aiokafka import AIOKafkaProducer  # type: ignore
        except ImportError:
            logger.error(
                "[broadcast:kafka] aiokafka 未安装，回落到 mock 模式。"
                "请执行 `pip install 'aiokafka>=0.11,<1'`"
            )
            self._mock = True
            return

        kwargs: Dict[str, Any] = {
            "bootstrap_servers": self._brokers,
            "linger_ms": 20,
            "acks": "all",
            "enable_idempotence": True,
            "request_timeout_ms": 10_000,
            "max_request_size": 4 * 1024 * 1024,
        }
        if self._sasl_username and self._sasl_password:
            kwargs.update(
                {
                    "security_protocol": "SASL_PLAINTEXT",
                    "sasl_mechanism": "PLAIN",
                    "sasl_plain_username": self._sasl_username,
                    "sasl_plain_password": self._sasl_password,
                }
            )

        try:
            self._producer = AIOKafkaProducer(**kwargs)
            await self._producer.start()
            logger.info(
                "[broadcast:kafka] producer 启动 | brokers={} topic={} sasl={}",
                self._brokers,
                self._topic,
                "on" if self._sasl_username else "off",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[broadcast:kafka] producer 启动失败，回落到 mock | brokers={} err={}",
                self._brokers,
                exc,
            )
            self._producer = None
            self._mock = True

    async def _send_async(self, key: str, payload: bytes) -> None:
        if self._mock or self._producer is None:
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
            return

        # 真发：aiokafka 内部已自带 retry + backoff + idempotent，外层只兜底异常
        await self._producer.send_and_wait(
            self._topic,
            value=payload,
            key=key.encode("utf-8") if key else None,
        )
        logger.bind(
            broadcast=True,
            topic=self._topic,
            kafka_key=key,
        ).debug(
            "[broadcast:kafka] sent topic={} key={} bytes={}",
            self._topic,
            key,
            len(payload),
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
        if self._producer is not None:
            try:
                await self._producer.stop()
                logger.info("[broadcast:kafka] producer 已关闭")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[broadcast:kafka] producer 关闭异常: {}", exc)
            finally:
                self._producer = None
        else:
            logger.info("[broadcast:kafka] producer 关闭（mock）")


class WebhookPublisher(ResultPublisher):
    """v1.8 一次性 HTTP 回调 publisher。

    定位：与 KafkaPublisher 平级但作用域不同——
      - KafkaPublisher：进程级单例，所有批次都广播到同一 broker / topic
      - WebhookPublisher：per-event 一次性使用。投递时 callbackUrl 跟批次走，
        scheduler 通知 worker 临时构造一个 WebhookPublisher，发一次就丢

    契约（按 Q1=B / Q3=A / Q4=A）：
      - 支持 submission.item.terminal（单条收口）与 submission.terminal（整批收口）
      - 不重试 / 不签名：发一次失败 WARN 吞异常，不影响主流程
      - 5s 超时，避免接收方挂掉拖死调度器
      - 推送 body 是终态事件原 JSON，与 Kafka 字节级等价

    与 Kafka 的关系：互不相干、可并存。后端 backend=kafka 时主 publisher 走 Kafka；
    同时只要批次带了 callbackUrl，scheduler 旁路也会对同一事件单发一次 webhook。
    """

    name = "webhook"

    def __init__(self, *, url: str, timeout_sec: float = 5.0) -> None:
        self._url = url
        self._timeout_sec = timeout_sec

    async def publish_terminal(self, event: Dict[str, Any]) -> None:
        try:
            payload = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[broadcast:webhook] 事件 JSON 化失败（跳过）: {}", exc)
            return

        try:
            import httpx  # type: ignore
        except ImportError:
            logger.warning(
                "[broadcast:webhook] httpx 未安装，跳过 webhook 推送 url={}",
                self._url,
            )
            return

        try:
            async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                resp = await client.post(
                    self._url,
                    content=payload,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
            if 200 <= resp.status_code < 300:
                logger.info(
                    "[broadcast:webhook] sent url={} status={} bytes={}",
                    self._url,
                    resp.status_code,
                    len(payload),
                )
            else:
                logger.warning(
                    "[broadcast:webhook] 接收方返回非 2xx url={} status={} body={!r}",
                    self._url,
                    resp.status_code,
                    resp.text[:200] if resp.text else "",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[broadcast:webhook] 发送失败（吞异常，不影响终态落位）url={} err={}",
                self._url,
                exc,
            )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def make_publisher(settings: Optional[Settings] = None) -> ResultPublisher:
    """按 ``AI_PHONE_BROADCAST_BACKEND`` 选择实现，不认的值回落到 stdout。

    注意：WebhookPublisher 不在这里构造——它是 per-event 旁路，由
    scheduler 通知 worker 根据 ``Submission.callback_url`` 临时新建。
    """
    s = settings or get_settings()
    backend = (s.broadcast_backend or "stdout").strip().lower()
    if backend == "kafka":
        brokers = getattr(s, "kafka_brokers", "") or ""
        topic = getattr(s, "kafka_topic", "") or DEFAULT_KAFKA_TOPIC
        sasl_user = getattr(s, "kafka_sasl_username", "") or ""
        sasl_pass = getattr(s, "kafka_sasl_password", "") or ""
        return KafkaPublisher(
            brokers=brokers,
            topic=topic,
            sasl_username=sasl_user,
            sasl_password=sasl_pass,
        )
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
    "WebhookPublisher",
    "make_publisher",
    "DEFAULT_KAFKA_TOPIC",
]
