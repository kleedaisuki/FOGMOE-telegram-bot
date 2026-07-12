"""@brief 持久化用户举报用例 / Persisted user-reporting use case."""

from __future__ import annotations

from datetime import timedelta

from fogmoe_bot.application.runtime import UtcClock
from fogmoe_bot.domain.moderation.reporting import (
    ReportOutcome,
    ReportRegistration,
    ReportRequest,
)

from .ports import ReportDelivery, ReportRepository


class ReportingService:
    """@brief 先幂等登记再通知管理员 / Idempotently register before notifying administrators.

    @param repository 举报仓储 / Report repository.
    @param delivery 管理员通知端口 / Administrator-notification port.
    @param clock UTC 时钟 / UTC clock.
    """

    def __init__(
        self,
        repository: ReportRepository,
        delivery: ReportDelivery,
        clock: UtcClock,
        *,
        deduplication_window: timedelta = timedelta(hours=1),
    ) -> None:
        """@brief 注入举报依赖 / Inject reporting dependencies.

        @param repository 举报仓储 / Report repository.
        @param delivery 通知端口 / Notification port.
        @param clock UTC 时钟 / UTC clock.
        @param deduplication_window 同一用户重复举报窗口 / Same-user duplicate-report window.
        @return None / None.
        @raises ValueError 去重窗口非正 / If the deduplication window is not positive.
        """

        if deduplication_window <= timedelta(0):
            raise ValueError("deduplication_window must be positive")
        self._repository = repository
        self._delivery = delivery
        self._clock = clock
        self._deduplication_window = deduplication_window

    async def report(self, request: ReportRequest) -> ReportOutcome:
        """@brief 登记并投递举报 / Register and deliver a report.

        @param request 类型化举报请求 / Typed report request.
        @return 登记与投递结果 / Registration and delivery outcome.
        """

        registration = await self._repository.register_report(
            request.key,
            now=self._clock.now(),
            deduplication_window=self._deduplication_window,
        )
        if registration is ReportRegistration.DUPLICATE:
            return ReportOutcome(registration=registration)
        return ReportOutcome(
            registration=registration,
            delivery=await self._delivery.deliver(request),
        )


__all__ = ["ReportingService"]
