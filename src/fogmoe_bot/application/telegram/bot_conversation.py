import asyncio
import base64
import time
import logging
from dataclasses import dataclass, field
from enum import Enum, auto

import telegram
from telegram import Update
from telegram.ext import ContextTypes

from fogmoe_bot.application.telegram.archive_utils import send_permanent_records_archive
from fogmoe_bot.application.chat import group_chat_history
from fogmoe_bot.application.economy import stake_reward_pool
from fogmoe_bot.application.accounts import service as process_user
from fogmoe_bot.application.accounts.context import load_user_state
from fogmoe_bot.domain.context import (
    ChatMessageContext,
    ConversationScope,
    build_context_state,
    create_runtime_replacement,
    render_chat_message,
    render_user_state,
)
from fogmoe_bot.infrastructure import config
from fogmoe_bot.infrastructure.database import db, connection as db_connection
from fogmoe_bot.infrastructure.telegram.telegram_utils import (
    describe_forward_for_context,
    describe_message_for_context,
    partial_send,
    safe_send_markdown,
)
from fogmoe_bot.application.assistant.tasks import summary
from fogmoe_bot.application.conversation_lock_manager import CONVERSATION_LOCK_MANAGER
from fogmoe_bot.application.telegram.generated_audio_sender import send_generated_audio_from_tool_logs
from fogmoe_bot.application.telegram.generated_image_sender import send_generated_images_from_tool_logs
from fogmoe_bot.application.assistant.reply_filter import normalize_ai_reply_text
from fogmoe_bot.application.assistant.inference.service import ASSISTANT_INFERENCE_SERVICE
from fogmoe_bot.application.telegram.sticker_sender import normalize_sticker_directives, send_ai_reply_with_stickers
from fogmoe_bot.application.telegram.assistant_visible_sender import TelegramVisibleContentHandler
from fogmoe_bot.application.assistant.tasks.vision import analyze_image
from fogmoe_bot.domain.agent_runtime.history import tool_logs_to_record_entries

logger = logging.getLogger(__name__)


_BOT_ID: int | None = None
_BOT_USERNAME: str = "FogMoeBot"
MAX_MEDIA_DOWNLOAD_BYTES = 8 * 1024 * 1024


@dataclass
class _QueuedUpdate:
    update: Update
    context: ContextTypes.DEFAULT_TYPE


@dataclass
class _MessageBatch:
    items: list[_QueuedUpdate] = field(default_factory=list)
    future: asyncio.Future | None = None


_MESSAGE_BATCHES: dict[tuple[int, int], _MessageBatch] = {}
_MESSAGE_BATCHES_LOCK = asyncio.Lock()


def _consume_batch_future_exception(future: asyncio.Future) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except Exception:
        return


def _cache_bot_identity(bot_user: telegram.User) -> None:
    """Cache bot identity globally and notify group history module."""
    global _BOT_ID, _BOT_USERNAME
    _BOT_ID = bot_user.id
    _BOT_USERNAME = bot_user.username or "FogMoeBot"
    group_chat_history.set_bot_identity(_BOT_ID, _BOT_USERNAME)


async def _refresh_bot_identity(bot, *, source: str) -> bool:
    try:
        bot_user = await bot.get_me()
    except telegram.error.NetworkError as exc:
        logger.warning(
            "Unable to fetch bot identity during %s; will retry later: %r",
            source,
            exc,
        )
        return False
    _cache_bot_identity(bot_user)
    return True


async def post_init(application) -> None:
    db.set_main_loop(asyncio.get_running_loop())
    await _refresh_bot_identity(application.bot, source="post_init")


# 添加一个帮助函数来获取实际的消息对象
def get_effective_message(update: Update):
    """获取有效的消息对象，无论是普通消息还是编辑后的消息"""
    return update.message or update.edited_message


def _format_message_timestamp(value) -> str | None:
    if not value:
        return None
    if hasattr(value, "strftime"):
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return str(value)


def _format_xml_message(
    *,
    chat_type: str,
    chat_title: str | None,
    timestamp: str,
    user_name: str,
    message_text: str,
    message_id: str | int | None = None,
    edited: bool = False,
    edited_at: str | None = None,
    forward_type: str | None = None,
    forward_origin_timestamp: str | None = None,
    forward_user: str | None = None,
    forward_name: str | None = None,
    forward_chat: str | None = None,
    forward_message_id: str | None = None,
    forward_author_signature: str | None = None,
    reply_user: str | None = None,
    reply_text: str | None = None,
    reply_type: str | None = None,
    reply_caption: str | None = None,
    reply_summary: str | None = None,
    reply_emoji: str | None = None,
    media_type: str | None = None,
    media_description: str | None = None,
    media_emoji: str | None = None,
) -> str:
    """@brief 兼容旧调用的消息渲染薄封装 / Thin compatibility wrapper for message rendering."""

    return render_chat_message(
        ChatMessageContext(
            chat_type=chat_type,
            chat_title=chat_title,
            timestamp=timestamp,
            user_name=user_name,
            message_text=message_text,
            message_id=message_id,
            edited=edited,
            edited_at=edited_at,
            forward_type=forward_type,
            forward_origin_timestamp=forward_origin_timestamp,
            forward_user=forward_user,
            forward_name=forward_name,
            forward_chat=forward_chat,
            forward_message_id=forward_message_id,
            forward_author_signature=forward_author_signature,
            reply_user=reply_user,
            reply_text=reply_text,
            reply_type=reply_type,
            reply_caption=reply_caption,
            reply_summary=reply_summary,
            reply_emoji=reply_emoji,
            media_type=media_type,
            media_description=media_description,
            media_emoji=media_emoji,
        )
    )


def _media_mime_type(media_type: str, effective_message) -> str | None:
    if media_type == "photo":
        return "image/jpeg"
    if media_type == "sticker":
        sticker = effective_message.sticker
        if getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False):
            return None
        return "image/webp"
    return None


def _build_reply_format_kwargs(reply_message) -> dict[str, str | None]:
    description = describe_message_for_context(reply_message)
    quoted_user = (
        getattr(getattr(reply_message, "from_user", None), "username", None)
        or "EmptyUsername"
    )

    if description.get("type") == "text":
        return {
            "reply_user": quoted_user,
            "reply_text": description.get("text") or "",
        }

    return {
        "reply_user": quoted_user,
        "reply_type": description.get("type") or "other",
        "reply_caption": description.get("caption"),
        "reply_summary": description.get("summary"),
        "reply_emoji": description.get("emoji"),
    }


def _build_forward_format_kwargs(message) -> dict[str, str | None]:
    description = describe_forward_for_context(message)
    if not description:
        return {}
    return {
        "forward_type": description.get("type"),
        "forward_origin_timestamp": description.get("origin_timestamp"),
        "forward_user": description.get("user"),
        "forward_name": description.get("name"),
        "forward_chat": description.get("chat"),
        "forward_message_id": description.get("message_id"),
        "forward_author_signature": description.get("author_signature"),
    }


def _build_multimodal_user_message(
    formatted_message: str,
    *,
    base64_str: str,
    mime_type: str | None,
) -> dict | None:
    if not mime_type:
        return None
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": formatted_message,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{base64_str}",
                },
            },
        ],
    }


def _message_batch_key(update: Update) -> tuple[int, int] | None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return None
    return (chat.id, user.id)


def _batch_item_sort_key(item_and_message) -> tuple[float, int, int]:
    item, message = item_and_message
    message_date = getattr(message, "date", None)
    timestamp = message_date.timestamp() if message_date else 0.0
    message_id = getattr(message, "message_id", 0) or 0
    update_id = getattr(item.update, "update_id", 0) or 0
    return (timestamp, message_id, update_id)


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    batch_key = _message_batch_key(update)
    if not batch_key:
        await _reply_unlocked(update, context)
        return
    if config.CHAT_BATCH_WINDOW_SECONDS <= 0:
        async with CONVERSATION_LOCK_MANAGER.hold(batch_key[1]):
            await _reply_unlocked(update, context)
        return

    loop = asyncio.get_running_loop()
    is_owner = False
    async with _MESSAGE_BATCHES_LOCK:
        batch = _MESSAGE_BATCHES.get(batch_key)
        if batch is None:
            future = loop.create_future()
            future.add_done_callback(_consume_batch_future_exception)
            batch = _MessageBatch(future=future)
            _MESSAGE_BATCHES[batch_key] = batch
            is_owner = True
        batch.items.append(_QueuedUpdate(update=update, context=context))
        future = batch.future

    if is_owner:
        ready_batch = None
        try:
            await asyncio.sleep(config.CHAT_BATCH_WINDOW_SECONDS)
            async with _MESSAGE_BATCHES_LOCK:
                ready_batch = _MESSAGE_BATCHES.pop(batch_key, batch)

            async with CONVERSATION_LOCK_MANAGER.hold(batch_key[1]):
                await _reply_batch_unlocked(ready_batch.items)

            if future and not future.done():
                future.set_result(None)
        except BaseException as exc:
            if future and not future.done():
                future.set_exception(exc)
            raise
        finally:
            async with _MESSAGE_BATCHES_LOCK:
                if _MESSAGE_BATCHES.get(batch_key) is batch:
                    _MESSAGE_BATCHES.pop(batch_key, None)
        return

    if future:
        await asyncio.shield(future)


async def _reply_unlocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply_batch_unlocked([_QueuedUpdate(update=update, context=context)])


class ConversationSessionState(Enum):
    """@brief 对话轮次状态 / Conversation-turn state.

    每个非终态都对应一个已完成的业务里程碑，避免通过零散局部变量推断请求进度。
    / Each non-terminal state represents a completed business milestone, avoiding
    implicit progress inferred from scattered local variables.
    """

    BATCHED = auto()
    VALIDATED = auto()
    GATED = auto()
    PRICED = auto()
    CHARGED = auto()
    PREPARED = auto()
    USER_PERSISTED = auto()
    INFERRED = auto()
    OUTPUT_PERSISTED = auto()
    DELIVERED = auto()
    COMPLETED = auto()
    IGNORED = auto()
    REJECTED = auto()
    FAILED = auto()


@dataclass
class _ConversationMessageJob:
    """@brief 单条待处理消息的计费与媒体标记 / Billing and media flags for one message."""

    message: object
    coin_cost: int
    is_media: bool
    is_edited: bool


@dataclass
class ConversationTurnSession:
    """@brief 单次对话状态机 / State machine for one frozen conversation batch.

    该对象只存活于一批消息的处理期间。消息聚合与用户级互斥仍由 ``reply``
    的外层协调器负责；长期聊天历史仍由数据库负责。
    / This object lives only while a frozen batch is processed. ``reply`` keeps
    ownership of message batching and per-user locking, while the database owns
    durable chat history.

    @param batch_items 已冻结的 Telegram 更新 / Frozen Telegram updates.
    """

    batch_items: list[_QueuedUpdate]
    state: ConversationSessionState = ConversationSessionState.BATCHED
    valid_items: list[tuple[_QueuedUpdate, object]] = field(default_factory=list)
    update: Update | None = None
    context: ContextTypes.DEFAULT_TYPE | None = None
    effective_message: object | None = None
    message_jobs: list[_ConversationMessageJob] = field(default_factory=list)
    total_coin_cost: int = 0
    user_id: int | None = None
    user_name: str = "EmptyUsername"
    conversation_id: int | None = None
    account: object | None = None
    user_coins: object | None = None
    user_plan: object | None = None
    user_state: object | None = None
    user_state_prompt: str = ""
    chat_type: str = "private"
    group_title: str = ""
    user_record_entries: list[tuple[str, str]] = field(default_factory=list)
    runtime_replacements: list[object] = field(default_factory=list)
    chat_history: list[dict] = field(default_factory=list)
    assistant_message: str = ""
    tool_logs: list[dict] = field(default_factory=list)
    sent_messages: list[object] = field(default_factory=list)
    pending_history_warning: str | None = None

    _ALLOWED_TRANSITIONS = {
        ConversationSessionState.BATCHED: {
            ConversationSessionState.VALIDATED,
            ConversationSessionState.IGNORED,
        },
        ConversationSessionState.VALIDATED: {
            ConversationSessionState.GATED,
            ConversationSessionState.IGNORED,
        },
        ConversationSessionState.GATED: {
            ConversationSessionState.PRICED,
            ConversationSessionState.IGNORED,
        },
        ConversationSessionState.PRICED: {
            ConversationSessionState.CHARGED,
            ConversationSessionState.REJECTED,
            ConversationSessionState.IGNORED,
        },
        ConversationSessionState.CHARGED: {
            ConversationSessionState.PREPARED,
            ConversationSessionState.FAILED,
        },
        ConversationSessionState.PREPARED: {
            ConversationSessionState.USER_PERSISTED,
            ConversationSessionState.FAILED,
        },
        ConversationSessionState.USER_PERSISTED: {
            ConversationSessionState.INFERRED,
            ConversationSessionState.FAILED,
        },
        ConversationSessionState.INFERRED: {
            ConversationSessionState.OUTPUT_PERSISTED,
            ConversationSessionState.FAILED,
        },
        ConversationSessionState.OUTPUT_PERSISTED: {
            ConversationSessionState.DELIVERED,
            ConversationSessionState.FAILED,
        },
        ConversationSessionState.DELIVERED: {
            ConversationSessionState.COMPLETED,
            ConversationSessionState.FAILED,
        },
    }
    _TERMINAL_STATES = {
        ConversationSessionState.COMPLETED,
        ConversationSessionState.IGNORED,
        ConversationSessionState.REJECTED,
        ConversationSessionState.FAILED,
    }

    async def run(self) -> None:
        """@brief 驱动一次对话至终态 / Drive one conversation turn to a terminal state.

        @return 无返回值；副作用为扣费、历史写入、AI 推理和 Telegram 投递 /
        No return value; effects include billing, persistence, inference, and delivery.
        """
        try:
            if not self._validate_batch():
                return
            if not await self._pass_gates():
                return
            if not await self._price_messages():
                return
            if not await self._charge_and_load_user_state():
                return
            if not await self._prepare_user_records():
                return
            await self._persist_user_records()
            await self._infer()
            await self._persist_inference_outputs()
            await self._deliver_outputs()
            self._transition(ConversationSessionState.COMPLETED)
        except BaseException:
            self._fail()
            raise

    def _transition(self, target: ConversationSessionState) -> None:
        """@brief 校验并推进状态 / Validate and advance the session state.

        @param target 目标状态 / Target state.
        @return 无返回值 / No return value.
        @note 只允许已定义的单向业务迁移 / Only defined forward business transitions are allowed.
        """
        if target not in self._ALLOWED_TRANSITIONS.get(self.state, set()):
            raise RuntimeError(
                f"Invalid conversation session transition: {self.state.name} -> {target.name}"
            )
        self.state = target

    def _ignore(self) -> None:
        """@brief 结束为静默忽略 / Finish as a silent ignore."""
        if self.state not in self._TERMINAL_STATES:
            self.state = ConversationSessionState.IGNORED

    def _reject(self) -> None:
        """@brief 结束为用户可见拒绝 / Finish as a user-visible rejection."""
        if self.state not in self._TERMINAL_STATES:
            self.state = ConversationSessionState.REJECTED

    def _fail(self) -> None:
        """@brief 标记失败而不吞掉异常 / Mark failure without swallowing the exception."""
        if self.state not in self._TERMINAL_STATES:
            self.state = ConversationSessionState.FAILED

    def _validate_batch(self) -> bool:
        """@brief 提取并排序有效消息 / Extract and sort valid messages.

        @return 是否存在可处理消息 / Whether processable messages exist.
        """
        if not self.batch_items:
            self._ignore()
            return False

        for item in self.batch_items:
            message = get_effective_message(item.update)
            if not message:
                logging.warning("收到无效的消息更新，忽略处理")
                continue
            self.valid_items.append((item, message))

        if not self.valid_items:
            self._ignore()
            return False

        self.valid_items.sort(key=_batch_item_sort_key)
        latest_item, self.effective_message = self.valid_items[-1]
        self.update = latest_item.update
        self.context = latest_item.context
        if not self.effective_message:
            logging.warning("收到无效的消息更新，忽略处理")
            self._ignore()
            return False

        self._transition(ConversationSessionState.VALIDATED)
        return True

    async def _pass_gates(self) -> bool:
        """@brief 处理群聊触发与冷却 / Apply group-trigger and cooldown gates.

        @return 是否允许进入计费阶段 / Whether billing may proceed.
        """
        assert self.update is not None
        assert self.context is not None

        if self.update.effective_chat.type in ("group", "supergroup"):
            if _BOT_ID is None:
                await _refresh_bot_identity(
                    self.context.bot,
                    source="group message handling",
                )

            should_process_group_batch = False
            for _, message in self.valid_items:
                await group_chat_history.log_group_message(
                    message,
                    self.update.effective_chat.id,
                )
                reply_from_user = getattr(
                    getattr(message.reply_to_message, "from_user", None),
                    "id",
                    None,
                )
                if (
                    message.reply_to_message
                    and _BOT_ID is not None
                    and reply_from_user == _BOT_ID
                ):
                    should_process_group_batch = True
                    continue

                text = message.text if message.text else ""
                if (
                    "/fogmoebot" in text
                    or "@FogMoeBot" in text
                    or "雾萌" in text
                    or "fog moe" in text.lower()
                    or "萌娘" in text
                    or "fogmoe" in text.lower()
                ):
                    should_process_group_batch = True

            if not should_process_group_batch:
                self._ignore()
                return False

        from fogmoe_bot.application.telegram.command_cooldown import check_chat_cooldown

        if not await check_chat_cooldown(self.update):
            self._ignore()
            return False

        self._transition(ConversationSessionState.GATED)
        return True

    async def _price_messages(self) -> bool:
        """@brief 计算本批消息费用 / Calculate the cost of the frozen batch.

        @return 是否存在可计费消息 / Whether billable messages exist.
        """
        for item, message in self.valid_items:
            if message.photo or message.sticker:
                coin_cost = 5
                is_media = True
            else:
                user_message = message.text
                if not user_message:
                    logging.warning("收到没有文本内容的消息，忽略处理")
                    continue
                if len(user_message) > 4096:
                    await message.reply_text(
                        "消息过长，无法处理。请缩短消息长度！\n"
                        "The message is too long to process. Please shorten the message."
                    )
                    self._reject()
                    return False
                if len(user_message) > 2000:
                    coin_cost = 5
                elif len(user_message) > 1000:
                    coin_cost = 4
                elif len(user_message) > 500:
                    coin_cost = 3
                elif len(user_message) > 100:
                    coin_cost = 2
                else:
                    coin_cost = 1
                is_media = False

            self.message_jobs.append(
                _ConversationMessageJob(
                    message=message,
                    coin_cost=coin_cost,
                    is_media=is_media,
                    is_edited=item.update.edited_message is message,
                )
            )
            self.total_coin_cost += coin_cost

        if not self.message_jobs:
            self._ignore()
            return False

        self._transition(ConversationSessionState.PRICED)
        return True

    async def _charge_and_load_user_state(self) -> bool:
        """@brief 原子扣费并加载用户状态 / Charge atomically and load user state.

        @return 是否成功构建用户状态 / Whether user state was built successfully.
        """
        assert self.update is not None
        assert self.effective_message is not None

        self.user_id = self.update.effective_user.id
        self.user_name = self.update.effective_user.username or "EmptyUsername"
        self.conversation_id = self.user_id

        async with db_connection.transaction() as connection:
            self.account = await process_user.get_user_account(
                self.user_id,
                connection=connection,
                for_update=True,
            )
            if not self.account:
                await self.effective_message.reply_text(
                    "请先使用 /me 命令注册个人信息后再聊天。\n"
                    "Please register first using the /me command before chatting."
                )
                self._reject()
                return False

            user_coins_free = self.account.coins
            user_coins_paid = self.account.coins_paid
            available_coins = self.account.total_coins
            if available_coins < self.total_coin_cost:
                await self.effective_message.reply_text(
                    f"您的硬币不足，无法与雾萌娘连接，需要{self.total_coin_cost}个硬币。试试通过 /lottery 抽奖吧！\n"
                    f"You don't have enough coins (need {self.total_coin_cost}), I don't want to talk to you. "
                    "Try using /lottery to get some coins!"
                )
                self._reject()
                return False

            await process_user.spend_user_coins(
                self.user_id,
                self.total_coin_cost,
                connection=connection,
            )
            pool_add = stake_reward_pool.calculate_pool_add(self.total_coin_cost)
            if pool_add > 0:
                await stake_reward_pool.add_to_pool(pool_add, connection=connection)
            if user_coins_free >= self.total_coin_cost:
                new_free = user_coins_free - self.total_coin_cost
                new_paid = user_coins_paid
            else:
                remaining = self.total_coin_cost - user_coins_free
                new_free = 0
                new_paid = max(user_coins_paid - remaining, 0)
            self.user_coins = new_free + new_paid
            self.user_plan = process_user.resolve_user_plan(self.user_id, new_paid)

        self._transition(ConversationSessionState.CHARGED)
        self.user_state = await load_user_state(
            self.user_id,
            account=self.account,
            coins=self.user_coins,
            plan=self.user_plan,
        )
        if self.user_state is None:
            logger.warning(
                "User disappeared while building AI context: user_id=%s",
                self.user_id,
            )
            self._fail()
            return False
        self.user_state_prompt = render_user_state(self.user_state)
        return True

    async def _prepare_user_records(self) -> bool:
        """@brief 规范化文本与媒体输入 / Normalize text and media input.

        @return 是否成功生成持久化记录 / Whether persisted records were prepared.
        """
        assert self.update is not None

        self.chat_type = self.update.effective_chat.type or "private"
        self.group_title = (
            (self.update.effective_chat.title or "").strip()
            if self.update.effective_chat
            else ""
        )

        for job in self.message_jobs:
            message = job.message
            current_message_time = _format_message_timestamp(message.date) or time.strftime(
                '%Y-%m-%d %H:%M:%S'
            )
            message_metadata_kwargs = {
                "message_id": getattr(message, "message_id", None),
                "edited": job.is_edited,
                "edited_at": (
                    _format_message_timestamp(getattr(message, "edit_date", None))
                    if job.is_edited
                    else None
                ),
            }
            forward_kwargs = _build_forward_format_kwargs(message)
            reply_kwargs = (
                _build_reply_format_kwargs(message.reply_to_message)
                if message.reply_to_message
                else {}
            )

            if job.is_media:
                formatted_message = await self._prepare_media_record(
                    message,
                    current_message_time,
                    message_metadata_kwargs,
                    forward_kwargs,
                    reply_kwargs,
                )
                if formatted_message is None:
                    self._fail()
                    return False
            else:
                formatted_message = _format_xml_message(
                    chat_type=self.chat_type,
                    chat_title=self.group_title or None,
                    timestamp=current_message_time,
                    user_name=self.user_name,
                    message_text=message.text or "",
                    **message_metadata_kwargs,
                    **forward_kwargs,
                    **reply_kwargs,
                )

            self.user_record_entries.append(("user", formatted_message))

        if not self.user_record_entries:
            self._fail()
            return False
        self._transition(ConversationSessionState.PREPARED)
        return True

    async def _prepare_media_record(
        self,
        message,
        current_message_time: str,
        message_metadata_kwargs: dict[str, object],
        forward_kwargs: dict[str, str | None],
        reply_kwargs: dict[str, str | None],
    ) -> str | None:
        """@brief 下载并分析一条媒体消息 / Download and analyse one media message.

        @param message Telegram 媒体消息 / Telegram media message.
        @param current_message_time 消息时间字符串 / Formatted message timestamp.
        @param message_metadata_kwargs 消息元数据 / Message metadata.
        @param forward_kwargs 转发元数据 / Forward metadata.
        @param reply_kwargs 引用元数据 / Reply metadata.
        @return 供持久化的规范化消息；失败时返回 ``None`` /
        Persisted normalized message, or ``None`` on failure.
        """
        try:
            if message.photo:
                media_type = "photo"
                file = await message.photo[-1].get_file()
                media_emoji = None
            else:
                media_type = "sticker"
                file = await message.sticker.get_file()
                media_emoji = getattr(message.sticker, "emoji", None)

            caption = message.caption if message.caption else ""
            file_size = getattr(file, "file_size", None)
            if file_size and file_size > MAX_MEDIA_DOWNLOAD_BYTES:
                await message.reply_text(
                    "图片太大啦，请压缩后再发送。\n"
                    "The image is too large. Please compress it and try again."
                )
                return None

            file_bytes = await file.download_as_bytearray()
            if len(file_bytes) > MAX_MEDIA_DOWNLOAD_BYTES:
                await message.reply_text(
                    "图片太大啦，请压缩后再发送。\n"
                    "The image is too large. Please compress it and try again."
                )
                return None

            base64_str = base64.b64encode(file_bytes).decode('utf-8')
            image_description = await analyze_image(base64_str)
            message_text = caption if caption else f"[{media_type}]"
            formatted_message = _format_xml_message(
                chat_type=self.chat_type,
                chat_title=self.group_title or None,
                timestamp=current_message_time,
                user_name=self.user_name,
                message_text=message_text,
                **message_metadata_kwargs,
                **forward_kwargs,
                **reply_kwargs,
                media_type=media_type,
                media_description=image_description,
                media_emoji=media_emoji,
            )
            runtime_formatted_message = _format_xml_message(
                chat_type=self.chat_type,
                chat_title=self.group_title or None,
                timestamp=current_message_time,
                user_name=self.user_name,
                message_text=message_text,
                **message_metadata_kwargs,
                **forward_kwargs,
                **reply_kwargs,
                media_type=media_type,
                media_emoji=media_emoji,
            )
            runtime_user_message = _build_multimodal_user_message(
                runtime_formatted_message,
                base64_str=base64_str,
                mime_type=_media_mime_type(media_type, message),
            )
            runtime_replacement = create_runtime_replacement(
                persisted_content=formatted_message,
                runtime_message=runtime_user_message,
            )
            if runtime_replacement:
                self.runtime_replacements.append(runtime_replacement)
            return formatted_message
        except Exception as exc:
            logging.error("处理媒体消息时出错: %s", exc)
            await message.reply_text(
                "抱歉呢，雾萌娘暂时无法处理您发送的媒体，请稍后再试试看喵~\n"
                "Sorry, I'm having trouble processing your image/sticker right now. Please try again later, meow!"
            )
            return None

    async def _persist_user_records(self) -> None:
        """@brief 写入用户记录并处理历史生命周期 / Persist user records and manage history lifecycle."""
        assert self.context is not None
        assert self.user_id is not None
        assert self.conversation_id is not None

        (
            user_snapshot_created,
            user_storage_warning,
            user_archived_records,
        ) = await db_connection.async_insert_chat_records(
            self.conversation_id,
            self.user_record_entries,
            system_prompt_extra=self.user_state_prompt,
        )
        if user_archived_records:
            await send_permanent_records_archive(
                self.context.bot,
                self.user_id,
                user_archived_records,
                logger=logger,
            )
        self._remember_history_warning(user_storage_warning)
        await self._handle_overflow_summary(user_storage_warning)
        if user_snapshot_created and user_storage_warning != "overflow":
            summary.schedule_summary_generation(self.conversation_id)

        self.chat_history = await db_connection.async_get_chat_history(
            self.conversation_id
        )
        self._transition(ConversationSessionState.USER_PERSISTED)

    async def _infer(self) -> None:
        """@brief 构建上下文并执行 AI 推理 / Build context and execute AI inference."""
        assert self.context is not None
        assert self.update is not None
        assert self.effective_message is not None
        assert self.user_id is not None
        assert self.user_state is not None

        try:
            await self.context.bot.send_chat_action(
                chat_id=self.update.effective_chat.id,
                action="typing",
            )
        except Exception:
            logger.debug("Failed to send typing action before AI request")

        is_group_chat = self.update.effective_chat.type in ("group", "supergroup")
        context_state = build_context_state(
            system_prompt=config.SYSTEM_PROMPT,
            history_messages=self.chat_history,
            scope=ConversationScope(
                user_id=self.user_id,
                is_group=is_group_chat,
                group_id=self.update.effective_chat.id if is_group_chat else None,
                message_id=getattr(self.effective_message, "message_id", None),
            ),
            user_state=self.user_state,
            runtime_replacements=self.runtime_replacements,
            text_fallback_messages=self.chat_history,
        )
        fallback_send = partial_send(
            self.context.bot.send_message,
            self.update.effective_chat.id,
        )
        visible_content_handler = TelegramVisibleContentHandler(
            loop=asyncio.get_running_loop(),
            bot=self.context.bot,
            chat_id=self.update.effective_chat.id,
            first_text_send=self.effective_message.reply_text,
            fallback_send=fallback_send,
            logger=logger,
            reply_to_message_id=getattr(self.effective_message, "message_id", None),
        )

        self.assistant_message, self.tool_logs = await ASSISTANT_INFERENCE_SERVICE.infer(
            context_state,
            visible_content_sink=visible_content_handler,
        )
        self.sent_messages.extend(visible_content_handler.sent_messages)
        self.assistant_message = normalize_ai_reply_text(self.assistant_message)
        if self.assistant_message.strip():
            self.assistant_message = await normalize_sticker_directives(
                self.assistant_message,
                logger=logger,
            )
        self._transition(ConversationSessionState.INFERRED)

    async def _persist_inference_outputs(self) -> None:
        """@brief 写入工具与助手输出 / Persist tool and assistant output."""
        assert self.context is not None
        assert self.user_id is not None
        assert self.conversation_id is not None

        tool_record_entries = tool_logs_to_record_entries(self.tool_logs)
        if tool_record_entries:
            (
                tool_snapshot_created,
                tool_storage_warning,
                tool_archived_records,
            ) = await db_connection.async_insert_chat_records(
                self.conversation_id,
                tool_record_entries,
            )
            if tool_archived_records:
                await send_permanent_records_archive(
                    self.context.bot,
                    self.user_id,
                    tool_archived_records,
                    logger=logger,
                )
            self._remember_history_warning(tool_storage_warning)
            await self._handle_overflow_summary(tool_storage_warning)
            if tool_snapshot_created and tool_storage_warning != "overflow":
                summary.schedule_summary_generation(self.conversation_id)

        if self.assistant_message.strip():
            (
                assistant_snapshot_created,
                assistant_storage_warning,
                assistant_archived_records,
            ) = await db_connection.async_insert_chat_record(
                self.conversation_id,
                "assistant",
                self.assistant_message,
            )
            if assistant_archived_records:
                await send_permanent_records_archive(
                    self.context.bot,
                    self.user_id,
                    assistant_archived_records,
                    logger=logger,
                )
            self._remember_history_warning(assistant_storage_warning)
            await self._handle_overflow_summary(assistant_storage_warning)
            if (
                assistant_snapshot_created
                and assistant_storage_warning != "overflow"
            ):
                summary.schedule_summary_generation(self.conversation_id)

        self._transition(ConversationSessionState.OUTPUT_PERSISTED)

    async def _deliver_outputs(self) -> None:
        """@brief 投递最终回复和工具媒体 / Deliver final reply and tool media."""
        assert self.context is not None
        assert self.update is not None
        assert self.effective_message is not None
        assert self.user_id is not None
        assert self.conversation_id is not None

        if self.pending_history_warning:
            await self._notify_history_warning(self.pending_history_warning)

        fallback_send = partial_send(
            self.context.bot.send_message,
            self.update.effective_chat.id,
        )
        if self.assistant_message.strip():
            has_visible_message = bool(self.sent_messages)
            try:
                await self.context.bot.send_chat_action(
                    chat_id=self.update.effective_chat.id,
                    action="typing",
                )
            except Exception:
                logger.debug("Failed to send typing action before final AI reply")
            self.sent_messages.extend(
                await send_ai_reply_with_stickers(
                    bot=self.context.bot,
                    chat_id=self.update.effective_chat.id,
                    text=self.assistant_message,
                    first_text_send=(
                        fallback_send
                        if has_visible_message
                        else self.effective_message.reply_text
                    ),
                    fallback_send=fallback_send,
                    logger=logger,
                    reply_to_message_id=(
                        None
                        if has_visible_message
                        else getattr(self.effective_message, "message_id", None)
                    ),
                )
            )
        self.sent_messages.extend(
            await send_generated_audio_from_tool_logs(
                bot=self.context.bot,
                chat_id=self.update.effective_chat.id,
                tool_logs=self.tool_logs,
                logger=logger,
            )
        )
        self.sent_messages.extend(
            await send_generated_images_from_tool_logs(
                bot=self.context.bot,
                chat_id=self.update.effective_chat.id,
                tool_logs=self.tool_logs,
                logger=logger,
            )
        )
        if not self.sent_messages and not self.assistant_message.strip():
            tool_log_types = [
                str(tool_log.get("type", "tool_result"))
                for tool_log in self.tool_logs
                if isinstance(tool_log, dict)
            ]
            logger.info(
                "AI produced empty response; no Telegram message sent: "
                "user_id=%s conversation_id=%s tool_log_types=%s",
                self.user_id,
                self.conversation_id,
                tool_log_types,
            )
        if self.update.effective_chat.type in ("group", "supergroup"):
            for sent_message in self.sent_messages:
                if sent_message is None:
                    continue
                await group_chat_history.log_group_message(
                    sent_message,
                    self.update.effective_chat.id,
                )

        self._transition(ConversationSessionState.DELIVERED)

    def _remember_history_warning(self, level: str | None) -> None:
        """@brief 聚合历史容量警告 / Aggregate history-capacity warnings.

        @param level 本次写入返回的警告等级 / Warning level from this persistence write.
        @return 无返回值 / No return value.
        """
        if not level or self.pending_history_warning == "overflow":
            return
        if level == "overflow":
            self.pending_history_warning = "overflow"
        elif self.pending_history_warning is None:
            self.pending_history_warning = level

    async def _notify_history_warning(self, level: str) -> None:
        """@brief 发送历史容量提醒 / Send history-capacity notification.

        @param level 警告等级 / Warning level.
        @return 无返回值 / No return value.
        """
        assert self.context is not None
        assert self.update is not None

        if level == "near_limit":
            warning_text = (
                "提醒：当前会话历史记录已接近系统容量上限。雾萌娘可能会在稍后自动压缩较早的消息以保持体验顺畅。"
            )
        elif level == "overflow":
            warning_text = (
                "提示：为了保证会话流畅，部分较早的聊天记录已被自动压缩保存。当前对话不受影响，若需要查看完整历史请告诉雾萌娘。"
            )
        else:
            return

        await safe_send_markdown(
            partial_send(
                self.context.bot.send_message,
                self.update.effective_chat.id,
            ),
            warning_text,
            logger=logger,
        )

    async def _handle_overflow_summary(self, level: str | None) -> None:
        """@brief 处理历史溢出的即时摘要 / Handle immediate summary for history overflow.

        @param level 本次写入返回的警告等级 / Warning level from this persistence write.
        @return 无返回值 / No return value.
        """
        if level != "overflow":
            return
        assert self.conversation_id is not None

        summary_text = await summary.generate_summary_immediately(self.conversation_id)
        if summary_text:
            await db_connection.async_update_latest_history_state_summary(
                self.conversation_id,
                summary_text,
            )
        else:
            summary.schedule_summary_generation(self.conversation_id)


async def _reply_batch_unlocked(batch_items: list[_QueuedUpdate]) -> None:
    """@brief 处理已解锁批次 / Process one batch after its caller acquired the lock.

    @param batch_items 已冻结的 Telegram 更新 / Frozen Telegram updates.
    @return 无返回值 / No return value.
    """
    await ConversationTurnSession(batch_items=batch_items).run()
