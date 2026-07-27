"""@brief Assistant 工具失败诊断的安全边界测试 / Safety-boundary tests for Assistant tool failure diagnostics."""

from unittest.mock import Mock, call

from fogmoe_bot.application.assistant.agent_loop import _annotate_workspace_failure
from fogmoe_bot.application.workspace.errors import WorkspaceRuntimeUnavailableError
from fogmoe_bot.infrastructure.database.assistant_tool_effects import (
    _safe_failure_detail,
)


def test_receipt_preserves_explicit_workspace_native_diagnostics() -> None:
    """@brief receipt 保存显式安全的 Workspace 诊断 / Receipts preserve explicitly safe Workspace diagnostics.

    @return None / None.
    """

    error = WorkspaceRuntimeUnavailableError(
        "wspctl RuntimeProcess execution failed",
        diagnostic_code="io_failure",
        diagnostic_message="write runtime journal failed",
    )

    assert _safe_failure_detail(error) == (
        "workspace native error [io_failure]: write runtime journal failed"
    )


def test_receipt_never_persists_arbitrary_exception_text() -> None:
    """@brief receipt 不持久化任意异常中的潜在秘密 / Receipts never persist potential secrets from arbitrary exceptions.

    @return None / None.
    """

    secret = "command=upload --token super-secret"
    error = RuntimeError(secret)

    detail = _safe_failure_detail(error)

    assert detail == "operation failed"
    assert secret not in detail


def test_workspace_diagnostics_reject_codes_and_strip_controls() -> None:
    """@brief Workspace 诊断拒绝非法码并清理控制字符 / Workspace diagnostics reject invalid codes and strip controls.

    @return None / None.
    """

    error = WorkspaceRuntimeUnavailableError(
        "unavailable",
        diagnostic_code="INVALID CODE",
        diagnostic_message="line one\r\nline two\x00",
    )

    assert error.diagnostic_code is None
    assert error.diagnostic_message == "line one line two"
    assert error.diagnostic_summary() is None


def test_tool_span_receives_workspace_native_diagnostics() -> None:
    """@brief 工具 span 收到 Workspace 机器诊断字段 / Tool spans receive Workspace machine diagnostics.

    @return None / None.
    """

    span = Mock()
    error = WorkspaceRuntimeUnavailableError(
        "unavailable",
        diagnostic_code="sandbox_preflight_failed",
        diagnostic_message="overlay mount denied",
    )

    _annotate_workspace_failure(span, error)

    span.set_attribute.assert_has_calls(
        [
            call("workspace.error.code", "sandbox_preflight_failed"),
            call("workspace.error.message", "overlay mount denied"),
        ]
    )
