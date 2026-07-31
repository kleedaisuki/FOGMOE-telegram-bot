"""@brief Assistant 到隔离 Workspace 的工具映射 / Assistant-to-isolated-Workspace tool mapping.

本模块是 ``ToolEffectRequest`` 与应用层 ``RuntimeProcess`` 之间唯一的翻译边界。
它不解析或过滤 Bash 程序：命令语言能力属于受限 runtime，而不是 Bot 进程。/
This module is the sole translation boundary between ``ToolEffectRequest`` and the application
``RuntimeProcess``.  It does not parse or filter Bash programs: command-language capability
belongs to the constrained runtime, not to the Bot process.
"""

from __future__ import annotations

import asyncio
import math

from fogmoe_bot.application.assistant.tool_runtime import (
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.workspace.errors import (
    WorkspaceInvocationOutcomeUnknownError,
)
from fogmoe_bot.application.workspace.models import (
    MAX_BASH_OUTPUT_LIMIT_BYTES,
    RunBashCommand,
)
from fogmoe_bot.application.workspace.ports import RuntimeProcess
from fogmoe_bot.domain.conversation.payloads import JsonValue
from fogmoe_bot.domain.workspace.path import WorkspaceRelativePath
from fogmoe_bot.domain.workspace.runtime import (
    WorkspaceRequestHash,
    WorkspaceRequestId,
)
from fogmoe_bot.domain.workspace.scope import (
    GroupRuntimeScope,
    PersonalRuntimeScope,
    RuntimeScope,
)


_WORKSPACE_COMPLETION_RESERVE_SECONDS = 15.0
"""@brief native command 前为 journal、cgroup 清理与 receipt finalize 保留的最小 attempt 余量 /
Minimum attempt headroom reserved before a native command for journal, cgroup cleanup, and receipt finalization.

该值不是用户可控的 command timeout，且不参与 request hash；它是 Bot 与 native control
plane 之间的 fail-closed admission policy。/ This is not a user-controlled command timeout and
does not participate in the request hash; it is fail-closed admission policy between the Bot and
the native control plane.
"""


async def execute_run_bash(
    request: ToolEffectRequest,
    *,
    runtime_process: RuntimeProcess,
    output_limit_bytes: int,
) -> JsonValue:
    """@brief 在当前已认证 scope 的 Workspace 中执行 Bash / Execute Bash in the current authenticated scope's Workspace.

    @param request 已经由 ToolCatalog 验证且由 receipt claim 保护的调用 /
        Invocation already validated by ToolCatalog and protected by a receipt claim.
    @param runtime_process fail-closed 的 RuntimeProcess 应用端口 / Fail-closed RuntimeProcess application port.
    @param output_limit_bytes 组合根提供的 stdout/stderr 合并预算 /
        Combined stdout/stderr budget supplied by the composition root.
    @return 可写入 durable receipt 的规范 JSON 结果 / Canonical JSON result suitable for a durable receipt.
    @raise ValueError request 不是合法 ``workspace.exec`` 或参数破坏既定契约时抛出 /
        Raised when the request is not a valid ``workspace.exec`` or its arguments violate the contract.
    @note 非零退出码和 timeout 都是一次完成的 command 结果；只有 native transport/
        isolation 失败才应由 ``RuntimeProcess`` 抛出并让 receipt 重试。/ A nonzero exit
        code and timeout are completed command results; only native transport/isolation failures
        should be raised by ``RuntimeProcess`` and cause the receipt to retry.
    """

    _validate_workspace_effect(request)
    timeout_seconds = _bounded_int(
        request.arguments,
        "timeout_seconds",
        minimum=1,
        maximum=300,
        default=30,
    )
    deadline_result = _deadline_exhausted_result(
        request.context,
        requested_timeout_seconds=timeout_seconds,
    )
    if deadline_result is not None:
        return deadline_result
    command = RunBashCommand(
        scope=_workspace_scope(request.context),
        command=_required_string(request.arguments, "command"),
        request_id=WorkspaceRequestId(
            f"{request.context.turn_id}:{request.invocation_id}"
        ),
        request_hash=WorkspaceRequestHash(request.request_hash),
        stdin=_optional_string(request.arguments, "stdin"),
        working_directory=WorkspaceRelativePath(
            _string_or_default(request.arguments, "working_directory", ".")
        ),
        timeout_seconds=timeout_seconds,
        output_limit_bytes=_validate_output_limit(output_limit_bytes),
    )
    try:
        result = await runtime_process.run_bash(command)
    except WorkspaceInvocationOutcomeUnknownError as error:
        if error.request_id != command.request_id:
            raise ValueError(
                "Workspace journal returned an unexpected request ID"
            ) from error
        # This result is terminal for the current durable receipt. Releasing the claim would
        # retry the identical journal key and violate the native at-most-once boundary.
        outcome: dict[str, JsonValue] = {
            "status": "outcome_unknown",
            "replayed_by_runtime": False,
        }
        if error.diagnostic_code is not None:
            outcome["diagnostic_code"] = error.diagnostic_code
        if error.diagnostic_message is not None:
            outcome["diagnostic_message"] = error.diagnostic_message
        return outcome
    return {
        "status": (
            "timed_out"
            if result.timed_out
            else "succeeded"
            if result.succeeded
            else "exited"
        ),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "replayed_by_runtime": result.replayed,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _deadline_exhausted_result(
    context: ToolExecutionContext,
    *,
    requested_timeout_seconds: int,
) -> JsonValue | None:
    """@brief 在整体 attempt 余量不足时拒绝启动 native command / Refuse to start a native command when overall attempt headroom is insufficient.

    @param context 当前 Turn 的易失执行上下文 / Ephemeral execution context of the current Turn.
    @param requested_timeout_seconds Agent 请求的原始 command deadline / Original command deadline requested by the Agent.
    @return 不足时返回应写入 receipt 的 terminal JSON；否则返回 None /
        A terminal JSON value to write to the receipt when insufficient, otherwise None.
    @note 此分支不触碰 native socket，也不会创建 command journal 项。它宁可把“未开始”
        明确回给 Agent，也不让 worker 的外层 cancellation 在命令已提交后制造
        ``outcome_unknown``。/ This branch does not touch the native socket and creates no
        command-journal entry. It explicitly reports "not started" to the Agent rather than
        letting outer worker cancellation create ``outcome_unknown`` after a command was sent.
    """

    deadline = context.execution_deadline_monotonic
    if deadline is None:
        return None
    remaining_seconds = deadline - asyncio.get_running_loop().time()
    if remaining_seconds > (
        float(requested_timeout_seconds) + _WORKSPACE_COMPLETION_RESERVE_SECONDS
    ):
        return None
    return {
        "status": "not_started_deadline_exhausted",
        "requested_timeout_seconds": requested_timeout_seconds,
        "remaining_seconds": max(0, math.floor(remaining_seconds)),
        "replayed_by_runtime": False,
    }


def _validate_workspace_effect(request: ToolEffectRequest) -> None:
    """@brief 验证 dispatcher 传来的 Workspace effect 分类 / Validate the Workspace effect classification supplied by the dispatcher.

    @param request 待验证的工具调用 / Tool invocation to validate.
    @return None / None.
    @raise ValueError 工具名、effect kind 或 mutation 语义不匹配时抛出 /
        Raised when tool name, effect kind, or mutation semantics do not match.
    """

    if (
        request.tool_name != "run_bash"
        or request.effect_kind != "workspace.exec"
        or not request.mutating
    ):
        raise ValueError("run_bash requires the mutating workspace.exec effect kind")


def _workspace_scope(context: ToolExecutionContext) -> RuntimeScope:
    """@brief 从授权上下文派生个人或整群 Workspace scope / Derive a personal-or-whole-group Workspace scope from authorization context.

    @param context durable Turn 的已认证工具上下文 / Authenticated tool context for a durable Turn.
    @return 仅以 user ID 或 whole-group ID 定义的 scope / Scope defined only by user ID or whole-group ID.
    @raise ValueError 群请求没有 group ID，或私聊上下文仍携带 group ID 时抛出 /
        Raised when a group request has no group ID or a private context still carries one.
    @note ``conversation_id`` 与 ``message_thread_id`` 故意不参与；前者会把 topic 路由
        误当作宿主身份，后者会把一个群裂成多个 Runtime。/ ``conversation_id`` and
        ``message_thread_id`` deliberately do not participate: the former would mistake topic
        routing for host identity, and the latter would split one group into multiple Runtimes.
    """

    if context.is_group:
        if context.group_id is None:
            raise ValueError("A group workspace request requires a group ID")
        return GroupRuntimeScope(context.group_id)
    if context.group_id is not None:
        raise ValueError("A private workspace request must not carry a group ID")
    return PersonalRuntimeScope(context.user_id)


def _required_string(values: object, key: str) -> str:
    """@brief 读取必需、非空的 JSON 字符串 / Read a required nonempty JSON string.

    @param values 已校验 JSON 参数对象 / Validated JSON argument object.
    @param key 字段名 / Field name.
    @return 未改变的字符串值 / Unmodified string value.
    @raise ValueError 字段缺失、为空或类型不正确时抛出 /
        Raised when the field is missing, blank, or has the wrong type.
    """

    if not isinstance(values, dict):
        raise ValueError("Workspace arguments must be an object")
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-blank string")
    return value


def _optional_string(values: object, key: str) -> str:
    """@brief 读取可选字符串，缺省时返回空 stdin / Read an optional string, returning empty stdin by default.

    @param values 已校验 JSON 参数对象 / Validated JSON argument object.
    @param key 字段名 / Field name.
    @return 调用方输入或空字符串 / Caller input or the empty string.
    @raise ValueError 字段存在却不是字符串时抛出 / Raised when a present field is not a string.
    """

    if not isinstance(values, dict):
        raise ValueError("Workspace arguments must be an object")
    value = values.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string when provided")
    return value


def _string_or_default(values: object, key: str, default: str) -> str:
    """@brief 读取可选字符串或使用默认值 / Read an optional string or use a default.

    @param values 已校验 JSON 参数对象 / Validated JSON argument object.
    @param key 字段名 / Field name.
    @param default 字段缺省时的规范默认值 / Canonical default used when the field is absent.
    @return 字符串字段或默认值 / String field or default value.
    @raise ValueError 字段存在却不是字符串时抛出 / Raised when a present field is not a string.
    """

    if not isinstance(values, dict):
        raise ValueError("Workspace arguments must be an object")
    value = values.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _bounded_int(
    values: object,
    key: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    """@brief 读取有界、非布尔整数 / Read a bounded non-boolean integer.

    @param values 已校验 JSON 参数对象 / Validated JSON argument object.
    @param key 字段名 / Field name.
    @param minimum 包含式下界 / Inclusive lower bound.
    @param maximum 包含式上界 / Inclusive upper bound.
    @param default 字段缺省值 / Value used when the field is absent.
    @return 已验证整数 / Validated integer.
    @raise ValueError 字段类型或范围不正确时抛出 / Raised when the field type or range is invalid.
    """

    if not isinstance(values, dict):
        raise ValueError("Workspace arguments must be an object")
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} is outside its allowed range")
    return value


def _validate_output_limit(value: int) -> int:
    """@brief 验证组合根输出预算 / Validate the composition-root output budget.

    @param value 配置中的合并输出字节数 / Combined output byte count from configuration.
    @return 原整数值 / The original integer value.
    @raise ValueError 配置不在 native 应用契约范围内时抛出 /
        Raised when configuration is outside the native application-contract range.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Workspace output limit must be an integer")
    if not 1 <= value <= MAX_BASH_OUTPUT_LIMIT_BYTES:
        raise ValueError("Workspace output limit is outside its allowed range")
    return value


__all__ = ["execute_run_bash"]
