"""@brief Assistant 当前时间工具操作 / Assistant current-time tool operation."""

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.timekeeping.service import TimeService
from fogmoe_bot.domain.conversation.payloads import JsonValue

from .parsing import optional_text

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
"""@brief ISO 顺序的英文星期名 / English weekday names in ISO order."""


def get_current_time(
    request: ToolEffectRequest,
    *,
    time: TimeService,
) -> JsonValue:
    """@brief 返回一次原子 clock read 派生的日期、时间和星期 / Return date, time, and weekday derived from one atomic clock read.

    @param request 已验证工具请求 / Validated tool request.
    @param time 统一时间应用服务 / Unified time application service.
    @return Provider-neutral JSON 时间读数 / Provider-neutral JSON time reading.
    """

    reading = time.now(optional_text(request.arguments, "timezone"))
    local = reading.local_datetime
    offset = local.utcoffset()
    if offset is None:
        raise RuntimeError("Aware local time unexpectedly has no UTC offset")
    offset_seconds = int(offset.total_seconds())
    offset_sign = "+" if offset_seconds >= 0 else "-"
    offset_minutes = abs(offset_seconds) // 60
    offset_text = f"{offset_sign}{offset_minutes // 60:02d}:{offset_minutes % 60:02d}"
    weekday = reading.iso_weekday
    return {
        "instant_utc": reading.instant_utc.isoformat().replace("+00:00", "Z"),
        "timezone": reading.time_zone.value,
        "local_datetime": local.isoformat(),
        "date": local.date().isoformat(),
        "time": local.time().isoformat(timespec="seconds"),
        "weekday": {
            "iso_number": weekday,
            "name": _WEEKDAY_NAMES[weekday - 1],
        },
        "utc_offset": offset_text,
    }


__all__ = ["get_current_time"]
