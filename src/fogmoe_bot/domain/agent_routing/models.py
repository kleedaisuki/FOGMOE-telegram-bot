"""@brief Agent 路由值对象 / Agent routing value objects."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderRoute:
    """@brief 一个可尝试的模型 provider 路由 / One model-provider route to attempt.

    @param service_name 配置中使用的服务名称 / Service name used in configuration.
    @param provider_name 传给 LiteLLM 的 provider 名称 / Provider name passed to LiteLLM.
    @param display_name 面向日志的显示名称 / Display name used in logs.
    @param models 按优先级排列的模型列表 / Models ordered by priority.
    @param completion_kwargs 模型调用补充参数 / Extra model-call parameters.
    @param skip_tools 当前 provider 不支持的 Runtime 能力 / Runtime capabilities unsupported by this provider.
    @param safety_block_on_error 是否将 safety 错误转换为语义错误 / Whether safety errors become semantic errors.
    """

    service_name: str
    provider_name: str
    display_name: str
    models: tuple[str | None, ...]
    completion_kwargs: dict[str, Any]
    skip_tools: tuple[str, ...] = ()
    safety_block_on_error: bool = False
