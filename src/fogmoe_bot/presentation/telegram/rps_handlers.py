"""@brief 猜拳 Telegram 薄适配器 / Thin Telegram adapter for rock-paper-scissors.

该模块只负责 Telegram DTO 映射、callback 编解码、文本渲染与投递；领域规则、金币
结算、并发控制和 timeout 均由 ``RpsService`` 拥有。/ This module only maps Telegram DTOs,
encodes callbacks, renders text, and delivers messages; ``RpsService`` owns domain rules,
coin settlement, concurrency, and timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.ext import ContextTypes

from fogmoe_bot.application.games.rps_service import (
    ChoiceRecorded,
    GameCancelled,
    GameDelivery,
    GameSettled,
    MatchStarted,
    MessageAddress,
    PlayerMessage,
    Rejected,
    RejectionCode,
    RPS_SERVICE_DATA_KEY,
    RpsService,
    WaitingCancelled,
    WaitingCreated,
    WaitingInvalidated,
)
from fogmoe_bot.domain.games import (
    Choice,
    GameCancellation,
    GameId,
    GameOutcome,
    GameSession,
    GameVersion,
    OutcomeKind,
    Player,
    UserId,
    WaitingRoom,
)


logger = logging.getLogger(__name__)

RPS_CALLBACK_PREFIX = "rps:"
"""@brief 猜拳 callback 的稳定 Telegram 前缀 / Stable Telegram callback prefix for RPS."""

_CHOICE_EMOJI: dict[Choice, str] = {
    Choice.ROCK: "👊",
    Choice.PAPER: "✋",
    Choice.SCISSORS: "✌️",
}
"""@brief 领域手势到展示表情的映射 / Mapping from domain choices to presentation emoji."""

_REJECTION_TEXT: dict[RejectionCode, str] = {
    RejectionCode.SERVICE_UNAVAILABLE: "游戏服务正在启动或关闭，请稍后重试。",
    RejectionCode.NOT_REGISTERED: "请先使用 /me 命令注册后再游玩。",
    RejectionCode.INSUFFICIENT_COINS: "您的金币不足，需要至少1枚金币才能开始游戏。",
    RejectionCode.ALREADY_WAITING: "您已经创建了一个游戏等待中，请等待其他玩家加入或取消当前游戏。",
    RejectionCode.ALREADY_IN_GAME: "您已经在一个游戏中，请先完成该游戏。",
    RejectionCode.SELF_JOIN: "这是您自己创建的游戏，请等待他人加入。",
    RejectionCode.CAPACITY_REACHED: "当前游戏会话已满，请稍后重试。",
    RejectionCode.NOT_FOUND: "该游戏不存在或已经结束。",
    RejectionCode.STALE_VERSION: "这个按钮已经过期，请使用最新的游戏消息。",
    RejectionCode.NOT_PARTICIPANT: "这不是您的游戏按钮。",
    RejectionCode.ALREADY_CHOSEN: "您已经做出了选择。",
    RejectionCode.GAME_NOT_READY: "游戏消息仍在准备中，请稍后再次点击。",
    RejectionCode.ROOM_INVALIDATED: "房主账户状态已变化，本次邀请已经失效。",
}
"""@brief 应用拒绝原因到中文用户提示的穷尽映射 / Exhaustive mapping from rejection codes to user-facing Chinese text."""


class CallbackAction(StrEnum):
    """@brief callback 协议动作 / Callback-protocol action."""

    JOIN = "j"
    """@brief 加入等待房间 / Join a waiting room."""

    CANCEL = "c"
    """@brief 取消等待房间 / Cancel a waiting room."""

    ROCK = "r"
    """@brief 选择石头 / Choose rock."""

    PAPER = "p"
    """@brief 选择布 / Choose paper."""

    SCISSORS = "s"
    """@brief 选择剪刀 / Choose scissors."""

    @property
    def choice(self) -> Choice | None:
        """@brief 将选择动作映射为领域手势 / Map a choice action to a domain hand shape.

        @return 加入/取消时为 None，否则为领域手势 / Domain choice, or None for join/cancel.
        """

        return {
            CallbackAction.ROCK: Choice.ROCK,
            CallbackAction.PAPER: Choice.PAPER,
            CallbackAction.SCISSORS: Choice.SCISSORS,
        }.get(self)


@dataclass(frozen=True, slots=True)
class RpsCallback:
    """@brief 版本化且绑定游戏身份的 callback DTO / Versioned callback DTO bound to a game identity.

    @param action callback 动作 / Callback action.
    @param game_id 游戏身份 / Game identity.
    @param version 按钮渲染时的聚合版本 / Aggregate version when the button was rendered.
    """

    action: CallbackAction
    """@brief callback 动作 / Callback action."""

    game_id: GameId
    """@brief 游戏身份 / Game identity."""

    version: GameVersion
    """@brief 聚合版本 / Aggregate version."""

    def encode(self) -> str:
        """@brief 编码为不超过 64 字节的 Telegram callback_data / Encode as Telegram callback_data within 64 bytes.

        @return 紧凑 callback 字符串 / Compact callback string.
        @raises ValueError 编码超过 Telegram 限制 / If encoding exceeds Telegram's limit.
        """

        scope = (
            "w" if self.action in {CallbackAction.JOIN, CallbackAction.CANCEL} else "g"
        )
        encoded = f"{RPS_CALLBACK_PREFIX}{scope}:{self.action.value}:{self.game_id.value}:{self.version.value}"
        if len(encoded.encode("utf-8")) > 64:
            raise ValueError("RPS callback_data exceeds Telegram's 64-byte limit")
        return encoded

    @classmethod
    def decode(cls, value: str) -> RpsCallback:
        """@brief 严格解析 callback_data / Strictly parse callback_data.

        @param value Telegram callback_data / Telegram callback_data.
        @return 类型化 callback DTO / Typed callback DTO.
        @raises ValueError 格式、scope、动作或版本无效 / If format, scope, action, or version is invalid.
        """

        if not isinstance(value, str):
            raise TypeError("callback data must be a string")
        parts = value.split(":")
        if len(parts) != 5 or parts[0] != "rps":
            raise ValueError("invalid RPS callback format")
        _prefix, scope, raw_action, raw_game_id, raw_version = parts
        action = CallbackAction(raw_action)
        expected_scope = (
            "w" if action in {CallbackAction.JOIN, CallbackAction.CANCEL} else "g"
        )
        if scope != expected_scope:
            raise ValueError("RPS callback action does not match its scope")
        try:
            version = GameVersion(int(raw_version))
        except (TypeError, ValueError) as error:
            raise ValueError("invalid RPS callback version") from error
        return cls(action=action, game_id=GameId(raw_game_id), version=version)


class TelegramRpsLifecycleSink:
    """@brief 将应用生命周期事件投递到 Telegram / Deliver application lifecycle events to Telegram.

    @param bot PTB Bot 投递客户端 / PTB Bot delivery client.
    """

    def __init__(self, bot: Bot) -> None:
        """@brief 创建 Telegram 生命周期适配器 / Create a Telegram lifecycle adapter.

        @param bot PTB Bot 投递客户端 / PTB Bot delivery client.
        @return None / None.
        """

        self._bot = bot

    async def waiting_expired(self, event: WaitingCancelled) -> None:
        """@brief 编辑已过期邀请 / Edit an expired invitation.

        @param event 等待房间移除事件 / Waiting-room removal event.
        @return None / None.
        """

        if event.invitation is None:
            return
        await _edit_best_effort(
            self._bot,
            event.invitation,
            "⌛ 石头剪刀布游戏邀请已超时取消。",
        )

    async def game_cancelled(self, event: GameCancelled) -> None:
        """@brief 编辑所有对局取消消息 / Edit every message for a cancelled game.

        @param event 已退款取消事件 / Refunded cancellation event.
        @return None / None.
        """

        text = _cancelled_text(event.session)
        delivery = event.delivery
        if delivery is None:
            return
        addresses = [message.address for message in delivery.player_messages]
        if delivery.announcement is not None:
            addresses.insert(0, delivery.announcement)
        for address in addresses:
            await _edit_best_effort(self._bot, address, text)


async def rps_game_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@brief 将 `/rps_game` DTO 映射为应用请求并投递结果 / Map `/rps_game` DTO to an application request and deliver it.

    @param update Telegram Update / Telegram Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or chat is None:
        return
    service = _service(context)
    result = await service.request_game(_player_from_user(user))
    if isinstance(result, Rejected):
        await message.reply_text(_rejection_text(result))
        return
    if isinstance(result, WaitingInvalidated):
        if result.invitation is not None:
            await _edit_best_effort(
                context.bot,
                result.invitation,
                "该邀请已失效：房主当前金币不足或账户不可用。",
            )
        await message.reply_text(
            "原等待邀请已经失效，请再次使用 /rps_game 创建新游戏。"
        )
        return
    if isinstance(result, WaitingCreated):
        sent = await message.reply_text(
            _waiting_text(),
            reply_markup=waiting_keyboard(result.room),
        )
        bound = await service.bind_waiting_delivery(
            result.room.game_id,
            result.room.version,
            MessageAddress(chat.id, sent.message_id),
        )
        if not bound:
            await sent.edit_text("该游戏邀请已过期，请重新使用 /rps_game。")
        return
    delivered = await _deliver_match(
        context.bot,
        service,
        result,
        joining_chat_id=chat.id,
    )
    if not delivered:
        await message.reply_text("创建游戏失败，双方金币已退还，请稍后重试。")


async def rps_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """@brief 解析版本化 callback 并投递应用结果 / Parse a versioned callback and deliver its application result.

    @param update Telegram callback Update / Telegram callback Update.
    @param context PTB callback context / PTB callback context.
    @return None / None.
    """

    query = update.callback_query
    if query is None:
        return
    try:
        callback = RpsCallback.decode(query.data or "")
    except TypeError, ValueError:
        await query.answer("无效的游戏按钮。", show_alert=True)
        return

    service = _service(context)
    actor = UserId(query.from_user.id)
    if callback.action is CallbackAction.JOIN:
        join_result = await service.join_game(
            _player_from_user(query.from_user),
            callback.game_id,
            callback.version,
        )
        if isinstance(join_result, Rejected):
            await query.answer(_rejection_text(join_result), show_alert=True)
            return
        if isinstance(join_result, WaitingInvalidated):
            await query.answer(
                _REJECTION_TEXT[RejectionCode.ROOM_INVALIDATED], show_alert=True
            )
            if join_result.invitation is not None:
                await _edit_best_effort(
                    context.bot,
                    join_result.invitation,
                    "该邀请已失效：房主当前金币不足或账户不可用。",
                )
            return
        await query.answer("匹配成功，游戏开始！")
        joining_chat = update.effective_chat
        await _deliver_match(
            context.bot,
            service,
            join_result,
            joining_chat_id=joining_chat.id if joining_chat is not None else None,
        )
        return

    if callback.action is CallbackAction.CANCEL:
        cancel_result = await service.cancel_waiting(
            actor, callback.game_id, callback.version
        )
        if isinstance(cancel_result, Rejected):
            await query.answer(_rejection_text(cancel_result), show_alert=True)
            return
        await query.answer("等待已取消。")
        if cancel_result.invitation is not None:
            await _edit_best_effort(
                context.bot,
                cancel_result.invitation,
                "石头剪刀布游戏等待已取消。",
            )
        return

    choice = callback.action.choice
    if choice is None:
        await query.answer("无效的游戏选择。", show_alert=True)
        return
    choice_result = await service.choose(
        actor, callback.game_id, callback.version, choice
    )
    if isinstance(choice_result, Rejected):
        await query.answer(_rejection_text(choice_result), show_alert=True)
        return
    await query.answer(f"您选择了 {_CHOICE_EMOJI[choice]}")
    if isinstance(choice_result, ChoiceRecorded):
        await _deliver_choice_recorded(context.bot, choice_result)
        return
    await _deliver_game_settled(context.bot, choice_result)


def waiting_keyboard(room: WaitingRoom) -> InlineKeyboardMarkup:
    """@brief 渲染绑定游戏与版本的等待按钮 / Render waiting buttons bound to game and version.

    @param room 当前等待房间 / Current waiting room.
    @return Telegram 内联键盘 / Telegram inline keyboard.
    """

    join = RpsCallback(CallbackAction.JOIN, room.game_id, room.version).encode()
    cancel = RpsCallback(CallbackAction.CANCEL, room.game_id, room.version).encode()
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("加入游戏 (消耗1金币)", callback_data=join),
                InlineKeyboardButton("取消等待", callback_data=cancel),
            ]
        ]
    )


def choice_keyboard(session: GameSession) -> InlineKeyboardMarkup:
    """@brief 渲染绑定当前聚合版本的选择按钮 / Render choice buttons bound to the current aggregate version.

    @param session 当前活动会话 / Current active session.
    @return Telegram 内联键盘 / Telegram inline keyboard.
    """

    def data(action: CallbackAction) -> str:
        """@brief 为一个选择动作编码 callback / Encode one choice action.

        @param action 选择动作 / Choice action.
        @return callback_data / callback_data.
        """

        return RpsCallback(action, session.game_id, session.version).encode()

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "石头 👊", callback_data=data(CallbackAction.ROCK)
                ),
                InlineKeyboardButton(
                    "剪刀 ✌️", callback_data=data(CallbackAction.SCISSORS)
                ),
                InlineKeyboardButton("布 ✋", callback_data=data(CallbackAction.PAPER)),
            ]
        ]
    )


async def _deliver_match(
    bot: Bot,
    service: RpsService,
    match: MatchStarted,
    *,
    joining_chat_id: int | None,
) -> bool:
    """@brief 按原聊天拓扑投递选择消息并绑定地址 / Deliver choice messages by chat topology and bind addresses.

    @param bot Telegram Bot / Telegram Bot.
    @param service 猜拳应用服务 / RPS application service.
    @param match 匹配结果 / Match result.
    @param joining_chat_id 第二位玩家发起匹配的聊天 / Chat where the second player initiated the match.
    @return 全部关键消息成功且绑定时为 True / True when all critical messages were sent and bound.
    """

    session = match.session
    sent_addresses: list[PlayerMessage] = []
    same_chat = (
        match.invitation is None
        or joining_chat_id is None
        or match.invitation.chat_id == joining_chat_id
    )
    try:
        if same_chat:
            if match.invitation is not None:
                await _edit_best_effort(
                    bot, match.invitation, _match_announcement_text(session)
                )
            for player, opponent in (
                (session.player_one, session.player_two),
                (session.player_two, session.player_one),
            ):
                sent = await bot.send_message(
                    chat_id=int(player.user_id),
                    text=_choice_prompt(opponent),
                    reply_markup=choice_keyboard(session),
                )
                sent_addresses.append(
                    PlayerMessage(
                        player.user_id,
                        MessageAddress(int(player.user_id), sent.message_id),
                    )
                )
        else:
            invitation = match.invitation
            if invitation is None or joining_chat_id is None:
                raise RuntimeError("cross-chat delivery requires both chat addresses")
            edited = await _edit_best_effort(
                bot,
                invitation,
                _choice_prompt(session.player_two),
                reply_markup=choice_keyboard(session),
            )
            if not edited:
                raise RuntimeError("failed to prepare the host choice message")
            sent_addresses.append(
                PlayerMessage(
                    session.player_one.user_id,
                    invitation,
                )
            )
            sent = await bot.send_message(
                chat_id=joining_chat_id,
                text=_choice_prompt(session.player_one),
                reply_markup=choice_keyboard(session),
            )
            sent_addresses.append(
                PlayerMessage(
                    session.player_two.user_id,
                    MessageAddress(joining_chat_id, sent.message_id),
                )
            )
    except Exception:
        logger.exception(
            "Failed to deliver RPS choice messages for %s", session.game_id
        )
        aborted = await service.abort_game(session.game_id, session.version)
        if isinstance(aborted, GameCancelled):
            text = "创建游戏失败，双方金币已退还。"
            for player_message in sent_addresses:
                await _edit_best_effort(bot, player_message.address, text)
            if match.invitation is not None:
                await _edit_best_effort(bot, match.invitation, text)
        return False

    delivery = GameDelivery(
        announcement=match.invitation if same_chat else None,
        player_messages=(sent_addresses[0], sent_addresses[1]),
    )
    bound = await service.bind_game_delivery(session.game_id, session.version, delivery)
    if bound:
        return True
    for player_message in sent_addresses:
        await _edit_best_effort(
            bot, player_message.address, "该游戏已经失效，请重新发起。"
        )
    return False


async def _deliver_choice_recorded(bot: Bot, result: ChoiceRecorded) -> None:
    """@brief 更新已选玩家、待选玩家与公共状态 / Update actor, pending player, and public status.

    @param bot Telegram Bot / Telegram Bot.
    @param result 非终局选择结果 / Non-terminal choice result.
    @return None / None.
    """

    delivery = result.delivery
    if delivery is None:
        return
    session = result.session
    actor_choice = session.choice_for(result.actor)
    if actor_choice is None:
        raise RuntimeError("choice result does not contain the actor's choice")
    opponent = session.opponent_of(result.actor)
    await _edit_best_effort(
        bot,
        delivery.for_player(result.actor),
        f"您已选择：{_CHOICE_EMOJI[actor_choice]}\n等待 @{opponent.display_name} 做出选择...",
    )
    pending_address = delivery.for_player(opponent.user_id)
    await _edit_best_effort(
        bot,
        pending_address,
        _choice_prompt(session.opponent_of(opponent.user_id)),
        reply_markup=choice_keyboard(session),
    )
    if delivery.announcement is not None:
        await _edit_best_effort(
            bot,
            delivery.announcement,
            _progress_text(session),
        )


async def _deliver_game_settled(bot: Bot, result: GameSettled) -> None:
    """@brief 将最终结果编辑到所有关联消息 / Edit the final result into every related message.

    @param bot Telegram Bot / Telegram Bot.
    @param result 已结算结果 / Settled result.
    @return None / None.
    """

    delivery = result.delivery
    if delivery is None:
        return
    text = _result_text(result.session)
    addresses = [message.address for message in delivery.player_messages]
    if delivery.announcement is not None:
        addresses.insert(0, delivery.announcement)
    for address in addresses:
        await _edit_best_effort(bot, address, text)


async def _edit_best_effort(
    bot: Bot,
    address: MessageAddress,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """@brief 尽力编辑单条 Telegram 消息并隔离投递失败 / Best-effort edit one Telegram message and isolate failure.

    @param bot Telegram Bot / Telegram Bot.
    @param address 消息地址 / Message address.
    @param text 新文本 / New text.
    @param reply_markup 可选新键盘 / Optional new keyboard.
    @return 编辑成功时为 True / True when the edit succeeds.
    """

    try:
        await bot.edit_message_text(
            chat_id=address.chat_id,
            message_id=address.message_id,
            text=text,
            reply_markup=reply_markup,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to edit RPS Telegram message chat=%s message=%s",
            address.chat_id,
            address.message_id,
        )
        return False


def _service(context: ContextTypes.DEFAULT_TYPE) -> RpsService:
    """@brief 从组合根取得类型化应用服务 / Get the typed application service from the composition root.

    @param context PTB callback context / PTB callback context.
    @return 猜拳应用服务 / RPS application service.
    @raises RuntimeError 服务未配置 / If the service is not configured.
    """

    service = context.application.bot_data.get(RPS_SERVICE_DATA_KEY)
    if not isinstance(service, RpsService):
        raise RuntimeError("RPS service is not configured")
    return service


def _player_from_user(user: User) -> Player:
    """@brief 将 Telegram User DTO 映射为领域玩家 / Map a Telegram User DTO to a domain player.

    @param user Telegram User / Telegram User.
    @return 领域玩家快照 / Domain player snapshot.
    """

    display_name = user.username or user.first_name or str(user.id)
    return Player(UserId(user.id), display_name)


def _rejection_text(rejection: Rejected) -> str:
    """@brief 渲染类型化拒绝 / Render a typed rejection.

    @param rejection 应用拒绝 / Application rejection.
    @return 中文用户提示 / Chinese user-facing message.
    """

    return _REJECTION_TEXT[rejection.code]


def _waiting_text() -> str:
    """@brief 渲染等待邀请 / Render a waiting invitation.

    @return 等待邀请文本 / Waiting-invitation text.
    """

    return (
        "🎲 等待其他玩家加入石头剪刀布游戏...\n"
        "输入 /rps_game 或点击下方按钮加入\n\n"
        "游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。"
    )


def _choice_prompt(opponent: Player) -> str:
    """@brief 渲染玩家选择提示 / Render a player choice prompt.

    @param opponent 对手 / Opponent.
    @return 选择提示文本 / Choice-prompt text.
    """

    return (
        f"您正在与 @{opponent.display_name} 对战石头剪刀布。\n请选择您的出招：\n\n"
        "游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。\n"
        "⚠️ 请在2分钟内做出选择，否则游戏将取消并退还金币。"
    )


def _match_announcement_text(session: GameSession) -> str:
    """@brief 渲染匹配成功公告 / Render a match-start announcement.

    @param session 初始会话 / Initial session.
    @return 公告文本 / Announcement text.
    """

    return (
        "🎮 石头剪刀布游戏开始！\n\n"
        f"玩家1: @{session.player_one.display_name} (未选择)\n"
        f"玩家2: @{session.player_two.display_name} (未选择)\n\n"
        "游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励。\n"
        "请双方查看私聊消息进行选择。"
    )


def _progress_text(session: GameSession) -> str:
    """@brief 渲染不泄露手势的选择进度 / Render choice progress without revealing hand shapes.

    @param session 活动会话 / Active session.
    @return 进度文本 / Progress text.
    """

    first_status = "✓ 已选择" if session.player_one_choice is not None else "(未选择)"
    second_status = "✓ 已选择" if session.player_two_choice is not None else "(未选择)"
    return (
        "🎮 石头剪刀布游戏进行中！\n\n"
        f"玩家1: @{session.player_one.display_name} {first_status}\n"
        f"玩家2: @{session.player_two.display_name} {second_status}\n\n"
        "游戏规则: 每位玩家消耗1金币，获胜者获得2金币奖励，平局各退还1金币。"
    )


def _result_text(session: GameSession) -> str:
    """@brief 渲染守恒结算后的最终结果 / Render the final conservation-safe settlement.

    @param session 完成会话 / Finished session.
    @return 结果文本 / Result text.
    @raises RuntimeError 完成字段缺失 / If finished fields are missing.
    """

    first = session.player_one_choice
    second = session.player_two_choice
    outcome = session.outcome
    if first is None or second is None or outcome is None:
        raise RuntimeError("RPS result rendering requires a finished session")
    winner_text = _winner_text(outcome, session)
    return (
        "🎮 石头剪刀布游戏结果：\n\n"
        f"@{session.player_one.display_name}: {_CHOICE_EMOJI[first]} vs "
        f"{_CHOICE_EMOJI[second]} :@{session.player_two.display_name}\n\n"
        f"{winner_text}"
    )


def _winner_text(outcome: GameOutcome, session: GameSession) -> str:
    """@brief 渲染赢家或平局说明 / Render winner or draw explanation.

    @param outcome 领域结果 / Domain outcome.
    @param session 完成会话 / Finished session.
    @return 结算说明 / Settlement explanation.
    """

    if outcome.kind is OutcomeKind.DRAW:
        return "游戏平局！双方各退还1金币。"
    winner = (
        session.player_one
        if outcome.winner == session.player_one.user_id
        else session.player_two
    )
    return f"@{winner.display_name} 获胜！\n获得2金币奖励。"


def _cancelled_text(session: GameSession) -> str:
    """@brief 渲染超时或关停取消文本 / Render timeout or shutdown cancellation text.

    @param session 已取消会话 / Cancelled session.
    @return 取消文本 / Cancellation text.
    """

    first_status = "已选择" if session.player_one_choice is not None else "未选择"
    second_status = "已选择" if session.player_two_choice is not None else "未选择"
    prefix = (
        "🕒 游戏已超时！"
        if session.cancellation is GameCancellation.TIMEOUT
        else "游戏已取消。"
    )
    return (
        f"{prefix}\n\n"
        f"玩家1: @{session.player_one.display_name} {first_status}\n"
        f"玩家2: @{session.player_two.display_name} {second_status}\n\n"
        "游戏已取消，已退还双方金币。"
    )
