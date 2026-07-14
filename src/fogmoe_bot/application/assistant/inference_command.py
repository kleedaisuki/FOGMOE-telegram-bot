"""@brief Durable Assistant 推理命令契约 / Durable Assistant inference-command contract.

该模块只定义 acceptance 与 inference worker 共享的版本化数据契约，不依赖具体
provider、Telegram SDK 或持久化实现。/ This module defines only the versioned data
contract shared by acceptance and the inference worker; it does not depend on a concrete
provider, Telegram SDK, or persistence implementation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    TurnId,
)
from fogmoe_bot.domain.user_profile.models import (
    ProfileClaimKind,
    ProfileConfidence,
    UserProfileSnapshot,
)
from fogmoe_bot.application.conversation.telegram_identity import (
    TelegramConversationAddress,
)


ASSISTANT_INFERENCE_SCHEMA_VERSION: Literal[2] = 2
"""@brief Durable inference request schema 版本 / Durable inference-request schema version."""

type AssistantTaskKind = Literal["assistant", "translation"]
"""@brief Durable Assistant 活动种类 / Durable Assistant activity kind."""


class _StrictFrozenModel(BaseModel):
    """@brief 严格且冻结的 durable request 基类 / Strict frozen base for durable requests."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    """@brief 禁止隐式强制转换与未知字段 / Forbid implicit coercion and unknown fields."""


class DurableProfileClaim(_StrictFrozenModel):
    """@brief acceptance 时冻结的一条 Profile claim / One Profile claim frozen at acceptance."""

    key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]{0,79}$")
    kind: ProfileClaimKind
    statement: str = Field(min_length=1, max_length=500)
    confidence: ProfileConfidence
    evidence_event_ids: tuple[int, ...] = Field(min_length=1, max_length=16)
    observed_at: datetime


class DurableUserProfile(_StrictFrozenModel):
    """@brief 一个 Turn 冻结的版本化 User Profile / Versioned User Profile frozen for one Turn."""

    revision: int = Field(ge=1)
    observed_through_event_id: int = Field(ge=1)
    prompt_version: int = Field(ge=1)
    route_key: str = Field(min_length=1, max_length=300)
    created_at: datetime
    updated_at: datetime
    claims: tuple[DurableProfileClaim, ...] = Field(max_length=64)

    @classmethod
    def from_snapshot(cls, snapshot: UserProfileSnapshot) -> DurableUserProfile:
        """@brief 从领域 snapshot 构造 durable DTO / Build a durable DTO from a domain snapshot.

        @param snapshot committed Profile snapshot / Committed Profile snapshot.
        @return durable Profile / Durable Profile.
        """

        return cls(
            revision=snapshot.revision,
            observed_through_event_id=snapshot.observed_through_event_id,
            prompt_version=snapshot.prompt_version,
            route_key=snapshot.route_key,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            claims=tuple(
                DurableProfileClaim(
                    key=claim.key,
                    kind=claim.kind,
                    statement=claim.statement,
                    confidence=claim.confidence,
                    evidence_event_ids=claim.evidence_event_ids,
                    observed_at=claim.observed_at,
                )
                for claim in snapshot.document.claims
            ),
        )


class DurableAssistantUser(_StrictFrozenModel):
    """@brief acceptance 时冻结的用户上下文 / User context frozen at acceptance time.

    @param user_id Telegram 用户 ID / Telegram user identifier.
    @param username 可选 Telegram username / Optional Telegram username.
    @param display_name 用户显示名 / User display name.
    @param coins acceptance 时的硬币余额 / Coin balance at acceptance.
    @param plan 订阅计划 / Subscription plan.
    @param permission 权限等级 / Permission level.
    @param profile acceptance 时冻结的 User Profile / User Profile frozen at acceptance.
    @param personal_info 用户个人信息 / User personal information.
    @param diary_exists 是否已有日记 / Whether a diary exists.
    """

    user_id: int = Field(ge=1)
    username: str | None = Field(default=None, min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=256)
    coins: int = Field(ge=0)
    plan: str = Field(min_length=1, max_length=32)
    permission: int
    profile: DurableUserProfile | None = None
    personal_info: str = Field(default="", max_length=500)
    diary_exists: bool = False


class DurableAssistantScope(_StrictFrozenModel):
    """@brief 当前 Turn 的 ConversationScope 快照 / ConversationScope snapshot for the current Turn.

    @param is_group 是否群聊 / Whether this is a group chat.
    @param group_id 可选群聊 ID / Optional group chat identifier.
    @param message_id 可选来源消息 ID / Optional source-message identifier.
    @param message_thread_id 可选 Telegram Topic ID / Optional Telegram topic identifier.
    """

    is_group: bool
    group_id: int | None = None
    message_id: int | None = Field(default=None, ge=1)
    message_thread_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_group_boundary(self) -> Self:
        """@brief 校验群聊标志与 ID 一致 / Validate consistency between group flag and identifier.

        @return 已校验模型 / Validated model.
        @raise ValueError 群聊边界不一致时抛出 / Raised for an inconsistent group boundary.
        """

        if self.is_group and self.group_id is None:
            raise ValueError("group_id is required for a group scope")
        if not self.is_group and self.group_id is not None:
            raise ValueError("group_id is only valid for a group scope")
        if self.group_id == 0:
            raise ValueError("group_id cannot be zero")
        if not self.is_group and self.message_thread_id is not None:
            raise ValueError("message_thread_id is only valid for a group scope")
        return self


class DurableAssistantInferenceCommand(_StrictFrozenModel):
    """@brief 版本化 durable Assistant 推理命令 / Versioned durable Assistant inference command.

    @param schema_version 命令 schema 版本 / Command schema version.
    @param task_kind 推理任务种类 / Inference task kind.
    @param translation_input 翻译活动的隔离输入 / Isolated input for a translation activity.
    @param conversation_id 长期会话键 / Long-lived conversation key.
    @param turn_id 当前 Turn UUID 文本 / Current Turn UUID text.
    @param delivery_stream_id 有序投递流 / Ordered delivery stream.
    @param chat_id Telegram chat ID 或频道 username / Telegram chat ID or channel username.
    @param reply_to_message_id 可选回复目标 / Optional reply target.
    @param message_thread_id 可选话题 ID / Optional topic identifier.
    @param user acceptance 时用户快照 / User snapshot at acceptance.
    @param scope 当前对话作用域 / Current conversation scope.
    @param disable_notification 是否静默投递 / Whether delivery is silent.
    @param protect_content 是否保护内容 / Whether content is protected.
    @param disable_web_page_preview 是否禁用链接预览 / Whether link previews are disabled.
    """

    schema_version: Literal[2] = ASSISTANT_INFERENCE_SCHEMA_VERSION
    task_kind: AssistantTaskKind = "assistant"
    translation_input: str | None = Field(default=None, min_length=1, max_length=3000)
    conversation_id: str = Field(min_length=1, max_length=512)
    turn_id: str
    delivery_stream_id: str = Field(min_length=1, max_length=512)
    chat_id: int | str
    reply_to_message_id: int | None = Field(default=None, ge=1)
    message_thread_id: int | None = Field(default=None, ge=1)
    user: DurableAssistantUser
    scope: DurableAssistantScope
    disable_notification: bool = False
    protect_content: bool = False
    disable_web_page_preview: bool = True

    @field_validator("turn_id")
    @classmethod
    def validate_turn_id(cls, value: str) -> str:
        """@brief 校验并规范化 Turn UUID / Validate and normalize the Turn UUID.

        @param value UUID 文本 / UUID text.
        @return 规范 UUID 文本 / Canonical UUID text.
        @raise ValueError UUID 非法时抛出 / Raised for an invalid UUID.
        """

        return str(UUID(value))

    @field_validator("chat_id")
    @classmethod
    def validate_chat_id(cls, value: int | str) -> int | str:
        """@brief 校验 Telegram chat ID / Validate the Telegram chat identifier.

        @param value 数字 ID 或频道 username / Numeric ID or channel username.
        @return 规范 chat ID / Canonical chat identifier.
        @raise ValueError ID 为空或为零时抛出 / Raised for an empty or zero identifier.
        """

        if isinstance(value, int):
            if value == 0:
                raise ValueError("chat_id cannot be zero")
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("chat_id cannot be empty")
        if len(normalized) > 256:
            raise ValueError("chat_id cannot exceed 256 characters")
        return normalized

    @model_validator(mode="after")
    def validate_cross_field_boundaries(self) -> Self:
        """@brief 校验 user、scope 与投递 chat 的交叉不变量 / Validate cross-field user, scope, and delivery invariants.

        @return 已校验模型 / Validated model.
        @raise ValueError ID 边界不一致时抛出 / Raised for inconsistent identifier boundaries.
        """

        if self.task_kind == "translation" and self.translation_input is None:
            raise ValueError("translation_input is required for translation")
        if self.task_kind == "assistant" and self.translation_input is not None:
            raise ValueError("translation_input is only valid for translation")
        if self.scope.is_group:
            if not isinstance(self.chat_id, int):
                raise ValueError("group scope requires an integer chat_id")
            if self.scope.group_id != self.chat_id:
                raise ValueError("group scope must target its group chat_id")
        if self.scope.is_group and (
            self.user.profile is not None
            or bool(self.user.personal_info)
            or self.user.diary_exists
        ):
            raise ValueError(
                "group scope cannot contain private User Profile, personal_info, "
                "or diary state"
            )
        if (
            self.scope.message_id is not None
            and self.reply_to_message_id is not None
            and self.scope.message_id != self.reply_to_message_id
        ):
            raise ValueError(
                "reply_to_message_id must match the current scope message_id"
            )
        if self.scope.message_thread_id != self.message_thread_id:
            raise ValueError(
                "scope message_thread_id must match the delivery message_thread_id"
            )
        if self.task_kind == "assistant":
            expected_conversation_id = TelegramConversationAddress(
                chat_type="group" if self.scope.is_group else "private",
                chat_id=(
                    self.scope.group_id if self.scope.is_group else self.user.user_id
                ),
                user_id=self.user.user_id,
                message_thread_id=self.scope.message_thread_id,
            ).conversation_id
            if self.conversation_id != str(expected_conversation_id):
                raise ValueError(
                    "conversation_id does not match its Telegram user/group/topic scope"
                )
        return self

    @property
    def typed_conversation_id(self) -> ConversationId:
        """@brief 返回领域 ConversationId / Return the domain ConversationId.

        @return 会话 ID / Conversation identifier.
        """

        return ConversationId(self.conversation_id)

    @property
    def typed_turn_id(self) -> TurnId:
        """@brief 返回领域 TurnId / Return the domain TurnId.

        @return 回合 ID / Turn identifier.
        """

        return TurnId.parse(self.turn_id)

    def to_json(self) -> JsonObject:
        """@brief 序列化为持久化 JSON 对象 / Serialize to a persistable JSON object.

        @return 只含 JSON 值的深拷贝 / Deep copy containing only JSON values.
        """

        return cast(JsonObject, self.model_dump(mode="json"))

    @classmethod
    def from_json(cls, payload: JsonObject) -> Self:
        """@brief 从持久化 JSON 重建严格命令 / Restore a strict command from persisted JSON.

        @param payload JSONB 解码后的对象 / Object decoded from JSONB.
        @return 已冻结且严格校验的命令 / Frozen command validated strictly.
        @raise TypeError 载荷不含 JSON 可表示值时抛出 / Raised when payload contains a value that JSON cannot represent.
        @raise ValueError JSON 非法或不符合命令契约时抛出 / Raised when JSON is invalid or violates the command contract.
        @note 必须经 JSON 通道验证，而非把已解码字典当作 Python 对象验证：JSON 中的
            ISO 8601 时间字符串和数组分别是 ``datetime``、``tuple`` 的规范持久化
            表示。/ Validation deliberately goes through the JSON channel rather than
            treating the decoded dictionary as Python input: ISO 8601 strings and arrays are
            the canonical persisted representations of ``datetime`` and ``tuple``.
        """

        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return cls.model_validate_json(encoded, strict=True)


__all__ = [
    "ASSISTANT_INFERENCE_SCHEMA_VERSION",
    "AssistantTaskKind",
    "DurableAssistantInferenceCommand",
    "DurableAssistantScope",
    "DurableAssistantUser",
    "DurableProfileClaim",
    "DurableUserProfile",
]
