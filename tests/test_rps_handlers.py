"""@brief 猜拳 Telegram callback 与薄适配器测试 / Tests for RPS Telegram callbacks and thin adapters."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from telegram import Update, User
from telegram.ext import ContextTypes

from fogmoe_bot.application.games.rps_service import (
    GameDelivery,
    MessageAddress,
    RPS_SERVICE_DATA_KEY,
    Rejected,
    RpsService,
    ServiceState,
    WaitingCreated,
)
from fogmoe_bot.application.games.rps_operations import (
    RestoredGame,
    RestoredWaiting,
    RpsMatchCode,
    RpsMatchResult,
    RpsMutationCode,
    RpsMutationResult,
    RpsRecoveryState,
    WaitingTerminalStatus,
)
from fogmoe_bot.domain.games import (
    AccountStatus,
    GameId,
    GameSession,
    GameVersion,
    Player,
    UserId,
    WaitingRoom,
)
from fogmoe_bot.presentation.telegram import rps_handlers
from fogmoe_bot.presentation.telegram.rps_handlers import (
    CallbackAction,
    RpsCallback,
    rps_callback_handler,
    waiting_keyboard,
)


class HandlerLedger:
    """@brief handler 测试所需的最小账户端口 / Minimal account port for handler tests."""

    def __init__(self) -> None:
        """@brief 初始化空持久状态 / Initialize empty durable state."""

        self.waiting: RestoredWaiting | None = None
        self.game: RestoredGame | None = None

    async def status(self, user_id: UserId) -> AccountStatus:
        """@brief 所有测试用户均可准入 / Admit every test user.

        @param user_id 玩家身份 / Player identity.
        @return 固定账户状态 / Fixed account status.
        """

        del user_id
        return AccountStatus(True, 10)

    async def load_recovery_state(self, *, tombstone_limit: int) -> RpsRecoveryState:
        """@brief 返回测试持久状态 / Return test durable state."""

        del tombstone_limit
        games = () if self.game is None else (self.game,)
        return RpsRecoveryState(self.waiting, games, ())

    async def create_waiting(self, room: WaitingRoom) -> bool:
        """@brief 创建等待房间 / Create a waiting room."""

        if self.waiting is not None:
            return False
        self.waiting = RestoredWaiting(room, None)
        return True

    async def finish_waiting(
        self,
        room: WaitingRoom,
        status: WaitingTerminalStatus,
        *,
        finished_at: datetime,
    ) -> RpsMutationResult:
        """@brief 结束等待房间 / Finish a waiting room."""

        del status, finished_at
        if self.waiting is None:
            return RpsMutationResult(RpsMutationCode.NOT_FOUND)
        self.waiting = None
        return RpsMutationResult(RpsMutationCode.APPLIED, room.version)

    async def start_game(
        self,
        room: WaitingRoom,
        session: GameSession,
        *,
        started_at: datetime,
    ) -> RpsMatchResult:
        """@brief 激活测试对局 / Activate a test game."""

        del room, started_at
        self.waiting = None
        self.game = RestoredGame(session, None)
        return RpsMatchResult(RpsMatchCode.STARTED, session.version, session)

    async def commit_choice(
        self,
        previous: GameSession,
        updated: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 提交测试选择 / Commit a test choice."""

        del previous, committed_at
        self.game = None if updated.outcome is not None else RestoredGame(updated, None)
        return RpsMutationResult(RpsMutationCode.APPLIED, updated.version)

    async def cancel_game(
        self,
        previous: GameSession,
        cancelled: GameSession,
        *,
        committed_at: datetime,
    ) -> RpsMutationResult:
        """@brief 取消测试对局 / Cancel a test game."""

        del previous, committed_at
        self.game = None
        return RpsMutationResult(RpsMutationCode.APPLIED, cancelled.version)

    async def bind_waiting_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        invitation: MessageAddress,
    ) -> bool:
        """@brief 绑定测试邀请 / Bind a test invitation."""

        if self.waiting is None:
            return False
        self.waiting = RestoredWaiting(self.waiting.room, invitation)
        return True

    async def bind_game_delivery(
        self,
        game_id: GameId,
        expected_version: GameVersion,
        delivery: GameDelivery,
    ) -> bool:
        """@brief 绑定测试对局地址 / Bind test game delivery."""

        del game_id, expected_version
        if self.game is None:
            return False
        self.game = RestoredGame(self.game.session, delivery)
        return True


class FakeQuery:
    """@brief 记录 callback answer 的测试查询 / Test query recording callback answers."""

    def __init__(self, data: str, user_id: int) -> None:
        """@brief 创建 callback 查询 / Create a callback query.

        @param data callback_data / callback_data.
        @param user_id 点击用户 / Clicking user.
        @return None / None.
        """

        self.data = data
        """@brief callback_data / callback_data."""
        self.from_user = User(id=user_id, first_name=f"user{user_id}", is_bot=False)
        """@brief Telegram 用户 DTO / Telegram user DTO."""
        self.answers: list[tuple[str | None, bool]] = []
        """@brief 发送过的 callback answers / Callback answers sent by the handler."""

    async def answer(
        self, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        """@brief 记录一次 callback answer / Record one callback answer.

        @param text 可选提示 / Optional text.
        @param show_alert 是否弹窗 / Whether to show an alert.
        @return None / None.
        """

        self.answers.append((text, show_alert))


class FakeBot:
    """@brief 记录 Telegram 编辑的测试 Bot / Test bot recording Telegram edits."""

    def __init__(self) -> None:
        """@brief 初始化编辑记录 / Initialize edit recording.

        @return None / None.
        """

        self.edits: list[dict[str, Any]] = []
        """@brief edit_message_text 调用 / edit_message_text calls."""

    async def edit_message_text(self, **kwargs: Any) -> None:
        """@brief 记录消息编辑 / Record a message edit.

        @param kwargs Telegram 编辑参数 / Telegram edit arguments.
        @return None / None.
        """

        self.edits.append(kwargs)


async def _run_service(
    service: RpsService,
) -> tuple[asyncio.Event, asyncio.Task[None]]:
    """@brief 通过 BackgroundService 契约运行 handler 测试服务 / Run a handler test service through the BackgroundService contract.

    @param service 猜拳应用服务 / RPS application service.
    @return 停止信号与运行任务 / Stop signal and run task.
    """

    stop_event = asyncio.Event()
    run_task = asyncio.create_task(service.run(stop_event))
    while service.state is ServiceState.NEW:
        await asyncio.sleep(0)
    assert service.state is ServiceState.RUNNING
    return stop_event, run_task


async def _stop_service(
    stop_event: asyncio.Event,
    run_task: asyncio.Task[None],
) -> None:
    """@brief 请求正常排空 / Request normal draining.

    @param stop_event 停止信号 / Stop signal.
    @param run_task 运行任务 / Run task.
    @return None / None.
    """

    stop_event.set()
    await run_task


def test_callback_codec_binds_action_to_game_and_version_within_telegram_limit() -> (
    None
):
    """@brief callback 同时绑定动作、游戏与版本且不超过 64 字节 / Callbacks bind action, game, and version within 64 bytes."""

    callback = RpsCallback(
        CallbackAction.SCISSORS,
        GameId("game_1234567890"),
        GameVersion(17),
    )
    encoded = callback.encode()

    assert encoded == "rps:g:s:game_1234567890:17"
    assert len(encoded.encode("utf-8")) <= 64
    assert RpsCallback.decode(encoded) == callback
    with pytest.raises(ValueError):
        RpsCallback.decode("rps_choice_rock_42")
    with pytest.raises(ValueError):
        RpsCallback.decode("rps:w:r:game_1234567890:17")


def test_waiting_keyboard_carries_the_same_game_and_version_on_every_button() -> None:
    """@brief 等待键盘的加入与取消按钮共享精确聚合身份 / Join and cancel buttons share the exact aggregate identity."""

    async def scenario() -> None:
        """@brief 创建真实应用房间后检查键盘 / Create a real application room and inspect its keyboard.

        @return None / None.
        """

        service = RpsService(
            ledger=HandlerLedger(),
            game_id_factory=lambda: GameId("game_keyboard"),
        )
        stop_event, run_task = await _run_service(service)
        try:
            result = await service.request_game(Player(UserId(1), "alice"))
            assert isinstance(result, WaitingCreated)
            keyboard = waiting_keyboard(result.room)
            raw_callbacks = [
                button.callback_data for button in keyboard.inline_keyboard[0]
            ]
            assert all(isinstance(value, str) for value in raw_callbacks)
            callbacks = [
                RpsCallback.decode(cast(str, value)) for value in raw_callbacks
            ]
            assert {callback.action for callback in callbacks} == {
                CallbackAction.JOIN,
                CallbackAction.CANCEL,
            }
            assert {callback.game_id for callback in callbacks} == {result.room.game_id}
            assert {callback.version for callback in callbacks} == {result.room.version}
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_handler_rejects_a_terminal_callback_as_stale_and_always_answers_query() -> (
    None
):
    """@brief handler 将终态墓碑 callback 映射为用户可见陈旧拒绝 / Handler maps a terminal callback tombstone to a visible stale rejection."""

    async def scenario() -> None:
        """@brief 创建并取消房间后点击旧按钮 / Click an old button after creating and cancelling its room.

        @return None / None.
        """

        service = RpsService(
            ledger=HandlerLedger(),
            game_id_factory=lambda: GameId("game_stale1"),
        )
        stop_event, run_task = await _run_service(service)
        try:
            waiting = await service.request_game(Player(UserId(1), "alice"))
            assert isinstance(waiting, WaitingCreated)
            cancelled = await service.cancel_waiting(
                waiting.room.host.user_id,
                waiting.room.game_id,
                waiting.room.version,
            )
            assert not isinstance(cancelled, Rejected)
            query = FakeQuery(
                RpsCallback(
                    CallbackAction.CANCEL,
                    waiting.room.game_id,
                    waiting.room.version,
                ).encode(),
                user_id=1,
            )
            bot = FakeBot()
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={RPS_SERVICE_DATA_KEY: service}),
                bot=bot,
            )
            update = SimpleNamespace(callback_query=query)

            await rps_callback_handler(
                cast(Update, update),
                cast(ContextTypes.DEFAULT_TYPE, context),
            )

            assert len(query.answers) == 1
            assert query.answers[0][1] is True
            assert "过期" in (query.answers[0][0] or "")
        finally:
            await _stop_service(stop_event, run_task)

    asyncio.run(scenario())


def test_presentation_adapter_has_no_database_or_account_dependencies() -> None:
    """@brief Telegram 适配器保持 DTO 与投递边界，不依赖数据库或账户实现 / Telegram adapter stays a DTO/delivery boundary without DB or account dependencies."""

    source = Path(rps_handlers.__file__).read_text(encoding="utf-8")
    application_source = (
        Path(rps_handlers.__file__).parents[2]
        / "application"
        / "games"
        / "rps_service.py"
    ).read_text(encoding="utf-8")
    infrastructure_source = (
        Path(rps_handlers.__file__).parents[2]
        / "infrastructure"
        / "database"
        / "rps_ledger.py"
    ).read_text(encoding="utf-8")
    assert "infrastructure.database" not in source
    assert "application.accounts" not in source
    assert "asyncio.create_task(" not in source
    assert "fogmoe_bot.infrastructure" not in application_source
    assert "fogmoe_bot.presentation" not in infrastructure_source
