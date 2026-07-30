#pragma once

#include <cerrno>
#include <cstring>
#include <expected>
#include <string>
#include <string_view>
#include <utility>

namespace wspctl {

/**
 * @brief 原生层可恢复错误类别 / Recoverable native-layer error categories.
 *
 * 错误码是跨 broker、supervisor 与 Python binding 的稳定语义边界。
 * Error codes form the stable semantic boundary across broker, supervisor, and Python binding.
 */
enum class ErrorCode : unsigned short {
    invalid_argument,
    malformed_frame,
    frame_too_large,
    unsupported_version,
    protocol_violation,
    authentication_failed,
    sandbox_preflight_failed,
    permission_denied,
    not_found,
    already_exists,
    busy,
    timeout,
    io_failure,
    journal_conflict,
    invocation_in_doubt,
    quota_recovery_required,
    binding_quarantined,
    child_failure,
    internal,
};

/**
 * @brief 结构化错误 / Structured error.
 * @note 不把 errno 裸露为跨进程协议的一部分，以避免平台相关语义。
 *       errno is not exposed raw as protocol semantics, avoiding platform coupling.
 */
struct Error final {
    /** @brief 机器可判定错误码 / Machine-readable error code. */
    ErrorCode code;
    /** @brief 供日志与调用方展示的说明 / Diagnostic message for logs and callers. */
    std::string message;
};

/** @brief 带 Error 的 std::expected 别名 / std::expected alias carrying Error. */
template <typename Value> using Result = std::expected<Value, Error>;

/**
 * @brief 构造错误值 / Construct an error value.
 * @param code 语义错误码 / Semantic error code.
 * @param message 诊断说明 / Diagnostic message.
 * @return 可传播的错误对象 / Propagatable error object.
 */
[[nodiscard]] inline Error make_error(const ErrorCode code, std::string message) {
    return Error{.code = code, .message = std::move(message)};
}

/**
 * @brief 为 syscall 失败构造错误 / Construct an error from a failed syscall.
 * @param code 语义错误码 / Semantic error code.
 * @param operation 失败操作名 / Failed operation name.
 * @return 带 errno 文本的错误 / Error with errno text.
 */
[[nodiscard]] inline Error errno_error(const ErrorCode code, const std::string_view operation) {
    return make_error(code, std::string(operation) + ": " + std::strerror(errno));
}

} // namespace wspctl
