"""@brief User Profile 的证据、声明与纯状态转移 / Evidence, claims, and pure transitions for User Profile."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType, Self
from uuid import UUID

from fogmoe_bot.domain.temporal import ensure_utc

DreamId = NewType("DreamId", UUID)
"""@brief Dreaming 工作项标识 / Dreaming work-item identifier."""

_CLAIM_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,79}$")
"""@brief Profile claim 稳定键语法 / Stable Profile-claim key grammar."""

_PROVIDER_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
"""@brief 身份 provider 的规范 ASCII key 语法 / Canonical ASCII-key grammar for identity providers."""

MAX_PROFILE_CLAIMS = 64
"""@brief 单个 Profile 的声明上限 / Maximum claims in one Profile."""


def _normalize_claim_key(key: str) -> str:
    """@brief 规范并校验稳定 claim key / Normalize and validate a stable claim key.

    @param key 候选语义键 / Candidate semantic key.
    @return 小写规范键 / Canonical lowercase key.
    """

    if not isinstance(key, str):
        raise TypeError("Profile claim key must be a string")
    normalized = key.strip().casefold()
    if _CLAIM_KEY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("Profile claim key has invalid syntax")
    return normalized


def _freeze_evidence_event_ids(values: Iterable[int]) -> tuple[int, ...]:
    """@brief 冻结并校验 operation provenance IDs / Freeze and validate operation provenance IDs.

    @param values 候选 evidence IDs / Candidate evidence identifiers.
    @return 去重且保持顺序的正整数 tuple / Deduplicated, order-preserving tuple of positive integers.
    """

    raw = tuple(values)
    if not raw or any(
        isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0
        for event_id in raw
    ):
        raise ValueError("Profile operation requires positive integer evidence IDs")
    return tuple(dict.fromkeys(raw))


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

        if not isinstance(self.display_name, str):
            raise TypeError("Profile display_name must be a string")
        if self.username is not None and not isinstance(self.username, str):
            raise TypeError("Profile username must be a string or None")
        if not isinstance(self.personal_info, str):
            raise TypeError("Profile personal_info must be a string")
        if not isinstance(self.provider, str):
            raise TypeError("Profile provider must be a string")
        display_name = self.display_name.strip()
        username = self.username.strip() if self.username is not None else None
        provider = self.provider.strip().casefold()
        if not display_name or len(display_name) > 256:
            raise ValueError("Profile display_name must contain 1-256 characters")
        if username is not None and (not username or len(username) > 64):
            raise ValueError("Profile username must contain 1-64 characters")
        if _PROVIDER_KEY_PATTERN.fullmatch(provider) is None:
            raise ValueError("Profile provider must be a canonical provider key")
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

        if not isinstance(self.source_turn_id, UUID):
            raise TypeError("Profile source_turn_id must be a UUID")
        if not isinstance(self.user_text, str):
            raise TypeError("Profile user_text must be a string")
        if not isinstance(self.assistant_text, str):
            raise TypeError("Profile assistant_text must be a string")
        if not isinstance(self.metadata, ProfileMetadata):
            raise TypeError("Profile evidence requires ProfileMetadata")
        user_text = self.user_text.strip()
        assistant_text = self.assistant_text.strip()
        if (
            isinstance(self.event_id, bool)
            or not isinstance(self.event_id, int)
            or self.event_id < 0
        ):
            raise ValueError("Profile event_id cannot be negative")
        if (
            isinstance(self.owner_user_id, bool)
            or not isinstance(self.owner_user_id, int)
            or self.owner_user_id <= 0
        ):
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

        if not isinstance(self.kind, ProfileClaimKind):
            raise TypeError("Profile claim requires ProfileClaimKind")
        if not isinstance(self.confidence, ProfileConfidence):
            raise TypeError("Profile claim requires ProfileConfidence")
        if not isinstance(self.statement, str):
            raise TypeError("Profile claim statement must be a string")
        key = _normalize_claim_key(self.key)
        statement = " ".join(self.statement.split())
        evidence_ids = _freeze_evidence_event_ids(self.evidence_event_ids)
        if not statement or len(statement) > 500:
            raise ValueError("Profile statement must contain 1-500 characters")
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

        claims = tuple(self.claims)
        if any(not isinstance(claim, ProfileClaim) for claim in claims):
            raise TypeError("Profile document requires ProfileClaim values")
        if len(claims) > MAX_PROFILE_CLAIMS:
            raise ValueError(f"Profile cannot exceed {MAX_PROFILE_CLAIMS} claims")
        by_key = {claim.key: claim for claim in claims}
        if len(by_key) != len(claims):
            raise ValueError("Profile claim keys must be unique")
        object.__setattr__(self, "claims", tuple(by_key[key] for key in sorted(by_key)))

    def apply(
        self,
        patch: ProfilePatch,
        *,
        evidence: Iterable[ProfileEvidence],
    ) -> Self:
        """@brief 校验 provenance 后确定性应用模型 patch / Deterministically apply a model patch after provenance validation.

        @param patch 模型提议 / Model proposal.
        @param evidence 本批冻结证据 / Frozen evidence in this batch.
        @return 新文档；NO_OP 返回同值 / New document; NO_OP returns the same value.
        @raise ValueError 操作引用批外证据、跨用户或删除未知 key / Out-of-batch evidence, cross-user data, or unknown deletion key.
        """

        if not isinstance(patch, ProfilePatch):
            raise TypeError("Profile document apply requires ProfilePatch")
        frozen_evidence = tuple(evidence)
        if any(not isinstance(item, ProfileEvidence) for item in frozen_evidence):
            raise TypeError("Profile patch evidence requires ProfileEvidence values")
        by_event = {item.event_id: item for item in frozen_evidence}
        if not by_event:
            raise ValueError("Profile patch requires a non-empty evidence batch")
        owners = {item.owner_user_id for item in frozen_evidence}
        if len(owners) != 1:
            raise ValueError("Profile evidence cannot cross user boundaries")
        claims = {claim.key: claim for claim in self.claims}
        for operation in patch.operations:
            event_ids = operation.evidence_event_ids
            if any(event_id not in by_event for event_id in event_ids):
                raise ValueError(
                    "Profile operation cites evidence outside the current batch"
                )
            if isinstance(operation, DeleteProfileClaim):
                if operation.key not in claims:
                    raise ValueError(
                        f"Profile patch deletes unknown key: {operation.key}"
                    )
                del claims[operation.key]
                continue
            observed_at = max(by_event[event_id].occurred_at for event_id in event_ids)
            claim = ProfileClaim(
                key=operation.key,
                kind=operation.kind,
                statement=operation.statement,
                confidence=operation.confidence,
                evidence_event_ids=event_ids,
                observed_at=observed_at,
            )
            claims[claim.key] = claim
        return type(self)(tuple(claims.values()))


@dataclass(frozen=True, slots=True)
class UpsertProfileClaim:
    """@brief 新增或替换一条声明的模型提议 / Model proposal to add or replace one claim.

    @param key 跨 revision 稳定语义键 / Stable semantic key across revisions.
    @param kind 声明类别 / Claim category.
    @param statement 面向模型的简洁陈述 / Concise model-facing statement.
    @param confidence 显式或推断 / Explicit or inferred confidence.
    @param evidence_event_ids 本批支持证据 / Supporting evidence in the current batch.
    """

    key: str
    kind: ProfileClaimKind
    statement: str
    confidence: ProfileConfidence
    evidence_event_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        """@brief 冻结并校验 upsert 提议 / Freeze and validate an upsert proposal.

        @return None / None.
        """

        if not isinstance(self.kind, ProfileClaimKind):
            raise TypeError("Profile upsert requires ProfileClaimKind")
        if not isinstance(self.confidence, ProfileConfidence):
            raise TypeError("Profile upsert requires ProfileConfidence")
        if not isinstance(self.statement, str):
            raise TypeError("Profile upsert statement must be a string")
        statement = " ".join(self.statement.split())
        if not statement or len(statement) > 500:
            raise ValueError("Profile upsert statement must contain 1-500 characters")
        object.__setattr__(self, "key", _normalize_claim_key(self.key))
        object.__setattr__(self, "statement", statement)
        object.__setattr__(
            self,
            "evidence_event_ids",
            _freeze_evidence_event_ids(self.evidence_event_ids),
        )


@dataclass(frozen=True, slots=True)
class DeleteProfileClaim:
    """@brief 基于新证据删除旧声明的模型提议 / Model proposal to delete an old claim using new evidence.

    @param key 待删除的稳定语义键 / Stable semantic key to delete.
    @param evidence_event_ids 本批撤销证据 / Retraction evidence in the current batch.
    """

    key: str
    evidence_event_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        """@brief 冻结并校验 delete 提议 / Freeze and validate a delete proposal.

        @return None / None.
        """

        object.__setattr__(self, "key", _normalize_claim_key(self.key))
        object.__setattr__(
            self,
            "evidence_event_ids",
            _freeze_evidence_event_ids(self.evidence_event_ids),
        )


type ProfileOperation = UpsertProfileClaim | DeleteProfileClaim
"""@brief Profile patch 操作闭集 / Closed set of Profile-patch operations."""


@dataclass(frozen=True, slots=True)
class ProfilePatch:
    """@brief Dreaming 模型产生的结构化 patch / Structured patch produced by the Dreaming model.

    @param operations 有序操作；空即 NO_OP / Ordered operations; empty means NO_OP.
    """

    operations: tuple[ProfileOperation, ...] = ()

    def __post_init__(self) -> None:
        """@brief 冻结闭集 operation 并限制单批 mutation 数量 / Freeze closed-set operations and bound mutations per batch.

        @return None / None.
        @raise TypeError operation 不属于领域闭集 / An operation is outside the domain's closed set.
        @raise ValueError 操作过多 / Too many operations.
        """

        operations = tuple(self.operations)
        if any(
            not isinstance(operation, UpsertProfileClaim | DeleteProfileClaim)
            for operation in operations
        ):
            raise TypeError("Profile patch contains an unknown operation")
        if len(operations) > MAX_PROFILE_CLAIMS:
            raise ValueError("Profile patch contains too many operations")
        object.__setattr__(self, "operations", operations)


@dataclass(frozen=True, slots=True)
class UserProfileSnapshot:
    """@brief acceptance 可冻结的版本化 Profile snapshot / Versioned Profile snapshot pinnable at acceptance.

    @param user_id Profile owner / Profile owner.
    @param revision 单用户单调 revision / Per-user monotonic revision.
    @param document 当前 Profile 文档 / Current Profile document.
    @param observed_through_event_id 形成此不可变 revision 时的 evidence provenance watermark；
        NO_OP 之后可能落后于 Profile head scheduler cursor / Evidence-provenance watermark
        when this immutable revision was formed; it may trail the Profile-head scheduler cursor
        after a NO_OP.
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
        if (
            isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id <= 0
        ):
            raise ValueError("Profile user_id must be positive")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision <= 0
        ):
            raise ValueError("Profile revision must be positive")
        if not isinstance(self.document, ProfileDocument):
            raise TypeError("Profile snapshot requires ProfileDocument")
        if (
            isinstance(self.observed_through_event_id, bool)
            or not isinstance(self.observed_through_event_id, int)
            or self.observed_through_event_id <= 0
        ):
            raise ValueError("Profile observed watermark must be positive")
        if not isinstance(self.route_key, str):
            raise TypeError("Profile route_key must be a string")
        if not self.route_key.strip() or len(self.route_key) > 300:
            raise ValueError("Profile route_key must contain 1-300 characters")
        if (
            isinstance(self.prompt_version, bool)
            or not isinstance(self.prompt_version, int)
            or self.prompt_version <= 0
        ):
            raise ValueError("Profile prompt_version must be positive")
        if updated_at < created_at:
            raise ValueError("Profile updated_at cannot precede created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "route_key", self.route_key.strip())


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
]
