#include "wspctl/infrastructure/protocol.hpp"

#include <openssl/sha.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstring>
#include <limits>
#include <sys/socket.h>
#include <unistd.h>

namespace wspctl {
namespace {

/** @brief wire 编码器 / Wire encoder. */
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
        for (unsigned int index = 0; index < 4U; ++index) {
            u8(static_cast<std::uint8_t>((value >> (index * 8U)) & 0xffU));
        }
    }

    /** @brief 写入 little-endian 64 位整数 / Write a little-endian 64-bit integer. */
    void u64(const std::uint64_t value) {
        for (unsigned int index = 0; index < 8U; ++index) {
            u8(static_cast<std::uint8_t>((value >> (index * 8U)) & 0xffU));
        }
    }

    /** @brief 写入长度前缀字符串 / Write a length-prefixed string. */
    void string(const std::string_view value) {
        u32(static_cast<std::uint32_t>(value.size()));
        for (const char character : value) {
            bytes_.push_back(static_cast<std::byte>(static_cast<unsigned char>(character)));
        }
    }

    /**
     * @brief 写入长度前缀的原始 bytes / Write length-prefixed raw bytes.
     * @param value 待写入的 bytes / Bytes to write.
     * @return None / None.
     */
    void bytes(const std::span<const std::byte> value) {
        u32(static_cast<std::uint32_t>(value.size()));
        bytes_.insert(bytes_.end(), value.begin(), value.end());
    }

    /** @brief 取得编码后的字节 / Take encoded bytes. */
    [[nodiscard]] std::vector<std::byte> take() { return std::move(bytes_); }

private:
    /** @brief 累积的 wire bytes / Accumulated wire bytes. */
    std::vector<std::byte> bytes_;
};

/** @brief 有界 wire 解码器 / Bounded wire decoder. */
class Reader final {
public:
    /**
     * @brief 建立 reader / Construct a reader.
     * @param bytes 待读取 bytes / Bytes to read.
     */
    explicit Reader(const std::span<const std::byte> bytes) : bytes_(bytes) {}

    /** @brief 读取无符号 8 位值 / Read an unsigned 8-bit value. */
    [[nodiscard]] Result<std::uint8_t> u8() {
        if (!has(1U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated u8"));
        }
        return std::to_integer<std::uint8_t>(bytes_[offset_++]);
    }

    /** @brief 读取 little-endian 16 位值 / Read a little-endian 16-bit value. */
    [[nodiscard]] Result<std::uint16_t> u16() {
        if (!has(2U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated u16"));
        }
        std::uint16_t value = 0;
        for (unsigned int index = 0; index < 2U; ++index) {
            /** @brief 当前 little-endian byte / Current little-endian byte. */
            const std::uint16_t byte =
                static_cast<std::uint16_t>(std::to_integer<std::uint8_t>(bytes_[offset_++]));
            value = static_cast<std::uint16_t>(value |
                                               static_cast<std::uint16_t>(byte << (index * 8U)));
        }
        return value;
    }

    /** @brief 读取 little-endian 32 位值 / Read a little-endian 32-bit value. */
    [[nodiscard]] Result<std::uint32_t> u32() {
        if (!has(4U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated u32"));
        }
        std::uint32_t value = 0;
        for (unsigned int index = 0; index < 4U; ++index) {
            value |= static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(bytes_[offset_++]))
                     << (index * 8U);
        }
        return value;
    }

    /** @brief 读取 little-endian 64 位值 / Read a little-endian 64-bit value. */
    [[nodiscard]] Result<std::uint64_t> u64() {
        if (!has(8U)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated u64"));
        }
        std::uint64_t value = 0;
        for (unsigned int index = 0; index < 8U; ++index) {
            value |= static_cast<std::uint64_t>(std::to_integer<std::uint8_t>(bytes_[offset_++]))
                     << (index * 8U);
        }
        return value;
    }

    /**
     * @brief 读取有配额的字符串 / Read a quota-bounded string.
     * @param maximum 最大长度 / Maximum length.
     * @return 字符串或格式错误 / String or format error.
     */
    [[nodiscard]] Result<std::string> string(const std::size_t maximum) {
        const auto length = u32();
        if (!length.has_value()) {
            return std::unexpected(length.error());
        }
        const auto size = static_cast<std::size_t>(*length);
        if (size > maximum || !has(size)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid string length"));
        }
        std::string value;
        value.reserve(size);
        for (std::size_t index = 0; index < size; ++index) {
            value.push_back(static_cast<char>(std::to_integer<unsigned char>(bytes_[offset_++])));
        }
        return value;
    }

    /**
     * @brief 读取有配额的原始 bytes / Read quota-bounded raw bytes.
     * @param maximum 最大字节数 / Maximum byte count.
     * @return bytes 或格式错误 / Bytes or a format error.
     */
    [[nodiscard]] Result<std::vector<std::byte>> bytes(const std::size_t maximum) {
        const auto length = u32();
        if (!length.has_value()) {
            return std::unexpected(length.error());
        }
        const auto size = static_cast<std::size_t>(*length);
        if (size > maximum || !has(size)) {
            return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid byte length"));
        }
        std::vector<std::byte> value;
        value.insert(value.end(), bytes_.begin() + static_cast<std::ptrdiff_t>(offset_),
                     bytes_.begin() + static_cast<std::ptrdiff_t>(offset_ + size));
        offset_ += size;
        return value;
    }

    /** @brief 检查是否恰好读完 / Check whether all bytes were consumed. */
    [[nodiscard]] bool finished() const noexcept { return offset_ == bytes_.size(); }

private:
    /** @brief 是否还有 n 个字节 / Whether n bytes remain. */
    [[nodiscard]] bool has(const std::size_t count) const noexcept {
        return count <= bytes_.size() - offset_;
    }

    /** @brief 输入 bytes / Input bytes. */
    std::span<const std::byte> bytes_;
    /** @brief 读取偏移 / Read offset. */
    std::size_t offset_{0};
};

/** @brief 将十六进制 byte 输出为小写字符 / Render one byte as lowercase hex. */
[[nodiscard]] char hex_digit(const unsigned int value) {
    constexpr std::string_view kDigits{"0123456789abcdef"};
    return kDigits[value & 0x0fU];
}

/** @brief 判断合法十六进制摘要 / Check a valid lowercase hexadecimal digest. */
[[nodiscard]] bool is_sha256(const std::string_view value) {
    return value.size() == SHA256_DIGEST_LENGTH * 2U &&
           std::all_of(value.begin(), value.end(), [](const char character) {
               return (character >= '0' && character <= '9') ||
                      (character >= 'a' && character <= 'f');
           });
}

/** @brief 判断安全 runtime/invocation 标识 / Check a safe runtime/invocation identifier. */
[[nodiscard]] bool is_safe_identifier(const std::string_view value) {
    if (value.empty() || value.size() > 128U) {
        return false;
    }
    return std::all_of(value.begin(), value.end(), [](const unsigned char character) {
        return (character >= static_cast<unsigned char>('a') &&
                character <= static_cast<unsigned char>('z')) ||
               (character >= static_cast<unsigned char>('A') &&
                character <= static_cast<unsigned char>('Z')) ||
               (character >= static_cast<unsigned char>('0') &&
                character <= static_cast<unsigned char>('9')) ||
               character == static_cast<unsigned char>('_') ||
               character == static_cast<unsigned char>('-') ||
               character == static_cast<unsigned char>('.') ||
               character == static_cast<unsigned char>(':');
    });
}

/** @brief 判断受限文件 opaque ID / Check a constrained file opaque ID. */
[[nodiscard]] bool is_safe_payload_opaque_id(const std::string_view value) {
    if (value.empty() || value.size() > kMaxFileOpaqueIdBytes) {
        return false;
    }
    const auto is_alphanumeric = [](const unsigned char character) noexcept {
        return (character >= static_cast<unsigned char>('a') &&
                character <= static_cast<unsigned char>('z')) ||
               (character >= static_cast<unsigned char>('A') &&
                character <= static_cast<unsigned char>('Z')) ||
               (character >= static_cast<unsigned char>('0') &&
                character <= static_cast<unsigned char>('9'));
    };
    if (!is_alphanumeric(static_cast<unsigned char>(value.front()))) {
        return false;
    }
    return std::all_of(value.begin() + 1, value.end(), [&](const unsigned char character) {
        return is_alphanumeric(character) || character == static_cast<unsigned char>('_') ||
               character == static_cast<unsigned char>('-');
    });
}

/** @brief 判断固定 uploads payload 路径 / Check a fixed uploads payload path. */
[[nodiscard]] bool is_safe_payload_runtime_path(const std::string_view path) {
    constexpr std::string_view kPrefix{"/workspace/uploads/"};
    constexpr std::string_view kSuffix{"/payload"};
    if (!path.starts_with(kPrefix) || !path.ends_with(kSuffix) ||
        path.size() <= kPrefix.size() + kSuffix.size()) {
        return false;
    }
    const std::string_view opaque_id =
        path.substr(kPrefix.size(), path.size() - kPrefix.size() - kSuffix.size());
    return opaque_id.find('/') == std::string_view::npos && is_safe_payload_opaque_id(opaque_id);
}

/** @brief 判断 runtime 内 cwd 是否安全 / Check that an in-runtime cwd is safe. */
[[nodiscard]] bool is_safe_workspace_cwd(const std::string_view cwd) {
    if (cwd == "/workspace") {
        return true;
    }
    if (!cwd.starts_with("/workspace/") || cwd.size() > 4096U ||
        cwd.find('\0') != std::string_view::npos || cwd.find("//") != std::string_view::npos) {
        return false;
    }
    std::size_t begin = 1U;
    while (begin < cwd.size()) {
        const std::size_t end = cwd.find('/', begin);
        const std::string_view component =
            cwd.substr(begin, end == std::string_view::npos ? cwd.size() - begin : end - begin);
        if (component.empty() || component == "." || component == "..") {
            return false;
        }
        if (end == std::string_view::npos) {
            break;
        }
        begin = end + 1U;
    }
    return true;
}

/** @brief 判断相对 workspace 普通文件路径是否安全 / Check a safe workspace-relative regular-file path. */
[[nodiscard]] bool is_safe_workspace_file_path(const std::string_view path) {
    if (path.empty() || path == "." || path.size() > kMaxWorkspaceFilePathBytes ||
        path.front() == '/' || path.back() == '/' || path.find('\0') != std::string_view::npos) {
        return false;
    }
    return is_safe_workspace_cwd("/workspace/" + std::string(path));
}

/** @brief 编码不含外部语义 hash 的规范请求 / Encode canonical request without caller semantic hash.
 */
[[nodiscard]] std::vector<std::byte> encode_canonical_request(const ExecuteRequest& request) {
    Writer writer;
    writer.string(request.runtime_key);
    // activation_id selects a live supervisor/overlay session, not the durable command intent.
    // Excluding it lets a crashed Bot replay the same invocation after a new activation attaches.
    writer.string(request.request_id);
    writer.u32(static_cast<std::uint32_t>(request.argv.size()));
    for (const std::string& argument : request.argv) {
        writer.string(argument);
    }
    writer.string(request.stdin_data);
    writer.string(request.cwd);
    writer.u64(static_cast<std::uint64_t>(request.timeout.count()));
    writer.u64(static_cast<std::uint64_t>(request.output_limit));
    return writer.take();
}

/**
 * @brief 编码不含外部语义 hash/activation 的规范文件元数据 / Encode canonical file metadata
 * excluding caller semantic hash and activation.
 * @param runtime_key 持久 runtime 标识 / Persistent runtime key.
 * @param request_id 稳定文件调用标识 / Stable file invocation ID.
 * @param opaque_id 受限 uploads capability / Constrained uploads capability.
 * @param byte_size 完整文件字节数 / Complete file byte count.
 * @param sha256 完整内容摘要 / Complete-content digest.
 * @return 用于 SHA-256 的 canonical bytes / Canonical bytes used for SHA-256.
 */
[[nodiscard]] std::vector<std::byte> encode_canonical_payload_metadata(
    const std::string_view runtime_key, const std::string_view request_id,
    const std::string_view opaque_id, const std::size_t byte_size, const std::string_view sha256) {
    Writer writer;
    writer.string(runtime_key);
    writer.string(request_id);
    writer.string(opaque_id);
    writer.u64(static_cast<std::uint64_t>(byte_size));
    writer.string(sha256);
    return writer.take();
}

/** @brief 将任意 bytes 的 SHA-256 渲染为小写十六进制 / Render the SHA-256 of arbitrary bytes as
 * lowercase hexadecimal. */
[[nodiscard]] std::string sha256_hex(const std::span<const std::byte> bytes) {
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(bytes.data()), bytes.size(), digest.data());
    std::string rendered;
    rendered.reserve(digest.size() * 2U);
    for (const unsigned char value : digest) {
        rendered.push_back(hex_digit(value >> 4U));
        rendered.push_back(hex_digit(value));
    }
    return rendered;
}

/** @brief 校验字符串不含 NUL / Check that a string has no NUL byte. */
[[nodiscard]] bool has_no_nul(const std::string_view value) {
    return value.find('\0') == std::string_view::npos;
}

/** @brief 校验无 NUL 的严格 UTF-8 scalar sequence / Validate NUL-free strict UTF-8 scalar
 * sequences. */
[[nodiscard]] bool is_nul_free_utf8(const std::string_view value) noexcept {
    for (std::size_t index = 0U; index < value.size();) {
        const unsigned char first = static_cast<unsigned char>(value[index]);
        if (first == 0U) {
            return false;
        }
        if (first <= 0x7fU) {
            ++index;
            continue;
        }
        std::size_t width = 0U;
        if (first >= 0xc2U && first <= 0xdfU) {
            width = 2U;
        } else if (first >= 0xe0U && first <= 0xefU) {
            width = 3U;
        } else if (first >= 0xf0U && first <= 0xf4U) {
            width = 4U;
        } else {
            return false;
        }
        if (value.size() - index < width) {
            return false;
        }
        const unsigned char second = static_cast<unsigned char>(value[index + 1U]);
        if ((first == 0xe0U && second < 0xa0U) || (first == 0xedU && second > 0x9fU) ||
            (first == 0xf0U && second < 0x90U) || (first == 0xf4U && second > 0x8fU)) {
            return false;
        }
        for (std::size_t continuation = 1U; continuation < width; ++continuation) {
            const unsigned char byte = static_cast<unsigned char>(value[index + continuation]);
            if (byte < 0x80U || byte > 0xbfU) {
                return false;
            }
        }
        index += width;
    }
    return true;
}

/** @brief 校验 MessageKind 枚举值 / Validate a MessageKind enum value. */
[[nodiscard]] bool is_known_kind(const std::uint16_t raw_kind) {
    return raw_kind >= static_cast<std::uint16_t>(MessageKind::execute) &&
           raw_kind <= static_cast<std::uint16_t>(MessageKind::fetch_file_chunk);
}

/** @brief 校验 execution result 可安全编码 / Validate an execution result before encoding. */
[[nodiscard]] Result<void> validate_execution_result(const ExecutionResult& result) {
    if (!is_safe_identifier(result.request_id)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid result request_id"));
    }
    if (result.exit_code.has_value() && (*result.exit_code < 0 || *result.exit_code > 255)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid exit code"));
    }
    if (!is_nul_free_utf8(result.stdout_data) || !is_nul_free_utf8(result.stderr_data) ||
        result.stdout_data.size() + result.stderr_data.size() > kMaxOutputBytes) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid result output"));
    }
    return {};
}

/** @brief 校验 payload ACK / Validate a payload acknowledgement. */
[[nodiscard]] Result<void> validate_payload_ack(const PayloadAck& acknowledgement) {
    if (!is_safe_identifier(acknowledgement.request_id) ||
        (acknowledgement.stage != PayloadAckStage::begun &&
         acknowledgement.stage != PayloadAckStage::chunk_written &&
         acknowledgement.stage != PayloadAckStage::sealed &&
         acknowledgement.stage != PayloadAckStage::aborted) ||
        acknowledgement.received_bytes > kMaxAddFileBytes) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid payload acknowledgement"));
    }
    return {};
}

} // namespace

Result<void> validate_execute_request(const ExecuteRequest& request) {
    if (!is_safe_identifier(request.runtime_key) || !is_safe_identifier(request.activation_id) ||
        !is_safe_identifier(request.request_id)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                          "invalid runtime, activation, or request identifier"));
    }
    if (!is_sha256(request.request_hash)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "request_hash must be lowercase SHA-256 hex"));
    }
    if (request.argv.empty() || request.argv.size() > kMaxArgvEntries ||
        request.argv.front().empty() || request.argv.front().front() == '-') {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid argv"));
    }
    std::size_t argv_bytes = 0;
    for (const std::string& argument : request.argv) {
        if (argument.empty() || argument.size() > 16U * 1024U || !has_no_nul(argument)) {
            return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid argv element"));
        }
        argv_bytes += argument.size();
        if (argv_bytes > 32U * 1024U) {
            return std::unexpected(make_error(ErrorCode::frame_too_large, "argv exceeds quota"));
        }
    }
    if (request.stdin_data.size() > kMaxStdinBytes || !has_no_nul(request.stdin_data)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid stdin"));
    }
    if (!is_safe_workspace_cwd(request.cwd)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                          "cwd must be a normalized path below /workspace"));
    }
    if (request.timeout.count() <= 0 || request.timeout > std::chrono::minutes(15)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "timeout is outside 1ms..15min"));
    }
    if (request.output_limit == 0U || request.output_limit > kMaxOutputBytes) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "output_limit is outside quota"));
    }
    return {};
}

Result<void> validate_runtime_status_request(const RuntimeStatusRequest& request) {
    if (!is_safe_identifier(request.runtime_key) || !is_safe_identifier(request.activation_id)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "invalid runtime or activation identifier for runtime status"));
    }
    return {};
}

Result<void> validate_runtime_status_result(const RuntimeStatusResult& result) {
    const auto runtime = domain::RuntimeId::parse(result.runtime_key);
    if (!runtime) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, runtime.error().message));
    }
    std::optional<domain::ActivationId> active_activation;
    if (result.active_activation_id.has_value()) {
        const auto parsed = domain::ActivationId::parse(*result.active_activation_id);
        if (!parsed) {
            return std::unexpected(make_error(ErrorCode::invalid_argument, parsed.error().message));
        }
        active_activation = *parsed;
    }
    if (const auto snapshot =
            domain::RuntimeSnapshot::create(*runtime, result.state, std::move(active_activation));
        !snapshot) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, snapshot.error().message));
    }
    if (result.idle_ttl.count() <= 0 ||
        result.idle_ttl.count() > std::numeric_limits<std::int64_t>::max() ||
        (result.idle_for.has_value() && result.idle_for->count() < 0) ||
        (result.idle_for.has_value() && result.state != domain::RuntimeState::ready)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid runtime status timing"));
    }
    if (result.supervisor_alive && (result.state == domain::RuntimeState::dormant ||
                                    result.state == domain::RuntimeState::failed)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                          "inactive runtime cannot report a live supervisor"));
    }
    if (result.handle_activation_matches && !result.active_activation_id.has_value()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument,
                                          "activation match requires an active activation"));
    }
    return {};
}

Result<void> validate_payload_begin_request(const PayloadBeginRequest& request) {
    if (!is_safe_identifier(request.runtime_key) || !is_safe_identifier(request.activation_id) ||
        !is_safe_identifier(request.request_id) || !is_safe_payload_opaque_id(request.opaque_id)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "invalid runtime, activation, request, or opaque file identifier"));
    }
    if (!is_sha256(request.request_hash) || !is_sha256(request.sha256)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "file request_hash and sha256 must be lowercase SHA-256 hex"));
    }
    if (request.byte_size > kMaxAddFileBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file byte size exceeds ingress quota"));
    }
    return {};
}

Result<void> validate_payload_replay_request(const PayloadReplayRequest& request) {
    if (!is_safe_identifier(request.runtime_key) || !is_safe_identifier(request.request_id) ||
        !is_safe_payload_opaque_id(request.opaque_id)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "invalid runtime, request, or opaque file identifier for read-only replay"));
    }
    if (!is_sha256(request.request_hash) || !is_sha256(request.sha256)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument,
                       "file replay request_hash and sha256 must be lowercase SHA-256 hex"));
    }
    if (request.byte_size > kMaxAddFileBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file replay byte size exceeds ingress quota"));
    }
    return {};
}

Result<void> validate_payload_chunk(const PayloadChunk& chunk) {
    if (!is_safe_identifier(chunk.request_id) || chunk.bytes.empty() ||
        chunk.bytes.size() > kMaxAddFileChunkBytes) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid file chunk"));
    }
    return {};
}

Result<void> validate_payload_control_request(const PayloadControlRequest& request) {
    if (!is_safe_identifier(request.request_id)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid file control request"));
    }
    return {};
}

Result<void> validate_payload_result(const PayloadResult& result) {
    if (!is_safe_identifier(result.request_id) || !is_safe_payload_runtime_path(result.path) ||
        result.byte_size > kMaxAddFileBytes || !is_sha256(result.sha256)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid file ingress receipt"));
    }
    return {};
}

Result<void> validate_fetch_file_request(const FetchFileRequest& request) {
    if (!is_safe_identifier(request.runtime_key) || !is_safe_workspace_file_path(request.path)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid workspace file-fetch identity or path"));
    }
    if (request.max_bytes == 0U || request.max_bytes > kMaxFetchFileBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "workspace file-fetch limit is out of range"));
    }
    return {};
}

Result<void> validate_fetch_file_result(const FetchFileResult& result) {
    if (!is_safe_workspace_file_path(result.path) || result.byte_size > kMaxFetchFileBytes ||
        !is_sha256(result.sha256)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid workspace file-fetch result"));
    }
    return {};
}

Result<void> validate_fetch_file_chunk(const FetchFileChunk& chunk) {
    if (chunk.bytes.empty() || chunk.bytes.size() > kMaxAddFileChunkBytes) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid workspace file-fetch chunk"));
    }
    return {};
}

std::string canonical_request_hash(const ExecuteRequest& request) {
    const std::vector<std::byte> canonical = encode_canonical_request(request);
    return sha256_hex(canonical);
}

std::string canonical_payload_hash(const PayloadBeginRequest& request) {
    const std::vector<std::byte> canonical =
        encode_canonical_payload_metadata(request.runtime_key, request.request_id,
                                          request.opaque_id, request.byte_size, request.sha256);
    return sha256_hex(canonical);
}

std::string canonical_payload_hash(const PayloadReplayRequest& request) {
    const std::vector<std::byte> canonical =
        encode_canonical_payload_metadata(request.runtime_key, request.request_id,
                                          request.opaque_id, request.byte_size, request.sha256);
    return sha256_hex(canonical);
}

Result<std::vector<std::byte>> encode_execute_request(const ExecuteRequest& request) {
    if (const auto valid = validate_execute_request(request); !valid.has_value()) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(request.runtime_key);
    writer.string(request.activation_id);
    writer.string(request.request_id);
    writer.string(request.request_hash);
    writer.u32(static_cast<std::uint32_t>(request.argv.size()));
    for (const std::string& argument : request.argv) {
        writer.string(argument);
    }
    writer.string(request.stdin_data);
    writer.string(request.cwd);
    writer.u64(static_cast<std::uint64_t>(request.timeout.count()));
    writer.u64(static_cast<std::uint64_t>(request.output_limit));
    std::vector<std::byte> encoded = writer.take();
    if (encoded.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "request payload exceeds frame quota"));
    }
    return encoded;
}

Result<ExecuteRequest> decode_execute_request(const std::span<const std::byte> payload) {
    if (payload.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "request payload exceeds quota"));
    }
    Reader reader(payload);
    ExecuteRequest request;
    const auto runtime_key = reader.string(128U);
    const auto activation_id = reader.string(128U);
    const auto request_id = reader.string(128U);
    const auto request_hash = reader.string(64U);
    const auto argument_count = reader.u32();
    if (!runtime_key || !activation_id || !request_id || !request_hash || !argument_count) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated execute request"));
    }
    if (*argument_count > kMaxArgvEntries) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "argv count exceeds quota"));
    }
    request.runtime_key = *runtime_key;
    request.activation_id = *activation_id;
    request.request_id = *request_id;
    request.request_hash = *request_hash;
    request.argv.reserve(*argument_count);
    for (std::uint32_t index = 0; index < *argument_count; ++index) {
        const auto argument = reader.string(16U * 1024U);
        if (!argument) {
            return std::unexpected(argument.error());
        }
        request.argv.push_back(*argument);
    }
    const auto stdin_data = reader.string(kMaxStdinBytes);
    const auto cwd = reader.string(4096U);
    const auto timeout = reader.u64();
    const auto output_limit = reader.u64();
    if (!stdin_data || !cwd || !timeout || !output_limit || !reader.finished() ||
        *timeout > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()) ||
        *output_limit > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid execute request tail"));
    }
    request.stdin_data = *stdin_data;
    request.cwd = *cwd;
    request.timeout = std::chrono::milliseconds(static_cast<std::int64_t>(*timeout));
    request.output_limit = static_cast<std::size_t>(*output_limit);
    if (const auto valid = validate_execute_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    return request;
}

Result<std::vector<std::byte>> encode_runtime_status_request(const RuntimeStatusRequest& request) {
    if (const auto valid = validate_runtime_status_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(request.runtime_key);
    writer.string(request.activation_id);
    return writer.take();
}

Result<RuntimeStatusRequest>
decode_runtime_status_request(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto runtime_key = reader.string(128U);
    const auto activation_id = reader.string(128U);
    if (!runtime_key || !activation_id || !reader.finished()) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid runtime status request"));
    }
    RuntimeStatusRequest request{
        .runtime_key = *runtime_key,
        .activation_id = *activation_id,
    };
    if (const auto valid = validate_runtime_status_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    return request;
}

Result<std::vector<std::byte>> encode_runtime_status_result(const RuntimeStatusResult& result) {
    if (const auto valid = validate_runtime_status_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(result.runtime_key);
    writer.u8(static_cast<std::uint8_t>(result.state));
    writer.u8(result.active_activation_id.has_value() ? 1U : 0U);
    if (result.active_activation_id.has_value()) {
        writer.string(*result.active_activation_id);
    }
    writer.u8(result.handle_activation_matches ? 1U : 0U);
    writer.u8(result.supervisor_alive ? 1U : 0U);
    writer.u8(result.idle_for.has_value() ? 1U : 0U);
    if (result.idle_for.has_value()) {
        writer.u64(static_cast<std::uint64_t>(result.idle_for->count()));
    }
    writer.u64(static_cast<std::uint64_t>(result.idle_ttl.count()));
    writer.u64(result.borrowed_dispatches);
    writer.u8(result.cleanup_pending ? 1U : 0U);
    return writer.take();
}

Result<RuntimeStatusResult> decode_runtime_status_result(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto runtime_key = reader.string(128U);
    const auto raw_state = reader.u8();
    const auto has_active_activation = reader.u8();
    if (!runtime_key || !raw_state || !has_active_activation || *has_active_activation > 1U ||
        *raw_state > static_cast<std::uint8_t>(domain::RuntimeState::failed)) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid runtime status header"));
    }
    std::optional<std::string> active_activation_id;
    if (*has_active_activation == 1U) {
        const auto active_activation = reader.string(128U);
        if (!active_activation) {
            return std::unexpected(
                make_error(ErrorCode::malformed_frame, "invalid runtime status activation"));
        }
        active_activation_id = *active_activation;
    }
    const auto handle_activation_matches = reader.u8();
    const auto supervisor_alive = reader.u8();
    const auto has_idle_for = reader.u8();
    if (!handle_activation_matches || !supervisor_alive || !has_idle_for ||
        *handle_activation_matches > 1U || *supervisor_alive > 1U || *has_idle_for > 1U) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid runtime status flags"));
    }
    std::optional<std::chrono::milliseconds> idle_for;
    if (*has_idle_for == 1U) {
        const auto idle_for_ms = reader.u64();
        if (!idle_for_ms ||
            *idle_for_ms > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) {
            return std::unexpected(
                make_error(ErrorCode::malformed_frame, "invalid runtime status idle age"));
        }
        idle_for = std::chrono::milliseconds(static_cast<std::int64_t>(*idle_for_ms));
    }
    const auto idle_ttl_ms = reader.u64();
    const auto borrowed_dispatches = reader.u64();
    const auto cleanup_pending = reader.u8();
    if (!idle_ttl_ms || !borrowed_dispatches || !cleanup_pending || *cleanup_pending > 1U ||
        *idle_ttl_ms > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()) ||
        !reader.finished()) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid runtime status tail"));
    }
    RuntimeStatusResult result{
        .runtime_key = *runtime_key,
        .state = static_cast<domain::RuntimeState>(*raw_state),
        .active_activation_id = std::move(active_activation_id),
        .handle_activation_matches = *handle_activation_matches == 1U,
        .supervisor_alive = *supervisor_alive == 1U,
        .idle_for = std::move(idle_for),
        .idle_ttl = std::chrono::milliseconds(static_cast<std::int64_t>(*idle_ttl_ms)),
        .borrowed_dispatches = *borrowed_dispatches,
        .cleanup_pending = *cleanup_pending == 1U,
    };
    if (const auto valid = validate_runtime_status_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    return result;
}

Result<std::vector<std::byte>> encode_payload_begin_request(const PayloadBeginRequest& request) {
    if (const auto valid = validate_payload_begin_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(request.runtime_key);
    writer.string(request.activation_id);
    writer.string(request.request_id);
    writer.string(request.request_hash);
    writer.string(request.opaque_id);
    writer.u64(static_cast<std::uint64_t>(request.byte_size));
    writer.string(request.sha256);
    std::vector<std::byte> encoded = writer.take();
    if (encoded.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file begin payload exceeds frame quota"));
    }
    return encoded;
}

Result<PayloadBeginRequest> decode_payload_begin_request(const std::span<const std::byte> payload) {
    if (payload.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file begin payload exceeds frame quota"));
    }
    Reader reader(payload);
    const auto runtime_key = reader.string(128U);
    const auto activation_id = reader.string(128U);
    const auto request_id = reader.string(128U);
    const auto request_hash = reader.string(64U);
    const auto opaque_id = reader.string(kMaxFileOpaqueIdBytes);
    const auto byte_size = reader.u64();
    const auto sha256 = reader.string(64U);
    if (!runtime_key || !activation_id || !request_id || !request_hash || !opaque_id ||
        !byte_size || !sha256 || !reader.finished() ||
        *byte_size > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid file begin request"));
    }
    PayloadBeginRequest request{
        .runtime_key = *runtime_key,
        .activation_id = *activation_id,
        .request_id = *request_id,
        .request_hash = *request_hash,
        .opaque_id = *opaque_id,
        .byte_size = static_cast<std::size_t>(*byte_size),
        .sha256 = *sha256,
    };
    if (const auto valid = validate_payload_begin_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    return request;
}

Result<std::vector<std::byte>> encode_payload_replay_request(const PayloadReplayRequest& request) {
    if (const auto valid = validate_payload_replay_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(request.runtime_key);
    writer.string(request.request_id);
    writer.string(request.request_hash);
    writer.string(request.opaque_id);
    writer.u64(static_cast<std::uint64_t>(request.byte_size));
    writer.string(request.sha256);
    std::vector<std::byte> encoded = writer.take();
    if (encoded.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file replay payload exceeds frame quota"));
    }
    return encoded;
}

Result<PayloadReplayRequest>
decode_payload_replay_request(const std::span<const std::byte> payload) {
    if (payload.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file replay payload exceeds quota"));
    }
    Reader reader(payload);
    const auto runtime_key = reader.string(128U);
    const auto request_id = reader.string(128U);
    const auto request_hash = reader.string(64U);
    const auto opaque_id = reader.string(kMaxFileOpaqueIdBytes);
    const auto byte_size = reader.u64();
    const auto sha256 = reader.string(64U);
    if (!runtime_key || !request_id || !request_hash || !opaque_id || !byte_size || !sha256 ||
        !reader.finished() ||
        *byte_size > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid file replay request"));
    }
    PayloadReplayRequest request{
        .runtime_key = *runtime_key,
        .request_id = *request_id,
        .request_hash = *request_hash,
        .opaque_id = *opaque_id,
        .byte_size = static_cast<std::size_t>(*byte_size),
        .sha256 = *sha256,
    };
    if (const auto valid = validate_payload_replay_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    return request;
}

Result<std::vector<std::byte>> encode_payload_chunk(const PayloadChunk& chunk) {
    if (const auto valid = validate_payload_chunk(chunk); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(chunk.request_id);
    writer.bytes(chunk.bytes);
    std::vector<std::byte> encoded = writer.take();
    if (encoded.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file chunk payload exceeds frame quota"));
    }
    return encoded;
}

Result<PayloadChunk> decode_payload_chunk(const std::span<const std::byte> payload) {
    if (payload.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "file chunk payload exceeds frame quota"));
    }
    Reader reader(payload);
    const auto request_id = reader.string(128U);
    const auto bytes = reader.bytes(kMaxAddFileChunkBytes);
    if (!request_id || !bytes || !reader.finished()) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid file chunk"));
    }
    PayloadChunk chunk{.request_id = *request_id, .bytes = *bytes};
    if (const auto valid = validate_payload_chunk(chunk); !valid) {
        return std::unexpected(valid.error());
    }
    return chunk;
}

Result<std::vector<std::byte>>
encode_payload_control_request(const PayloadControlRequest& request) {
    if (const auto valid = validate_payload_control_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(request.request_id);
    return writer.take();
}

Result<PayloadControlRequest>
decode_payload_control_request(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto request_id = reader.string(128U);
    if (!request_id || !reader.finished()) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid file control request"));
    }
    PayloadControlRequest request{.request_id = *request_id};
    if (const auto valid = validate_payload_control_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    return request;
}

Result<std::vector<std::byte>> encode_payload_ack(const PayloadAck& acknowledgement) {
    if (const auto valid = validate_payload_ack(acknowledgement); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(acknowledgement.request_id);
    writer.u8(static_cast<std::uint8_t>(acknowledgement.stage));
    writer.u64(static_cast<std::uint64_t>(acknowledgement.received_bytes));
    return writer.take();
}

Result<PayloadAck> decode_payload_ack(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto request_id = reader.string(128U);
    const auto raw_stage = reader.u8();
    const auto received_bytes = reader.u64();
    if (!request_id || !raw_stage || !received_bytes || !reader.finished() ||
        *received_bytes > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid file acknowledgement"));
    }
    PayloadAck acknowledgement{
        .request_id = *request_id,
        .stage = static_cast<PayloadAckStage>(*raw_stage),
        .received_bytes = static_cast<std::size_t>(*received_bytes),
    };
    if (const auto valid = validate_payload_ack(acknowledgement); !valid) {
        return std::unexpected(valid.error());
    }
    return acknowledgement;
}

Result<std::vector<std::byte>> encode_payload_result(const PayloadResult& result) {
    if (const auto valid = validate_payload_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(result.request_id);
    writer.u8(result.replayed ? 1U : 0U);
    writer.string(result.path);
    writer.u64(static_cast<std::uint64_t>(result.byte_size));
    writer.string(result.sha256);
    return writer.take();
}

Result<PayloadResult> decode_payload_result(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto request_id = reader.string(128U);
    const auto replayed = reader.u8();
    const auto path = reader.string(512U);
    const auto byte_size = reader.u64();
    const auto sha256 = reader.string(64U);
    if (!request_id || !replayed || *replayed > 1U || !path || !byte_size || !sha256 ||
        !reader.finished() ||
        *byte_size > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid file ingress receipt"));
    }
    PayloadResult result{
        .request_id = *request_id,
        .replayed = *replayed == 1U,
        .path = *path,
        .byte_size = static_cast<std::size_t>(*byte_size),
        .sha256 = *sha256,
    };
    if (const auto valid = validate_payload_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    return result;
}

Result<std::vector<std::byte>> encode_fetch_file_request(const FetchFileRequest& request) {
    if (const auto valid = validate_fetch_file_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(request.runtime_key);
    writer.string(request.path);
    writer.u64(static_cast<std::uint64_t>(request.max_bytes));
    return writer.take();
}

Result<FetchFileRequest>
decode_fetch_file_request(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto runtime_key = reader.string(128U);
    const auto path = reader.string(kMaxWorkspaceFilePathBytes);
    const auto max_bytes = reader.u64();
    if (!runtime_key || !path || !max_bytes || !reader.finished() ||
        *max_bytes > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid workspace file-fetch request"));
    }
    FetchFileRequest request{
        .runtime_key = *runtime_key,
        .path = *path,
        .max_bytes = static_cast<std::size_t>(*max_bytes),
    };
    if (const auto valid = validate_fetch_file_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    return request;
}

Result<std::vector<std::byte>> encode_fetch_file_result(const FetchFileResult& result) {
    if (const auto valid = validate_fetch_file_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(result.path);
    writer.u64(static_cast<std::uint64_t>(result.byte_size));
    writer.string(result.sha256);
    return writer.take();
}

Result<FetchFileResult>
decode_fetch_file_result(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto path = reader.string(kMaxWorkspaceFilePathBytes);
    const auto byte_size = reader.u64();
    const auto sha256 = reader.string(64U);
    if (!path || !byte_size || !sha256 || !reader.finished() ||
        *byte_size > static_cast<std::uint64_t>(std::numeric_limits<std::size_t>::max())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid workspace file-fetch result"));
    }
    FetchFileResult result{
        .path = *path,
        .byte_size = static_cast<std::size_t>(*byte_size),
        .sha256 = *sha256,
    };
    if (const auto valid = validate_fetch_file_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    return result;
}

Result<std::vector<std::byte>> encode_fetch_file_chunk(const FetchFileChunk& chunk) {
    if (const auto valid = validate_fetch_file_chunk(chunk); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.bytes(chunk.bytes);
    return writer.take();
}

Result<FetchFileChunk>
decode_fetch_file_chunk(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto bytes = reader.bytes(kMaxAddFileChunkBytes);
    if (!bytes || !reader.finished()) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid workspace file-fetch chunk"));
    }
    FetchFileChunk chunk{.bytes = *bytes};
    if (const auto valid = validate_fetch_file_chunk(chunk); !valid) {
        return std::unexpected(valid.error());
    }
    return chunk;
}

Result<std::vector<std::byte>> encode_execution_result(const ExecutionResult& result) {
    if (const auto valid = validate_execution_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    Writer writer;
    writer.string(result.request_id);
    writer.u8(result.exit_code.has_value() ? 1U : 0U);
    if (result.exit_code.has_value()) {
        writer.u32(static_cast<std::uint32_t>(*result.exit_code));
    }
    writer.u8(result.timed_out ? 1U : 0U);
    writer.u8(result.truncated ? 1U : 0U);
    writer.u8(result.replayed ? 1U : 0U);
    writer.string(result.stdout_data);
    writer.string(result.stderr_data);
    std::vector<std::byte> encoded = writer.take();
    if (encoded.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "result payload exceeds frame quota"));
    }
    return encoded;
}

Result<ExecutionResult> decode_execution_result(const std::span<const std::byte> payload) {
    if (payload.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "result payload exceeds quota"));
    }
    Reader reader(payload);
    const auto request_id = reader.string(128U);
    const auto has_exit_code = reader.u8();
    if (!request_id || !has_exit_code || *has_exit_code > 1U) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid result header"));
    }
    ExecutionResult result;
    result.request_id = *request_id;
    if (*has_exit_code == 1U) {
        const auto exit_code = reader.u32();
        if (!exit_code || *exit_code > 255U) {
            return std::unexpected(
                make_error(ErrorCode::malformed_frame, "invalid result exit code"));
        }
        result.exit_code = static_cast<std::int32_t>(*exit_code);
    }
    const auto timed_out = reader.u8();
    const auto truncated = reader.u8();
    const auto replayed = reader.u8();
    const auto stdout_data = reader.string(kMaxOutputBytes);
    const auto stderr_data = reader.string(kMaxOutputBytes);
    if (!timed_out || !truncated || !replayed || *timed_out > 1U || *truncated > 1U ||
        *replayed > 1U || !stdout_data || !stderr_data || !reader.finished()) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid result tail"));
    }
    result.timed_out = *timed_out == 1U;
    result.truncated = *truncated == 1U;
    result.replayed = *replayed == 1U;
    result.stdout_data = *stdout_data;
    result.stderr_data = *stderr_data;
    if (const auto valid = validate_execution_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    return result;
}

Result<std::vector<std::byte>> encode_error(const Error& error) {
    if (error.message.size() > 4096U || error.message.find('\0') != std::string::npos) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid error message"));
    }
    Writer writer;
    writer.u16(static_cast<std::uint16_t>(error.code));
    writer.string(error.message);
    return writer.take();
}

Result<Error> decode_error(const std::span<const std::byte> payload) {
    Reader reader(payload);
    const auto raw_code = reader.u16();
    const auto message = reader.string(4096U);
    if (!raw_code || !message || !reader.finished() ||
        *raw_code > static_cast<std::uint16_t>(ErrorCode::internal)) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid error payload"));
    }
    return Error{.code = static_cast<ErrorCode>(*raw_code), .message = *message};
}

Result<std::vector<std::byte>> encode_frame(const MessageKind kind,
                                            const std::span<const std::byte> payload) {
    if (!is_known_kind(static_cast<std::uint16_t>(kind)) ||
        payload.size() > kMaxFrameBytes - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "invalid frame kind or payload length"));
    }
    Writer writer;
    writer.u32(kProtocolMagic);
    writer.u16(kProtocolVersion);
    writer.u16(static_cast<std::uint16_t>(kind));
    writer.u32(static_cast<std::uint32_t>(payload.size()));
    std::vector<std::byte> frame = writer.take();
    frame.insert(frame.end(), payload.begin(), payload.end());
    return frame;
}

Result<Frame> decode_frame(const std::span<const std::byte> wire) {
    if (wire.size() > kMaxFrameBytes) {
        return std::unexpected(make_error(ErrorCode::frame_too_large, "frame exceeds hard quota"));
    }
    if (wire.size() < kFrameHeaderBytes) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated frame header"));
    }
    Reader reader(wire.first(kFrameHeaderBytes));
    const auto magic = reader.u32();
    const auto version = reader.u16();
    const auto raw_kind = reader.u16();
    const auto payload_length = reader.u32();
    if (!magic || !version || !raw_kind || !payload_length) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated frame header"));
    }
    if (*magic != kProtocolMagic) {
        return std::unexpected(
            make_error(ErrorCode::protocol_violation, "unexpected protocol magic"));
    }
    if (*version != kProtocolVersion) {
        return std::unexpected(
            make_error(ErrorCode::unsupported_version, "unsupported protocol version"));
    }
    if (!is_known_kind(*raw_kind) || *payload_length != wire.size() - kFrameHeaderBytes) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid frame kind or length"));
    }
    Frame frame{.kind = static_cast<MessageKind>(*raw_kind), .payload = {}};
    frame.payload.insert(frame.payload.end(),
                         wire.begin() + static_cast<std::ptrdiff_t>(kFrameHeaderBytes), wire.end());
    return frame;
}

Result<void> send_frame(const int fd, const std::span<const std::byte> frame) {
    if (fd < 0 || frame.empty() || frame.size() > kMaxFrameBytes) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "invalid send frame arguments"));
    }
    ssize_t written = 0;
    do {
        written = send(fd, frame.data(), frame.size(), MSG_NOSIGNAL);
    } while (written < 0 && errno == EINTR);
    if (written < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "send SOCK_SEQPACKET frame"));
    }
    if (static_cast<std::size_t>(written) != frame.size()) {
        return std::unexpected(make_error(ErrorCode::io_failure, "short SOCK_SEQPACKET send"));
    }
    return {};
}

Result<std::vector<std::byte>> receive_frame(const int fd) {
    if (fd < 0) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid receive fd"));
    }
    std::vector<std::byte> buffer(kMaxFrameBytes);
    iovec vector{.iov_base = buffer.data(), .iov_len = buffer.size()};
    std::array<std::byte, CMSG_SPACE(sizeof(int) * 4U)> ancillary{};
    msghdr message{};
    message.msg_iov = &vector;
    message.msg_iovlen = 1U;
    message.msg_control = ancillary.data();
    message.msg_controllen = ancillary.size();
    ssize_t received = 0;
    do {
        received = recvmsg(fd, &message, MSG_TRUNC | MSG_CMSG_CLOEXEC);
    } while (received < 0 && errno == EINTR);
    if (received < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "receive SOCK_SEQPACKET frame"));
    }
    if (received == 0) {
        return std::unexpected(make_error(ErrorCode::io_failure, "SOCK_SEQPACKET peer closed"));
    }
    bool has_ancillary = false;
    for (cmsghdr* header = CMSG_FIRSTHDR(&message); header != nullptr;
         header = CMSG_NXTHDR(&message, header)) {
        has_ancillary = true;
        if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_RIGHTS &&
            header->cmsg_len >= CMSG_LEN(0)) {
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
    if ((message.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) != 0 ||
        static_cast<std::size_t>(received) > buffer.size()) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "truncated SOCK_SEQPACKET frame"));
    }
    if (has_ancillary) {
        return std::unexpected(make_error(ErrorCode::protocol_violation,
                                          "SCM ancillary data is forbidden on control frames"));
    }
    buffer.resize(static_cast<std::size_t>(received));
    return buffer;
}

} // namespace wspctl
