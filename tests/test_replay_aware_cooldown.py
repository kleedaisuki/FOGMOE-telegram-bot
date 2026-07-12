"""@brief 重放感知冷却门测试 / Tests for the replay-aware cooldown gate."""

from __future__ import annotations

import pytest

from fogmoe_bot.application.runtime import ReplayAwareCooldownGate


class ManualMonotonic:
    """@brief 可控 monotonic clock / Controllable monotonic clock."""

    def __init__(self) -> None:
        """@brief 从零初始化 / Initialize at zero."""

        self.value = 0.0
        """@brief 当前 monotonic 秒 / Current monotonic seconds."""

    def __call__(self) -> float:
        """@brief 返回当前秒数 / Return current seconds.

        @return monotonic 秒 / Monotonic seconds.
        """

        return self.value


def _gate(
    clock: ManualMonotonic, *, max_entries: int = 8
) -> ReplayAwareCooldownGate[str]:
    """@brief 构造一秒冷却门 / Build a one-second cooldown gate.

    @param clock 可控时钟 / Controllable clock.
    @param max_entries 最大决定数 / Maximum decision count.
    @return 测试 gate / Test gate.
    """

    return ReplayAwareCooldownGate(
        cooldown_seconds=1.0,
        max_entries=max_entries,
        retention_seconds=10.0,
        monotonic=clock,
    )


def test_replays_admitted_and_rejected_requests_stably() -> None:
    """@brief 新旧 Update 交错时仍重放各自首次决定 / Interleaved old and new Updates replay their first decisions."""

    clock = ManualMonotonic()
    gate = _gate(clock)

    assert gate.try_acquire("user:command", 10)
    clock.value = 0.2
    assert not gate.try_acquire("user:command", 11)
    clock.value = 2.0
    assert gate.try_acquire("user:command", 12)

    assert gate.try_acquire("user:command", 10)
    assert not gate.try_acquire("user:command", 11)
    assert gate.try_acquire("user:command", 12)


def test_keys_have_independent_windows() -> None:
    """@brief 不同用户或命令互不阻塞 / Different users or commands do not block one another."""

    clock = ManualMonotonic()
    gate = _gate(clock)

    assert gate.try_acquire("user-a:one", 1)
    assert gate.try_acquire("user-a:two", 2)
    assert gate.try_acquire("user-b:one", 3)


def test_capacity_eviction_and_retention_only_relax_policy() -> None:
    """@brief 容量与 TTL 淘汰后请求可重新求值 / Capacity and TTL eviction allow a request to be evaluated anew."""

    clock = ManualMonotonic()
    gate = _gate(clock, max_entries=2)
    assert gate.try_acquire("one", 1)
    clock.value = 0.1
    assert gate.try_acquire("two", 2)
    clock.value = 0.2
    assert gate.try_acquire("three", 3)

    assert gate.try_acquire("one", 1)
    clock.value = 20.0
    assert gate.try_acquire("two", 2)


@pytest.mark.parametrize("request_id", [-1, True, 1.5])
def test_rejects_invalid_request_identity(request_id: object) -> None:
    """@brief request identity 必须是非负整数 / Request identity must be a non-negative integer.

    @param request_id 非法 identity / Invalid identity.
    """

    clock = ManualMonotonic()
    gate = _gate(clock)
    with pytest.raises(ValueError, match="non-negative integer"):
        gate.try_acquire("key", request_id)  # type: ignore[arg-type]
