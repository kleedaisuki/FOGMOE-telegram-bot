"""@brief 可重放治理副作用执行器 / Replayable moderation-effect executor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fogmoe_bot.application.runtime import UtcClock
from fogmoe_bot.domain.moderation.aggregate import StaleModerationVersion
from fogmoe_bot.domain.moderation.effects import (
    KeywordReplyPlan,
    ModerationEffect,
    ModerationEffectStatus,
    SpamEnforcementPlan,
)

from .ports import ModerationEffectRepository, ModerationEffectSink


class ModerationEffectDeliveryError(RuntimeError):
    """@brief 外部治理副作用未完成 / External moderation effect did not complete."""


@dataclass(frozen=True, slots=True)
class SpamEnforcementOutcome:
    """@brief 一次垃圾处置结果 / Result of one spam-enforcement attempt.

    @param message_deleted 消息是否已删除 / Whether the message was deleted.
    @param warning_sent 警告是否已发送 / Whether the warning was sent.
    @param error 可选错误摘要 / Optional error summary.
    """

    message_deleted: bool
    warning_sent: bool
    error: str | None = None


class ModerationEffectService:
    """@brief 持久化进度后执行 Telegram 外部效果 / Execute external Telegram effects around persisted progress.

    @param repository 效果仓储 / Effect repository.
    @param sink 外部副作用端口 / External effect sink.
    @param clock UTC 时钟 / UTC clock.
    @param warning_window 警告计数窗口 / Warning-count window.
    @note Telegram Bot API 不提供客户端幂等键，因此崩溃发生在远端成功与本地确认之间时，
    文本投递仍是 at-least-once；数据库意图、警告计数与阶段转移本身是幂等的。/
    Telegram Bot API has no client idempotency key, so text delivery remains at-least-once
    if a crash lands between remote success and local acknowledgement; the persisted intent,
    warning count, and stage transitions themselves are idempotent.
    """

    def __init__(
        self,
        repository: ModerationEffectRepository,
        sink: ModerationEffectSink,
        clock: UtcClock,
        *,
        warning_window: timedelta = timedelta(hours=1),
    ) -> None:
        """@brief 注入效果依赖 / Inject effect dependencies.

        @param repository 效果仓储 / Effect repository.
        @param sink 外部副作用端口 / External effect sink.
        @param clock UTC 时钟 / UTC clock.
        @param warning_window 警告窗口 / Warning window.
        @return None / None.
        @raises ValueError 警告窗口非正 / If the warning window is not positive.
        """

        if warning_window <= timedelta(0):
            raise ValueError("warning_window must be positive")
        self._repository = repository
        self._sink = sink
        self._clock = clock
        self._warning_window = warning_window

    async def enforce_spam(
        self,
        plan: SpamEnforcementPlan,
    ) -> SpamEnforcementOutcome:
        """@brief 删除垃圾消息并发送警告 / Delete spam and send a warning.

        @param plan 处置意图 / Enforcement intent.
        @return 类型化尝试结果 / Typed attempt result.
        @raises ModerationEffectDeliveryError 删除后警告失败，需要 inbox 重放 / If warning delivery fails after deletion and inbox replay is required.
        """

        effect = await self._repository.reserve_effect(
            plan,
            now=self._clock.now(),
            warning_window=self._warning_window,
        )
        if effect.status is ModerationEffectStatus.DELIVERED:
            return SpamEnforcementOutcome(True, True)

        if effect.status is not ModerationEffectStatus.MESSAGE_DELETED:
            try:
                await self._sink.delete_spam(plan)
            except Exception as error:
                await self._record_failure(effect, error)
                return SpamEnforcementOutcome(False, False, str(error))
            effect = await self._advance_deleted(effect)

        warning_count = effect.warning_count
        if warning_count is None:
            raise RuntimeError("Persisted spam effect lost its warning count")
        try:
            await self._sink.send_spam_warning(
                plan,
                warning_count=warning_count,
            )
        except Exception as error:
            await self._record_failure(effect, error)
            raise ModerationEffectDeliveryError(str(error)) from error
        await self._advance_delivered(effect)
        return SpamEnforcementOutcome(True, True)

    async def deliver_keyword(self, plan: KeywordReplyPlan) -> None:
        """@brief 幂等执行关键词回复意图 / Idempotently execute a keyword-reply intent.

        @param plan 回复意图 / Reply intent.
        @return None / None.
        @raises ModerationEffectDeliveryError 投递失败 / If delivery fails.
        """

        effect = await self._repository.reserve_effect(
            plan,
            now=self._clock.now(),
            warning_window=self._warning_window,
        )
        if effect.status is ModerationEffectStatus.DELIVERED:
            return
        try:
            await self._sink.send_keyword_reply(plan)
        except Exception as error:
            await self._record_failure(effect, error)
            raise ModerationEffectDeliveryError(str(error)) from error
        await self._advance_delivered(effect)

    async def _advance_deleted(self, effect: ModerationEffect) -> ModerationEffect:
        """@brief 持久化删除阶段 / Persist the deletion stage.

        @param effect 当前效果 / Current effect.
        @return 更新效果 / Updated effect.
        """

        updated = effect.deleted(now=self._clock.now())
        await self._repository.save_effect(
            updated,
            expected_version=effect.version,
        )
        return updated

    async def _advance_delivered(self, effect: ModerationEffect) -> None:
        """@brief 持久化完成阶段 / Persist the delivered stage.

        @param effect 当前效果 / Current effect.
        @return None / None.
        """

        updated = effect.delivered(now=self._clock.now())
        if updated is effect:
            return
        await self._repository.save_effect(
            updated,
            expected_version=effect.version,
        )

    async def _record_failure(
        self,
        effect: ModerationEffect,
        error: Exception,
    ) -> None:
        """@brief 尽力持久化失败 / Best-effort persist a failure.

        @param effect 当前效果 / Current effect.
        @param error 外部错误 / External error.
        @return None / None.
        @note OCC 冲突表示另一个重放者已推进状态，不覆盖其结果 / An OCC conflict means another replayer advanced state, so its result is not overwritten.
        """

        failed = effect.failed(str(error), now=self._clock.now())
        try:
            await self._repository.save_effect(
                failed,
                expected_version=effect.version,
            )
        except StaleModerationVersion:
            return


__all__ = [
    "ModerationEffectDeliveryError",
    "ModerationEffectService",
    "SpamEnforcementOutcome",
]
