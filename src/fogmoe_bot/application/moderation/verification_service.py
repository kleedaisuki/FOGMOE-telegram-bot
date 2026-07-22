"""@brief 成员验证应用服务 / Member-verification application service."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from fogmoe_bot.application.runtime import SystemUtcClock, UtcClock
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    MessageId,
    ModerationToggleResult,
    UserId,
)
from fogmoe_bot.domain.moderation.verification import (
    InvalidVerificationTransition,
    StaleVerificationVersion,
    VerificationClaim,
    VerificationEvent,
    VerificationFencingError,
    VerificationKey,
    VerificationStatus,
    VerificationTask,
    VerificationVersion,
    hash_verification_token,
)

VERIFICATION_SERVICE_DATA_KEY = "fogmoe.verification_service"
"""@brief 组合根保存验证服务的稳定键 / Stable composition-root key for the verification service."""

DEFAULT_VERIFICATION_LIFETIME = timedelta(minutes=5)
"""@brief 默认验证时限 / Default verification lifetime."""

DEFAULT_CREATION_RECOVERY = timedelta(seconds=30)
"""@brief CREATING 失联后的补偿等待时间 / Recovery delay for abandoned CREATING workflows."""

logger = logging.getLogger(__name__)


@runtime_checkable
class VerificationRepository(Protocol):
    """@brief 验证用例所需的持久化端口 / Persistence port required by verification use cases."""

    async def group_enabled(self, chat_id: ChatId) -> bool:
        """@brief 查询群组开关 / Read group switch.

        @param chat_id 群组 ID / Chat ID.
        @return 是否启用 / Whether enabled.
        """

        ...

    async def enable_group(self, chat_id: ChatId, group_name: str) -> None:
        """@brief 开启群组验证 / Enable group verification.

        @param chat_id 群组 ID / Chat ID.
        @param group_name 群组名称 / Group name.
        @return None / None.
        """

        ...

    async def disable_group(self, chat_id: ChatId) -> None:
        """@brief 关闭群组验证 / Disable group verification.

        @param chat_id 群组 ID / Chat ID.
        @return None / None.
        """

        ...

    async def create(
        self, task: VerificationTask, *, recover_at: datetime
    ) -> VerificationTask:
        """@brief 创建 CREATING 聚合 / Create a CREATING aggregate.

        @param task 创建意图 / Creation intent.
        @param recover_at 失联恢复时间 / Abandonment recovery time.
        @return 规范聚合 / Canonical aggregate.
        """

        ...

    async def load(self, key: VerificationKey) -> VerificationTask | None:
        """@brief 读取聚合 / Load an aggregate.

        @param key 聚合键 / Aggregate key.
        @return 聚合或 None / Aggregate or None.
        """

        ...

    async def apply(
        self,
        key: VerificationKey,
        *,
        expected_version: VerificationVersion,
        event: VerificationEvent,
        now: datetime,
        message_id: MessageId | None = None,
    ) -> VerificationTask:
        """@brief 乐观并发应用事件 / Apply an event with optimistic concurrency.

        @param key 聚合键 / Aggregate key.
        @param expected_version 预期版本 / Expected version.
        @param event 领域事件 / Domain event.
        @param now 事件时刻 / Event time.
        @param message_id 可选消息 ID / Optional message ID.
        @return 更新聚合 / Updated aggregate.
        """

        ...

    async def claim_ready(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> Sequence[VerificationClaim]:
        """@brief 有界领取就绪工作 / Claim bounded ready work.

        @param now 当前时刻 / Current time.
        @param limit 最大数量 / Maximum count.
        @param lease_for 租约时长 / Lease duration.
        @return claims / Claims.
        """

        ...

    async def claim_one(
        self,
        key: VerificationKey,
        *,
        now: datetime,
        lease_for: timedelta,
    ) -> VerificationClaim | None:
        """@brief 领取指定聚合 / Claim one aggregate.

        @param key 聚合键 / Aggregate key.
        @param now 当前时刻 / Current time.
        @param lease_for 租约 / Lease duration.
        @return claim 或 None / Claim or None.
        """

        ...

    async def complete(
        self, claim: VerificationClaim, *, now: datetime
    ) -> VerificationTask:
        """@brief fencing 完成副作用 / Complete effects with fencing.

        @param claim claim / Claim.
        @param now 完成时刻 / Completion time.
        @return 终态聚合 / Terminal aggregate.
        """

        ...

    async def retry(
        self,
        claim: VerificationClaim,
        *,
        retry_at: datetime,
        error: str,
        now: datetime,
    ) -> None:
        """@brief fencing 安排重试 / Schedule retry with fencing.

        @param claim claim / Claim.
        @param retry_at 下次时间 / Next time.
        @param error 错误摘要 / Error summary.
        @param now 当前时刻 / Current time.
        @return None / None.
        """

        ...

    async def recover_expired_leases(self, *, now: datetime) -> int:
        """@brief 回收过期租约 / Recover expired leases.

        @param now 当前时刻 / Current time.
        @return 回收数 / Recovery count.
        """

        ...


@runtime_checkable
class VerificationToggleRepository(Protocol):
    """@brief 仅由管理命令使用的原子开关端口 / Atomic toggle port used only by the admin command."""

    async def toggle_group(
        self,
        chat_id: ChatId,
        *,
        group_name: str,
        actor_id: UserId,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 原子切换验证并保存 source receipt / Atomically toggle verification and save the source receipt.

        @param chat_id 群组 ID / Chat ID.
        @param group_name 群组名称 / Group name.
        @param actor_id 管理员 ID / Administrator ID.
        @param idempotency_key source Update 稳定键 / Stable source-Update key.
        @return 首次提交或回放结果 / First committed or replayed result.
        """

        ...


@runtime_checkable
class VerificationDelivery(Protocol):
    """@brief 执行验证过渡态 Telegram 副作用的端口 / Port delivering Telegram effects for transitional states."""

    async def deliver(self, task: VerificationTask) -> None:
        """@brief 执行一次可重放副作用尝试 / Perform one replayable effect attempt.

        @param task PASSING、EXPIRING 或 CANCELLING 聚合 / PASSING, EXPIRING, or CANCELLING aggregate.
        @return None / None.
        @note 外部 API 不支持通用幂等键，因此该端口是 at-least-once。/
            External APIs expose no general idempotency key, so this port is at-least-once.
        """

        ...


class VerificationRejectionCode(StrEnum):
    """@brief callback/事件的稳定拒绝原因 / Stable callback/event rejection reason."""

    NOT_FOUND = "not_found"
    """@brief 聚合不存在 / Aggregate not found."""

    STALE_VERSION = "stale_version"
    """@brief callback 版本陈旧 / Callback version is stale."""

    INVALID_TOKEN = "invalid_token"
    """@brief token 不正确 / Token is invalid."""

    EXPIRED = "expired"
    """@brief deadline 已过 / Deadline passed."""

    NOT_PENDING = "not_pending"
    """@brief 聚合已被其他事件线性化 / Another event already linearized the aggregate."""


@dataclass(frozen=True, slots=True)
class VerificationInvitation:
    """@brief 创建流程返回的 token 与聚合 / Token and aggregate returned by creation.

    @param task CREATING 聚合 / CREATING aggregate.
    @param token 仅用于 Telegram callback 的明文 token / Plain token used only by Telegram callback.
    """

    task: VerificationTask
    """@brief 创建聚合 / Creation aggregate."""

    token: str
    """@brief 明文 token / Plain token."""


@dataclass(frozen=True, slots=True)
class VerificationAccepted:
    """@brief 状态决定已持久化 / State decision was persisted.

    @param task 过渡态聚合 / Transitional aggregate.
    @param effect_completed 本次请求是否同时确认外部副作用 / Whether this request also acknowledged external effects.
    """

    task: VerificationTask
    """@brief 过渡态聚合 / Transitional aggregate."""

    effect_completed: bool
    """@brief 即时副作用完成标记 / Immediate-effect completion flag."""


@dataclass(frozen=True, slots=True)
class VerificationRejected:
    """@brief 可预期验证拒绝 / Expected verification rejection.

    @param code 拒绝原因 / Rejection reason.
    @param current_version 可选当前版本 / Optional current version.
    """

    code: VerificationRejectionCode
    """@brief 拒绝原因 / Rejection reason."""

    current_version: VerificationVersion | None = None
    """@brief 当前版本 / Current version."""


type PassResult = VerificationAccepted | VerificationRejected
"""@brief 验证 callback 的穷尽结果 / Exhaustive verification-callback result."""


class VerificationService:
    """@brief 版本化验证用例与副作用处理器 / Versioned verification use cases and effect processor."""

    def __init__(
        self,
        *,
        repository: VerificationRepository,
        delivery: VerificationDelivery,
        clock: UtcClock | None = None,
        lease_for: timedelta = timedelta(seconds=30),
        creation_recovery: timedelta = DEFAULT_CREATION_RECOVERY,
    ) -> None:
        """@brief 注入持久化、投递与时间端口 / Inject persistence, delivery, and time ports.

        @param repository 验证仓储 / Verification repository.
        @param delivery Telegram 副作用端口 / Telegram effect port.
        @param clock UTC 时钟 / UTC clock.
        @param lease_for 即时 claim 租约 / Immediate-claim lease.
        @param creation_recovery 创建失联恢复延迟 / Abandoned-creation recovery delay.
        @return None / None.
        """

        if not isinstance(repository, VerificationRepository):
            raise TypeError("repository must implement VerificationRepository")
        if not isinstance(delivery, VerificationDelivery):
            raise TypeError("delivery must implement VerificationDelivery")
        if lease_for <= timedelta(0) or creation_recovery <= timedelta(0):
            raise ValueError("verification durations must be positive")
        self._repository = repository
        self._toggle_repository = (
            repository if isinstance(repository, VerificationToggleRepository) else None
        )
        self._delivery = delivery
        self._clock = clock or SystemUtcClock()
        self._lease_for = lease_for
        self._creation_recovery = creation_recovery

    async def group_enabled(self, chat_id: ChatId) -> bool:
        """@brief 查询群组验证开关 / Read group verification switch.

        @param chat_id 群组 ID / Chat ID.
        @return 是否启用 / Whether enabled.
        """

        return await self._repository.group_enabled(chat_id)

    async def enable_group(self, chat_id: ChatId, group_name: str) -> None:
        """@brief 开启群组验证 / Enable group verification.

        @param chat_id 群组 ID / Chat ID.
        @param group_name 群组名称 / Group name.
        @return None / None.
        """

        await self._repository.enable_group(chat_id, group_name)

    async def disable_group(self, chat_id: ChatId) -> None:
        """@brief 关闭群组验证 / Disable group verification.

        @param chat_id 群组 ID / Chat ID.
        @return None / None.
        """

        await self._repository.disable_group(chat_id)

    async def toggle_group(
        self,
        chat_id: ChatId,
        *,
        group_name: str,
        actor_id: UserId,
        idempotency_key: str,
    ) -> ModerationToggleResult:
        """@brief 原子切换验证并返回首次结果 / Atomically toggle verification and return the first result.

        @param chat_id 群组 ID / Chat ID.
        @param group_name 群组名称 / Group name.
        @param actor_id 管理员 ID / Administrator ID.
        @param idempotency_key source Update 稳定键 / Stable source-Update key.
        @return 首次提交或回放结果 / First committed or replayed result.
        """

        repository = self._toggle_repository
        if repository is None:
            raise RuntimeError("verification toggle repository is not configured")
        return await repository.toggle_group(
            chat_id,
            group_name=group_name,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    async def begin(
        self,
        key: VerificationKey,
        *,
        member_name: str,
        lifetime: timedelta = DEFAULT_VERIFICATION_LIFETIME,
    ) -> VerificationInvitation:
        """@brief 先持久化创建意图并生成 callback token / Persist creation intent before generating Telegram effects.

        @param key 聚合键 / Aggregate key.
        @param member_name 成员展示名 / Member display name.
        @param lifetime 验证时限 / Verification lifetime.
        @return 创建邀请 / Creation invitation.
        """

        if lifetime <= timedelta(0):
            raise ValueError("verification lifetime must be positive")
        now = self._clock.now()
        token = secrets.token_hex(8)
        task = VerificationTask(
            key=key,
            version=VerificationVersion(0),
            token_hash=hash_verification_token(token),
            member_name=member_name,
            expires_at=now + lifetime,
        )
        canonical = await self._repository.create(
            task,
            recover_at=now + self._creation_recovery,
        )
        return VerificationInvitation(canonical, token)

    async def activate(
        self,
        invitation: VerificationInvitation,
        message_id: MessageId,
    ) -> VerificationTask:
        """@brief 绑定欢迎消息并进入 PENDING / Bind the welcome message and enter PENDING.

        @param invitation 创建邀请 / Creation invitation.
        @param message_id 欢迎消息 ID / Welcome-message ID.
        @return PENDING 聚合 / PENDING aggregate.
        """

        return await self._repository.apply(
            invitation.task.key,
            expected_version=invitation.task.version,
            event=VerificationEvent.ACTIVATE,
            now=self._clock.now(),
            message_id=message_id,
        )

    async def abort_creation(
        self, invitation: VerificationInvitation
    ) -> VerificationAccepted | VerificationRejected:
        """@brief 将失败创建转为可重放补偿 / Turn failed creation into replayable compensation.

        @param invitation 创建邀请 / Creation invitation.
        @return 接受或竞争拒绝 / Accepted or race rejection.
        """

        try:
            task = await self._repository.apply(
                invitation.task.key,
                expected_version=invitation.task.version,
                event=VerificationEvent.ABORT_CREATION,
                now=self._clock.now(),
            )
        except StaleVerificationVersion, InvalidVerificationTransition:
            current = await self._repository.load(invitation.task.key)
            return VerificationRejected(
                VerificationRejectionCode.STALE_VERSION,
                current.version if current is not None else None,
            )
        completed = await self.deliver_ready(invitation.task.key)
        return VerificationAccepted(task, completed)

    async def request_pass(
        self,
        key: VerificationKey,
        *,
        expected_version: VerificationVersion,
        token: str,
    ) -> PassResult:
        """@brief 线性化 callback 通过决定并尝试即时投递 / Linearize a callback pass decision and attempt immediate delivery.

        @param key 聚合键 / Aggregate key.
        @param expected_version callback 版本 / Callback version.
        @param token callback token / Callback token.
        @return 接受或拒绝 / Accepted or rejected.
        """

        current = await self._repository.load(key)
        if current is None:
            return VerificationRejected(VerificationRejectionCode.NOT_FOUND)
        if current.version != expected_version:
            return VerificationRejected(
                VerificationRejectionCode.STALE_VERSION, current.version
            )
        now = self._clock.now()
        if current.status is not VerificationStatus.PENDING:
            return VerificationRejected(
                VerificationRejectionCode.NOT_PENDING, current.version
            )
        if now >= current.expires_at:
            return VerificationRejected(
                VerificationRejectionCode.EXPIRED, current.version
            )
        if not current.accepts(token, now):
            return VerificationRejected(
                VerificationRejectionCode.INVALID_TOKEN, current.version
            )
        try:
            passing = await self._repository.apply(
                key,
                expected_version=expected_version,
                event=VerificationEvent.PASS_REQUESTED,
                now=now,
            )
        except StaleVerificationVersion, InvalidVerificationTransition:
            latest = await self._repository.load(key)
            return VerificationRejected(
                VerificationRejectionCode.STALE_VERSION,
                latest.version if latest is not None else None,
            )
        completed = await self.deliver_ready(key)
        return VerificationAccepted(passing, completed)

    async def member_left(
        self, key: VerificationKey
    ) -> VerificationAccepted | VerificationRejected:
        """@brief 线性化成员离群取消 / Linearize member-left cancellation.

        @param key 聚合键 / Aggregate key.
        @return 接受或竞争拒绝 / Accepted or race rejection.
        """

        current = await self._repository.load(key)
        if current is None:
            return VerificationRejected(VerificationRejectionCode.NOT_FOUND)
        if current.status is VerificationStatus.CREATING:
            event = VerificationEvent.ABORT_CREATION
        elif current.status is VerificationStatus.PENDING:
            event = VerificationEvent.MEMBER_LEFT
        else:
            return VerificationRejected(
                VerificationRejectionCode.NOT_PENDING, current.version
            )
        try:
            cancelling = await self._repository.apply(
                key,
                expected_version=current.version,
                event=event,
                now=self._clock.now(),
            )
        except StaleVerificationVersion, InvalidVerificationTransition:
            latest = await self._repository.load(key)
            return VerificationRejected(
                VerificationRejectionCode.STALE_VERSION,
                latest.version if latest is not None else None,
            )
        completed = await self.deliver_ready(key)
        return VerificationAccepted(cancelling, completed)

    async def deliver_ready(self, key: VerificationKey) -> bool:
        """@brief 尝试领取并同步处理指定过渡态 / Try to claim and synchronously process one transitional aggregate.

        @param key 聚合键 / Aggregate key.
        @return 本调用确认终态时为 True / True when this call acknowledged the terminal state.
        """

        claim = await self._repository.claim_one(
            key,
            now=self._clock.now(),
            lease_for=self._lease_for,
        )
        if claim is None:
            return False
        return await self.process_claim(claim)

    async def process_claim(self, claim: VerificationClaim) -> bool:
        """@brief 执行 at-least-once 副作用并 fencing 确认或重试 / Deliver at-least-once effects and fence completion or retry.

        @param claim 当前 claim / Current claim.
        @return 已确认终态时为 True / True when terminal state was acknowledged.
        @note Telegram 成功后到数据库确认前崩溃会重放副作用；不承诺 exactly-once。/
            A crash after Telegram success but before DB acknowledgement replays effects; exactly-once is not claimed.
        """

        remaining = (claim.lease_expires_at - self._clock.now()).total_seconds()
        if remaining <= 0:
            return False
        try:
            async with asyncio.timeout(remaining * 0.9):
                await self._delivery.deliver(claim.task)
                await self._repository.complete(claim, now=self._clock.now())
            return True
        except asyncio.CancelledError:
            raise
        except VerificationFencingError:
            return False
        except Exception as error:
            now = self._clock.now()
            delay = min(60.0, float(2 ** min(claim.attempt_count - 1, 6)))
            try:
                await self._repository.retry(
                    claim,
                    retry_at=now + timedelta(seconds=delay),
                    error=f"{type(error).__name__}: {error}",
                    now=now,
                )
            except VerificationFencingError:
                return False
            except Exception:
                logger.exception(
                    "Verification retry scheduling failed; lease recovery will replay it: "
                    "chat=%s user=%s",
                    claim.task.chat_id,
                    claim.task.user_id,
                )
                return False
            logger.warning(
                "Verification effect failed and was scheduled for retry: chat=%s user=%s error=%s",
                claim.task.chat_id,
                claim.task.user_id,
                error,
            )
            return False
