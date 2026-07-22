"""@brief User Profile 的证据、声明与纯状态转移 / Evidence, claims, and pure transitions for User Profile."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType
from uuid import UUID

from fogmoe_bot.domain.temporal import ensure_utc

DreamId = NewType("DreamId", UUID)
"""@brief Dreaming 工作项标识 / Dreaming work-item identifier."""

_CLAIM_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
"""@brief Profile claim 稳定键语法 / Stable Profile-claim key grammar."""

MAX_PROFILE_CLAIMS = 64
"""@brief 单个 Profile 的声明上限 / Maximum claims in one Profile."""


class ProfileClaimKind(StrEnum):
    """@brief 可进入 Profile 的声明类别 / Claim categories admitted to a Profile."""

    FACT = "fact"
    PREFERENCE = "preference"
    GOAL = "goal"
    INTERACTION_STYLE = "interaction_style"


class ProfileConfidence(StrEnum):
    """@brief 声明证据强度 / Evidence strength for a claim."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class ProfileMetadata:
    """@brief Dreaming 可见的冻结用户元信息 / Frozen user metadata visible to Dreaming.

    @param display_name acceptance 时的显示名 / Display name at acceptance.
    @param username acceptance 时的用户名 / Username at acceptance.
    @param personal_info 用户显式维护的信息 / User-maintained personal information.
    @param provider 身份 provider / Identity provider.
    """

    display_name: str
    username: str | None = None
    personal_info: str = ""
    provider: str = "telegram"

    def __post_init__(self) -> None:
        """@brief 规范化元信息 / Normalize metadata.

        @return None / None.
        @raise ValueError 显示名、用户名或 provider 非法 / Invalid display name, username, or provider.
        """

        display_name = self.display_name.strip()
        username = self.username.strip() if self.username is not None else None
        provider = self.provider.strip().casefold()
        if not display_name or len(display_name) > 256:
            raise ValueError("Profile display_name must contain 1-256 characters")
        if username is not None and (not username or len(username) > 64):
            raise ValueError("Profile username must contain 1-64 characters")
        if not provider or len(provider) > 32:
            raise ValueError("Profile provider must contain 1-32 characters")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "personal_info", self.personal_info.strip()[:500])
        object.__setattr__(self, "provider", provider)


@dataclass(frozen=True, slots=True)
class ProfileEvidence:
    """@brief 一次完整私聊 Turn 的 Profile 证据 / Profile evidence from one complete private Turn.

    @param event_id Profile 内全局单调事件 ID / Profile-global monotonic event identifier.
    @param source_turn_id Conversation source Turn / Source Conversation Turn.
    @param owner_user_id 认证用户 / Authenticated owner.
    @param user_text 用户原文 / Original user text.
    @param assistant_text Assistant 回应上下文 / Assistant-response context.
    @param occurred_at Turn 完成时间 / Turn completion time.
    @param metadata acceptance 时冻结的用户元信息 / User metadata frozen at acceptance.
    """

    event_id: int
    source_turn_id: UUID
    owner_user_id: int
    user_text: str
    assistant_text: str
    occurred_at: datetime
    metadata: ProfileMetadata

    def __post_init__(self) -> None:
        """@brief 校验证据边界 / Validate evidence boundaries.

        @return None / None.
        @raise ValueError ID、文本或时间非法 / Invalid identity, text, or time.
        """

        user_text = self.user_text.strip()
        assistant_text = self.assistant_text.strip()
        if isinstance(self.event_id, bool) or self.event_id < 0:
            raise ValueError("Profile event_id cannot be negative")
        if isinstance(self.owner_user_id, bool) or self.owner_user_id <= 0:
            raise ValueError("Profile owner_user_id must be positive")
        if not user_text or len(user_text) > 100_000:
            raise ValueError("Profile user_text must contain 1-100000 characters")
        if not assistant_text or len(assistant_text) > 100_000:
            raise ValueError("Profile assistant_text must contain 1-100000 characters")
        object.__setattr__(self, "user_text", user_text)
        object.__setattr__(self, "assistant_text", assistant_text)
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))


@dataclass(frozen=True, slots=True)
class ProfileClaim:
    """@brief Profile 中一条可追溯的当前声明 / One provenance-bearing current Profile claim.

    @param key 跨 revision 稳定语义键 / Stable semantic key across revisions.
    @param kind 声明类别 / Claim category.
    @param statement 面向模型的简洁陈述 / Concise model-facing statement.
    @param confidence 显式或推断 / Explicit or inferred confidence.
    @param evidence_event_ids 最新支持证据 / Latest supporting evidence.
    @param observed_at 最新证据时间 / Latest evidence time.
    """

    key: str
    kind: ProfileClaimKind
    statement: str
    confidence: ProfileConfidence
    evidence_event_ids: tuple[int, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        """@brief 规范并校验声明 / Normalize and validate the claim.

        @return None / None.
        @raise ValueError key、正文或 provenance 非法 / Invalid key, statement, or provenance.
        """

        key = self.key.strip().casefold()
        statement = " ".join(self.statement.split())
        evidence_ids = tuple(dict.fromkeys(self.evidence_event_ids))
        if _CLAIM_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("Profile claim key has invalid syntax")
        if not statement or len(statement) > 500:
            raise ValueError("Profile statement must contain 1-500 characters")
        if not evidence_ids or any(
            isinstance(event_id, bool) or event_id <= 0 for event_id in evidence_ids
        ):
            raise ValueError("Profile claim requires positive evidence event IDs")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "evidence_event_ids", evidence_ids)
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))


@dataclass(frozen=True, slots=True)
class ProfileDocument:
    """@brief 有界且键唯一的当前 User Profile / Bounded current User Profile with unique keys.

    @param claims 按稳定键排序的声明 / Claims ordered by stable key.
    """

    claims: tuple[ProfileClaim, ...] = ()

    def __post_init__(self) -> None:
        """@brief 强制唯一性和规范顺序 / Enforce uniqueness and canonical order.

        @return None / None.
        @raise ValueError 声明过多或 key 重复 / Too many claims or duplicate keys.
        """

        if len(self.claims) > MAX_PROFILE_CLAIMS:
            raise ValueError(f"Profile cannot exceed {MAX_PROFILE_CLAIMS} claims")
        by_key = {claim.key: claim for claim in self.claims}
        if len(by_key) != len(self.claims):
            raise ValueError("Profile claim keys must be unique")
        object.__setattr__(self, "claims", tuple(by_key[key] for key in sorted(by_key)))


@dataclass(frozen=True, slots=True)
class UpsertProfileClaim:
    """@brief 新增或替换一条声明的模型提议 / Model proposal to add or replace one claim."""

    key: str
    kind: ProfileClaimKind
    statement: str
    confidence: ProfileConfidence
    evidence_event_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DeleteProfileClaim:
    """@brief 基于新证据删除旧声明的模型提议 / Model proposal to delete an old claim using new evidence."""

    key: str
    evidence_event_ids: tuple[int, ...]


type ProfileOperation = UpsertProfileClaim | DeleteProfileClaim
"""@brief Profile patch 操作闭集 / Closed set of Profile-patch operations."""


@dataclass(frozen=True, slots=True)
class ProfilePatch:
    """@brief Dreaming 模型产生的结构化 patch / Structured patch produced by the Dreaming model.

    @param operations 有序操作；空即 NO_OP / Ordered operations; empty means NO_OP.
    """

    operations: tuple[ProfileOperation, ...] = ()

    def __post_init__(self) -> None:
        """@brief 限制单批 mutation 数量 / Bound mutations per batch.

        @return None / None.
        @raise ValueError 操作过多 / Too many operations.
        """

        if len(self.operations) > MAX_PROFILE_CLAIMS:
            raise ValueError("Profile patch contains too many operations")


@dataclass(frozen=True, slots=True)
class UserProfileSnapshot:
    """@brief acceptance 可冻结的版本化 Profile snapshot / Versioned Profile snapshot pinnable at acceptance.

    @param user_id Profile owner / Profile owner.
    @param revision 单用户单调 revision / Per-user monotonic revision.
    @param document 当前 Profile 文档 / Current Profile document.
    @param observed_through_event_id 已消费 evidence watermark / Consumed-evidence watermark.
    @param created_at 首次形成时间 / First materialization time.
    @param updated_at 当前 revision 形成时间 / Current revision time.
    @param route_key 形成模型 route / Producing model route.
    @param prompt_version Dreaming prompt 版本 / Dreaming-prompt version.
    """

    user_id: int
    revision: int
    document: ProfileDocument
    observed_through_event_id: int
    created_at: datetime
    updated_at: datetime
    route_key: str
    prompt_version: int

    def __post_init__(self) -> None:
        """@brief 校验 snapshot 单调元数据 / Validate snapshot monotonic metadata.

        @return None / None.
        @raise ValueError owner、revision、watermark 或 route 非法 / Invalid owner, revision, watermark, or route.
        """

        created_at = ensure_utc(self.created_at)
        updated_at = ensure_utc(self.updated_at)
        if isinstance(self.user_id, bool) or self.user_id <= 0:
            raise ValueError("Profile user_id must be positive")
        if isinstance(self.revision, bool) or self.revision <= 0:
            raise ValueError("Profile revision must be positive")
        if (
            isinstance(self.observed_through_event_id, bool)
            or self.observed_through_event_id <= 0
        ):
            raise ValueError("Profile observed watermark must be positive")
        if not self.route_key.strip() or len(self.route_key) > 300:
            raise ValueError("Profile route_key must contain 1-300 characters")
        if isinstance(self.prompt_version, bool) or self.prompt_version <= 0:
            raise ValueError("Profile prompt_version must be positive")
        if updated_at < created_at:
            raise ValueError("Profile updated_at cannot precede created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "route_key", self.route_key.strip())


def apply_profile_patch(
    current: ProfileDocument,
    patch: ProfilePatch,
    *,
    evidence: tuple[ProfileEvidence, ...],
) -> ProfileDocument:
    """@brief 校验 provenance 后确定性应用模型 patch / Deterministically apply a model patch after provenance validation.

    @param current 当前文档 / Current document.
    @param patch 模型提议 / Model proposal.
    @param evidence 本批冻结证据 / Frozen evidence in this batch.
    @return 新文档；NO_OP 返回同值 / New document; NO_OP returns the same value.
    @raise ValueError 操作引用批外证据、跨用户或删除未知 key / Out-of-batch evidence, cross-user data, or unknown deletion key.
    """

    by_event = {item.event_id: item for item in evidence}
    if not by_event:
        raise ValueError("Profile patch requires a non-empty evidence batch")
    owners = {item.owner_user_id for item in evidence}
    if len(owners) != 1:
        raise ValueError("Profile evidence cannot cross user boundaries")
    claims = {claim.key: claim for claim in current.claims}
    for operation in patch.operations:
        key = operation.key.strip().casefold()
        event_ids = tuple(dict.fromkeys(operation.evidence_event_ids))
        if not event_ids or any(event_id not in by_event for event_id in event_ids):
            raise ValueError(
                "Profile operation cites evidence outside the current batch"
            )
        if isinstance(operation, DeleteProfileClaim):
            if key not in claims:
                raise ValueError(f"Profile patch deletes unknown key: {key}")
            del claims[key]
            continue
        observed_at = max(by_event[event_id].occurred_at for event_id in event_ids)
        claim = ProfileClaim(
            key=key,
            kind=operation.kind,
            statement=operation.statement,
            confidence=operation.confidence,
            evidence_event_ids=event_ids,
            observed_at=observed_at,
        )
        claims[claim.key] = claim
    return ProfileDocument(tuple(claims.values()))


__all__ = [
    "DeleteProfileClaim",
    "DreamId",
    "MAX_PROFILE_CLAIMS",
    "ProfileClaim",
    "ProfileClaimKind",
    "ProfileConfidence",
    "ProfileDocument",
    "ProfileEvidence",
    "ProfileMetadata",
    "ProfileOperation",
    "ProfilePatch",
    "UpsertProfileClaim",
    "UserProfileSnapshot",
    "apply_profile_patch",
]
