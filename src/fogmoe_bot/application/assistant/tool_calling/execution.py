import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping

from pydantic import ValidationError

from .protocol import json_safe


@dataclass(frozen=True)
class ToolExecution:
    """@brief 单次工具执行结果 / Single tool execution result.

    @note logged_args 用于日志，function_args 用于真实 handler 调用 /
    logged_args is for logs; function_args is for the real handler call.
    """

    function_args: Dict[str, Any]
    logged_args: Any
    validation_error: Dict[str, Any] | None
    internal_result: Any


def format_validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    """@brief 格式化 Pydantic 校验错误 / Format Pydantic validation errors.

    @param exc Pydantic 校验异常 / Pydantic validation exception.
    @return 面向 LLM 的错误详情 / Error details for the LLM.
    """
    details: list[dict[str, str]] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(item) for item in error.get("loc", ())) or "__root__"
        details.append({
            "field": loc,
            "message": str(error.get("msg") or "Invalid value"),
            "type": str(error.get("type") or "validation_error"),
        })
    return details


def validate_tool_args(
    function_name: str,
    raw_args: Any,
    arg_models: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any] | None]:
    """@brief 校验工具参数 / Validate tool arguments.

    @param function_name 工具函数名 / Tool function name.
    @param raw_args 原始参数 / Raw arguments.
    @param arg_models 工具参数模型映射 / Tool argument model mapping.
    @return 已校验参数和可选错误 / Validated args and optional error.
    """
    model = arg_models.get(function_name)
    if model is None:
        if isinstance(raw_args, dict):
            return raw_args, None
        return {}, None

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
    """@brief 校验并执行一个工具调用 / Validate and execute one tool call.

    @param function_name 工具函数名 / Tool function name.
    @param raw_function_args JSON 解析后的原始参数 / JSON-decoded raw arguments.
    @param provider_name provider 名称，用于日志 / Provider name for logging.
    @param arg_models 工具参数模型映射 / Tool argument model mapping.
    @param handlers 工具 handler 映射 / Tool handler mapping.
    @return 工具执行结果对象 / Tool execution result.
    """
    function_args, validation_error = validate_tool_args(
        function_name,
        raw_function_args,
        arg_models,
    )
    logged_args = (
        function_args
        if validation_error is None
        else json_safe(raw_function_args)
    )

    if validation_error is not None:
        logging.warning(
            "%s 工具参数校验失败: %s, args=%s, error=%s",
            provider_name,
            function_name,
            json.dumps(json_safe(raw_function_args), ensure_ascii=False),
            validation_error.get("details"),
        )
        return ToolExecution(
            function_args=function_args,
            logged_args=logged_args,
            validation_error=validation_error,
            internal_result=validation_error,
        )

    handler = handlers.get(function_name)
    if handler is None:
        logging.warning("%s 未知工具: %s", provider_name, function_name)
        return ToolExecution(
            function_args=function_args,
            logged_args=logged_args,
            validation_error=None,
            internal_result={"error": f"未知工具: {function_name}"},
        )

    try:
        internal_result = handler(**function_args)
    except TypeError as exc:
        logging.error("%s 工具参数错误: %s, %s", provider_name, function_name, exc)
        internal_result = {"error": f"参数错误: {str(exc)}"}
    except Exception as exc:
        logging.exception("%s 工具执行失败: %s, %s", provider_name, function_name, exc)
        internal_result = {"error": f"执行失败: {str(exc)}"}
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

    return ToolExecution(
        function_args=function_args,
        logged_args=logged_args,
        validation_error=None,
        internal_result=internal_result,
    )
