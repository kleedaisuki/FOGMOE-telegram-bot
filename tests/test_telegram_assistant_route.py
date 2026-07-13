"""@brief Telegram durable Assistant route 测试 / Tests for the Telegram durable Assistant route."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from telegram import Update

from fogmoe_bot.application.conversation.assistant_ingress import (
    AssistantAccountContext,
    AssistantFeedbackReason,
    AssistantIngressCoordinator,
    AssistantInsufficientCoins,
    AssistantTurnAccepted,
    AssistantTurnRequest,
    AssistantUserNotRegistered,
    assistant_text_cost,
)
from fogmoe_bot.application.conversation.standalone_outbound import (
    StandaloneOutboundCommand,
)
from fogmoe_bot.domain.conversation.payloads import (
    JsonObject,
    JsonValue,
)
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    UpdateId,
)
from fogmoe_bot.domain.conversation.inbox import InboundUpdate
from fogmoe_bot.presentation.telegram import (
    assistant_primary_route,
    assistant_update_models,
    assistant_update_parser,
)
from fogmoe_bot.presentation.telegram.assistant_primary_route import (
    TelegramAssistantPrimaryRoute,
)
from fogmoe_bot.presentation.telegram.assistant_update_models import (
    MalformedTelegramAssistantUpdate,
)
from fogmoe_bot.presentation.telegram.assistant_update_parser import (
    parse_telegram_assistant_update,
)
from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)
from fogmoe_bot.presentation.telegram.update_mapper import TelegramUpdateMapper


NOW = datetime(2030, 1, 1, tzinfo=UTC)
"""@brief 固定测试时刻 / Fixed test instant."""


class ManualClock:
    """@brief 固定 UTC 时钟 / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定接受时间 / Return the fixed acceptance time.

        @return 接收后一秒 / One second after receipt.
        """

        return NOW + timedelta(seconds=1)


class RecordingAcceptance:
    """@brief 记录请求并返回可控结果的 UoW 替身 / UoW double recording requests and returning a controlled result."""

    def __init__(self, result: object | None = None) -> None:
        """@brief 初始化替身 / Initialize the double.

        @param result acceptance 结果 / Acceptance result.
        """

        self.result = result or AssistantTurnAccepted(acceptance=None, replayed=True)
        """@brief 固定结果 / Fixed result."""
        self.calls: list[tuple[AssistantTurnRequest, datetime]] = []
        """@brief UoW 调用 / UoW calls."""

    async def accept(
        self,
        request: AssistantTurnRequest,
        *,
        accepted_at: datetime,
    ) -> object:
        """@brief 记录调用并返回固定结果 / Record the call and return the fixed result.

        @param request Assistant 请求 / Assistant request.
        @param accepted_at 接受时刻 / Acceptance time.
        @return 固定结果 / Fixed result.
        """

        self.calls.append((request, accepted_at))
        return self.result


class RecordingFeedback:
    """@brief 以幂等键去重的 feedback capability 替身 / Feedback-capability double deduplicating by idempotency key."""

    def __init__(self) -> None:
        """@brief 初始化记录 / Initialize recording.

        @return None / None.
        """

        self.commands: dict[str, StandaloneOutboundCommand] = {}
        """@brief 幂等反馈记录 / Idempotent feedback records."""

    async def enqueue(self, command: StandaloneOutboundCommand) -> None:
        """@brief 按幂等键记录一次 / Record once by idempotency key.

        @param command feedback command / Feedback command.
        @return None / None.
        """

        self.commands.setdefault(command.idempotency_key, command)


def _message_payload(
    *,
    update_id: int = 100,
    text: str | None = "hello",
    chat_type: str = "private",
    chat_id: int = 42,
    entities: list[JsonObject] | None = None,
    photo: JsonObject | None = None,
    sticker: JsonObject | None = None,
    reply_user_id: int | None = None,
    thread_id: int | None = None,
    service_event: bool = False,
) -> JsonObject:
    """@brief 构造 PTB ``to_json`` 形状 payload / Build a PTB ``to_json``-shaped payload.

    @return Telegram Update payload / Telegram Update payload.
    """

    message: JsonObject = {
        "message_id": 7,
        "date": 1_893_456_000,
        "chat": {
            "id": chat_id,
            "type": chat_type,
            "title": "Lab" if chat_type != "private" else None,
        },
        "from": {
            "id": 42,
            "is_bot": False,
            "first_name": "Klee",
            "last_name": "Spark",
            "username": "klee",
        },
    }
    if text is not None:
        message["text"] = text
    if entities is not None:
        entity_values: list[JsonValue] = []
        """@brief JSON-union typed entity list / JSON-union typed entity list."""
        entity_values.extend(entities)
        message["entities"] = entity_values
    if photo is not None:
        message.pop("text", None)
        message["photo"] = [photo]
        message["caption"] = "look"
    if sticker is not None:
        message.pop("text", None)
        message["sticker"] = sticker
    if reply_user_id is not None:
        message["reply_to_message"] = {
            "message_id": 6,
            "date": 1_893_455_999,
            "chat": message["chat"],
            "from": {
                "id": reply_user_id,
                "is_bot": reply_user_id == 999,
                "first_name": "Fog",
                "username": "FogMoeBot" if reply_user_id == 999 else "other",
            },
            "text": "previous",
        }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    if service_event:
        message.pop("text", None)
        message["new_chat_members"] = [message["from"]]
    return {"update_id": update_id, "message": message}


def _photo(*, file_size: int | None = 1024) -> JsonObject:
    """@brief 构造 PhotoSize / Build a PhotoSize.

    @param file_size 声明大小 / Declared size.
    @return PhotoSize JSON / PhotoSize JSON.
    """

    photo: JsonObject = {
        "file_id": "photo-file",
        "file_unique_id": "photo-unique",
        "width": 640,
        "height": 480,
    }
    if file_size is not None:
        photo["file_size"] = file_size
    return photo


def _sticker(*, file_size: int = 1024) -> JsonObject:
    """@brief 构造 Sticker / Build a Sticker.

    @param file_size 声明大小 / Declared size.
    @return Sticker JSON / Sticker JSON.
    """

    return {
        "file_id": "sticker-file",
        "file_unique_id": "sticker-unique",
        "file_size": file_size,
        "width": 512,
        "height": 512,
        "is_animated": False,
        "is_video": False,
        "emoji": "✨",
        "type": "regular",
    }


def _inbound(payload: JsonObject) -> InboundUpdate:
    """@brief 将 payload 包装为 durable Update / Wrap a payload in a durable Update.

    @param payload Telegram payload / Telegram payload.
    @return 待路由 Update / Routable Update.
    """

    sender = payload["message"]
    assert isinstance(sender, dict)
    user = sender["from"]
    assert isinstance(user, dict)
    user_id = user["id"]
    chat = sender["chat"]
    assert isinstance(chat, dict)
    chat_id = chat["id"]
    chat_type = chat["type"]
    thread_id = sender.get("message_thread_id")
    update_id = payload["update_id"]
    assert isinstance(user_id, int) and isinstance(update_id, int)
    assert isinstance(chat_id, int) and isinstance(chat_type, str)
    assert thread_id is None or isinstance(thread_id, int)
    return InboundUpdate.pending(
        update_id=UpdateId(update_id),
        conversation_id=TelegramConversationAddress(
            chat_type=chat_type,
            chat_id=chat_id,
            user_id=user_id,
            message_thread_id=thread_id,
        ).conversation_id,
        payload=payload,
        received_at=NOW,
    )


def _route(
    acceptance: RecordingAcceptance | None = None,
    feedback: RecordingFeedback | None = None,
) -> tuple[TelegramAssistantPrimaryRoute, RecordingAcceptance, RecordingFeedback]:
    """@brief 构造 route 与记录端口 / Build a route and recording ports.

    @return route、UoW 与 feedback / Route, UoW, and feedback.
    """

    acceptance_port = acceptance or RecordingAcceptance()
    feedback_port = feedback or RecordingFeedback()
    coordinator = AssistantIngressCoordinator(
        acceptance=acceptance_port,  # type: ignore[arg-type]
        feedback=feedback_port,
        clock=ManualClock(),
    )
    return (
        TelegramAssistantPrimaryRoute(
            coordinator=coordinator,
            bot_user_id=999,
            bot_username="FogMoeBot",
        ),
        acceptance_port,
        feedback_port,
    )


def test_primary_route_exclusively_matches_supported_messages_and_command() -> None:
    """@brief 普通内容与 /fogmoebot 匹配，其他命令/回调/成员事件不匹配 / Ordinary content and /fogmoebot match; other commands, callbacks, and membership events do not."""

    route, _acceptance, _feedback = _route()
    command_entity: JsonObject = {"type": "bot_command", "offset": 0, "length": 10}
    other_entity: JsonObject = {"type": "bot_command", "offset": 0, "length": 5}

    assert route.matches(_inbound(_message_payload(text="hello")))
    assert route.matches(_inbound(_message_payload(text=None, photo=_photo())))
    assert route.matches(_inbound(_message_payload(text=None, sticker=_sticker())))
    assert route.matches(
        _inbound(
            _message_payload(
                text="/fogmoebot hello",
                entities=[command_entity],
            )
        )
    )
    assert route.matches(
        _inbound(
            _message_payload(
                text="/fogmoebot@FogMoeBot hello",
                entities=[{"type": "bot_command", "offset": 0, "length": 20}],
            )
        )
    )
    assert not route.matches(
        _inbound(
            _message_payload(
                text="/fogmoebot@OtherBot hello",
                entities=[{"type": "bot_command", "offset": 0, "length": 19}],
            )
        )
    )
    assert not route.matches(
        _inbound(_message_payload(text="/help", entities=[other_entity]))
    )
    assert not route.matches(_inbound(_message_payload(service_event=True)))
    callback = InboundUpdate.pending(
        update_id=UpdateId(200),
        conversation_id=ConversationId("assistant-user:42"),
        payload={"update_id": 200, "callback_query": {"id": "q"}},
        received_at=NOW,
    )
    assert not route.matches(callback)


def test_primary_route_accepts_real_ptb_json_with_false_service_flags() -> None:
    """@brief PTB 的 False 服务字段不能把普通消息误判为服务事件 / PTB's false service flags must not turn an ordinary message into a service event."""

    update = Update.de_json(
        {
            "update_id": 201,
            "message": {
                "message_id": 8,
                "date": 1_893_456_000,
                "chat": {"id": 42, "type": "private"},
                "from": {
                    "id": 42,
                    "is_bot": False,
                    "first_name": "Klee",
                },
                "text": "hello",
            },
        },
        bot=None,
    )
    inbound = TelegramUpdateMapper().map(update, received_at=NOW)
    message = inbound.payload["message"]
    assert isinstance(message, dict)
    assert message["channel_chat_created"] is False

    route, _acceptance, _feedback = _route()
    parsed = parse_telegram_assistant_update(inbound)

    assert parsed.text == "hello"
    assert route.matches(inbound)


def test_command_entities_use_telegram_utf16_offsets() -> None:
    """@brief emoji 后的合法 entity 使用 UTF-16 偏移且不能破坏命令路由 / A valid entity after an emoji uses UTF-16 offsets and must not break command routing."""

    text = "/fogmoebot 🔥abc"
    inbound = _inbound(
        _message_payload(
            text=text,
            entities=[
                {"type": "bot_command", "offset": 0, "length": 10},
                {"type": "bold", "offset": 13, "length": 3},
            ],
        )
    )
    route, _acceptance, _feedback = _route()

    assert route.matches(inbound)


def test_group_trigger_preserves_mentions_keywords_and_reply_to_bot() -> None:
    """@brief 群聊仅由旧关键词、命令或回复 Bot 触发 / Group chats trigger only by legacy keywords, command, or reply-to-bot."""

    route, _acceptance, _feedback = _route()
    quiet = _inbound(
        _message_payload(chat_type="supergroup", chat_id=-1001, text="hello everyone")
    )
    mentioned = _inbound(
        _message_payload(chat_type="group", chat_id=-1001, text="雾萌 在吗")
    )
    replied_photo = _inbound(
        _message_payload(
            chat_type="supergroup",
            chat_id=-1001,
            text=None,
            photo=_photo(),
            reply_user_id=999,
        )
    )

    assert not route.matches(quiet)
    assert route.matches(mentioned)
    assert route.matches(replied_photo)


def test_request_contains_strict_adapter_metadata_and_normalized_user_content() -> None:
    """@brief user_content 与 inference request 携带 chat/user/scope/reply metadata / User content and inference request carry chat, user, scope, and reply metadata."""

    inbound = _inbound(
        _message_payload(
            chat_type="supergroup",
            chat_id=-1001,
            text="@FogMoeBot hello",
            reply_user_id=50,
            thread_id=9,
        )
    )
    parsed = parse_telegram_assistant_update(inbound)
    request = parsed.to_request(inbound)
    command = request.to_accept_turn(
        AssistantAccountContext(
            coins=9,
            plan="paid",
            permission=1,
            profile=None,
            personal_info="",
            diary_exists=False,
        ),
        accepted_at=NOW + timedelta(seconds=1),
    )

    assert set(command.inference_request) == {
        "schema_version",
        "conversation_id",
        "turn_id",
        "delivery_stream_id",
        "chat_id",
        "reply_to_message_id",
        "message_thread_id",
        "user",
        "scope",
        "disable_notification",
        "protect_content",
        "disable_web_page_preview",
        "task_kind",
        "translation_input",
    }
    assert command.inference_request["task_kind"] == "assistant"
    assert command.inference_request["translation_input"] is None
    assert command.inference_request["delivery_stream_id"] == (
        "telegram:primary:chat:-1001:thread:9"
    )
    assert command.inference_request["scope"] == {
        "is_group": True,
        "group_id": -1001,
        "message_id": 7,
        "message_thread_id": 9,
    }
    assert command.inference_request["user"] == {
        "user_id": 42,
        "username": "klee",
        "display_name": "Klee Spark",
        "coins": 9,
        "plan": "paid",
        "permission": 1,
        "profile": None,
        "personal_info": "",
        "diary_exists": False,
    }
    assert command.user_content["chat"] == {
        "chat_id": -1001,
        "type": "supergroup",
        "title": "Lab",
    }
    model_message = command.user_content["model_message"]
    assert isinstance(model_message, dict)
    assert (
        'user="Klee Spark" username="@klee" user_id="42" thread_id="9"'
        in str(model_message["content"])
    )
    assert command.user_content["reply"] == {
        "message_id": 6,
        "user_id": 50,
        "username": "other",
        "kind": "text",
        "text": "previous",
        "emoji": None,
    }


def test_group_request_rejects_private_state_before_durable_serialization() -> None:
    """@brief 群请求在 durable 序列化前拒绝私人状态 / A group request rejects private state before durable serialization."""

    inbound = _inbound(
        _message_payload(
            chat_type="supergroup",
            chat_id=-1001,
            text="@FogMoeBot hello",
            thread_id=9,
        )
    )
    request = parse_telegram_assistant_update(inbound).to_request(inbound)

    with pytest.raises(ValueError, match="cannot freeze private User Profile"):
        request.to_accept_turn(
            AssistantAccountContext(
                coins=9,
                plan="paid",
                permission=1,
                profile=None,
                personal_info="private",
                diary_exists=False,
            ),
            accepted_at=NOW + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (1, 1),
        (100, 1),
        (101, 2),
        (500, 2),
        (501, 3),
        (1000, 3),
        (1001, 4),
        (2000, 4),
        (2001, 5),
        (4096, 5),
    ],
)
def test_text_pricing_preserves_every_boundary(length: int, expected: int) -> None:
    """@brief 文本计费边界与旧产品一致 / Every text-pricing boundary matches the legacy product.

    @param length 文本长度 / Text length.
    @param expected 费用 / Expected charge.
    """

    assert assistant_text_cost("x" * length) == expected


def test_overlong_text_enqueues_idempotent_feedback_without_accepting() -> None:
    """@brief 4097 字符只产生幂等 feedback，不扣费、不建 Turn / 4097 characters produce idempotent feedback without charge or Turn."""

    async def scenario() -> None:
        """@brief 重放同一超长 Update / Replay one overlong Update.

        @return None / None.
        """

        route, acceptance, feedback = _route()
        inbound = _inbound(_message_payload(text="x" * 4097))
        operation = await route.operation(inbound)
        assert operation.key.aggregate_type == "conversation"
        assert operation.key.identity == ("assistant-user:42",)
        await operation.call()
        await operation.call()

        assert acceptance.calls == []
        assert list(feedback.commands) == [
            "update:100:assistant-feedback:text_too_long"
        ]
        command = next(iter(feedback.commands.values()))
        assert command.payload["text"] == (
            "消息过长，无法处理。请缩短消息长度！\n"
            "The message is too long to process. Please shorten the message."
        )

    asyncio.run(scenario())


def test_declared_oversize_media_enqueues_feedback_without_accepting() -> None:
    """@brief 已声明超过 8 MiB 的媒体不进入 acceptance / Media declared above 8 MiB never enters acceptance."""

    async def scenario() -> None:
        """@brief 执行超限媒体操作 / Execute an oversize-media operation.

        @return None / None.
        """

        route, acceptance, feedback = _route()
        inbound = _inbound(
            _message_payload(text=None, photo=_photo(file_size=8 * 1024 * 1024 + 1))
        )
        await (await route.operation(inbound)).call()

        assert acceptance.calls == []
        assert list(feedback.commands) == [
            "update:100:assistant-feedback:media_too_large"
        ]

    asyncio.run(scenario())


def test_media_size_boundary_and_unknown_size_reach_acceptance_with_download_cap() -> (
    None
):
    """@brief 8 MiB 与未知大小媒体进入 acceptance，且后续下载上限被持久化 / Media at 8 MiB or with unknown size reaches acceptance with a persisted download cap."""

    async def scenario() -> None:
        """@brief 执行媒体边界操作 / Execute media-boundary operations.

        @return None / None.
        """

        route, acceptance, feedback = _route()
        at_limit = _inbound(
            _message_payload(
                update_id=101,
                text=None,
                photo=_photo(file_size=8 * 1024 * 1024),
            )
        )
        unknown = _inbound(
            _message_payload(
                update_id=102,
                text=None,
                photo=_photo(file_size=None),
            )
        )

        await (await route.operation(at_limit)).call()
        await (await route.operation(unknown)).call()

        assert len(acceptance.calls) == 2
        assert feedback.commands == {}
        media_at_limit = acceptance.calls[0][0].user_content["media"]
        media_unknown = acceptance.calls[1][0].user_content["media"]
        assert isinstance(media_at_limit, dict)
        assert isinstance(media_unknown, dict)
        assert media_at_limit["file_size"] == 8 * 1024 * 1024
        assert media_unknown["file_size"] is None
        assert media_at_limit["max_download_bytes"] == 8 * 1024 * 1024
        assert media_unknown["max_download_bytes"] == 8 * 1024 * 1024

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (AssistantUserNotRegistered(), AssistantFeedbackReason.USER_NOT_REGISTERED),
        (
            AssistantInsufficientCoins(available=0, required=1),
            AssistantFeedbackReason.INSUFFICIENT_COINS,
        ),
    ],
)
def test_account_rejections_use_typed_feedback_capability(
    result: object,
    reason: AssistantFeedbackReason,
) -> None:
    """@brief 未注册和余额不足通过 typed feedback，不直发 Telegram / Registration and balance rejections use typed feedback instead of direct Telegram sends.

    @param result UoW 拒绝 / UoW rejection.
    @param reason 期望 feedback 原因 / Expected feedback reason.
    """

    async def scenario() -> None:
        """@brief 执行业务拒绝 / Execute a business rejection.

        @return None / None.
        """

        acceptance = RecordingAcceptance(result)
        route, _acceptance, feedback = _route(acceptance=acceptance)
        inbound = _inbound(_message_payload(text="hello"))

        await (await route.operation(inbound)).call()

        assert len(acceptance.calls) == 1
        assert list(feedback.commands) == [
            f"update:100:assistant-feedback:{reason.value}"
        ]

    asyncio.run(scenario())


def test_malformed_candidate_is_permanent_and_layers_have_no_sdk_or_direct_io() -> None:
    """@brief 畸形候选永久失败，route 不依赖 SDK/DB/PTB Context / Malformed candidates fail permanently and the route has no SDK, DB, or PTB Context dependency."""

    payload = _message_payload(text="hello")
    message = payload["message"]
    assert isinstance(message, dict)
    message["date"] = True
    inbound = _inbound(payload)

    with pytest.raises(MalformedTelegramAssistantUpdate):
        parse_telegram_assistant_update(inbound)

    route_path = Path(assistant_primary_route.__file__)
    assert not route_path.with_name("assistant_route.py").exists()
    sources = "\n".join(
        Path(module_path).read_text(encoding="utf-8")
        for module in (
            assistant_primary_route,
            assistant_update_models,
            assistant_update_parser,
        )
        if (module_path := module.__file__) is not None
    )
    assert "from telegram" not in sources
    assert "import telegram" not in sources
    assert "infrastructure.database" not in sources
    assert "ContextTypes" not in sources
    assert "create_task(" not in sources
    assert "asyncio.Lock" not in sources
