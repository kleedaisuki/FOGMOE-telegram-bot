"""@brief Transactional outbox worker 测试 / Transactional-outbox worker tests."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from observability_testkit import make_telemetry

from fogmoe_bot.application.conversation.outbox_worker import (
    DeliveryErrorCategory,
    DeliveryReceipt,
    FullJitterDeliveryRetryPolicy,
    OutboxWorker,
    PermanentDeliveryError,
    RetryableDeliveryError,
)
from fogmoe_bot.application.runtime import AdaptivePollingPolicy
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    LeaseToken,
    MessageSequence,
    OutboundMessageId,
    TurnId,
)
from fogmoe_bot.domain.conversation.outbox import (
    SEND_TELEGRAM_MESSAGE,
    OutboundClaim,
    OutboundDeadLettered,
    OutboundDeliverySucceeded,
    OutboundDraft,
    OutboundMessage,
    OutboundRetryScheduled,
    OutboundStatus,
)

NOW = datetime(2026, 7, 11, 10, tzinfo=timezone.utc)
"""@brief 测试基准时间 / Test reference time."""


def _claim(
    *,
    attempt_count: int = 1,
    sequence: int = 1,
) -> OutboundClaim:
    """@brief 构造 processing outbox claim / Build a processing outbox claim.

    @param attempt_count 已领取次数 / Recorded claim count.
    @param sequence 投递流序号 / Delivery-stream sequence.
    @return 测试 claim / Test claim.
    """

    draft = OutboundDraft(
        message_id=OutboundMessageId.new(),
        conversation_id=ConversationId(f"assistant-user:{sequence}"),
        turn_id=TurnId.new(),
        delivery_stream_id=DeliveryStreamId(f"telegram:chat:{sequence}"),
        kind=SEND_TELEGRAM_MESSAGE,
        payload={"chat_id": sequence, "text": f"message {sequence}"},
        idempotency_key=f"answer:{sequence}",
        created_at=NOW,
    )
    message = OutboundMessage.restore(
        draft=draft,
        stream_sequence=MessageSequence(sequence),
        status=OutboundStatus.PROCESSING,
        version=attempt_count * 2 - 1,
        attempt_count=attempt_count,
        next_attempt_at=None,
        updated_at=NOW + timedelta(seconds=1),
        delivered_at=None,
        external_message_id=None,
        last_error=None,
    )
    return OutboundClaim.from_processing(
        message,
        token=LeaseToken.new(),
        lease_expires_at=NOW + timedelta(minutes=1),
    )


class _Clock:
    """@brief 固定 UTC 时钟 / Fixed UTC clock."""

    def now(self) -> datetime:
        """@brief 返回固定时间 / Return the fixed time.

        @return 测试时间 / Test time.
        """

        return NOW + timedelta(seconds=2)


class _Repository:
    """@brief 记录 outbox 状态调用的 repository 替身 / Repository double recording outbox state calls."""

    def __init__(
        self,
        claims: tuple[OutboundClaim, ...] = (),
        *,
        recovery_failures: int = 0,
    ) -> None:
        """@brief 创建 repository 替身 / Create the repository double.

        @param claims 首轮可领取 claims / Claims available in the first poll.
        """

        self.claims = list(claims)
        self.claim_limits: list[int] = []
        self.delivered: list[OutboundDeliverySucceeded] = []
        self.retried: list[OutboundRetryScheduled] = []
        self.failed: list[OutboundDeadLettered] = []
        self.recover_calls = 0
        self.recovery_failures = recovery_failures

    async def claim_outbound(
        self,
        *,
        now: datetime,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[OutboundClaim, ...]:
        """@brief 按容量领取 claims / Claim up to capacity.

        @param now 当前时间 / Current time.
        @param limit 领取上限 / Claim limit.
        @param lease_for 租约时长 / Lease duration.
        @return 领取结果 / Claimed values.
        """

        del now, lease_for
        self.claim_limits.append(limit)
        claimed = tuple(self.claims[:limit])
        del self.claims[:limit]
        return claimed

    async def complete_outbound(
        self,
        decision: OutboundDeliverySucceeded,
    ) -> None:
        """@brief 记录成功确认 / Record successful acknowledgement.

        @param decision 成功 settlement / Successful settlement.
        @return None / None.
        """

        self.delivered.append(decision)

    async def schedule_outbound_retry(
        self,
        decision: OutboundRetryScheduled,
    ) -> None:
        """@brief 记录重试 / Record retry.

        @param decision 重试 settlement / Retry settlement.
        @return None / None.
        """

        self.retried.append(decision)

    async def dead_letter_outbound(
        self,
        decision: OutboundDeadLettered,
    ) -> None:
        """@brief 记录永久失败 / Record final failure.

        @param decision dead-letter settlement / Dead-letter settlement.
        @return None / None.
        """

        self.failed.append(decision)

    async def recover_expired_outbound_leases(self, *, now: datetime) -> int:
        """@brief 记录租约回收 / Record lease recovery.

        @param now 当前时间 / Current time.
        @return 0 / Zero.
        """

        del now
        self.recover_calls += 1
        if self.recovery_failures:
            self.recovery_failures -= 1
            raise OSError("temporary outbox lease-recovery failure")
        return 0


class _Delivery:
    """@brief 返回固定回执或异常的投递替身 / Delivery double returning a receipt or exception."""

    def __init__(self, result: DeliveryReceipt | Exception) -> None:
        """@brief 创建投递替身 / Create the delivery double.

        @param result 固定回执或异常 / Fixed receipt or exception.
        """

        self.result = result
        self.started: list[OutboundMessageId] = []
        self.release: asyncio.Event | None = None

    async def deliver(self, message: OutboundMessage) -> DeliveryReceipt:
        """@brief 返回回执或抛出异常 / Return a receipt or raise an exception.

        @param message 当前消息 / Current message.
        @return 固定回执 / Fixed receipt.
        """

        self.started.append(message.draft.message_id)
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _worker(
    repository: _Repository,
    delivery: _Delivery,
    *,
    worker_count: int = 1,
    lease_for: timedelta = timedelta(minutes=1),
    attempt_timeout: timedelta = timedelta(seconds=30),
    polling_policy: AdaptivePollingPolicy | None = None,
) -> OutboxWorker:
    """@brief 构造确定性 outbox worker / Build a deterministic outbox worker.

    @param repository repository 替身 / Repository double.
    @param delivery 投递替身 / Delivery double.
    @param worker_count worker 数 / Worker count.
    @param lease_for claim lease / Claim lease.
    @param attempt_timeout 外部投递尝试上限 / External-delivery attempt limit.
    @param polling_policy 可选轮询策略 / Optional polling policy.
    @return outbox worker / Outbox worker.
    """

    return OutboxWorker(
        repository=repository,
        delivery=delivery,
        worker_count=worker_count,
        polling_policy=polling_policy
        or AdaptivePollingPolicy(0.01, 0.02, jitter_ratio=0.0),
        lease_for=lease_for,
        attempt_timeout=attempt_timeout,
        retry_policy=FullJitterDeliveryRetryPolicy(
            initial_delay=timedelta(seconds=4),
            max_delay=timedelta(seconds=30),
            retry_after_jitter=timedelta(seconds=1),
            jitter=lambda lower, upper: lower,
        ),
        clock=_Clock(),
        telemetry=make_telemetry(),
    )


def test_success_marks_claim_delivered_with_external_id() -> None:
    """@brief 成功投递用 fencing claim 写入外部 ID / Success records the external ID with the fenced claim."""

    async def scenario() -> None:
        """@brief 运行成功场景 / Run the success scenario."""

        repository = _Repository()
        worker = _worker(repository, _Delivery(DeliveryReceipt("telegram:42")))
        claim = _claim()

        await worker.process_claim(claim)

        assert len(repository.delivered) == 1
        assert repository.delivered[0].claim == claim
        assert repository.delivered[0].message.delivered_at == NOW + timedelta(
            seconds=2
        )
        assert repository.delivered[0].message.external_message_id == "telegram:42"
        assert repository.retried == []
        assert repository.failed == []

    asyncio.run(scenario())


def test_retry_after_is_honoured_before_jitter() -> None:
    """@brief Retry-After 是重试下界 / Retry-After is a lower bound for retry."""

    async def scenario() -> None:
        """@brief 运行 Retry-After 场景 / Run the Retry-After scenario."""

        repository = _Repository()
        error = RetryableDeliveryError(
            "flood control",
            category=DeliveryErrorCategory.RATE_LIMIT,
            retry_after=timedelta(seconds=17),
        )
        worker = _worker(repository, _Delivery(error))

        await worker.process_claim(_claim())

        retry = repository.retried[0]
        failed_at = retry.message.updated_at
        assert failed_at == NOW + timedelta(seconds=2)
        assert retry.message.next_attempt_at == failed_at + timedelta(seconds=17)
        assert retry.message.last_error is not None
        assert "retry_after_seconds=17" in retry.message.last_error

    asyncio.run(scenario())


def test_permanent_failure_is_not_retried() -> None:
    """@brief 永久错误直接进入 failed_final / Permanent error goes directly to failed_final."""

    async def scenario() -> None:
        """@brief 运行永久失败场景 / Run the permanent-failure scenario."""

        repository = _Repository()
        error = PermanentDeliveryError(
            "bot was blocked",
            category=DeliveryErrorCategory.PERMISSION,
        )
        claim = _claim()
        worker = _worker(repository, _Delivery(error))

        await worker.process_claim(claim)

        assert repository.retried == []
        assert repository.failed[0].claim == claim
        assert repository.failed[0].message.updated_at == NOW + timedelta(seconds=2)
        assert repository.failed[0].message.last_error is not None
        assert "category=permission" in repository.failed[0].message.last_error

    asyncio.run(scenario())


def test_run_never_claims_above_worker_capacity() -> None:
    """@brief 已领取未完成数量不超过 worker_count / Claimed unfinished work never exceeds worker_count."""

    async def scenario() -> None:
        """@brief 运行容量场景 / Run the capacity scenario."""

        claims = (_claim(sequence=1), _claim(sequence=2), _claim(sequence=3))
        repository = _Repository(claims)
        delivery = _Delivery(DeliveryReceipt("ok"))
        delivery.release = asyncio.Event()
        worker = _worker(repository, delivery, worker_count=2)
        stop_event = asyncio.Event()
        task = asyncio.create_task(worker.run(stop_event))

        while len(delivery.started) < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0.03)
        assert repository.claim_limits == [2]
        assert len(delivery.started) == 2
        assert repository.recover_calls == 1

        stop_event.set()
        delivery.release.set()
        await asyncio.wait_for(task, timeout=1)
        assert len(repository.delivered) == 2

    asyncio.run(scenario())


def test_lease_recovery_survives_failure_and_saturated_capacity() -> None:
    """@brief 恢复故障不阻断投递 claim，容量饱和也不暂停 cadence / Recovery failure does not block delivery claims, and saturated capacity does not pause the cadence."""

    async def scenario() -> None:
        """@brief 阻塞唯一投递 consumer 并等待第二次恢复 / Block the sole delivery consumer and await a second recovery pass."""

        repository = _Repository((_claim(),), recovery_failures=1)
        delivery = _Delivery(DeliveryReceipt("ok"))
        delivery.release = asyncio.Event()
        worker = _worker(
            repository,
            delivery,
            lease_for=timedelta(milliseconds=100),
            attempt_timeout=timedelta(milliseconds=90),
            polling_policy=AdaptivePollingPolicy(
                0.001,
                0.002,
                jitter_ratio=0.0,
            ),
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(worker.run(stop_event))

        async with asyncio.timeout(1):
            while not delivery.started or repository.recover_calls < 2:
                await asyncio.sleep(0.001)

        assert repository.claim_limits == [1]
        assert repository.recover_calls >= 2

        stop_event.set()
        delivery.release.set()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_cancellation_leaves_processing_claim_for_lease_recovery() -> None:
    """@brief 取消不伪造失败终态而留给租约回收 / Cancellation leaves processing state for lease recovery."""

    async def scenario() -> None:
        """@brief 运行取消场景 / Run the cancellation scenario."""

        repository = _Repository()
        delivery = _Delivery(DeliveryReceipt("never"))
        delivery.release = asyncio.Event()
        worker = _worker(repository, delivery)
        claim = _claim()
        task = asyncio.create_task(worker.process_claim(claim))

        while not delivery.started:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert repository.delivered == []
        assert repository.retried == []
        assert repository.failed == []

    asyncio.run(scenario())


def test_attempt_timeout_is_persisted_as_ambiguous_retry() -> None:
    """@brief 本地超时明确标记结果未知 / Local timeout explicitly marks an unknown outcome."""

    async def scenario() -> None:
        """@brief 运行模糊超时场景 / Run the ambiguous-timeout scenario."""

        repository = _Repository()
        delivery = _Delivery(DeliveryReceipt("late"))
        delivery.release = asyncio.Event()
        worker = OutboxWorker(
            repository=repository,
            delivery=delivery,
            worker_count=1,
            polling_policy=AdaptivePollingPolicy(0.01, 0.02, jitter_ratio=0.0),
            lease_for=timedelta(seconds=1),
            attempt_timeout=timedelta(milliseconds=1),
            retry_policy=FullJitterDeliveryRetryPolicy(
                jitter=lambda lower, upper: lower,
            ),
            clock=_Clock(),
            telemetry=make_telemetry(),
        )

        await worker.process_claim(_claim())

        retry = repository.retried[0].message
        assert retry.last_error is not None
        assert "category=ambiguous_timeout" in retry.last_error
        assert "outcome_ambiguous=true" in retry.last_error
        assert retry.next_attempt_at is not None
        assert retry.next_attempt_at > retry.updated_at

    asyncio.run(scenario())
