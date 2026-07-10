import logging
import time


class ProviderCircuit:
    """@brief AI provider 熔断器 / AI provider circuit breaker.

    @note 该类只负责记录 provider 的短期失败窗口和冷却时间，不关心具体
    AI provider 的调用方式 / This class tracks short-term provider failures and
    cooldowns only; it does not know how providers are invoked.
    """

    def __init__(
        self,
        *,
        failure_threshold: int,
        window_seconds: int,
        cooldown_seconds: int,
    ) -> None:
        """@brief 初始化熔断器 / Initialize the circuit breaker.

        @param failure_threshold 触发熔断的失败次数 / Failure count that opens the circuit.
        @param window_seconds 统计失败的时间窗口 / Rolling failure window in seconds.
        @param cooldown_seconds 熔断冷却时间 / Open-circuit cooldown in seconds.
        """
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.failure_streaks: dict[str, list[float]] = {}
        self.open_until: dict[str, float] = {}

    def is_open(self, service_name: str, now: float | None = None) -> bool:
        """@brief 判断 provider 是否仍处于熔断状态 / Check whether provider is open.

        @param service_name provider 名称 / Provider name.
        @param now 可注入的单调时间，便于测试 / Injectable monotonic time for tests.
        @return True 表示应跳过调用 / True means the provider should be skipped.
        """
        current_time = time.monotonic() if now is None else now
        open_until = self.open_until.get(service_name)
        if not open_until:
            return False
        if current_time < open_until:
            return True

        self.open_until.pop(service_name, None)
        self.failure_streaks.pop(service_name, None)
        return False

    def record_success(self, service_name: str) -> None:
        """@brief 记录成功并清理失败状态 / Record success and clear failure state.

        @param service_name provider 名称 / Provider name.
        """
        self.failure_streaks.pop(service_name, None)
        self.open_until.pop(service_name, None)

    def record_failure(self, service_name: str, now: float | None = None) -> None:
        """@brief 记录失败并在达到阈值时熔断 / Record failure and open if threshold is met.

        @param service_name provider 名称 / Provider name.
        @param now 可注入的单调时间，便于测试 / Injectable monotonic time for tests.
        """
        current_time = time.monotonic() if now is None else now
        cutoff = current_time - self.window_seconds
        recent_failures = [
            failure_time
            for failure_time in self.failure_streaks.get(service_name, [])
            if failure_time >= cutoff
        ]
        recent_failures.append(current_time)
        self.failure_streaks[service_name] = recent_failures

        if len(recent_failures) < self.failure_threshold:
            return

        open_until = current_time + self.cooldown_seconds
        self.open_until[service_name] = open_until
        self.failure_streaks.pop(service_name, None)
        logging.warning(
            "%s 熔断 %s 秒：%s 秒内连续失败 %s 次",
            service_name,
            self.cooldown_seconds,
            self.window_seconds,
            self.failure_threshold,
        )
