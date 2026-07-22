"""@brief Assistant 当前时间工具测试 / Tests for the Assistant current-time tool."""

from datetime import UTC, datetime

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.assistant.tools.catalog import (
    DEFAULT_TOOL_CATALOG,
    ToolResultResidency,
    ValidatedToolInvocation,
)
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.domain.conversation.identity import (
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.temporal import TimeZoneId
from fogmoe_bot.infrastructure.assistant.tool_operations.time import get_current_time


class _Clock:
    """@brief 记录读取次数的固定 UTC clock / Fixed UTC clock recording read count."""

    def __init__(self) -> None:
        """@brief 初始化固定瞬间 / Initialize the fixed instant."""

        self.calls = 0

    def now(self) -> datetime:
        """@brief 返回跨上海午夜后的瞬间 / Return an instant just after midnight in Shanghai.

        @return 固定 UTC aware 瞬间 / Fixed UTC-aware instant.
        """

        self.calls += 1
        return datetime(2026, 7, 22, 16, 30, 45, tzinfo=UTC)


def _request(arguments: dict[str, object]) -> ToolEffectRequest:
    """@brief 构造当前时间工具请求 / Build a current-time tool request.

    @param arguments 已校验参数 / Validated arguments.
    @return 工具效果请求 / Tool-effect request.
    """

    return ToolEffectRequest(
        context=ToolExecutionContext(
            turn_id=TurnId.new(),
            conversation_id=ConversationId("assistant-user:7"),
            delivery_stream_id=DeliveryStreamId("telegram:user:7"),
            user_id=7,
            chat_id=7,
            is_group=False,
            group_id=None,
            message_id=1,
        ),
        invocation_id="step:0:call:0",
        provider_call_id="provider-time-call",
        tool_name="get_current_time",
        effect_kind="read.get_current_time",
        mutating=False,
        arguments=arguments,
        request_hash="c" * 64,
    )


def test_current_time_is_derived_from_one_clock_sample() -> None:
    """@brief 日期、时间和星期来自同一次 clock read / Date, time, and weekday come from one clock read."""

    clock = _Clock()
    service = TimeService(
        default_time_zone=TimeZoneId("Asia/Shanghai"),
        clock=clock,
    )

    result = get_current_time(_request({}), time=service)

    assert clock.calls == 1
    assert result == {
        "instant_utc": "2026-07-22T16:30:45Z",
        "timezone": "Asia/Shanghai",
        "local_datetime": "2026-07-23T00:30:45+08:00",
        "date": "2026-07-23",
        "time": "00:30:45",
        "weekday": {"iso_number": 4, "name": "Thursday"},
        "utc_offset": "+08:00",
    }


def test_current_time_accepts_an_explicit_iana_zone() -> None:
    """@brief 显式时区覆盖部署默认值 / An explicit zone overrides the deployment default."""

    result = get_current_time(
        _request({"timezone": "America/St_Johns"}),
        time=TimeService(default_time_zone=TimeZoneId("Asia/Shanghai"), clock=_Clock()),
    )

    assert result["timezone"] == "America/St_Johns"
    assert result["utc_offset"] == "-02:30"


def test_time_tool_is_turn_local_but_replayable_per_invocation() -> None:
    """@brief 时间结果不污染未来对话但可恢复同一 invocation / Time stays turn-local while remaining replayable for the invocation."""

    validated = DEFAULT_TOOL_CATALOG.validate("get_current_time", {})

    assert isinstance(validated, ValidatedToolInvocation)
    assert validated.result_residency is ToolResultResidency.AGENT_TURN
    assert validated.result_cacheable is True
