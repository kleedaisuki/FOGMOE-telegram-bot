#include "wspctl/presentation/operator_gateway.hpp"

#include "wspctl/infrastructure/operator_protocol.hpp"

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

namespace wspctl::presentation {
namespace {

/** @brief operator client 单次 I/O deadline / Per-I/O deadline for an operator client. */
constexpr std::chrono::milliseconds kOperatorClientDeadline{5'000};

/**
 * @brief 关闭临时 operator client FD / Close a temporary operator-client FD.
 * @param descriptor 待关闭 FD / FD to close.
 */
void close_fd(const int descriptor) noexcept {
    if (descriptor >= 0) {
        static_cast<void>(close(descriptor));
    }
}

/**
 * @brief 设置 operator socket 的帧缓冲和 deadline / Set operator-socket frame buffers and
 * deadlines.
 * @param descriptor operator client FD / Operator client FD.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 */
[[nodiscard]] Result<void> configure_operator_client_socket(const int descriptor) {
    const int requested_buffer = static_cast<int>(operator_protocol::kOperatorMaxFrameBytes * 2U);
    if (setsockopt(descriptor, SOL_SOCKET, SO_SNDBUF, &requested_buffer,
                   sizeof(requested_buffer)) != 0 ||
        setsockopt(descriptor, SOL_SOCKET, SO_RCVBUF, &requested_buffer,
                   sizeof(requested_buffer)) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::io_failure, "configure operator SOCK_SEQPACKET buffer"));
    }
    int actual_buffer{0};
    socklen_t actual_size = sizeof(actual_buffer);
    if (getsockopt(descriptor, SOL_SOCKET, SO_SNDBUF, &actual_buffer, &actual_size) != 0 ||
        actual_buffer < static_cast<int>(operator_protocol::kOperatorMaxFrameBytes)) {
        return std::unexpected(make_error(
            ErrorCode::io_failure, "operator SOCK_SEQPACKET buffer is below protocol minimum"));
    }
    const timeval timeout{
        .tv_sec = static_cast<time_t>(kOperatorClientDeadline.count() / 1000),
        .tv_usec = static_cast<suseconds_t>((kOperatorClientDeadline.count() % 1000) * 1000),
    };
    if (setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0 ||
        setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0) {
        return std::unexpected(
            errno_error(ErrorCode::io_failure, "configure operator SOCK_SEQPACKET deadline"));
    }
    return {};
}

/**
 * @brief 连接并验证 root-owned operator broker process / Connect to and authenticate the root-owned
 * operator broker process.
 * @param socket_path 已验证 endpoint 路径 / Validated endpoint path.
 * @return 已连接 client FD 或错误 / Connected client FD or an error.
 */
[[nodiscard]] Result<int> connect_authenticated_operator(const std::string& socket_path) {
    const int descriptor = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (descriptor < 0) {
        return std::unexpected(
            errno_error(ErrorCode::io_failure, "create operator client SOCK_SEQPACKET"));
    }
    if (const auto configured = configure_operator_client_socket(descriptor); !configured) {
        close_fd(descriptor);
        return std::unexpected(configured.error());
    }
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, socket_path.c_str(), sizeof(address.sun_path) - 1U);
    if (connect(descriptor, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        const Error error = errno_error(ErrorCode::io_failure, "connect wspctld operator endpoint");
        close_fd(descriptor);
        return std::unexpected(error);
    }
    ucred credentials{};
    socklen_t credential_size = sizeof(credentials);
    if (getsockopt(descriptor, SOL_SOCKET, SO_PEERCRED, &credentials, &credential_size) != 0 ||
        credential_size != sizeof(credentials) || credentials.uid != 0U || credentials.pid <= 0) {
        close_fd(descriptor);
        return std::unexpected(make_error(ErrorCode::authentication_failed,
                                          "operator peer is not the root-owned wspctld"));
    }
    return descriptor;
}

/**
 * @brief 将无文本的 operator wire 错误转换为本地错误 / Convert a text-free operator wire error into
 * a local error.
 * @param response operator wire 错误 / Operator wire error.
 * @return 结构化本地错误 / Structured local error.
 */
[[nodiscard]] Error map_operator_error(const operator_protocol::ErrorResponse& response) {
    switch (response.code) {
    case operator_protocol::OperatorErrorCode::invalid_request:
        return make_error(ErrorCode::invalid_argument, "operator request was rejected");
    case operator_protocol::OperatorErrorCode::not_found:
        return make_error(ErrorCode::not_found, "operator workspace object was not found");
    case operator_protocol::OperatorErrorCode::unavailable:
        return make_error(ErrorCode::io_failure, "operator workspace read model is unavailable");
    case operator_protocol::OperatorErrorCode::unauthorized:
        return make_error(ErrorCode::authentication_failed, "operator endpoint rejected this UID");
    case operator_protocol::OperatorErrorCode::protocol_violation:
        return make_error(ErrorCode::protocol_violation,
                          "operator endpoint rejected the wire protocol");
    }
    return make_error(ErrorCode::protocol_violation,
                      "operator endpoint returned an unknown error code");
}

/**
 * @brief 接收并解码一条 operator 帧 / Receive and decode one operator frame.
 * @param descriptor 已认证 operator client FD / Authenticated operator client FD.
 * @return 已解码帧或错误 / Decoded frame or an error.
 */
[[nodiscard]] Result<operator_protocol::OperatorFrame>
receive_operator_response(const int descriptor) {
    const auto wire = operator_protocol::receive_operator_frame(descriptor);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return operator_protocol::decode_operator_frame(*wire);
}

/**
 * @brief 检查并返回 operator 错误帧 / Check and return an operator error frame.
 * @param frame 已解码 operator 帧 / Decoded operator frame.
 * @return 必定为 error 的 expected / Expected that always carries an error.
 */
[[nodiscard]] Result<void> return_operator_error(const operator_protocol::OperatorFrame& frame) {
    if (frame.kind != operator_protocol::OperatorMessageKind::error_response) {
        return std::unexpected(make_error(ErrorCode::protocol_violation,
                                          "operator endpoint returned an unexpected frame kind"));
    }
    const auto error = operator_protocol::decode_error_response(frame.payload);
    if (!error) {
        return std::unexpected(error.error());
    }
    return std::unexpected(map_operator_error(*error));
}

} // namespace

OperatorGatewayClient::OperatorGatewayClient(std::string socket_path)
    : socket_path_(std::move(socket_path)) {}

const std::string& OperatorGatewayClient::socket_path() const noexcept { return socket_path_; }

Result<void> OperatorGatewayClient::validate_socket_path(const std::string_view socket_path) {
    if (socket_path.empty() || socket_path.front() != '/' ||
        socket_path.find('\0') != std::string_view::npos ||
        socket_path.size() >= sizeof(sockaddr_un::sun_path)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "operator socket path must be an absolute AF_UNIX endpoint"));
    }
    return {};
}

Result<domain::OperatorWorkspaceStatus>
OperatorGatewayClient::status(const domain::RuntimeId& runtime) const {
    if (const auto valid = validate_socket_path(socket_path_); !valid) {
        return std::unexpected(valid.error());
    }
    const auto request_payload = operator_protocol::encode_status_request(
        operator_protocol::StatusRequest{.runtime_key = runtime.value()});
    if (!request_payload) {
        return std::unexpected(request_payload.error());
    }
    const auto request_frame = operator_protocol::encode_operator_frame(
        operator_protocol::OperatorMessageKind::status_request, *request_payload);
    if (!request_frame) {
        return std::unexpected(request_frame.error());
    }
    const auto descriptor = connect_authenticated_operator(socket_path_);
    if (!descriptor) {
        return std::unexpected(descriptor.error());
    }
    const auto close_with_error =
        [&](const Error& error) -> Result<domain::OperatorWorkspaceStatus> {
        close_fd(*descriptor);
        return std::unexpected(error);
    };
    if (const auto sent = operator_protocol::send_operator_frame(*descriptor, *request_frame);
        !sent) {
        return close_with_error(sent.error());
    }
    const auto response = receive_operator_response(*descriptor);
    if (!response) {
        return close_with_error(response.error());
    }
    if (response->kind == operator_protocol::OperatorMessageKind::error_response) {
        const auto error = return_operator_error(*response);
        return close_with_error(error.error());
    }
    if (response->kind != operator_protocol::OperatorMessageKind::status_response) {
        return close_with_error(
            make_error(ErrorCode::protocol_violation,
                       "operator endpoint returned an unexpected status frame"));
    }
    const auto decoded = operator_protocol::decode_status_response(response->payload);
    if (!decoded) {
        return close_with_error(decoded.error());
    }
    close_fd(*descriptor);
    return decoded->status;
}

Result<domain::WorkspaceListing>
OperatorGatewayClient::list(const domain::RuntimeId& runtime,
                            const domain::OperatorWorkspacePath& path) const {
    if (const auto valid = validate_socket_path(socket_path_); !valid) {
        return std::unexpected(valid.error());
    }
    const auto request_payload = operator_protocol::encode_list_request(
        operator_protocol::ListRequest{.runtime_key = runtime.value(), .path = path.value()});
    if (!request_payload) {
        return std::unexpected(request_payload.error());
    }
    const auto request_frame = operator_protocol::encode_operator_frame(
        operator_protocol::OperatorMessageKind::list_request, *request_payload);
    if (!request_frame) {
        return std::unexpected(request_frame.error());
    }
    const auto descriptor = connect_authenticated_operator(socket_path_);
    if (!descriptor) {
        return std::unexpected(descriptor.error());
    }
    const auto close_with_error = [&](const Error& error) -> Result<domain::WorkspaceListing> {
        close_fd(*descriptor);
        return std::unexpected(error);
    };
    if (const auto sent = operator_protocol::send_operator_frame(*descriptor, *request_frame);
        !sent) {
        return close_with_error(sent.error());
    }
    const auto response = receive_operator_response(*descriptor);
    if (!response) {
        return close_with_error(response.error());
    }
    if (response->kind == operator_protocol::OperatorMessageKind::error_response) {
        const auto error = return_operator_error(*response);
        return close_with_error(error.error());
    }
    if (response->kind != operator_protocol::OperatorMessageKind::list_response) {
        return close_with_error(make_error(ErrorCode::protocol_violation,
                                           "operator endpoint returned an unexpected list frame"));
    }
    const auto decoded = operator_protocol::decode_list_response(response->payload);
    if (!decoded) {
        return close_with_error(decoded.error());
    }
    close_fd(*descriptor);
    return decoded->listing;
}

} // namespace wspctl::presentation
