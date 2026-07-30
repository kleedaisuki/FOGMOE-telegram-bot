#include "wspctl/infrastructure/journal.hpp"

#include <openssl/sha.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <limits>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <vector>

namespace wspctl {
namespace {

/** @brief journal 文件魔数 / Journal file magic. */
constexpr std::array<std::byte, 4> kJournalMagic{std::byte{'W'}, std::byte{'S'}, std::byte{'P'},
                                                 std::byte{'J'}};
/** @brief journal 文件版本 / Journal file version. */
constexpr std::uint16_t kJournalVersion = 2;
/** @brief 仅用于读取既有执行 journal 的旧版本 / Legacy version used only to read existing execution
 * journals. */
constexpr std::uint16_t kLegacyJournalVersion = 1;
/** @brief journal 单文件硬上限 / Hard per-journal-file cap. */
constexpr std::size_t kMaxJournalBytes = kMaxFrameBytes + 4096U;

/**
 * @brief 判断规范 SHA-256 小写十六进制文本 / Check canonical lowercase SHA-256 hexadecimal text.
 * @param value 待检查文本 / Text to inspect.
 * @return 是否为 64 个小写十六进制字符 / Whether it is 64 lowercase hexadecimal characters.
 */
[[nodiscard]] bool is_sha256_hex(const std::string_view value) noexcept {
    return value.size() == 64U &&
           std::ranges::all_of(value, [](const unsigned char character) noexcept {
               return (character >= static_cast<unsigned char>('0') &&
                       character <= static_cast<unsigned char>('9')) ||
                      (character >= static_cast<unsigned char>('a') &&
                       character <= static_cast<unsigned char>('f'));
           });
}

/** @brief 安全写入 little-endian u16 / Safely append a little-endian u16. */
void append_u16(std::vector<std::byte>& bytes, const std::uint16_t value) {
    bytes.push_back(static_cast<std::byte>(value & 0xffU));
    bytes.push_back(static_cast<std::byte>((value >> 8U) & 0xffU));
}

/** @brief 安全写入 little-endian u32 / Safely append a little-endian u32. */
void append_u32(std::vector<std::byte>& bytes, const std::uint32_t value) {
    for (unsigned int index = 0; index < 4U; ++index) {
        bytes.push_back(static_cast<std::byte>((value >> (index * 8U)) & 0xffU));
    }
}

/** @brief 写入长度前缀字符串 / Append a length-prefixed string. */
void append_string(std::vector<std::byte>& bytes, const std::string_view value) {
    append_u32(bytes, static_cast<std::uint32_t>(value.size()));
    for (const char character : value) {
        bytes.push_back(static_cast<std::byte>(static_cast<unsigned char>(character)));
    }
}

/** @brief 读取 little-endian u16 / Read a little-endian u16. */
[[nodiscard]] Result<std::uint16_t> read_u16(std::span<const std::byte> bytes,
                                             std::size_t& offset) {
    if (bytes.size() - offset < 2U) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated journal u16"));
    }
    const std::uint16_t value =
        static_cast<std::uint16_t>(std::to_integer<unsigned char>(bytes[offset])) |
        (static_cast<std::uint16_t>(std::to_integer<unsigned char>(bytes[offset + 1U])) << 8U);
    offset += 2U;
    return value;
}

/** @brief 读取 little-endian u32 / Read a little-endian u32. */
[[nodiscard]] Result<std::uint32_t> read_u32(std::span<const std::byte> bytes,
                                             std::size_t& offset) {
    if (bytes.size() - offset < 4U) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated journal u32"));
    }
    std::uint32_t value = 0;
    for (unsigned int index = 0; index < 4U; ++index) {
        value |= static_cast<std::uint32_t>(std::to_integer<unsigned char>(bytes[offset + index]))
                 << (index * 8U);
    }
    offset += 4U;
    return value;
}

/** @brief 读取有界长度前缀字符串 / Read a bounded length-prefixed string. */
[[nodiscard]] Result<std::string> read_string(const std::span<const std::byte> bytes,
                                              std::size_t& offset, const std::size_t maximum) {
    const auto length = read_u32(bytes, offset);
    if (!length) {
        return std::unexpected(length.error());
    }
    const auto size = static_cast<std::size_t>(*length);
    if (size > maximum || size > bytes.size() - offset) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid journal string length"));
    }
    std::string value;
    value.reserve(size);
    for (std::size_t index = 0; index < size; ++index) {
        value.push_back(static_cast<char>(std::to_integer<unsigned char>(bytes[offset + index])));
    }
    offset += size;
    return value;
}

/** @brief 将任意文本 SHA-256 渲染为 hex / Render SHA-256 of arbitrary text as hex. */
[[nodiscard]] std::string sha256_hex(const std::string_view text) {
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(text.data()), text.size(), digest.data());
    constexpr std::string_view digits{"0123456789abcdef"};
    std::string output;
    output.reserve(digest.size() * 2U);
    for (const unsigned char value : digest) {
        output.push_back(digits[(value >> 4U) & 0x0fU]);
        output.push_back(digits[value & 0x0fU]);
    }
    return output;
}

/** @brief 将 journal record 序列化 / Serialize a journal record. */
[[nodiscard]] Result<std::vector<std::byte>> encode_record(const JournalRecord& record) {
    const bool valid_operation = record.operation == JournalOperation::execution ||
                                 record.operation == JournalOperation::payload;
    const bool has_execution_result = record.execution_result.has_value();
    const bool has_payload_result = record.payload_result.has_value();
    const bool valid_completion = record.state == JournalState::pending
                                      ? !has_execution_result && !has_payload_result
                                      : record.state == JournalState::completed &&
                                            ((record.operation == JournalOperation::execution &&
                                              has_execution_result && !has_payload_result) ||
                                             (record.operation == JournalOperation::payload &&
                                              !has_execution_result && has_payload_result));
    if (!is_sha256_hex(record.request_hash) || !is_sha256_hex(record.payload_hash) ||
        !valid_operation || !valid_completion) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid journal record"));
    }
    std::vector<std::byte> bytes;
    bytes.insert(bytes.end(), kJournalMagic.begin(), kJournalMagic.end());
    append_u16(bytes, kJournalVersion);
    bytes.push_back(static_cast<std::byte>(record.state));
    bytes.push_back(static_cast<std::byte>(record.operation));
    bytes.push_back(std::byte{0});
    append_string(bytes, record.request_hash);
    append_string(bytes, record.payload_hash);
    if (record.execution_result.has_value() || record.payload_result.has_value()) {
        const auto result_payload = record.operation == JournalOperation::execution
                                        ? encode_execution_result(*record.execution_result)
                                        : encode_payload_result(*record.payload_result);
        if (!result_payload) {
            return std::unexpected(result_payload.error());
        }
        append_u32(bytes, static_cast<std::uint32_t>(result_payload->size()));
        bytes.insert(bytes.end(), result_payload->begin(), result_payload->end());
    } else {
        append_u32(bytes, 0U);
    }
    if (bytes.size() > kMaxJournalBytes) {
        return std::unexpected(
            make_error(ErrorCode::frame_too_large, "journal record exceeds quota"));
    }
    return bytes;
}

/** @brief 反序列化既有 v1 execution journal record / Deserialize an existing v1 execution journal
 * record. */
[[nodiscard]] Result<JournalRecord>
decode_legacy_execution_record(const std::span<const std::byte> bytes, std::size_t offset) {
    if (offset + 2U > bytes.size()) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "truncated legacy journal state"));
    }
    const auto raw_state = std::to_integer<unsigned char>(bytes[offset++]);
    const auto reserved = std::to_integer<unsigned char>(bytes[offset++]);
    if (reserved != 0U || (raw_state != static_cast<unsigned char>(JournalState::pending) &&
                           raw_state != static_cast<unsigned char>(JournalState::completed))) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid legacy journal state"));
    }
    const auto request_hash = read_string(bytes, offset, 64U);
    const auto payload_hash = read_string(bytes, offset, 64U);
    const auto result_length = read_u32(bytes, offset);
    if (!request_hash || !payload_hash || !result_length || !is_sha256_hex(*request_hash) ||
        !is_sha256_hex(*payload_hash) || *result_length > bytes.size() - offset) {
        return std::unexpected(make_error(ErrorCode::malformed_frame,
                                          "invalid legacy journal hashes or result length"));
    }
    JournalRecord record{
        .state = static_cast<JournalState>(raw_state),
        .operation = JournalOperation::execution,
        .request_hash = *request_hash,
        .payload_hash = *payload_hash,
        .execution_result = std::nullopt,
        .payload_result = std::nullopt,
    };
    if (record.state == JournalState::pending) {
        if (*result_length != 0U || offset != bytes.size()) {
            return std::unexpected(
                make_error(ErrorCode::malformed_frame, "pending legacy journal contains result"));
        }
        return record;
    }
    if (*result_length == 0U || static_cast<std::size_t>(*result_length) != bytes.size() - offset) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "completed legacy journal lacks result"));
    }
    const auto result = decode_execution_result(bytes.subspan(offset, *result_length));
    if (!result) {
        return std::unexpected(result.error());
    }
    record.execution_result = *result;
    return record;
}

/** @brief 反序列化 journal record / Deserialize a journal record. */
[[nodiscard]] Result<JournalRecord> decode_record(const std::span<const std::byte> bytes) {
    if (bytes.size() < 4U + 2U + 2U || bytes.size() > kMaxJournalBytes ||
        !std::equal(kJournalMagic.begin(), kJournalMagic.end(), bytes.begin())) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid journal magic or size"));
    }
    std::size_t offset = 4U;
    const auto version = read_u16(bytes, offset);
    if (!version) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "unsupported journal version"));
    }
    if (*version == kLegacyJournalVersion) {
        return decode_legacy_execution_record(bytes, offset);
    }
    if (*version != kJournalVersion || offset + 3U > bytes.size()) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "unsupported journal version"));
    }
    const auto raw_state = std::to_integer<unsigned char>(bytes[offset++]);
    const auto raw_operation = std::to_integer<unsigned char>(bytes[offset++]);
    const auto reserved = std::to_integer<unsigned char>(bytes[offset++]);
    if (reserved != 0U ||
        (raw_state != static_cast<unsigned char>(JournalState::pending) &&
         raw_state != static_cast<unsigned char>(JournalState::completed)) ||
        (raw_operation != static_cast<unsigned char>(JournalOperation::execution) &&
         raw_operation != static_cast<unsigned char>(JournalOperation::payload))) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid journal state"));
    }
    const auto request_hash = read_string(bytes, offset, 64U);
    const auto payload_hash = read_string(bytes, offset, 64U);
    const auto result_length = read_u32(bytes, offset);
    if (!request_hash || !payload_hash || !result_length || !is_sha256_hex(*request_hash) ||
        !is_sha256_hex(*payload_hash) || *result_length > bytes.size() - offset) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "invalid journal hashes or result length"));
    }
    JournalRecord record{
        .state = static_cast<JournalState>(raw_state),
        .operation = static_cast<JournalOperation>(raw_operation),
        .request_hash = *request_hash,
        .payload_hash = *payload_hash,
        .execution_result = std::nullopt,
        .payload_result = std::nullopt,
    };
    if (record.state == JournalState::pending) {
        if (*result_length != 0U || offset != bytes.size()) {
            return std::unexpected(
                make_error(ErrorCode::malformed_frame, "pending journal contains result"));
        }
        return record;
    }
    if (*result_length == 0U || static_cast<std::size_t>(*result_length) != bytes.size() - offset) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "completed journal lacks result"));
    }
    if (record.operation == JournalOperation::execution) {
        const auto result = decode_execution_result(bytes.subspan(offset, *result_length));
        if (!result) {
            return std::unexpected(result.error());
        }
        record.execution_result = *result;
        return record;
    }
    const auto payload_result = decode_payload_result(bytes.subspan(offset, *result_length));
    if (!payload_result) {
        return std::unexpected(payload_result.error());
    }
    record.payload_result = *payload_result;
    return record;
}

/**
 * @brief 验证 journal 文件名索引与完成收据的调用身份绑定 / Validate that a journal filename index
 * is bound to its completed receipt identity.
 * @param record 已解码 journal 记录 / Decoded journal record.
 * @param request_id 从安全文件名材料得出的调用 ID / Invocation ID from the safe filename material.
 * @return 成功或 fail-closed 格式错误 / Success or a fail-closed format error.
 * @note pending record 没有 receipt；completed record 必须只有本 operation 的 receipt，且 receipt
 *       不能标记为 replay。/ A pending record has no receipt; a completed record must have only
 *       its operation's receipt, and that receipt must not be marked as a replay.
 */
[[nodiscard]] Result<void> validate_record_identity(const JournalRecord& record,
                                                    const std::string_view request_id) {
    if (record.state == JournalState::pending) {
        if (record.execution_result.has_value() || record.payload_result.has_value()) {
            return std::unexpected(
                make_error(ErrorCode::malformed_frame, "pending journal contains a receipt"));
        }
        return {};
    }
    if (record.state != JournalState::completed) {
        return std::unexpected(
            make_error(ErrorCode::malformed_frame, "journal has an unknown state"));
    }
    if (record.operation == JournalOperation::execution && record.execution_result.has_value() &&
        !record.payload_result.has_value() && record.execution_result->request_id == request_id &&
        !record.execution_result->replayed) {
        return {};
    }
    if (record.operation == JournalOperation::payload && !record.execution_result.has_value() &&
        record.payload_result.has_value() && record.payload_result->request_id == request_id &&
        !record.payload_result->replayed) {
        return {};
    }
    return std::unexpected(
        make_error(ErrorCode::malformed_frame,
                   "completed journal receipt does not match its request identity or operation"));
}

/** @brief 完整写入并 fsync 一个 FD / Fully write and fsync an FD. */
[[nodiscard]] Result<void> write_and_sync(const int fd, const std::span<const std::byte> bytes) {
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const ssize_t written =
            write(fd, bytes.data() + static_cast<std::ptrdiff_t>(offset), bytes.size() - offset);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return std::unexpected(errno_error(ErrorCode::io_failure, "write journal"));
        }
        if (written == 0) {
            return std::unexpected(make_error(ErrorCode::io_failure, "zero-byte journal write"));
        }
        offset += static_cast<std::size_t>(written);
    }
    if (fsync(fd) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "fsync journal"));
    }
    return {};
}

/** @brief 相对已验证 journal FD 读取 record / Read a record relative to a verified journal FD. */
[[nodiscard]] Result<std::optional<std::vector<std::byte>>>
read_record_file_at(const int journal_fd, const std::string_view name) {
    const int fd = openat(journal_fd, std::string(name).c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        if (errno == ENOENT) {
            return std::optional<std::vector<std::byte>>{};
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "openat journal"));
    }
    struct stat metadata {};
    if (fstat(fd, &metadata) != 0 || !S_ISREG(metadata.st_mode) || metadata.st_size < 0 ||
        metadata.st_size > static_cast<off_t>(kMaxJournalBytes)) {
        const int saved_errno = errno;
        close(fd);
        errno = saved_errno;
        return std::unexpected(
            make_error(ErrorCode::io_failure, "journal is not a bounded regular file"));
    }
    std::vector<std::byte> bytes(static_cast<std::size_t>(metadata.st_size));
    std::size_t offset = 0;
    while (offset < bytes.size()) {
        const ssize_t count =
            read(fd, bytes.data() + static_cast<std::ptrdiff_t>(offset), bytes.size() - offset);
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            const Error error = errno_error(ErrorCode::io_failure, "read journal");
            close(fd);
            return std::unexpected(error);
        }
        if (count == 0) {
            close(fd);
            return std::unexpected(
                make_error(ErrorCode::io_failure, "journal changed while reading"));
        }
        offset += static_cast<std::size_t>(count);
    }
    close(fd);
    return std::optional<std::vector<std::byte>>{std::move(bytes)};
}

/**
 * @brief 打开一个已存在的 no-follow journal 目录子项 / Open one existing no-follow
 * journal-directory child.
 * @param parent_fd 已验证 parent directory 的 FD / FD of the verified parent directory.
 * @param name 固定 child basename / Fixed child basename.
 * @param purpose 诊断语义 / Diagnostic purpose.
 * @return child directory FD 或错误 / Child directory FD or an error.
 * @note 此函数绝不 mkdir；quota service 必须先完成 runtime 的 project-bound layout provisioning。
 *       This never calls mkdir; the quota service must have provisioned the project's bound runtime
 * layout first.
 */
[[nodiscard]] Result<int> open_journal_directory_child(const int parent_fd,
                                                       const std::string_view name,
                                                       const std::string_view purpose) {
    if (name.empty() || name == "." || name == ".." || name.find('/') != std::string_view::npos) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "unsafe journal directory component"));
    }
    const int child_fd = openat(parent_fd, std::string(name).c_str(),
                                O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (child_fd < 0) {
        if (errno == ENOENT) {
            return std::unexpected(
                make_error(ErrorCode::not_found, std::string(purpose) + " does not exist"));
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "open " + std::string(purpose)));
    }
    struct stat metadata {};
    if (fstat(child_fd, &metadata) != 0 || !S_ISDIR(metadata.st_mode) ||
        (metadata.st_mode & (S_IWGRP | S_IWOTH)) != 0) {
        const int saved_errno = errno;
        close(child_fd);
        errno = saved_errno;
        return std::unexpected(make_error(
            ErrorCode::io_failure, std::string(purpose) + " is not a private real directory"));
    }
    return child_fd;
}

/**
 * @brief 打开一个 runtime 自己 control project 下的 journal directory / Open a journal directory
 * below one runtime's control project.
 * @param state_root 受 broker 管理的绝对状态根 / Broker-managed absolute state root.
 * @param runtime_key canonical runtime 标识 / Canonical runtime identifier.
 * @return 已打开的 per-runtime journal directory FD / Opened per-runtime journal directory FD.
 * @note 这里不创建 global ``state_root/journal``；broker 必须在 lookup/begin 前调用 quota
 *       ``ensure_runtime``。 This never creates a global ``state_root/journal``; the broker must
 *       call quota ``ensure_runtime`` before lookup/begin.
 */
[[nodiscard]] Result<int> open_runtime_journal_directory(const std::filesystem::path& state_root,
                                                         const std::string_view runtime_key) {
    const int state_fd = open(state_root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (state_fd < 0) {
        if (errno == ENOENT) {
            return std::unexpected(
                make_error(ErrorCode::not_found, "journal state_root does not exist"));
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "open journal state_root"));
    }
    struct stat state_metadata {};
    if (fstat(state_fd, &state_metadata) != 0 || !S_ISDIR(state_metadata.st_mode)) {
        close(state_fd);
        return std::unexpected(
            make_error(ErrorCode::io_failure, "journal state_root is not a directory"));
    }
    const auto runtimes_fd =
        open_journal_directory_child(state_fd, "runtimes", "journal runtime root");
    close(state_fd);
    if (!runtimes_fd) {
        return std::unexpected(runtimes_fd.error());
    }
    const auto runtime_fd = open_journal_directory_child(*runtimes_fd, sha256_hex(runtime_key),
                                                         "journal runtime directory");
    close(*runtimes_fd);
    if (!runtime_fd) {
        return std::unexpected(runtime_fd.error());
    }
    const auto control_fd =
        open_journal_directory_child(*runtime_fd, "control", "journal control directory");
    close(*runtime_fd);
    if (!control_fd) {
        return std::unexpected(control_fd.error());
    }
    const auto journal_fd =
        open_journal_directory_child(*control_fd, "journal", "runtime control journal directory");
    close(*control_fd);
    return journal_fd;
}

} // namespace

Journal::Journal(std::filesystem::path state_root) : state_root_(std::move(state_root)) {}

std::filesystem::path Journal::record_path(const std::string& runtime_key,
                                           const std::string& request_id) const {
    std::string material;
    material.reserve(runtime_key.size() + request_id.size() + 1U);
    material.append(runtime_key);
    material.push_back('\0');
    material.append(request_id);
    return state_root_ / "runtimes" / sha256_hex(runtime_key) / "control" / "journal" /
           sha256_hex(material);
}

Result<std::optional<JournalRecord>> Journal::lookup(const std::string& runtime_key,
                                                     const std::string& request_id) const {
    const auto journal_directory = open_runtime_journal_directory(state_root_, runtime_key);
    if (!journal_directory) {
        return std::unexpected(journal_directory.error());
    }
    const std::string name = record_path(runtime_key, request_id).filename().string();
    const auto checked_bytes = read_record_file_at(*journal_directory, name);
    close(*journal_directory);
    if (!checked_bytes) {
        return std::unexpected(checked_bytes.error());
    }
    if (!checked_bytes->has_value()) {
        return std::optional<JournalRecord>{};
    }
    const auto record = decode_record(**checked_bytes);
    if (!record) {
        return std::unexpected(record.error());
    }
    if (const auto identity = validate_record_identity(*record, request_id); !identity) {
        return std::unexpected(identity.error());
    }
    return std::optional<JournalRecord>{*record};
}

Result<void> Journal::begin(const ExecuteRequest& request) const {
    if (const auto valid = validate_execute_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (!state_root_.is_absolute()) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "journal state_root must be absolute"));
    }
    const auto journal_directory = open_runtime_journal_directory(state_root_, request.runtime_key);
    if (!journal_directory) {
        return std::unexpected(journal_directory.error());
    }
    const JournalRecord record{
        .state = JournalState::pending,
        .operation = JournalOperation::execution,
        .request_hash = request.request_hash,
        .payload_hash = canonical_request_hash(request),
        .execution_result = std::nullopt,
        .payload_result = std::nullopt,
    };
    const auto encoded = encode_record(record);
    if (!encoded) {
        close(*journal_directory);
        return std::unexpected(encoded.error());
    }
    const std::string name =
        record_path(request.runtime_key, request.request_id).filename().string();
    const int fd = openat(*journal_directory, name.c_str(),
                          O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        close(*journal_directory);
        if (errno == EEXIST) {
            return std::unexpected(
                make_error(ErrorCode::already_exists, "journal invocation already exists"));
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "create pending journal"));
    }
    const auto written = write_and_sync(fd, *encoded);
    const int saved_errno = errno;
    static_cast<void>(close(fd));
    errno = saved_errno;
    if (!written) {
        static_cast<void>(unlinkat(*journal_directory, name.c_str(), 0));
        close(*journal_directory);
        return std::unexpected(written.error());
    }
    if (fsync(*journal_directory) != 0) {
        // The marker may exist but is not provably durable; dispatch must not occur.
        const Error error =
            errno_error(ErrorCode::io_failure, "fsync journal directory after pending marker");
        close(*journal_directory);
        return std::unexpected(error);
    }
    close(*journal_directory);
    return {};
}

Result<void> Journal::complete(const ExecuteRequest& request, const ExecutionResult& result) const {
    if (const auto valid = validate_execute_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (result.request_id != request.request_id || result.replayed) {
        return std::unexpected(
            make_error(ErrorCode::journal_conflict,
                       "execution receipt cannot be replayed or belong to another request"));
    }
    const auto current = lookup(request.runtime_key, request.request_id);
    if (!current) {
        return std::unexpected(current.error());
    }
    if (!current->has_value() || (*current)->state != JournalState::pending ||
        (*current)->operation != JournalOperation::execution ||
        (*current)->request_hash != request.request_hash ||
        (*current)->payload_hash != canonical_request_hash(request)) {
        return std::unexpected(
            make_error(ErrorCode::journal_conflict, "journal record cannot be completed"));
    }
    const JournalRecord record{
        .state = JournalState::completed,
        .operation = JournalOperation::execution,
        .request_hash = request.request_hash,
        .payload_hash = canonical_request_hash(request),
        .execution_result = result,
        .payload_result = std::nullopt,
    };
    const auto encoded = encode_record(record);
    if (!encoded) {
        return std::unexpected(encoded.error());
    }
    const auto journal_directory = open_runtime_journal_directory(state_root_, request.runtime_key);
    if (!journal_directory) {
        return std::unexpected(journal_directory.error());
    }
    const std::string name =
        record_path(request.runtime_key, request.request_id).filename().string();
    const std::string temporary_name = name + ".tmp." + std::to_string(getpid());
    const int fd = openat(*journal_directory, temporary_name.c_str(),
                          O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        close(*journal_directory);
        return std::unexpected(errno_error(ErrorCode::io_failure, "create journal replacement"));
    }
    const auto written = write_and_sync(fd, *encoded);
    const int saved_errno = errno;
    static_cast<void>(close(fd));
    errno = saved_errno;
    if (!written) {
        static_cast<void>(unlinkat(*journal_directory, temporary_name.c_str(), 0));
        close(*journal_directory);
        return std::unexpected(written.error());
    }
    if (renameat(*journal_directory, temporary_name.c_str(), *journal_directory, name.c_str()) !=
        0) {
        const Error error = errno_error(ErrorCode::io_failure, "replace journal");
        static_cast<void>(unlinkat(*journal_directory, temporary_name.c_str(), 0));
        close(*journal_directory);
        return std::unexpected(error);
    }
    if (fsync(*journal_directory) != 0) {
        const Error error =
            errno_error(ErrorCode::io_failure, "fsync journal directory after completion");
        close(*journal_directory);
        return std::unexpected(error);
    }
    close(*journal_directory);
    return {};
}

Result<void> Journal::begin_payload(const PayloadBeginRequest& request) const {
    if (const auto valid = validate_payload_begin_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    if (!state_root_.is_absolute()) {
        return std::unexpected(
            make_error(ErrorCode::invalid_argument, "journal state_root must be absolute"));
    }
    const auto journal_directory = open_runtime_journal_directory(state_root_, request.runtime_key);
    if (!journal_directory) {
        return std::unexpected(journal_directory.error());
    }
    const JournalRecord record{
        .state = JournalState::pending,
        .operation = JournalOperation::payload,
        .request_hash = request.request_hash,
        .payload_hash = canonical_payload_hash(request),
        .execution_result = std::nullopt,
        .payload_result = std::nullopt,
    };
    const auto encoded = encode_record(record);
    if (!encoded) {
        close(*journal_directory);
        return std::unexpected(encoded.error());
    }
    const std::string name =
        record_path(request.runtime_key, request.request_id).filename().string();
    const int fd = openat(*journal_directory, name.c_str(),
                          O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        close(*journal_directory);
        if (errno == EEXIST) {
            return std::unexpected(
                make_error(ErrorCode::already_exists, "journal invocation already exists"));
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "create pending file journal"));
    }
    const auto written = write_and_sync(fd, *encoded);
    const int saved_errno = errno;
    static_cast<void>(close(fd));
    errno = saved_errno;
    if (!written) {
        static_cast<void>(unlinkat(*journal_directory, name.c_str(), 0));
        close(*journal_directory);
        return std::unexpected(written.error());
    }
    if (fsync(*journal_directory) != 0) {
        // The marker may exist but is not provably durable; atomic publish must not begin.
        const Error error =
            errno_error(ErrorCode::io_failure, "fsync journal directory after pending file marker");
        close(*journal_directory);
        return std::unexpected(error);
    }
    close(*journal_directory);
    return {};
}

Result<void> Journal::complete_payload(const PayloadBeginRequest& request,
                                       const PayloadResult& result) const {
    if (const auto valid_request = validate_payload_begin_request(request); !valid_request) {
        return std::unexpected(valid_request.error());
    }
    if (const auto valid_result = validate_payload_result(result); !valid_result) {
        return std::unexpected(valid_result.error());
    }
    const std::string expected_path = "/workspace/uploads/" + request.opaque_id + "/payload";
    if (result.request_id != request.request_id || result.replayed ||
        result.path != expected_path || result.byte_size != request.byte_size ||
        result.sha256 != request.sha256) {
        return std::unexpected(make_error(ErrorCode::journal_conflict,
                                          "file journal receipt does not match begin request"));
    }
    const auto current = lookup(request.runtime_key, request.request_id);
    if (!current) {
        return std::unexpected(current.error());
    }
    if (!current->has_value() || (*current)->state != JournalState::pending ||
        (*current)->operation != JournalOperation::payload ||
        (*current)->request_hash != request.request_hash ||
        (*current)->payload_hash != canonical_payload_hash(request)) {
        return std::unexpected(
            make_error(ErrorCode::journal_conflict, "file journal record cannot be completed"));
    }
    const JournalRecord record{
        .state = JournalState::completed,
        .operation = JournalOperation::payload,
        .request_hash = request.request_hash,
        .payload_hash = canonical_payload_hash(request),
        .execution_result = std::nullopt,
        .payload_result = result,
    };
    const auto encoded = encode_record(record);
    if (!encoded) {
        return std::unexpected(encoded.error());
    }
    const auto journal_directory = open_runtime_journal_directory(state_root_, request.runtime_key);
    if (!journal_directory) {
        return std::unexpected(journal_directory.error());
    }
    const std::string name =
        record_path(request.runtime_key, request.request_id).filename().string();
    const std::string temporary_name = name + ".tmp." + std::to_string(getpid());
    const int fd = openat(*journal_directory, temporary_name.c_str(),
                          O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        close(*journal_directory);
        return std::unexpected(
            errno_error(ErrorCode::io_failure, "create file journal replacement"));
    }
    const auto written = write_and_sync(fd, *encoded);
    const int saved_errno = errno;
    static_cast<void>(close(fd));
    errno = saved_errno;
    if (!written) {
        static_cast<void>(unlinkat(*journal_directory, temporary_name.c_str(), 0));
        close(*journal_directory);
        return std::unexpected(written.error());
    }
    if (renameat(*journal_directory, temporary_name.c_str(), *journal_directory, name.c_str()) !=
        0) {
        const Error error = errno_error(ErrorCode::io_failure, "replace file journal");
        static_cast<void>(unlinkat(*journal_directory, temporary_name.c_str(), 0));
        close(*journal_directory);
        return std::unexpected(error);
    }
    if (fsync(*journal_directory) != 0) {
        const Error error =
            errno_error(ErrorCode::io_failure, "fsync journal directory after file completion");
        close(*journal_directory);
        return std::unexpected(error);
    }
    close(*journal_directory);
    return {};
}

} // namespace wspctl
