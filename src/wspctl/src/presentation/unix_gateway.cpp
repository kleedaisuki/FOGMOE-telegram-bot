#include "wspctl/presentation/unix_gateway.hpp"

#include "wspctl/infrastructure/protocol.hpp"

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

/** @brief 关闭临时 client FD / Close a temporary client FD. */
void close_fd(const int fd) noexcept {
    if (fd >= 0) {
        static_cast<void>(close(fd));
    }
}

/**
 * @brief 为 client socket 设置有界 packet 与 deadline / Set bounded packets and a deadline on a client socket.
 * @param fd client socket FD / Client socket FD.
 * @param deadline I/O deadline / I/O deadline.
 * @return 成功或 I/O 错误 / Success or I/O error.
 */
[[nodiscard]] Result<void> configure_client_socket(const int fd, const std::chrono::milliseconds deadline) {
    const int requested_buffer = static_cast<int>(kMaxFrameBytes * 2U);
    if (setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &requested_buffer, sizeof(requested_buffer)) != 0 ||
        setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &requested_buffer, sizeof(requested_buffer)) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "configure client SOCK_SEQPACKET buffer"));
    }
    int actual_buffer = 0;
    socklen_t actual_size = sizeof(actual_buffer);
    if (getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &actual_buffer, &actual_size) != 0 ||
        actual_buffer < static_cast<int>(kMaxFrameBytes)) {
        return std::unexpected(make_error(ErrorCode::io_failure, "client SOCK_SEQPACKET buffer is below protocol minimum"));
    }
    const auto milliseconds = std::max<std::int64_t>(1, deadline.count());
    const timeval timeout{
        .tv_sec = static_cast<time_t>(milliseconds / 1000),
        .tv_usec = static_cast<suseconds_t>((milliseconds % 1000) * 1000),
    };
    if (setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0 ||
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "configure client SOCK_SEQPACKET deadline"));
    }
    return {};
}

/**
 * @brief 将 presentation DTO 转换为 control request / Convert a presentation DTO into a control request.
 * @param request presentation DTO / Presentation DTO.
 * @return control request / Control request.
 */
[[nodiscard]] ExecuteRequest to_control_request(const ClientExecuteRequest& request) {
    return ExecuteRequest{
        .runtime_key = request.runtime_key,
        .activation_id = request.activation_id,
        .request_id = request.request_id,
        .request_hash = request.request_hash,
        .argv = request.argv,
        .stdin_data = request.stdin_data,
        .cwd = request.cwd,
        .timeout = request.timeout,
        .output_limit = request.output_limit,
    };
}

/**
 * @brief 将 control result 转换为 presentation DTO / Convert a control result into a presentation DTO.
 * @param result control result / Control result.
 * @return presentation result / Presentation result.
 */
[[nodiscard]] ClientExecutionResult to_client_result(const ExecutionResult& result) {
    return ClientExecutionResult{
        .request_id = result.request_id,
        .exit_code = result.exit_code,
        .timed_out = result.timed_out,
        .truncated = result.truncated,
        .replayed = result.replayed,
        .stdout_data = result.stdout_data,
        .stderr_data = result.stderr_data,
    };
}

/**
 * @brief 将 presentation DTO 转换为只读状态请求 / Convert a presentation DTO into a read-only status request.
 * @param request presentation 状态查询 DTO / Presentation status-query DTO.
 * @return control-plane 状态查询 / Control-plane status query.
 */
[[nodiscard]] RuntimeStatusRequest to_runtime_status_request(const ClientRuntimeStatusRequest& request) {
    return RuntimeStatusRequest{
        .runtime_key = request.runtime_key,
        .activation_id = request.activation_id,
    };
}

/**
 * @brief 将 control-plane 状态转换为 presentation DTO / Convert control-plane status into a presentation DTO.
 * @param result control-plane 状态结果 / Control-plane status result.
 * @return presentation 状态 DTO / Presentation status DTO.
 */
[[nodiscard]] ClientRuntimeStatus to_client_runtime_status(const RuntimeStatusResult& result) {
    return ClientRuntimeStatus{
        .runtime_key = result.runtime_key,
        .state = result.state,
        .active_activation_id = result.active_activation_id,
        .handle_activation_matches = result.handle_activation_matches,
        .supervisor_alive = result.supervisor_alive,
        .idle_for = result.idle_for,
        .idle_ttl = result.idle_ttl,
        .borrowed_dispatches = result.borrowed_dispatches,
        .cleanup_pending = result.cleanup_pending,
    };
}

/**
 * @brief 将文件 DTO 转换为 control-plane 开始请求 / Convert a file DTO into a control-plane begin request.
 * @param request presentation 文件写入 DTO / Presentation file-ingress DTO.
 * @return control-plane 文件开始请求 / Control-plane file-begin request.
 */
[[nodiscard]] PayloadBeginRequest to_payload_begin_request(const ClientAddFileRequest& request) {
    return PayloadBeginRequest{
        .runtime_key = request.runtime_key,
        .activation_id = request.activation_id,
        .request_id = request.request_id,
        .request_hash = request.request_hash,
        .opaque_id = request.opaque_id,
        .byte_size = request.byte_size,
        .sha256 = request.sha256,
    };
}

/**
 * @brief 将 presentation DTO 转换为只读 replay control request / Convert a presentation DTO into a read-only replay control request.
 * @param request presentation 文件恢复 DTO / Presentation file-replay DTO.
 * @return control-plane 文件恢复查询 / Control-plane file-replay query.
 */
[[nodiscard]] PayloadReplayRequest to_payload_replay_request(const ClientReplayFileRequest& request) {
    return PayloadReplayRequest{
        .runtime_key = request.runtime_key,
        .request_id = request.request_id,
        .request_hash = request.request_hash,
        .opaque_id = request.opaque_id,
        .byte_size = request.byte_size,
        .sha256 = request.sha256,
    };
}

/**
 * @brief 将 control-plane 文件收据转换为 presentation DTO / Convert a control-plane file receipt into a presentation DTO.
 * @param result control-plane 文件收据 / Control-plane file receipt.
 * @return presentation 文件收据 / Presentation file receipt.
 */
[[nodiscard]] ClientAddFileResult to_client_file_result(const PayloadResult& result) {
    return ClientAddFileResult{
        .request_id = result.request_id,
        .replayed = result.replayed,
        .path = result.path,
        .byte_size = result.byte_size,
        .sha256 = result.sha256,
    };
}

/**
 * @brief 连接并验证 root-owned broker / Connect to and authenticate the root-owned broker.
 * @param socket_path 已验证绝对 socket 路径 / Validated absolute socket path.
 * @param deadline 单 I/O deadline / Per-I/O deadline.
 * @return 已连接 client FD 或错误 / Connected client FD or an error.
 */
[[nodiscard]] Result<int> connect_authenticated_broker(
    const std::string& socket_path,
    const std::chrono::milliseconds deadline) {
    const int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create client SOCK_SEQPACKET"));
    }
    if (const auto configured = configure_client_socket(fd, deadline); !configured) {
        close_fd(fd);
        return std::unexpected(configured.error());
    }
    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, socket_path.c_str(), sizeof(address.sun_path) - 1U);
    if (connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        const Error error = errno_error(ErrorCode::io_failure, "connect wspctld");
        close_fd(fd);
        return std::unexpected(error);
    }
    ucred credentials {};
    socklen_t credential_size = sizeof(credentials);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credentials, &credential_size) != 0 ||
        credential_size != sizeof(credentials) || credentials.uid != 0U || credentials.pid <= 0) {
        close_fd(fd);
        return std::unexpected(make_error(
            ErrorCode::authentication_failed,
            "broker peer is not the root-owned wspctld"));
    }
    return fd;
}

/**
 * @brief 接收并解码一条 broker 帧 / Receive and decode one broker frame.
 * @param fd 已认证 client socket FD / Authenticated client-socket FD.
 * @return 解码帧或错误 / Decoded frame or an error.
 */
[[nodiscard]] Result<Frame> receive_decoded_frame(const int fd) {
    const auto inbound = receive_frame(fd);
    if (!inbound) {
        return std::unexpected(inbound.error());
    }
    return decode_frame(*inbound);
}

/**
 * @brief 从 broker 帧中提取结构化错误 / Extract a structured error from a broker frame.
 * @param frame 已解码 broker 帧 / Decoded broker frame.
 * @return broker 错误或协议错误 / Broker error or a protocol error.
 */
[[nodiscard]] Result<void> return_broker_error(const Frame& frame) {
    if (frame.kind != MessageKind::error) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker returned an unexpected file frame"));
    }
    const auto error = decode_error(frame.payload);
    if (!error) {
        return std::unexpected(error.error());
    }
    return std::unexpected(*error);
}

/** @brief 文件传输异常退出时的 abort/FD RAII 守卫 / Abort-and-FD RAII guard for exceptional file-transfer exits. */
class PayloadTransferGuard final {
public:
    /**
     * @brief 绑定一个短连接和稳定请求 ID / Bind one short connection and stable request ID.
     * @param fd 待关闭的 client FD 引用 / Reference to the client FD to close.
     * @param request_id 稳定文件写入调用 ID / Stable file-ingress invocation ID.
     */
    PayloadTransferGuard(int& fd, std::string request_id) : fd_(fd), request_id_(std::move(request_id)) {}

    /** @brief 禁止复制 / Copying is forbidden. */
    PayloadTransferGuard(const PayloadTransferGuard&) = delete;
    /** @brief 禁止复制赋值 / Copy assignment is forbidden. */
    PayloadTransferGuard& operator=(const PayloadTransferGuard&) = delete;

    /** @brief 标记 PID 1 已持有一个未发布临时文件 / Mark that PID 1 holds an unpublished temporary file. */
    void arm() noexcept { armed_ = true; }

    /** @brief 标记 publish 或 replay 已取得确定终态 / Mark that publish or replay reached a deterministic terminal state. */
    void disarm() noexcept { armed_ = false; }

    /** @brief 析构时尽力 abort 未发布文件并关闭 FD / Best-effort abort an unpublished file and close the FD on destruction. */
    ~PayloadTransferGuard() {
        if (armed_ && fd_ >= 0) {
            try {
                const PayloadControlRequest request{.request_id = request_id_};
                const auto payload = encode_payload_control_request(request);
                if (payload) {
                    const auto frame = encode_frame(MessageKind::payload_abort, *payload);
                    if (frame) {
                        static_cast<void>(send_frame(fd_, *frame));
                    }
                }
            } catch (...) {
                // Destruction must not terminate a Python callback unwind merely because best-effort
                // cleanup allocation failed. Closing the peer still makes the broker clean up state.
            }
        }
        close_fd(fd_);
    }

private:
    /** @brief 被守卫的 client FD / Guarded client FD. */
    int& fd_;
    /** @brief 未发布传输的稳定调用 ID / Stable invocation ID of the unpublished transfer. */
    std::string request_id_;
    /** @brief 是否必须在异常退出时发送 abort / Whether abort must be sent on an exceptional exit. */
    bool armed_{false};
};

}  // namespace

UnixGatewayClient::UnixGatewayClient(std::string socket_path) : socket_path_(std::move(socket_path)) {}

const std::string& UnixGatewayClient::socket_path() const noexcept {
    return socket_path_;
}

Result<void> UnixGatewayClient::validate_socket_path(const std::string_view socket_path) {
    if (socket_path.empty() || socket_path.front() != '/' || socket_path.find('\0') != std::string_view::npos ||
        socket_path.size() >= sizeof(sockaddr_un::sun_path)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "socket_path must be an absolute AF_UNIX pathname"));
    }
    return {};
}

Result<ClientExecutionResult> UnixGatewayClient::execute(const ClientExecuteRequest& client_request) const {
    if (const auto endpoint = validate_socket_path(socket_path_); !endpoint) {
        return std::unexpected(endpoint.error());
    }
    const ExecuteRequest request = to_control_request(client_request);
    if (const auto valid = validate_execute_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    const int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create client SOCK_SEQPACKET"));
    }
    const auto close_with_error = [fd](const Error& error) -> Result<ClientExecutionResult> {
        close_fd(fd);
        return std::unexpected(error);
    };
    if (const auto configured = configure_client_socket(fd, request.timeout + std::chrono::seconds(5)); !configured) {
        return close_with_error(configured.error());
    }
    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, socket_path_.c_str(), sizeof(address.sun_path) - 1U);
    if (connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        return close_with_error(errno_error(ErrorCode::io_failure, "connect wspctld"));
    }
    ucred credentials {};
    socklen_t credential_size = sizeof(credentials);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credentials, &credential_size) != 0 ||
        credential_size != sizeof(credentials) || credentials.uid != 0U || credentials.pid <= 0) {
        return close_with_error(make_error(ErrorCode::authentication_failed, "broker peer is not the root-owned wspctld"));
    }
    const auto payload = encode_execute_request(request);
    if (!payload) {
        return close_with_error(payload.error());
    }
    const auto outbound = encode_frame(MessageKind::execute, *payload);
    if (!outbound) {
        return close_with_error(outbound.error());
    }
    if (const auto sent = send_frame(fd, *outbound); !sent) {
        return close_with_error(sent.error());
    }
    const auto inbound = receive_frame(fd);
    if (!inbound) {
        return close_with_error(inbound.error());
    }
    close_fd(fd);
    const auto frame = decode_frame(*inbound);
    if (!frame) {
        return std::unexpected(frame.error());
    }
    if (frame->kind == MessageKind::error) {
        const auto error = decode_error(frame->payload);
        if (!error) {
            return std::unexpected(error.error());
        }
        return std::unexpected(*error);
    }
    if (frame->kind != MessageKind::result) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker returned an unexpected frame"));
    }
    const auto result = decode_execution_result(frame->payload);
    if (!result || result->request_id != request.request_id) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker result identity mismatch"));
    }
    return to_client_result(*result);
}

Result<ClientRuntimeStatus> UnixGatewayClient::status(const ClientRuntimeStatusRequest& client_request) const {
    if (const auto endpoint = validate_socket_path(socket_path_); !endpoint) {
        return std::unexpected(endpoint.error());
    }
    const RuntimeStatusRequest request = to_runtime_status_request(client_request);
    if (const auto valid = validate_runtime_status_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    /** @brief 单个只读 runtime 状态查询的本机 I/O deadline / Local I/O deadline for one read-only runtime-status query. */
    constexpr auto kStatusIoDeadline = std::chrono::seconds(5);
    const auto connected = connect_authenticated_broker(socket_path_, kStatusIoDeadline);
    if (!connected) {
        return std::unexpected(connected.error());
    }
    const int fd = *connected;
    const auto close_with_error = [fd](const Error& error) -> Result<ClientRuntimeStatus> {
        close_fd(fd);
        return std::unexpected(error);
    };
    const auto payload = encode_runtime_status_request(request);
    if (!payload) {
        return close_with_error(payload.error());
    }
    const auto outbound = encode_frame(MessageKind::runtime_status, *payload);
    if (!outbound) {
        return close_with_error(outbound.error());
    }
    if (const auto sent = send_frame(fd, *outbound); !sent) {
        return close_with_error(sent.error());
    }
    const auto inbound = receive_decoded_frame(fd);
    if (!inbound) {
        return close_with_error(inbound.error());
    }
    close_fd(fd);
    if (inbound->kind == MessageKind::error) {
        const auto error = return_broker_error(*inbound);
        return std::unexpected(error.error());
    }
    if (inbound->kind != MessageKind::runtime_status_result) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker returned an unexpected runtime status frame"));
    }
    const auto result = decode_runtime_status_result(inbound->payload);
    if (!result || result->runtime_key != request.runtime_key) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker returned a runtime status for another runtime"));
    }
    const bool derived_activation_match = result->active_activation_id.has_value() &&
        *result->active_activation_id == request.activation_id;
    if (result->handle_activation_matches != derived_activation_match) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker returned an inconsistent runtime activation match"));
    }
    return to_client_runtime_status(*result);
}

Result<ClientAddFileResult> UnixGatewayClient::add_file(
    const ClientAddFileRequest& client_request,
    PayloadChunkSource& source) const {
    if (const auto endpoint = validate_socket_path(socket_path_); !endpoint) {
        return std::unexpected(endpoint.error());
    }
    const PayloadBeginRequest request = to_payload_begin_request(client_request);
    if (const auto valid = validate_payload_begin_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    /** @brief 本机 8 MiB 流传输的单 I/O deadline / Per-I/O deadline for a local 8 MiB stream transfer. */
    constexpr auto kPayloadIoDeadline = std::chrono::seconds(30);
    const auto connected = connect_authenticated_broker(socket_path_, kPayloadIoDeadline);
    if (!connected) {
        return std::unexpected(connected.error());
    }
    int fd = *connected;
    PayloadTransferGuard transfer_guard(fd, request.request_id);
    const auto close_with_error = [](const Error& error) -> Result<ClientAddFileResult> {
        return std::unexpected(error);
    };
    const auto send_payload_frame = [fd](
                                        const MessageKind kind,
                                        const std::vector<std::byte>& payload) -> Result<void> {
        const auto frame = encode_frame(kind, payload);
        if (!frame) {
            return std::unexpected(frame.error());
        }
        return send_frame(fd, *frame);
    };
    const auto begin_payload = encode_payload_begin_request(request);
    if (!begin_payload) {
        return close_with_error(begin_payload.error());
    }
    if (const auto sent = send_payload_frame(MessageKind::payload_begin, *begin_payload); !sent) {
        return close_with_error(sent.error());
    }
    const auto first_frame = receive_decoded_frame(fd);
    if (!first_frame) {
        return close_with_error(first_frame.error());
    }
    if (first_frame->kind == MessageKind::error) {
        const auto error = return_broker_error(*first_frame);
        return close_with_error(error.error());
    }
    if (first_frame->kind == MessageKind::payload_result) {
        const auto replay = decode_payload_result(first_frame->payload);
        if (!replay || !replay->replayed || replay->request_id != request.request_id ||
            replay->byte_size != request.byte_size || replay->sha256 != request.sha256) {
            return close_with_error(make_error(ErrorCode::protocol_violation, "broker returned an invalid file replay"));
        }
        transfer_guard.disarm();
        return to_client_file_result(*replay);
    }
    if (first_frame->kind != MessageKind::payload_ack) {
        return close_with_error(make_error(ErrorCode::protocol_violation, "broker did not acknowledge file begin"));
    }
    const auto begin_ack = decode_payload_ack(first_frame->payload);
    if (!begin_ack || begin_ack->request_id != request.request_id || begin_ack->stage != PayloadAckStage::begun ||
        begin_ack->received_bytes != 0U) {
        return close_with_error(make_error(ErrorCode::protocol_violation, "broker returned an invalid file begin acknowledgement"));
    }
    transfer_guard.arm();
    std::size_t received_bytes = 0U;
    for (;;) {
        auto next = source.next_chunk();
        if (!next) {
            return close_with_error(next.error());
        }
        if (!next->has_value()) {
            break;
        }
        PayloadChunk chunk{
            .request_id = request.request_id,
            .bytes = std::move(**next),
        };
        if (chunk.bytes.size() > request.byte_size - received_bytes) {
            return close_with_error(make_error(ErrorCode::invalid_argument, "file chunks exceed declared byte size"));
        }
        const auto encoded_chunk = encode_payload_chunk(chunk);
        if (!encoded_chunk) {
            return close_with_error(encoded_chunk.error());
        }
        if (const auto sent = send_payload_frame(MessageKind::payload_chunk, *encoded_chunk); !sent) {
            return close_with_error(sent.error());
        }
        const auto response = receive_decoded_frame(fd);
        if (!response) {
            return close_with_error(response.error());
        }
        if (response->kind == MessageKind::error) {
            const auto error = return_broker_error(*response);
            return close_with_error(error.error());
        }
        if (response->kind != MessageKind::payload_ack) {
            return close_with_error(make_error(ErrorCode::protocol_violation, "broker did not acknowledge file chunk"));
        }
        const auto acknowledgement = decode_payload_ack(response->payload);
        received_bytes += chunk.bytes.size();
        if (!acknowledgement || acknowledgement->request_id != request.request_id ||
            acknowledgement->stage != PayloadAckStage::chunk_written ||
            acknowledgement->received_bytes != received_bytes) {
            return close_with_error(make_error(ErrorCode::protocol_violation, "broker returned an invalid file chunk acknowledgement"));
        }
    }
    if (received_bytes != request.byte_size) {
        return close_with_error(make_error(ErrorCode::invalid_argument, "file chunk source ended before declared byte size"));
    }
    const PayloadControlRequest control{.request_id = request.request_id};
    const auto seal_payload = encode_payload_control_request(control);
    if (!seal_payload) {
        return close_with_error(seal_payload.error());
    }
    if (const auto sent = send_payload_frame(MessageKind::payload_seal, *seal_payload); !sent) {
        return close_with_error(sent.error());
    }
    const auto seal_frame = receive_decoded_frame(fd);
    if (!seal_frame) {
        return close_with_error(seal_frame.error());
    }
    if (seal_frame->kind == MessageKind::error) {
        const auto error = return_broker_error(*seal_frame);
        return close_with_error(error.error());
    }
    if (seal_frame->kind != MessageKind::payload_ack) {
        return close_with_error(make_error(ErrorCode::protocol_violation, "broker did not acknowledge file seal"));
    }
    const auto seal_ack = decode_payload_ack(seal_frame->payload);
    if (!seal_ack || seal_ack->request_id != request.request_id || seal_ack->stage != PayloadAckStage::sealed ||
        seal_ack->received_bytes != request.byte_size) {
        return close_with_error(make_error(ErrorCode::protocol_violation, "broker returned an invalid file seal acknowledgement"));
    }
    const auto publish_payload = encode_payload_control_request(control);
    if (!publish_payload) {
        return close_with_error(publish_payload.error());
    }
    if (const auto sent = send_payload_frame(MessageKind::payload_publish, *publish_payload); !sent) {
        return close_with_error(sent.error());
    }
    const auto publish_frame = receive_decoded_frame(fd);
    if (!publish_frame) {
        return close_with_error(publish_frame.error());
    }
    if (publish_frame->kind == MessageKind::error) {
        const auto error = return_broker_error(*publish_frame);
        return close_with_error(error.error());
    }
    if (publish_frame->kind != MessageKind::payload_result) {
        return close_with_error(make_error(ErrorCode::protocol_violation, "broker did not return a file receipt"));
    }
    const auto result = decode_payload_result(publish_frame->payload);
    if (!result || result->replayed || result->request_id != request.request_id ||
        result->byte_size != request.byte_size || result->sha256 != request.sha256) {
        return close_with_error(make_error(ErrorCode::protocol_violation, "broker returned an invalid file receipt"));
    }
    transfer_guard.disarm();
    return to_client_file_result(*result);
}

Result<ClientAddFileResult> UnixGatewayClient::replay_file(const ClientReplayFileRequest& client_request) const {
    if (const auto endpoint = validate_socket_path(socket_path_); !endpoint) {
        return std::unexpected(endpoint.error());
    }
    const PayloadReplayRequest request = to_payload_replay_request(client_request);
    if (const auto valid = validate_payload_replay_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    /** @brief 单个只读 recovery lookup 的本机 I/O deadline / Local I/O deadline for one read-only recovery lookup. */
    constexpr auto kReplayIoDeadline = std::chrono::seconds(5);
    const auto connected = connect_authenticated_broker(socket_path_, kReplayIoDeadline);
    if (!connected) {
        return std::unexpected(connected.error());
    }
    const int fd = *connected;
    const auto close_with_error = [fd](const Error& error) -> Result<ClientAddFileResult> {
        close_fd(fd);
        return std::unexpected(error);
    };
    const auto payload = encode_payload_replay_request(request);
    if (!payload) {
        return close_with_error(payload.error());
    }
    const auto outbound = encode_frame(MessageKind::payload_replay, *payload);
    if (!outbound) {
        return close_with_error(outbound.error());
    }
    if (const auto sent = send_frame(fd, *outbound); !sent) {
        return close_with_error(sent.error());
    }
    const auto inbound = receive_decoded_frame(fd);
    if (!inbound) {
        return close_with_error(inbound.error());
    }
    close_fd(fd);
    if (inbound->kind == MessageKind::error) {
        const auto error = return_broker_error(*inbound);
        return std::unexpected(error.error());
    }
    if (inbound->kind != MessageKind::payload_result) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker returned an unexpected file replay frame"));
    }
    const auto result = decode_payload_result(inbound->payload);
    const std::string expected_path = "/workspace/uploads/" + request.opaque_id + "/payload";
    if (!result || !result->replayed || result->request_id != request.request_id || result->path != expected_path ||
        result->byte_size != request.byte_size || result->sha256 != request.sha256) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "broker returned an invalid file replay receipt"));
    }
    return to_client_file_result(*result);
}

}  // namespace wspctl::presentation
