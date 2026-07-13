"""@brief Assistant 群上下文 operation / Assistant group-context operation."""

from collections.abc import Sequence
import json
from typing import Protocol, cast

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.chat.group_messages import (
    DEFAULT_GROUP_CONTEXT_MESSAGES,
    MAX_GROUP_CONTEXT_MESSAGES,
    GroupMessage,
)
from fogmoe_bot.domain.context.token_estimator import estimate_tokens
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.payloads import JsonValue

from .parsing import bounded_int


_GROUP_CONTEXT_MAX_TOKENS = 16_384
"""@brief 单次群上下文换入预算 / Token budget for one group-context page-in."""


class GroupContextReader(Protocol):
    """读取当前消息之前 canonical group projection 的窄端口。"""

    async def fetch_before(
        self,
        group_id: int,
        *,
        message_thread_id: int | None,
        before_message_id: int | None,
        limit: int,
    ) -> Sequence[GroupMessage]:
        """@brief 读取同一 Topic 的有界群消息窗口 / Read a bounded same-topic group-message window."""

        ...


async def fetch_group_context(
    request: ToolEffectRequest,
    *,
    groups: GroupContextReader,
) -> JsonValue:
    """@brief 读取当前消息之前的 canonical 群上下文 / Read canonical group context before the current message.

    @param request 已认证工具请求 / Authenticated tool request.
    @param groups 群消息读取端口 / Group-message reader.
    @return 当前 Agent Turn 独占的有界上下文 / Bounded context owned by the current Agent turn.
    """

    group_id = request.context.group_id
    if not request.context.is_group or group_id is None:
        return {"error": "This tool is available only in a group chat"}
    window_size = bounded_int(
        request.arguments,
        "window_size",
        minimum=1,
        maximum=MAX_GROUP_CONTEXT_MESSAGES,
        default=DEFAULT_GROUP_CONTEXT_MESSAGES,
    )
    messages = await groups.fetch_before(
        group_id,
        message_thread_id=request.context.message_thread_id,
        before_message_id=request.context.message_id,
        limit=window_size,
    )
    entries = [_entry(message) for message in messages]
    selected: list[JsonObject] = []
    for entry in reversed(entries):
        candidate = [entry, *selected]
        if _fits(request, group_id, candidate, total_count=len(entries)):
            selected = candidate
            continue
        truncated = _largest_fitting_entry(
            request,
            group_id,
            entry,
            selected,
            total_count=len(entries),
        )
        if truncated is not None:
            selected.insert(0, truncated)
        break
    return _payload(
        request,
        group_id,
        selected,
        omitted_count=len(entries) - len(selected),
    )


def _payload(
    request: ToolEffectRequest,
    group_id: int,
    messages: list[JsonObject],
    *,
    omitted_count: int,
) -> JsonObject:
    """@brief 构造群上下文工具结果 / Build a group-context tool result.

    @param request 已认证请求 / Authenticated request.
    @param group_id 当前群 ID / Current group identifier.
    @param messages 已预算的时间正序消息 / Budgeted chronological messages.
    @param omitted_count 因预算省略的更旧消息数 / Older messages omitted by the budget.
    @return 显式不可信、Topic-scoped payload / Explicitly untrusted topic-scoped payload.
    """

    return {
        "group_id": group_id,
        "message_thread_id": request.context.message_thread_id,
        "before_message_id": request.context.message_id,
        "trust": "untrusted_group_context",
        "omitted_older_messages": omitted_count,
        "messages": cast(list[JsonValue], messages),
    }


def _entry(message: GroupMessage) -> JsonObject:
    """@brief 映射带 speaker identity 的群消息 / Map a group message with speaker identity.

    @param message 规范群消息 / Canonical group message.
    @return JSON entry / JSON entry.
    """

    return {
        "message_id": message.message_id,
        "user_id": message.sender_user_id,
        "username": message.sender_username,
        "display_name": message.sender_name,
        "message_type": message.kind.value,
        "content": message.content,
        "created_at": message.created_at.isoformat(),
        "edited": message.edited,
        "truncated": False,
    }


def _fits(
    request: ToolEffectRequest,
    group_id: int,
    messages: list[JsonObject],
    *,
    total_count: int,
) -> bool:
    """@brief 判断候选结果是否满足硬预算 / Test whether a candidate obeys the hard budget.

    @param request 已认证请求 / Authenticated request.
    @param group_id 当前群 ID / Current group identifier.
    @param messages 候选消息 / Candidate messages.
    @param total_count 数据库返回总数 / Total rows returned by storage.
    @return 未超预算为 True / True when within budget.
    """

    encoded = json.dumps(
        _payload(
            request,
            group_id,
            messages,
            omitted_count=total_count - len(messages),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimate_tokens(encoded) <= _GROUP_CONTEXT_MAX_TOKENS


def _largest_fitting_entry(
    request: ToolEffectRequest,
    group_id: int,
    entry: JsonObject,
    newer: list[JsonObject],
    *,
    total_count: int,
) -> JsonObject | None:
    """@brief 二分截断最旧候选并保留更新消息 / Truncate the oldest candidate while retaining newer messages.

    @param request 已认证请求 / Authenticated request.
    @param group_id 当前群 ID / Current group identifier.
    @param entry 待截断消息 / Entry to truncate.
    @param newer 已接受的更新消息 / Already accepted newer entries.
    @param total_count 数据库返回总数 / Total rows returned by storage.
    @return 最大可容纳前缀；完全放不下为 None / Largest fitting prefix, or None.
    """

    content = entry.get("content")
    if not isinstance(content, str) or not content:
        return None
    low = 0
    high = len(content)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = dict(entry)
        candidate["content"] = content[:middle].rstrip() + "…"
        candidate["truncated"] = True
        if _fits(
            request,
            group_id,
            [candidate, *newer],
            total_count=total_count,
        ):
            low = middle
        else:
            high = middle - 1
    if low == 0:
        return None
    result = dict(entry)
    result["content"] = content[:low].rstrip() + "…"
    result["truncated"] = True
    return result
