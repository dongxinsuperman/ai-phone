"""多协议 LLM 适配层 · 通用基类与协议契约。

本模块只定义"对外统一的形状"，不绑定任何家协议实现。下游 ``main/`` 与
``assistants/`` 子模块各自按家落地。设计原则（与上游讨论一致）：

1. **高冗余、低耦合**：每家协议一个独立文件，互不 import。Doubao 调整不会
   动 Claude / GPT，反之亦然。
2. **vlm_loop 单点接入**：runner 永远只通过 :func:`create_main_vlm` 与
   :func:`create_assistant` 工厂拿实例，不直接 import 各家实现。
3. **Decision 复用现有定义**：避免重复 dataclass 造成"两个 Decision 偷偷不
   兼容"的隐患——直接 re-export ``shared.vlm.Decision``，作为多协议的统一
   决策结果类型。
4. **Protocol 而非 ABC**：Python 鸭子类型 + ``typing.Protocol`` 比抽象基类
   更轻、更灵活。每家实现写四个签名一致的方法即可，不需要继承。

辅助系统目前固化为 3 个底层方法（覆盖 vlm_loop 现状的 5 个调用场景）：

    1. :meth:`BaseAssistant.match_package` - 起跑线包名匹配
       （特殊协议：返回"未匹配"时各家原始返回 NONE / null / 空，统一在
       适配层翻译成 ``""``，对外行为一致）
    2. :meth:`BaseAssistant.chat_text` - 通用纯文本对话
       （承载"通道判定 / 审判 / 子步骤拆解"3 个文本场景，由调用方传不同
       prompt + 是否开思考；返回原始文本，由调用方按各自协议解析）
    3. :meth:`BaseAssistant.verify_finished` - 断言系统终局裁决
       （唯一带图的辅助调用，前/后双图对照 + thought 文本，三态返回）

如未来需要再加辅助调用（例如"瞬态 UI 判定"如果改成走模型），只需在本协议
里加一个新方法 + 三家实现各加一份，再在 vlm_loop 接入即可——其他家会因
Protocol 校验报错暴露出"漏实现"的事实。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

# Decision / TokenCounter 复用现有定义，避免重复 dataclass 漂移。
# Doubao Responses 客户端是历史"既成事实"——它定义了 Decision 的字段，
# 多协议层全部围绕它转换。
from ai_phone.shared.vlm import Decision, TokenCounter

__all__ = [
    "Decision",
    "TokenCounter",
    "AnalysisResult",
    "BaseMainVLM",
    "BaseAssistant",
]


@dataclass
class AnalysisResult:
    """``BaseAssistant.analyze_text`` 的结构化返回。

    服务"大盘 AI 分析"这类需要展示 token 消耗 / 耗时的高级文本任务，与
    ``chat_text`` 仅返回 str 形成对照——后者面向辅助系统内部 4 处轻量
    Q&A，不需要透出 usage。

    各家协议 usage 字段差异已在适配层归一化为统一的 prompt/completion/
    total 三元组（Claude 的 input_tokens/output_tokens 在适配层翻译过来）。
    """

    model: str
    text: str
    elapsed_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class BaseMainVLM(Protocol):
    """主 VLM 协议契约：runner 只关心"喂截图 → 拿 Decision"这一件事。

    各家实现负责把 Decision.action_str / action_strs 填成项目统一动作 DSL
    （click(point=...) 等），坐标统一归一化到 0-1000 或在 ParsedAction 上
    显式标注 coord_space="absolute"，由 runner 反向缩放。
    """

    counter: TokenCounter
    system_prompt: str

    @property
    def last_prompt_tokens(self) -> int:
        """最近一次决策的 prompt_tokens，主循环用来判定是否触发会话分段。"""
        ...

    def add_hint(self, text: str) -> None:
        """主循环注入提示文本（卡死检测 / 未知动作保护等）。下一轮 decide 会带上。"""
        ...

    def should_reset_session(self) -> bool:
        """上一轮 prompt 超过阈值且已有会话 → 应该在下一轮请求前重置。

        非会话型协议（如 Claude / GPT 走 stateless tool 调用）实现可恒返
        ``False``；豆包 Responses API 这种服务端续历史的协议才会真有阈值
        判断。
        """
        ...

    def reset_session(self, resume_hint: Optional[str] = None) -> Optional[str]:
        """主动切断服务端会话，下一轮从 system 前缀重新开一段。

        返回被清理的旧 response id（字符串或 None），方便上层打日志。
        非会话型协议返回 ``None``。
        """
        ...

    async def decide(
        self,
        screenshot_bytes: bytes,
        *,
        mime: str = "image/jpeg",
    ) -> Decision:
        """输入一张截图 bytes，返回一次 VLM 决策（Thought + Action 字符串）。"""
        ...


class BaseAssistant(Protocol):
    """辅助系统协议契约：3 个底层方法，覆盖 vlm_loop 5 个调用场景。

    设计上"高冗余、低耦合"——每家自家协议+模型实现一份，vlm_loop 通过
    ``create_assistant()`` 拿实例后调用对应方法，不需要关心底层是哪家。
    """

    counter: TokenCounter
    """辅助系统调用产生的 token 统计同步累加到主 counter 上，便于一份报
    告里完整呈现 ``vlm:xxx / assistant:xxx`` 的总成本。"""

    async def match_package(
        self,
        app_name: str,
        packages: List[str],
    ) -> str:
        """① 起跑线：根据应用名从已安装包列表里挑出最佳包名。

        返回包名字符串，若模型判定"列表里没有匹配项"则返回空串 ``""``
        （历史协议是返回 "NONE"，统一在适配层翻译成空串，对外表现一致）。
        """
        ...

    async def chat_text(
        self,
        prompt: str,
        *,
        label: str = "辅助",
        thinking: bool = False,
    ) -> str:
        """通用纯文本对话调用，对应 vlm_loop 的 3 个场景：

        - "结构化分类"（通道判定）：``thinking=False``，返回 STRUCTURED/FREEFORM
        - "审判"（防偏移）：``thinking=True``，返回 OK / KILL:<reason>
        - "子步骤拆解"（起跑线对 case 操作步骤分段）：``thinking=False``，返回编号清单或 NONE

        ``label`` 仅用于 token 统计分桶（在 ``counter.record(scene=label, ...)``
        里区分场景），不影响协议本身。

        ``thinking`` 在三家协议下的语义：
        - 豆包：``payload.thinking.type = enabled/disabled``
        - Claude：``thinking.type = enabled`` + ``budget_tokens``（仅 4-thinking 系列生效）
        - OpenAI：仅 o-系列推理模型生效（``reasoning.effort``），普通模型忽略
        """
        ...

    async def verify_finished(
        self,
        *,
        prompt: str,
        prev_before_bytes: Optional[bytes],
        final_bytes: bytes,
        thinking: bool = True,
    ) -> str:
        """断言系统：finished 终局裁决，唯一带图的辅助调用。

        输入双图（前/后对照）+ 文本提示词。返回模型输出的原始文本，调用
        方按"PASS / FAIL / SKIP"协议解析（与现有 vlm_loop 行为一致）。

        ``prev_before_bytes`` 可为 ``None``（第一步就 finished），此时退
        化为单图模式（仅 final_bytes）。
        """
        ...

    async def analyze_text(
        self,
        *,
        system: str,
        user: str,
        label: str = "AI 分析",
        thinking: bool = False,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> AnalysisResult:
        """高级文本分析：system + user 两条消息，返回带 usage 的结构化结果。

        与 :meth:`chat_text` 的差异：

        - ``chat_text`` 只返回 str，面向辅助系统内部 4 处轻量 Q&A（包名匹配 /
          通道判定 / 审判 / 子步骤拆解），不需要 system / temperature / usage
        - ``analyze_text`` 返回 :class:`AnalysisResult`，面向需要展示 token
          消耗与耗时的高级文本任务（如大盘 AI 分析），强 system 控制格式 +
          可调 temperature 取得创造性总结

        ``temperature`` 在三家协议下的支持差异：

        - 豆包 Chat Completions：原生支持
        - Claude Messages API：原生支持
        - OpenAI Chat Completions：o-系列推理模型不支持 temperature，会被
          OpenAI 服务端静默忽略（不报错）；普通 GPT 模型按设置生效

        ``thinking`` 在三家下的语义同 :meth:`chat_text`。
        """
        ...
