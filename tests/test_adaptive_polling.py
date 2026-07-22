"""@brief 自适应空闲轮询策略测试 / Tests for adaptive idle polling."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from fogmoe_bot.application.runtime import (
    AdaptivePollingPolicy,
    LeaseRecoveryCadence,
)


def test_idle_waits_double_to_the_cap_and_work_resets_to_base() -> None:
    """@brief 连续空轮截断指数退避，工作重置到 base / Consecutive idle polls back off to the cap and work resets to base."""

    async def scenario() -> None:
        """@brief 执行确定性等待序列 / Run the deterministic wait sequence.

        @return None / None.
        """

        polling = AdaptivePollingPolicy(
            0.001,
            0.004,
            jitter_ratio=0.0,
        ).start()
        stop = asyncio.Event()

        assert polling.next_interval_seconds == 0.001
        await polling.wait(stop)
        assert polling.next_interval_seconds == 0.002
        await polling.wait(stop)
        assert polling.next_interval_seconds == 0.004
        await polling.wait(stop)
        assert polling.next_interval_seconds == 0.004

        polling.reset()
        assert polling.next_interval_seconds == 0.001

    asyncio.run(scenario())


def test_first_idle_wait_is_exact_and_later_waits_use_bounded_jitter() -> None:
    """@brief 首次空轮保持 base，后续轻量 jitter 不突破 nominal cap / First idle wait keeps the base and later jitter never exceeds the nominal cap."""

    async def scenario() -> None:
        """@brief 捕获 jitter 请求边界 / Capture the requested jitter bounds.

        @return None / None.
        """

        samples: list[tuple[float, float]] = []

        def lower_bound(lower: float, upper: float) -> float:
            """@brief 记录并返回下界 / Record and return the lower bound.

            @param lower 请求下界 / Requested lower bound.
            @param upper 请求上界 / Requested upper bound.
            @return 下界 / Lower bound.
            """

            samples.append((lower, upper))
            return lower

        polling = AdaptivePollingPolicy(0.001, 0.004).start(jitter=lower_bound)
        stop = asyncio.Event()

        await polling.wait(stop)
        assert samples == []
        await polling.wait(stop)
        assert samples == [(pytest.approx(0.0018), pytest.approx(0.002))]
        assert polling.next_interval_seconds == 0.004

    asyncio.run(scenario())


def test_set_stop_event_interrupts_even_a_large_idle_interval() -> None:
    """@brief stop event 立即打断长退避 / A stop event immediately interrupts a long backoff."""

    async def scenario() -> None:
        """@brief 用已置位 event 验证无 sleep / Verify no sleep with an already-set event.

        @return None / None.
        """

        stop = asyncio.Event()
        stop.set()
        polling = AdaptivePollingPolicy(30.0, 60.0).start()
        await asyncio.wait_for(polling.wait(stop), timeout=0.05)

    asyncio.run(scenario())


def test_stop_event_interrupts_an_in_progress_idle_wait() -> None:
    """@brief 等待过程中置位 stop 也会立即中断 / Setting stop during a wait interrupts it immediately."""

    async def scenario() -> None:
        """@brief 启动长等待后再发送停止信号 / Start a long wait before sending the stop signal.

        @return None / None.
        """

        stop = asyncio.Event()
        polling = AdaptivePollingPolicy(30.0, 60.0).start()
        waiting = asyncio.create_task(polling.wait(stop))
        await asyncio.sleep(0)
        assert not waiting.done()

        stop.set()
        await asyncio.wait_for(waiting, timeout=0.05)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("base", "maximum", "message"),
    (
        (0.0, 1.0, "base_interval_seconds"),
        (1.0, float("inf"), "max_interval_seconds"),
        (2.0, 1.0, "max_interval_seconds must be"),
    ),
)
def test_policy_rejects_invalid_bounds(
    base: float,
    maximum: float,
    message: str,
) -> None:
    """@brief 策略拒绝非正、非有限和反向区间 / Policy rejects non-positive, non-finite, and reversed bounds.

    @param base 基础间隔 / Base interval.
    @param maximum 最大间隔 / Maximum interval.
    @param message 预期错误片段 / Expected error fragment.
    @return None / None.
    """

    with pytest.raises(ValueError, match=message):
        AdaptivePollingPolicy(base, maximum)


def test_lease_recovery_is_due_at_start_then_at_half_lease_or_five_seconds() -> None:
    """@brief lease recovery 启动即运行，之后按半 lease 与五秒较小值 / Lease recovery runs at startup then at the smaller of half a lease and five seconds."""

    now = [10.0]

    def monotonic() -> float:
        """@brief 返回可推进单调时间 / Return an advanceable monotonic instant.

        @return 当前测试秒数 / Current test seconds.
        """

        return now[0]

    cadence = LeaseRecoveryCadence.for_lease(
        timedelta(seconds=60),
        monotonic=monotonic,
    )
    assert cadence.interval_seconds == 5.0
    assert cadence.take_due()
    assert not cadence.take_due()

    now[0] = 14.999
    assert not cadence.take_due()
    now[0] = 15.0
    assert cadence.take_due()

    short = LeaseRecoveryCadence.for_lease(timedelta(seconds=4))
    assert short.interval_seconds == 2.0
