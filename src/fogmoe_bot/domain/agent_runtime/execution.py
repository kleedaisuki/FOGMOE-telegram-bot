"""@brief Runtime 工具执行原语 / Runtime tool execution primitives."""

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

from pydantic import ValidationError

from .protocol import json_safe


@dataclass(frozen=True)
class ToolExecution:
    """@brief 单次工具执行结果 / Single tool execution result.

    @note logged_args 仅用于审计日志；function_args 是传给 handler 的已校验参数 /
    logged_args is for audit logs only; function_args is validated handler input.
    """

    function_args: Dict[str, Any]
    logged_args: Any
    validation_error: Dict[str, Any] | None
    internal_result: Any


def format_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    """@brief 格式化 Pydantic 校验错误 / Format Pydantic validation errors.

    @param exc Pydantic 校验异常 / Pydantic validation exception.
    @return 面向 Agent 的错误详情 / Error details for the Agent.
    """
    details: list[dict[str, str]] = []
    for error in exc.errors(include_url=False):
        location = ".".join(str(item) for item in error.get("loc", ())) or "__root__"
        details.append(
            {
                "field": location,
                "message": str(error.get("msg") or "Invalid value"),
                "type": str(error.get("type") or "validation_error"),
            }
        )
    return details


def validate_tool_args(
    function_name: str,
    raw_args: Any,
    arg_models: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
    """@brief 校验能力参数 / Validate capability arguments.

    @param function_name 能力名称 / Capability name.
    @param raw_args Agent 提交的原始参数 / Raw arguments submitted by the Agent.
    @param arg_models 参数模型映射 / Argument model mapping.
    @return 已校验参数与可选错误 / Validated args and optional error.
    """
    model = arg_models.get(function_name)
    if model is None:
        return (raw_args, None) if isinstance(raw_args, dict) else ({}, None)

    try:
        validated = model.model_validate(raw_args)
    except ValidationError as exc:
        return {}, {
            "error": "Tool arguments failed validation",
            "details": format_validation_errors(exc),
        }

    return validated.model_dump(
        mode="json",
        exclude_none=True,
        exclude_unset=True,
    ), None


def execute_tool_call(
    *,
    function_name: str,
    raw_function_args: Any,
    provider_name: str,
    arg_models: Mapping[str, Any],
    handlers: Mapping[str, Callable[..., Any]],
) -> ToolExecution:
    """@brief 校验并执行一个能力 / Validate and execute one capability.

    @param function_name 能力名称 / Capability name.
    @param raw_function_args JSON 解析后的原始参数 / JSON-decoded raw arguments.
    @param provider_name Agent 标识，用于日志 / Agent identifier for logging.
    @param arg_models 参数模型映射 / Argument model mapping.
    @param handlers 能力执行器映射 / Capability executor mapping.
    @return 执行结果 / Execution result.
    """
    function_args, validation_error = validate_tool_args(
        function_name,
        raw_function_args,
        arg_models,
    )
    logged_args = function_args if validation_error is None else json_safe(raw_function_args)

    if validation_error is not None:
        logging.warning(
            "%s 工具参数校验失败: %s, args=%s, error=%s",
            provider_name,
            function_name,
            json.dumps(json_safe(raw_function_args), ensure_ascii=False),
            validation_error.get("details"),
        )
        return ToolExecution(function_args, logged_args, validation_error, validation_error)

    handler = handlers.get(function_name)
    if handler is None:
        logging.warning("%s 未知工具: %s", provider_name, function_name)
        return ToolExecution(
            function_args,
            logged_args,
            None,
            {"error": f"未知工具: {function_name}"},
        )

    try:
        internal_result = handler(**function_args)
    except TypeError as exc:
        logging.error("%s 工具参数错误: %s, %s", provider_name, function_name, exc)
        internal_result = {"error": f"参数错误: {exc}"}
    except Exception as exc:
        logging.exception("%s 工具执行失败: %s, %s", provider_name, function_name, exc)
        internal_result = {"error": f"执行失败: {exc}"}
    else:
        if isinstance(internal_result, dict) and internal_result.get("error"):
            logging.warning(
                "%s 工具返回错误: %s, args=%s, error=%s",
                provider_name,
                function_name,
                json.dumps(function_args, ensure_ascii=False),
                internal_result.get("error"),
            )
        else:
            logging.info(
                "%s 工具执行成功: %s, args=%s",
                provider_name,
                function_name,
                json.dumps(function_args, ensure_ascii=False),
            )

    return ToolExecution(function_args, logged_args, None, internal_result)
