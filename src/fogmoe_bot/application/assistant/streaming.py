"""@brief Assistant 流投影应用会话 / Assistant stream-projection application session.

本模块只拥有投影目标、出站端口与失败隔离编排。流活动、帧与状态迁移属于
``domain.assistant.streaming``；Telegram draft ID 与网络节流属于 Telegram adapter。/
This module owns only the projection target, outbound port, and failure-isolation orchestration.
Stream activities, frames, and transitions belong to ``domain.assistant.streaming``; Telegram
draft identifiers and network throttling belong to the Telegram adapter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from fogmoe_bot.domain.assistant.streaming import (
    AssistantStreamFrame,
    AssistantStreamState,
)

_LOGGER = logging.getLogger(__name__)
"""@brief Assistant 流投影失败日志器 / Logger for Assistant stream-projection failures."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantStreamTarget:
    """@brief 一条 Assistant 流的显式外部目标 / Explicit external target of one Assistant stream.

    @param chat_id 数值 chat ID 或私聊兼容目标 / Numeric chat ID or a private-chat-compatible target.
    @param is_group 是否属于群聊 / Whether the target belongs to a group chat.
    @param message_thread_id 可选 Topic ID / Optional topic identifier.
    """

    chat_id: int | str
    """@brief 外部 chat 目标 / External chat target."""

    is_group: bool
    """@brief 是否为群聊目标 / Whether this is a group target."""

    message_thread_id: int | None
    """@brief 可选 Topic 标识 / Optional topic identity."""

    def __post_init__(self) -> None:
        """@brief 校验私聊、群聊与 Topic 闭集 / Validate the private/group/topic closed set.

        @return None / None.
        @raise TypeError 目标字段类型非法时抛出 / Raised when target field types are invalid.
        @raise ValueError 目标组合非法时抛出 / Raised for an invalid target combination.
        """

        if isinstance(self.chat_id, int):
            if isinstance(self.chat_id, bool) or self.chat_id == 0:
                raise ValueError("Assistant stream chat_id cannot be zero")
        elif isinstance(self.chat_id, str):
            if not self.chat_id.strip():
                raise ValueError("Assistant stream chat_id cannot be blank")
        else:
            raise TypeError("Assistant stream chat_id must be an integer or string")
        if not isinstance(self.is_group, bool):
            raise TypeError("Assistant stream is_group must be a bool")
        if self.is_group and not isinstance(self.chat_id, int):
            raise ValueError("Group Assistant streams require an integer chat_id")
        if not self.is_group and self.message_thread_id is not None:
            raise ValueError("Private Assistant streams cannot have a message thread")
        if self.message_thread_id is not None and (
            isinstance(self.message_thread_id, bool)
            or not isinstance(self.message_thread_id, int)
            or self.message_thread_id <= 0
        ):
            raise ValueError("Assistant stream message_thread_id must be positive")


class AssistantStreamProjection(Protocol):
    """@brief Assistant 流帧的最佳努力投影端口 / Best-effort projection port for Assistant stream frames."""

    async def project(
        self,
        target: AssistantStreamTarget,
        frame: AssistantStreamFrame,
    ) -> None:
        """@brief 向显式目标投影最新累计帧 / Project the latest cumulative frame to an explicit target.

        @param target 本次流的外部目标 / External target of this stream.
        @param frame 已验证的 provider-neutral 累计帧 / Validated provider-neutral cumulative frame.
        @return None / None.
        @note 调用方必须隔离 adapter 失败；预览失败不能改变 durable inference 结果。/
            Callers must isolate adapter failures; preview failures cannot alter durable
            inference outcomes.
        """

        ...


@dataclass(slots=True, kw_only=True)
class AssistantStreamSession:
    """@brief 隔离 adapter 失败的一次 generation 流会话 / One generation stream session isolating adapter failures.

    @param target 显式投影目标 / Explicit projection target.
    @param state 领域流状态机 / Domain stream state machine.
    @param projection 最佳努力投影端口 / Best-effort projection port.
    """

    target: AssistantStreamTarget
    """@brief 本次流的外部目标 / External target of this stream."""

    state: AssistantStreamState
    """@brief 纯领域流状态 / Pure domain stream state."""

    projection: AssistantStreamProjection
    """@brief 最佳努力出站端口 / Best-effort outbound port."""

    async def start(self) -> None:
        """@brief 投影 STARTED 或 REVISED / Project STARTED or REVISED.

        @return None / None.
        """

        await self._project(self.state.current_frame)

    async def append(self, text: str, *, emitted_at: datetime) -> None:
        """@brief 累计 provider 文本并投影 delta / Accumulate provider text and project a delta.

        @param text provider 新增文本 / Provider text delta.
        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if not text or self.state.terminal:
            return
        await self._project(self.state.append(text, emitted_at=emitted_at))

    async def commentary(
        self,
        item_id: str,
        text: str,
        *,
        emitted_at: datetime,
    ) -> None:
        """@brief 投影已 checkpoint 的公开 commentary / Project public checkpointed commentary.

        @param item_id 当前 revision 内稳定 item ID / Stable item ID in the current revision.
        @param text 模型明确给用户的工作说明 / Work note explicitly addressed to the user.
        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project(self.state.commentary(item_id, text, emitted_at=emitted_at))

    async def tool_started(
        self,
        invocation_id: str,
        tool_name: str,
        *,
        emitted_at: datetime,
    ) -> None:
        """@brief 投影工具开始 / Project tool start.

        @param invocation_id 稳定调用身份 / Stable invocation identity.
        @param tool_name 目录工具名称 / Catalog tool name.
        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project(
            self.state.start_tool(
                invocation_id,
                tool_name,
                emitted_at=emitted_at,
            )
        )

    async def tool_finished(
        self,
        invocation_id: str,
        tool_name: str,
        *,
        succeeded: bool,
        emitted_at: datetime,
    ) -> None:
        """@brief 投影工具终态 / Project a tool terminal state.

        @param invocation_id 稳定调用身份 / Stable invocation identity.
        @param tool_name 目录工具名称 / Catalog tool name.
        @param succeeded 工具是否形成可用结果 / Whether the tool produced a usable result.
        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project(
            self.state.finish_tool(
                invocation_id,
                tool_name,
                succeeded=succeeded,
                emitted_at=emitted_at,
            )
        )

    async def complete(self, *, emitted_at: datetime) -> None:
        """@brief 最佳努力投影成功终态 / Best-effort project the successful terminal state.

        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project_terminal(self.state.complete(emitted_at=emitted_at))

    async def suspend(self, *, emitted_at: datetime) -> None:
        """@brief 最佳努力投影暂停态 / Best-effort project a suspended state.

        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project_terminal(self.state.suspend(emitted_at=emitted_at))

    async def fail(self, code: str, *, emitted_at: datetime) -> None:
        """@brief 最佳努力投影安全失败码 / Best-effort project a safe failure code.

        @param code 稳定低基数错误码 / Stable low-cardinality error code.
        @param emitted_at 观察时刻 / Observation instant.
        @return None / None.
        """

        if self.state.terminal:
            return
        await self._project_terminal(self.state.fail(code, emitted_at=emitted_at))

    async def _project_terminal(self, frame: AssistantStreamFrame) -> None:
        """@brief 让终态投影相对 task cancellation 原子化 / Make terminal projection atomic with respect to task cancellation.

        @param frame 已形成且使 session terminal 的帧 / Frame that has made the session terminal.
        @return None / None.
        """

        projection_task = asyncio.create_task(
            self._project(frame),
            name=(
                f"assistant-stream-terminal-{frame.turn_id.value.hex}-{frame.sequence}"
            ),
        )
        try:
            await asyncio.shield(projection_task)
        except asyncio.CancelledError:
            await projection_task
            raise

    async def _project(self, frame: AssistantStreamFrame) -> None:
        """@brief 隔离任意 connector 异常 / Isolate every connector exception.

        @param frame 待投影帧 / Frame to project.
        @return None / None.
        """

        try:
            await self.projection.project(self.target, frame)
        except Exception:
            _LOGGER.warning(
                "Assistant stream projection failed turn_id=%s kind=%s",
                frame.turn_id,
                frame.kind.value,
                exc_info=True,
            )


__all__ = [
    "AssistantStreamProjection",
    "AssistantStreamSession",
    "AssistantStreamTarget",
]
