"""@brief Agent 资产确认 Telegram callback 测试 / Tests for Agent asset-confirmation Telegram callbacks."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from telegram.ext import ContextTypes

from fogmoe_bot.application.asset_actions.callbacks import AssetActionCallbackData
from fogmoe_bot.application.asset_actions.models import (
    AssetActionDecisionCode,
    AssetActionDecisionCommand,
    AssetActionDecisionResult,
)
from fogmoe_bot.domain.asset_actions.confirmation import AssetActionDecision
from fogmoe_bot.presentation.telegram import asset_confirmation_handlers


_CONFIRMATION_ID = UUID("00000000-0000-0000-0000-000000000701")
"""@brief 测试确认 ID / Test confirmation identifier."""


class _Service:
    """@brief 记录 callback 决策的确认服务替身 / Confirmation-service double recording callback decisions."""

    def __init__(self, events: list[str]) -> None:
        """@brief 初始化事件序列 / Initialize the event sequence.

        @param events 共享可观察顺序 / Shared observable order.
        @return None / None.
        """

        self._events = events
        self.commands: list[AssetActionDecisionCommand] = []
        """@brief 接收到的决策命令 / Received decision commands."""

    async def decide(
        self,
        command: AssetActionDecisionCommand,
    ) -> AssetActionDecisionResult:
        """@brief 记录决策并返回取消结果 / Record a decision and return a cancellation result.

        @param command 经私聊边界校验的选择 / Choice validated by the private-chat boundary.
        @return 不改变资产的取消结果 / Cancellation result that changes no assets.
        """

        self._events.append("decide")
        self.commands.append(command)
        return AssetActionDecisionResult(AssetActionDecisionCode.CANCELLED)


def _update(
    *,
    callback_data: str,
    chat_type: str = "private",
    chat_id: int = 7,
    actor_id: int = 7,
    answer: object | None = None,
) -> object:
    """@brief 构造 callback Update 替身 / Build a callback Update double.

    @param callback_data 不可信但格式化的按钮数据 / Untrusted but formatted button data.
    @param chat_type Telegram chat 类型 / Telegram chat type.
    @param chat_id callback 所在 chat ID / Chat containing the callback.
    @param actor_id Telegram 认证点击者 ID / Telegram-authenticated clicking user ID.
    @param answer 可选 query.answer 替身 / Optional query.answer double.
    @return 最小 Update 形状 / Minimal Update shape.
    """

    query = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(id=actor_id),
        answer=answer or AsyncMock(),
    )
    return SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        update_id=701,
    )


def test_private_callback_answers_before_durable_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 私聊 callback 先清除 Telegram spinner 再调用确认服务 / A private callback clears the Telegram spinner before invoking the confirmation service."""

    async def scenario() -> None:
        """@brief 执行私聊批准 callback / Run the private approval callback.

        @return None / None.
        """

        events: list[str] = []

        async def answer(*args: object, **kwargs: object) -> None:
            """@brief 记录 callback 应答 / Record callback acknowledgement.

            @param args 位置参数 / Positional arguments.
            @param kwargs 关键字参数 / Keyword arguments.
            @return None / None.
            """

            del args, kwargs
            events.append("answer")

        service = _Service(events)
        monkeypatch.setattr(asset_confirmation_handlers, "_service", lambda _: service)
        update = _update(
            callback_data=AssetActionCallbackData(
                confirmation_id=_CONFIRMATION_ID,
                decision=AssetActionDecision.APPROVE,
            ).encode(),
            answer=answer,
        )

        await asset_confirmation_handlers.asset_action_confirmation_callback(
            update,  # type: ignore[arg-type]
            cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace()),
        )

        assert events == ["answer", "decide"]
        assert len(service.commands) == 1
        command = service.commands[0]
        assert command.confirmation_id == _CONFIRMATION_ID
        assert command.decision is AssetActionDecision.APPROVE
        assert command.actor_user_id == 7
        assert command.chat_id == 7
        assert command.update_id == 701

    asyncio.run(scenario())


def test_group_callback_is_rejected_before_confirmation_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 群聊 callback 在状态机之前被拒绝 / A group callback is rejected before the state machine."""

    async def scenario() -> None:
        """@brief 执行群聊拒绝 callback / Run the group-rejection callback.

        @return None / None.
        """

        def unexpected_service(_: object) -> object:
            """@brief 断言群聊不能读取 capability / Assert a group cannot read the capability.

            @param _ 忽略上下文 / Ignored context.
            @return 永不返回 / Never returns.
            @raise AssertionError capability 被错误读取时抛出 / Raised when the capability is read incorrectly.
            """

            raise AssertionError("group callback reached confirmation capability")

        monkeypatch.setattr(asset_confirmation_handlers, "_service", unexpected_service)
        answer = AsyncMock()
        update = _update(
            callback_data=AssetActionCallbackData(
                confirmation_id=_CONFIRMATION_ID,
                decision=AssetActionDecision.CANCEL,
            ).encode(),
            chat_type="supergroup",
            chat_id=-100,
            answer=answer,
        )

        await asset_confirmation_handlers.asset_action_confirmation_callback(
            update,  # type: ignore[arg-type]
            cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace()),
        )

        answer.assert_awaited_once_with(
            "资产确认只能由所有者在私聊中操作。",
            show_alert=True,
        )

    asyncio.run(scenario())


def test_answer_failure_never_enters_asset_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief Telegram 应答失败时不进入资产变更路径 / A Telegram acknowledgement failure never enters the asset-mutation path."""

    async def scenario() -> None:
        """@brief 执行应答失败场景 / Run the acknowledgement-failure scenario.

        @return None / None.
        """

        events: list[str] = []
        service = _Service(events)
        monkeypatch.setattr(asset_confirmation_handlers, "_service", lambda _: service)
        answer = AsyncMock(side_effect=RuntimeError("Telegram unavailable"))
        update = _update(
            callback_data=AssetActionCallbackData(
                confirmation_id=_CONFIRMATION_ID,
                decision=AssetActionDecision.APPROVE,
            ).encode(),
            answer=answer,
        )

        with pytest.raises(RuntimeError, match="Telegram unavailable"):
            await asset_confirmation_handlers.asset_action_confirmation_callback(
                update,  # type: ignore[arg-type]
                cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace()),
            )

        assert service.commands == []

    asyncio.run(scenario())
