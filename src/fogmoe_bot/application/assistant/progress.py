"""@brief Codex 式 Agent 稳定进度项 / Codex-style stable Agent progress items.

进度项不是私有思维链，也不是 provider token 流。它们只在一个模型步骤已经形成
durable checkpoint，或一个工具调用已经形成 receipt 后发布。每个完成项通过
transactional outbox 成为独立 Telegram 消息，因此最终回答不会覆盖或删除先前过程。/
Progress items are neither private chain-of-thought nor provider token streams. They are published
only after a model step has a durable checkpoint or a tool call has a receipt. Every completed item
becomes an independent Telegram message through the transactional outbox, so the final answer never
overwrites or removes earlier work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from fogmoe_bot.domain.temporal import ensure_utc

from .tool_runtime import ToolExecutionContext

_ITEM_ID = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,199}$")
"""@brief Turn generation 内稳定 item ID 的语法 / Grammar for stable item IDs inside a Turn generation."""

_MAX_PROGRESS_TEXT = 4_096
"""@brief Telegram 稳定过程消息的字符上限 / Character limit for stable Telegram progress messages."""


class AssistantProgressKind(StrEnum):
    """@brief 可持久投递的 Agent 过程项类别 / Durably deliverable Agent progress-item kinds."""

    COMMENTARY = "commentary"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class AssistantProgressItem:
    """@brief 一个已经稳定、可以追加投递的 Agent 过程项 / One stable Agent progress item ready for append-only delivery.

    @param item_id 当前 input revision 内的稳定身份 / Stable identity inside the current input revision.
    @param kind 过程项类别 / Progress-item kind.
    @param text 用户可见的完整稳定文本 / Complete stable user-visible text.
    @param created_at 项形成时间 / Instant when the item became stable.
    @note ``text`` 必须是可公开摘要，不能包含工具参数、原始结果、日志或私有推理。/
        ``text`` must be a public summary and cannot contain tool arguments, raw results, logs, or
        private reasoning.
    """

    item_id: str
    kind: AssistantProgressKind
    text: str
    created_at: datetime

    def __post_init__(self) -> None:
        """@brief 校验稳定身份、文本和时间 / Validate stable identity, text, and timestamp.

        @return None / None.
        @raise ValueError ID 或文本不满足有界公开契约时抛出 /
            Raised when the ID or text violates the bounded public contract.
        """

        if _ITEM_ID.fullmatch(self.item_id) is None:
            raise ValueError("Assistant progress item_id has invalid syntax")
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or len(self.text) > _MAX_PROGRESS_TEXT
            or any(
                ord(character) < 32 and character not in {"\n", "\t"}
                for character in self.text
            )
        ):
            raise ValueError(
                "Assistant progress text must contain 1..4096 safe characters"
            )
        object.__setattr__(self, "text", self.text.strip())
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))


class AssistantProgressPersistence(Protocol):
    """@brief 以 generation fence 发布稳定过程项的持久化端口 /
    Persistence port publishing stable progress items behind a generation fence.
    """

    async def publish_progress(
        self,
        context: ToolExecutionContext,
        item: AssistantProgressItem,
    ) -> None:
        """@brief 幂等追加一个 durable progress outbound / Idempotently append one durable progress outbound.

        @param context 当前 Turn、投递流和 generation fence / Current Turn, delivery stream, and generation fence.
        @param item 已稳定过程项 / Stable progress item.
        @return None / None.
        @raise StaleClaimError generation 已被 steer 或其他 worker 取代时抛出 /
            Raised when steering or another worker superseded the generation.
        """

        ...


def commentary_progress_item(
    *,
    step: int,
    text: str,
    created_at: datetime,
) -> AssistantProgressItem:
    """@brief 从 checkpoint 文本形成 commentary 项 / Build a commentary item from checkpointed text.

    @param step Agent 模型步骤 / Agent model step.
    @param text 工具调用前的自然工作说明 / Natural work note preceding tool calls.
    @param created_at checkpoint 后的观察时刻 / Observation instant after checkpointing.
    @return 有界且稳定的 commentary 项 / Bounded stable commentary item.
    """

    if step < 0:
        raise ValueError("Assistant progress step cannot be negative")
    return AssistantProgressItem(
        item_id=f"step:{step}:commentary",
        kind=AssistantProgressKind.COMMENTARY,
        text=_bounded_progress_text(text),
        created_at=created_at,
    )


def tool_progress_item(
    *,
    invocation_id: str,
    tool_name: str,
    succeeded: bool,
    created_at: datetime,
) -> AssistantProgressItem:
    """@brief 形成 receipt-backed 工具完成项 / Build a receipt-backed tool completion item.

    @param invocation_id Turn 内稳定工具调用身份 / Stable tool invocation identity inside the Turn.
    @param tool_name 目录工具名称 / Catalog tool name.
    @param succeeded 工具是否形成可用结果 / Whether the tool produced a usable result.
    @param created_at receipt 后的观察时刻 / Observation instant after the receipt.
    @return 不含参数和结果的稳定工具项 / Stable tool item containing no arguments or results.
    """

    normalized_name = _validated_tool_name(tool_name)
    state_text = _tool_state_text(normalized_name, succeeded=succeeded)
    marker = "✓" if succeeded else "×"
    return AssistantProgressItem(
        item_id=f"tool:{invocation_id}",
        kind=AssistantProgressKind.TOOL,
        text=f"{marker} {state_text}\n  能力：{normalized_name}",
        created_at=created_at,
    )


def tool_started_progress_item(
    *,
    invocation_id: str,
    tool_name: str,
    created_at: datetime,
) -> AssistantProgressItem:
    """@brief 形成 checkpoint-backed 工具开始项 / Build a checkpoint-backed tool-start item.

    @param invocation_id Turn 内稳定工具调用身份 / Stable tool invocation identity inside the Turn.
    @param tool_name 目录工具名称 / Catalog tool name.
    @param created_at 工具开始前的观察时刻 / Observation instant before tool execution.
    @return 不含参数的 append-only 工具开始项 / Append-only tool-start item containing no arguments.
    """

    normalized_name = _validated_tool_name(tool_name)
    return AssistantProgressItem(
        item_id=f"tool:{invocation_id}:started",
        kind=AssistantProgressKind.TOOL,
        text=(f"✦ {_tool_action_text(normalized_name)}\n  能力：{normalized_name}"),
        created_at=created_at,
    )


def _bounded_progress_text(text: str) -> str:
    """@brief 在句子边界附近收束过长 commentary / Bound oversized commentary near a sentence boundary.

    @param text 模型 checkpoint 的公开文本 / Public text from a model checkpoint.
    @return Telegram 上限内完整或带省略号的文本 / Complete or ellipsized text within Telegram's limit.
    """

    normalized = text.strip()
    if len(normalized) <= _MAX_PROGRESS_TEXT:
        return normalized
    limit = _MAX_PROGRESS_TEXT - 1
    prefix = normalized[:limit]
    boundary = max(prefix.rfind(mark) for mark in ("。", "！", "？", ".", "!", "?"))
    if boundary >= limit // 2:
        prefix = prefix[: boundary + 1]
    return prefix.rstrip() + "…"


def _tool_state_text(tool_name: str, *, succeeded: bool) -> str:
    """@brief 渲染角色一致且不泄露结果的工具终态 / Render a persona-consistent tool terminal state without result leakage.

    @param tool_name 稳定目录工具名 / Stable catalog tool name.
    @param succeeded 是否成功 / Whether the call succeeded.
    @return 用户可见的一行完成说明 / One-line user-visible completion note.
    """

    copy: dict[str, tuple[str, str]] = {
        "get_help_text": ("能做的事情确认好啦", "唔，没能读到帮助信息"),
        "get_current_time": ("时间确认好啦", "时间暂时没确认上"),
        "list_available_stickers": ("贴纸包看完啦", "贴纸包暂时没打开"),
        "send_sticker": ("贴纸已经安排好啦", "贴纸没能顺利送出"),
        "send_workspace_file": ("工作区文件已经安排发送啦", "工作区文件没能顺利送出"),
        "google_search": ("网上资料查完啦", "网络资料暂时没查到"),
        "fetch_url": ("页面内容读完啦", "这个页面暂时读不到"),
        "fetch_group_context": ("前面的聊天线索看完啦", "聊天线索暂时没取到"),
        "run_bash": ("工作区里的验证跑完啦", "工作区验证时遇到问题了"),
        "generate_image": ("图片任务已经安排好啦", "这次图片没能生成"),
        "generate_voice": ("语音任务已经安排好啦", "这次语音没能生成"),
        "search_memory": ("相关回忆查过啦", "回忆里的线索暂时没找到"),
        "search_memory_by_time": ("那段时间的记录查过啦", "那段记录暂时没取到"),
        "schedule_ai_message": ("安排已经处理好啦", "这次安排没能处理好"),
        "user_diary": ("小日记处理好啦", "小日记暂时没处理好"),
    }
    completed, failed = copy.get(
        tool_name,
        ("这个步骤处理好啦", "这个步骤暂时没处理好"),
    )
    return completed if succeeded else failed


def _validated_tool_name(tool_name: str) -> str:
    """@brief 校验并规范化公开工具名 / Validate and normalize a public tool name.

    @param tool_name 目录工具名称 / Catalog tool name.
    @return 去除首尾空白后的名称 / Trimmed name.
    @raise ValueError 名称为空、过长或含控制字符时抛出 /
        Raised when the name is blank, oversized, or contains control characters.
    """

    normalized_name = tool_name.strip()
    if (
        not normalized_name
        or len(normalized_name) > 160
        or any(ord(character) < 32 for character in normalized_name)
    ):
        raise ValueError("Assistant progress tool_name has invalid syntax")
    return normalized_name


def _tool_action_text(tool_name: str) -> str:
    """@brief 渲染角色一致的工具当前动作 / Render a persona-consistent current tool action.

    @param tool_name 稳定目录工具名 / Stable catalog tool name.
    @return 用户可见的一行开始说明 / One-line user-visible start note.
    """

    copy: dict[str, str] = {
        "get_help_text": "我去看看现在能做些什么…",
        "get_current_time": "我确认一下现在的时间…",
        "list_available_stickers": "我去翻翻贴纸包…",
        "send_sticker": "我在挑合适的贴纸…",
        "send_workspace_file": "我在从工作区准备文件…",
        "google_search": "我去网上查查最新资料…",
        "fetch_url": "我在认真读这个页面…",
        "fetch_group_context": "我先看看前面的聊天线索…",
        "run_bash": "我在工作区里动手验证…",
        "generate_image": "我开始准备这张图啦…",
        "generate_voice": "我开始准备这段声音啦…",
        "search_memory": "我去回忆里找找相关线索…",
        "search_memory_by_time": "我按时间翻翻以前的记录…",
        "schedule_ai_message": "我在认真安排这件事…",
        "user_diary": "我去看看小日记…",
    }
    return copy.get(tool_name, "我正在用一个能力帮你处理…")


__all__ = [
    "AssistantProgressItem",
    "AssistantProgressKind",
    "AssistantProgressPersistence",
    "commentary_progress_item",
    "tool_progress_item",
    "tool_started_progress_item",
]
