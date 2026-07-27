#include "wspctl/infrastructure/operator_protocol.hpp"

#include <array>
#include <cerrno>
#include <cstring>
#include <limits>
#include <sys/socket.h>
#include <unistd.h>

namespace wspctl::operator_protocol {
namespace {

/** @brief 有界 operator wire 编码器 / Bounded operator-wire encoder. */
class Writer final {
public:
    /** @brief 写入无符号 8 位整数 / Write an unsigned 8-bit integer. */
    void u8(const std::uint8_t value) { bytes_.push_back(static_cast<std::byte>(value)); }

    /** @brief 写入 little-endian 16 位整数 / Write a little-endian 16-bit integer. */
    void u16(const std::uint16_t value) {
        u8(static_cast<std::uint8_t>(value & 0xffU));
        u8(static_cast<std::uint8_t>((value >> 8U) & 0xffU));
    }

    /** @brief 写入 little-endian 32 位整数 / Write a little-endian 32-bit integer. */
    void u32(const std::uint32_t value) {
        for (unsigned int index = 0U; index < 4U; ++index) {
            u8(static_cast<std::uint8_t>((value >> (index * 8U)) & 0xffU));
        }
    }

    /** @brief 写入 little-endian 64 位整数 / Write a little-endian 64-bit integer. */
    void u64(const std::uint64_t value) {
        for (unsigned int index = 0U; index < 8U; ++index) {
            u8(static_cast<std::uint8_t>((value >> (index * 8U)) & 0xffU));
        }
    }

    /**
     * @brief 写入长度前缀字符串 / Write a length-prefixed string.
     * @param value 待写字符串 / String to write.
     */
    void string(const std::string_view value) {
        u32(static_cast<std::uint32_t>(value.size()));
        for (const char character : value) {
            bytes_.push_back(static_cast<std::byte>(static_cast<unsigned char>(character)));
        }
    }

    /** @brief 移交累计 bytes / Take accumulated bytes. */
    [[nodiscard]] std::vector<std::byte> take() { return std::move(bytes_); }

private:
    /** @brief 累计 wire bytes / Accumulated wire bytes. */
    std::vector<std::byte> bytes_;
};

/** @brief 有界 operator wire 解码器 / Bounded operator-wire decoder. */
class Reader final {
public:
    /**
     * @brief 构造 reader / Construct a reader.
     * @param bytes 待读取 bytes / Bytes to read.
     */
    explicit Reader(const std::span<const std::byte> bytes) : bytes_(bytes) {}

    /** @brief 读取无符号 8 位值 / Read an unsigned 8-bit value. */
    [[nodiscard]] Result<std::uint8_t> u8() {
        if (!has(1U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated operator u8"));
        }
        return std::to_integer<std::uint8_t>(bytes_[offset_++]);
    }

    /** @brief 读取 little-endian 16 位值 / Read a little-endian 16-bit value. */
    [[nodiscard]] Result<std::uint16_t> u16() {
        if (!has(2U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated operator u16"));
        }
        /** @brief little-endian 16 位结果 / Little-endian 16-bit result. */
        std::uint16_t value{0U};
        for (unsigned int index = 0U; index < 2U; ++index) {
            value = static_cast<std::uint16_t>(
                value | static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(bytes_[offset_++]) << (index * 8U)));
        }
        return value;
    }

    /** @brief 读取 little-endian 32 位值 / Read a little-endian 32-bit value. */
    [[nodiscard]] Result<std::uint32_t> u32() {
        if (!has(4U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated operator u32"));
        }
        /** @brief little-endian 32 位结果 / Little-endian 32-bit result. */
        std::uint32_t value{0U};
        for (unsigned int index = 0U; index < 4U; ++index) {
            value |= static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(bytes_[offset_++])) << (index * 8U);
        }
        return value;
    }

    /** @brief 读取 little-endian 64 位值 / Read a little-endian 64-bit value. */
    [[nodiscard]] Result<std::uint64_t> u64() {
        if (!has(8U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated operator u64"));
        }
        /** @brief little-endian 64 位结果 / Little-endian 64-bit result. */
        std::uint64_t value{0U};
        for (unsigned int index = 0U; index < 8U; ++index) {
            value |= static_cast<std::uint64_t>(std::to_integer<std::uint8_t>(bytes_[offset_++])) << (index * 8U);
        }
        return value;
    }

    /**
     * @brief 读取长度受限、无 NUL 的字符串 / Read a length-bounded NUL-free string.
     * @param maximum 最大 byte 数 / Maximum byte count.
     * @return 字符串或格式错误 / String or a format error.
     */
    [[nodiscard]] Result<std::string> string(const std::size_t maximum) {
        const auto length = u32();
        if (!length) {
            return std::unexpected(length.error());
        }
        /** @brief 经过无符号扩展的长度 / Length after unsigned extension. */
        const std::size_t size = static_cast<std::size_t>(*length);
        if (size > maximum || !has(size)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator string length"));
        }
        std::string value;
        value.reserve(size);
        for (std::size_t index = 0U; index < size; ++index) {
            const char character = static_cast<char>(std::to_integer<unsigned char>(bytes_[offset_++]));
            if (character == '\0') {
                return std::unexpected(make_error(ErrorCode::malformed_frame, "operator strings cannot contain NUL"));
            }
            value.push_back(character);
        }
        return value;
    }

    /** @brief 判断是否恰好读尽 / Check whether all bytes were consumed. */
    [[nodiscard]] bool finished() const noexcept { return offset_ == bytes_.size(); }

private:
    /**
     * @brief 判断剩余空间是否足够 / Check whether enough input remains.
     * @param count 待读取 byte 数 / Byte count to read.
     * @return 是否足够 / Whether it is sufficient.
     */
    [[nodiscard]] bool has(const std::size_t count) const noexcept {
        return count <= bytes_.size() - offset_;
    }

    /** @brief 输入 bytes / Input bytes. */
    std::span<const std::byte> bytes_;
    /** @brief 当前读取偏移 / Current read offset. */
    std::size_t offset_{0U};
};

/**
 * @brief 判断字符串是否无 NUL 且在指定上限内 / Check a string is NUL-free and within the specified cap.
 * @param value 待验证字符串 / String to validate.
 * @param maximum 最大 byte 数 / Maximum byte count.
 * @return 是否可安全编码 / Whether it may be safely encoded.
 */
[[nodiscard]] bool is_bounded_nul_free(const std::string_view value, const std::size_t maximum) noexcept {
    return !value.empty() && value.size() <= maximum && value.find('\0') == std::string_view::npos;
}

/**
 * @brief 判断 operator 帧类别是否已知 / Check whether an operator frame kind is known.
 * @param raw_kind 原始枚举值 / Raw enum value.
 * @return 是否为已知类别 / Whether it is a known kind.
 */
[[nodiscard]] bool is_known_message_kind(const std::uint16_t raw_kind) noexcept {
    return raw_kind >= static_cast<std::uint16_t>(OperatorMessageKind::status_request) &&
           raw_kind <= static_cast<std::uint16_t>(OperatorMessageKind::error_response);
}

/**
 * @brief 判断 operator 错误类别是否已知 / Check whether an operator error category is known.
 * @param raw_code 原始错误码 / Raw error code.
 * @return 是否为已知错误码 / Whether it is known.
 */
[[nodiscard]] bool is_known_error_code(const std::uint8_t raw_code) noexcept {
    return raw_code >= static_cast<std::uint8_t>(OperatorErrorCode::invalid_request) &&
           raw_code <= static_cast<std::uint8_t>(OperatorErrorCode::protocol_violation);
}

/**
 * @brief 判断 workspace entry kind 枚举是否已知 / Check whether a workspace-entry-kind enum is known.
 * @param raw_value 原始枚举值 / Raw enum value.
 * @return 是否为已知值 / Whether it is known.
 */
[[nodiscard]] bool is_known_entry_kind(const std::uint8_t raw_value) noexcept {
    return raw_value >= static_cast<std::uint8_t>(domain::WorkspaceEntryKind::regular_file) &&
           raw_value <= static_cast<std::uint8_t>(domain::WorkspaceEntryKind::symbolic_link);
}

/**
 * @brief 将过大的字节序列拒绝为 operator 帧错误 / Reject an oversized byte sequence as an operator-frame error.
 * @param bytes 待检查 bytes / Bytes to inspect.
 * @return 成功或 frame-too-large 错误 / Success or a frame-too-large error.
 */
[[nodiscard]] Result<void> require_operator_payload_bound(const std::span<const std::byte> bytes) {
    if (bytes.size() > kOperatorMaxFrameBytes - kOperatorFrameHeaderBytes) {
        return std::unexpected(make_error(ErrorCode::frame_too_large, "operator payload exceeds hard frame quota"));
    }
    return {};
}

/**
 * @brief 关闭意外接收的 SCM_RIGHTS 文件描述符 / Close unexpectedly received SCM_RIGHTS descriptors.
 * @param message 带 ancillary data 的接收消息 / Received message carrying ancillary data.
 */
void close_received_rights(msghdr& message) noexcept {
    for (cmsghdr* header = CMSG_FIRSTHDR(&message); header != nullptr; header = CMSG_NXTHDR(&message, header)) {
        if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_RIGHTS && header->cmsg_len >= CMSG_LEN(0)) {
            /** @brief ancillary 描述符 payload byte 数 / Ancillary descriptor payload bytes. */
            const std::size_t payload_bytes = header->cmsg_len - CMSG_LEN(0);
            if (payload_bytes % sizeof(int) == 0U) {
                const auto* descriptors = reinterpret_cast<const int*>(CMSG_DATA(header));
                for (std::size_t index = 0U; index < payload_bytes / sizeof(int); ++index) {
                    if (descriptors[index] >= 0) {
                        static_cast<void>(close(descriptors[index]));
                    }
                }
            }
        }
    }
}

}  // namespace

Result<std::vector<std::byte>> encode_status_request(const StatusRequest& request) {
    if (!is_bounded_nul_free(request.runtime_key, kOperatorRuntimeTextBytes)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid operator runtime status request"));
    }
    Writer writer;
    writer.string(request.runtime_key);
    std::vector<std::byte> payload = writer.take();
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    return payload;
}

Result<StatusRequest> decode_status_request(const std::span<const std::byte> payload) {
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    Reader reader(payload);
    const auto runtime_key = reader.string(kOperatorRuntimeTextBytes);
    if (!runtime_key || !reader.finished() || !is_bounded_nul_free(*runtime_key, kOperatorRuntimeTextBytes)) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator runtime status request"));
    }
    return StatusRequest{.runtime_key = *runtime_key};
}

Result<std::vector<std::byte>> encode_list_request(const ListRequest& request) {
    if (!is_bounded_nul_free(request.runtime_key, kOperatorRuntimeTextBytes) ||
        !is_bounded_nul_free(request.path, kOperatorWorkspacePathBytes)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid operator workspace list request"));
    }
    Writer writer;
    writer.string(request.runtime_key);
    writer.string(request.path);
    std::vector<std::byte> payload = writer.take();
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    return payload;
}

Result<ListRequest> decode_list_request(const std::span<const std::byte> payload) {
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    Reader reader(payload);
    const auto runtime_key = reader.string(kOperatorRuntimeTextBytes);
    const auto path = reader.string(kOperatorWorkspacePathBytes);
    if (!runtime_key || !path || !reader.finished() || !is_bounded_nul_free(*runtime_key, kOperatorRuntimeTextBytes) ||
        !is_bounded_nul_free(*path, kOperatorWorkspacePathBytes)) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator workspace list request"));
    }
    return ListRequest{.runtime_key = *runtime_key, .path = *path};
}

Result<std::vector<std::byte>> encode_status_response(const StatusResponse& response) {
    Writer writer;
    writer.string(response.status.runtime().value());
    writer.u8(static_cast<std::uint8_t>(response.status.persistence()));
    writer.u8(static_cast<std::uint8_t>(response.status.activity()));
    writer.u8(response.status.quota().has_value() ? 1U : 0U);
    if (response.status.quota().has_value()) {
        writer.u64(response.status.quota()->used_bytes());
        writer.u64(response.status.quota()->hard_bytes());
        writer.u64(response.status.quota()->used_inodes());
        writer.u64(response.status.quota()->hard_inodes());
    }
    std::vector<std::byte> payload = writer.take();
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    return payload;
}

Result<StatusResponse> decode_status_response(const std::span<const std::byte> payload) {
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    Reader reader(payload);
    const auto runtime_key = reader.string(kOperatorRuntimeTextBytes);
    const auto raw_persistence = reader.u8();
    const auto raw_activity = reader.u8();
    const auto quota_present = reader.u8();
    if (!runtime_key || !raw_persistence || !raw_activity || !quota_present ||
        (*quota_present != 0U && *quota_present != 1U)) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator status response"));
    }
    const auto runtime = domain::RuntimeId::parse(*runtime_key);
    if (!runtime) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "operator status response runtime is invalid"));
    }
    std::optional<domain::WorkspaceQuotaUsage> quota;
    if (*quota_present == 1U) {
        const auto used_bytes = reader.u64();
        const auto hard_bytes = reader.u64();
        const auto used_inodes = reader.u64();
        const auto hard_inodes = reader.u64();
        if (!used_bytes || !hard_bytes || !used_inodes || !hard_inodes) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated operator quota response"));
        }
        if (*hard_bytes == 0U || *hard_inodes == 0U) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "operator quota limits must be non-zero"));
        }
        const auto created_quota = domain::WorkspaceQuotaUsage::create(
            *used_bytes,
            *hard_bytes,
            *used_inodes,
            *hard_inodes);
        if (!created_quota) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "operator quota violates the domain contract"));
        }
        quota = *created_quota;
    }
    if (!reader.finished()) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "operator status response violates quota invariant"));
    }
    const auto status = domain::OperatorWorkspaceStatus::create(
        *runtime,
        static_cast<domain::WorkspacePersistence>(*raw_persistence),
        static_cast<domain::WorkspaceActivity>(*raw_activity),
        std::move(quota));
    if (!status) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "operator status response violates the domain contract"));
    }
    return StatusResponse{.status = *status};
}

Result<std::vector<std::byte>> encode_list_response(const ListResponse& response) {
    if (response.listing.entries.size() > domain::kOperatorWorkspaceListingLimit) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "operator listing exceeds entry cap"));
    }
    Writer writer;
    writer.string(response.listing.path.value());
    writer.u8(response.listing.truncated ? 1U : 0U);
    writer.u32(static_cast<std::uint32_t>(response.listing.entries.size()));
    for (const domain::WorkspaceEntry& entry : response.listing.entries) {
        writer.string(entry.encoded_name());
        writer.u8(static_cast<std::uint8_t>(entry.kind()));
        writer.u64(entry.size_bytes());
    }
    std::vector<std::byte> payload = writer.take();
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    return payload;
}

Result<ListResponse> decode_list_response(const std::span<const std::byte> payload) {
    if (const auto bounded = require_operator_payload_bound(payload); !bounded) {
        return std::unexpected(bounded.error());
    }
    Reader reader(payload);
    const auto path_text = reader.string(kOperatorWorkspacePathBytes);
    const auto truncated = reader.u8();
    const auto entry_count = reader.u32();
    if (!path_text || !truncated || !entry_count || (*truncated != 0U && *truncated != 1U) ||
        *entry_count > domain::kOperatorWorkspaceListingLimit) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator list response header"));
    }
    const auto path = domain::OperatorWorkspacePath::parse(*path_text);
    if (!path) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "operator list response path is invalid"));
    }
    std::vector<domain::WorkspaceEntry> entries;
    entries.reserve(*entry_count);
    for (std::uint32_t index = 0U; index < *entry_count; ++index) {
        const auto encoded_name = reader.string(kOperatorEncodedFilenameBytes);
        const auto raw_kind = reader.u8();
        const auto size_bytes = reader.u64();
        if (!encoded_name || !raw_kind || !size_bytes || !is_known_entry_kind(*raw_kind)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator list response entry"));
        }
        const auto entry = domain::WorkspaceEntry::create(
            *encoded_name,
            static_cast<domain::WorkspaceEntryKind>(*raw_kind),
            *size_bytes);
        if (!entry) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "operator list response entry encoding is unsafe"));
        }
        entries.push_back(*entry);
    }
    if (!reader.finished()) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "operator list response has trailing bytes"));
    }
    return ListResponse{.listing = domain::WorkspaceListing{
                            .path = *path,
                            .entries = std::move(entries),
                            .truncated = *truncated == 1U,
                        }};
}

Result<std::vector<std::byte>> encode_error_response(const ErrorResponse& response) {
    if (!is_known_error_code(static_cast<std::uint8_t>(response.code))) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid operator error code"));
    }
    Writer writer;
    writer.u8(static_cast<std::uint8_t>(response.code));
    return writer.take();
}

Result<ErrorResponse> decode_error_response(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto raw_code = reader.u8();
    if (!raw_code || !reader.finished() || !is_known_error_code(*raw_code)) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator error response"));
    }
    return ErrorResponse{.code = static_cast<OperatorErrorCode>(*raw_code)};
}

Result<std::vector<std::byte>> encode_operator_frame(
    const OperatorMessageKind kind,
    const std::span<const std::byte> payload) {
    if (!is_known_message_kind(static_cast<std::uint16_t>(kind)) ||
        payload.size() > kOperatorMaxFrameBytes - kOperatorFrameHeaderBytes) {
        return std::unexpected(make_error(ErrorCode::frame_too_large, "invalid operator frame kind or payload length"));
    }
    Writer writer;
    writer.u32(kOperatorProtocolMagic);
    writer.u16(kOperatorProtocolVersion);
    writer.u16(static_cast<std::uint16_t>(kind));
    writer.u32(static_cast<std::uint32_t>(payload.size()));
    std::vector<std::byte> frame = writer.take();
    frame.insert(frame.end(), payload.begin(), payload.end());
    return frame;
}

Result<OperatorFrame> decode_operator_frame(const std::span<const std::byte> wire) {
    if (wire.size() > kOperatorMaxFrameBytes) {
        return std::unexpected(make_error(ErrorCode::frame_too_large, "operator frame exceeds hard quota"));
    }
    if (wire.size() < kOperatorFrameHeaderBytes) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated operator frame header"));
    }
    Reader reader(wire.first(kOperatorFrameHeaderBytes));
    const auto magic = reader.u32();
    const auto version = reader.u16();
    const auto raw_kind = reader.u16();
    const auto payload_length = reader.u32();
    if (!magic || !version || !raw_kind || !payload_length) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated operator frame header"));
    }
    if (*magic != kOperatorProtocolMagic) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "unexpected operator protocol magic"));
    }
    if (*version != kOperatorProtocolVersion) {
        return std::unexpected(make_error(ErrorCode::unsupported_version, "unsupported operator protocol version"));
    }
    if (!is_known_message_kind(*raw_kind) || *payload_length != wire.size() - kOperatorFrameHeaderBytes) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid operator frame kind or length"));
    }
    OperatorFrame frame{.kind = static_cast<OperatorMessageKind>(*raw_kind), .payload = {}};
    frame.payload.insert(
        frame.payload.end(),
        wire.begin() + static_cast<std::ptrdiff_t>(kOperatorFrameHeaderBytes),
        wire.end());
    return frame;
}

Result<void> send_operator_frame(const int fd, const std::span<const std::byte> frame) {
    if (fd < 0 || frame.empty() || frame.size() > kOperatorMaxFrameBytes) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid operator send frame arguments"));
    }
    /** @brief 实际写入 byte 数 / Actual number of bytes written. */
    ssize_t written{0};
    do {
        written = send(fd, frame.data(), frame.size(), MSG_NOSIGNAL);
    } while (written < 0 && errno == EINTR);
    if (written < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "send operator SOCK_SEQPACKET frame"));
    }
    if (static_cast<std::size_t>(written) != frame.size()) {
        return std::unexpected(make_error(ErrorCode::io_failure, "short operator SOCK_SEQPACKET send"));
    }
    return {};
}

Result<std::vector<std::byte>> receive_operator_frame(const int fd) {
    if (fd < 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid operator receive fd"));
    }
    std::vector<std::byte> buffer(kOperatorMaxFrameBytes);
    iovec vector{.iov_base = buffer.data(), .iov_len = buffer.size()};
    std::array<std::byte, CMSG_SPACE(sizeof(int) * 4U)> ancillary{};
    msghdr message{};
    message.msg_iov = &vector;
    message.msg_iovlen = 1U;
    message.msg_control = ancillary.data();
    message.msg_controllen = ancillary.size();
    /** @brief 实际接收 byte 数 / Actual number of bytes received. */
    ssize_t received{0};
    do {
        received = recvmsg(fd, &message, MSG_TRUNC | MSG_CMSG_CLOEXEC);
    } while (received < 0 && errno == EINTR);
    if (received < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "receive operator SOCK_SEQPACKET frame"));
    }
    if (received == 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, "operator SOCK_SEQPACKET peer closed"));
    }
    const bool has_ancillary = CMSG_FIRSTHDR(&message) != nullptr;
    close_received_rights(message);
    if ((message.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) != 0 || static_cast<std::size_t>(received) > buffer.size()) {
        return std::unexpected(make_error(ErrorCode::frame_too_large, "truncated operator SOCK_SEQPACKET frame"));
    }
    if (has_ancillary) {
        return std::unexpected(make_error(ErrorCode::protocol_violation, "SCM ancillary data is forbidden on operator frames"));
    }
    buffer.resize(static_cast<std::size_t>(received));
    return buffer;
}

}  // namespace wspctl::operator_protocol
