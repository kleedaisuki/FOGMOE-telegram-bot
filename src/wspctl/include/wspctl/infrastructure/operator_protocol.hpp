#pragma once

#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/common.hpp"

#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace wspctl::operator_protocol {

/** @brief operator wire 协议魔数 / Operator wire-protocol magic number. */
inline constexpr std::uint32_t kOperatorProtocolMagic{0x31504f57U};  // "WOP1" in little endian.
/** @brief operator wire 协议版本 / Operator wire-protocol version. */
inline constexpr std::uint16_t kOperatorProtocolVersion{1U};
/** @brief operator 固定帧头大小 / Operator fixed-frame header size. */
inline constexpr std::size_t kOperatorFrameHeaderBytes{12U};
/** @brief 一条 operator SOCK_SEQPACKET 消息的最大大小 / Maximum one operator SOCK_SEQPACKET message size. */
inline constexpr std::size_t kOperatorMaxFrameBytes{128U * 1024U};
/** @brief operator 请求中 runtime 文本的最大大小 / Maximum runtime-text size in an operator request. */
inline constexpr std::size_t kOperatorRuntimeTextBytes{128U};
/** @brief operator 请求中 workspace 逻辑路径的最大大小 / Maximum workspace logical-path size in an operator request. */
inline constexpr std::size_t kOperatorWorkspacePathBytes{4096U};
/** @brief operator wire 中安全 filename 的最大大小 / Maximum safe filename size in the operator wire protocol. */
inline constexpr std::size_t kOperatorEncodedFilenameBytes{255U * 3U};

/** @brief operator 协议的有限帧类别 / Finite frame kinds of the operator protocol. */
enum class OperatorMessageKind : std::uint16_t {
    /** @brief 请求 runtime 状态 / Request runtime status. */
    status_request = 1,
    /** @brief 返回 runtime 状态 / Return runtime status. */
    status_response = 2,
    /** @brief 请求一层 workspace 目录 / Request one workspace directory level. */
    list_request = 3,
    /** @brief 返回一层 workspace 目录 / Return one workspace directory level. */
    list_response = 4,
    /** @brief 返回不携带敏感文本的 operator 错误 / Return an operator error without sensitive text. */
    error_response = 5,
};

/** @brief operator 协议可见的错误分类 / Error categories visible in the operator protocol. */
enum class OperatorErrorCode : std::uint8_t {
    /** @brief 请求字段不符合 operator 语义 / Request fields violate operator semantics. */
    invalid_request = 1,
    /** @brief 请求的已持久化对象不存在 / Requested persistent object does not exist. */
    not_found = 2,
    /** @brief read-only control plane 暂不可用 / The read-only control plane is unavailable. */
    unavailable = 3,
    /** @brief 对端身份不被 endpoint 授权 / The peer identity is not authorized by the endpoint. */
    unauthorized = 4,
    /** @brief operator wire 违反协议 / The operator wire violates the protocol. */
    protocol_violation = 5,
};

/** @brief runtime 状态查询的 wire DTO / Wire DTO for a runtime-status query. */
struct StatusRequest final {
    /** @brief 待查询的 canonical runtime UUID 文本 / Canonical runtime UUID text to query. */
    std::string runtime_key;
};

/** @brief workspace 单层目录查询的 wire DTO / Wire DTO for a one-level workspace directory query. */
struct ListRequest final {
    /** @brief 待查询的 canonical runtime UUID 文本 / Canonical runtime UUID text to query. */
    std::string runtime_key;
    /** @brief `/workspace` 下的规范逻辑路径 / Canonical logical path under `/workspace`. */
    std::string path;
};

/** @brief operator runtime 状态响应的 wire DTO / Wire DTO for an operator runtime-status response. */
struct StatusResponse final {
    /** @brief 经 allowlist 限定的 runtime 状态 / Allowlisted runtime status. */
    domain::OperatorWorkspaceStatus status;
};

/** @brief operator 目录响应的 wire DTO / Wire DTO for an operator directory response. */
struct ListResponse final {
    /** @brief 有界、只读目录列举 / Bounded read-only directory listing. */
    domain::WorkspaceListing listing;
};

/** @brief operator 错误响应 DTO / Operator error-response DTO. */
struct ErrorResponse final {
    /** @brief 不含诊断文本的稳定错误分类 / Stable error category without diagnostic text. */
    OperatorErrorCode code{OperatorErrorCode::protocol_violation};
};

/** @brief 已解码的 operator wire 帧 / Decoded operator wire frame. */
struct OperatorFrame final {
    /** @brief 帧类别 / Frame kind. */
    OperatorMessageKind kind{OperatorMessageKind::error_response};
    /** @brief 有界二进制载荷 / Bounded binary payload. */
    std::vector<std::byte> payload;
};

/**
 * @brief 编码 runtime 状态查询 / Encode a runtime-status query.
 * @param request 待编码查询 / Query to encode.
 * @return 有界二进制载荷或错误 / Bounded binary payload or an error.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_status_request(const StatusRequest& request);
/** @brief 解码 runtime 状态查询 / Decode a runtime-status query. */
[[nodiscard]] Result<StatusRequest> decode_status_request(std::span<const std::byte> payload);

/**
 * @brief 编码 workspace 目录查询 / Encode a workspace-directory query.
 * @param request 待编码查询 / Query to encode.
 * @return 有界二进制载荷或错误 / Bounded binary payload or an error.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_list_request(const ListRequest& request);
/** @brief 解码 workspace 目录查询 / Decode a workspace-directory query. */
[[nodiscard]] Result<ListRequest> decode_list_request(std::span<const std::byte> payload);

/**
 * @brief 编码 runtime 状态响应 / Encode a runtime-status response.
 * @param response 待编码响应 / Response to encode.
 * @return 有界二进制载荷或错误 / Bounded binary payload or an error.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_status_response(const StatusResponse& response);
/** @brief 解码 runtime 状态响应 / Decode a runtime-status response. */
[[nodiscard]] Result<StatusResponse> decode_status_response(std::span<const std::byte> payload);

/**
 * @brief 编码 workspace 目录响应 / Encode a workspace-directory response.
 * @param response 待编码响应 / Response to encode.
 * @return 有界二进制载荷或错误 / Bounded binary payload or an error.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_list_response(const ListResponse& response);
/** @brief 解码 workspace 目录响应 / Decode a workspace-directory response. */
[[nodiscard]] Result<ListResponse> decode_list_response(std::span<const std::byte> payload);

/**
 * @brief 编码无敏感文本的 operator 错误 / Encode an operator error without sensitive text.
 * @param response 待编码错误 / Error to encode.
 * @return 有界二进制载荷或错误 / Bounded binary payload or an error.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_error_response(const ErrorResponse& response);
/** @brief 解码无敏感文本的 operator 错误 / Decode an operator error without sensitive text. */
[[nodiscard]] Result<ErrorResponse> decode_error_response(std::span<const std::byte> payload);

/**
 * @brief 以 operator 独立头封装载荷 / Wrap a payload in an operator-specific header.
 * @param kind operator 帧类别 / Operator frame kind.
 * @param payload 已验证载荷 / Validated payload.
 * @return 完整 SOCK_SEQPACKET 消息 / Complete SOCK_SEQPACKET message.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_operator_frame(
    OperatorMessageKind kind,
    std::span<const std::byte> payload);

/**
 * @brief 严格解码一条 operator 独立帧 / Strictly decode one independent operator frame.
 * @param wire 从 operator SOCK_SEQPACKET 收到的消息 / Message received from the operator SOCK_SEQPACKET endpoint.
 * @return 已验证帧或协议错误 / Validated frame or a protocol error.
 */
[[nodiscard]] Result<OperatorFrame> decode_operator_frame(std::span<const std::byte> wire);

/**
 * @brief 发送一条 operator 帧 / Send one operator frame.
 * @param fd operator UNIX SOCK_SEQPACKET 文件描述符 / Operator UNIX SOCK_SEQPACKET descriptor.
 * @param frame 完整 operator 帧 / Complete operator frame.
 * @return 成功或 I/O 错误 / Success or an I/O error.
 */
[[nodiscard]] Result<void> send_operator_frame(int fd, std::span<const std::byte> frame);

/**
 * @brief 接收一条 operator 帧 / Receive one operator frame.
 * @param fd operator UNIX SOCK_SEQPACKET 文件描述符 / Operator UNIX SOCK_SEQPACKET descriptor.
 * @return 完整帧或 I/O/截断错误 / Complete frame or an I/O/truncation error.
 */
[[nodiscard]] Result<std::vector<std::byte>> receive_operator_frame(int fd);

}  // namespace wspctl::operator_protocol
