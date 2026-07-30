#include "wspctl/infrastructure/detail/payload_replay.hpp"

#include <openssl/evp.h>
#include <openssl/sha.h>

#include <array>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <fcntl.h>
#include <memory>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>

namespace wspctl::detail {
namespace {

/**
 * @brief replay object 验证期间的最小 FD RAII owner / Minimal FD RAII owner for replay-object
 * verification.
 */
class ReplayOwnedFd final {
public:
    /**
     * @brief 接管一个已打开 FD / Take ownership of an already-open FD.
     * @param descriptor 要接管的 FD / FD to take ownership of.
     */
    explicit ReplayOwnedFd(const int descriptor = -1) noexcept : descriptor_(descriptor) {}

    /** @brief 析构时关闭 FD / Close the FD on destruction. */
    ~ReplayOwnedFd() {
        if (descriptor_ >= 0) {
            static_cast<void>(close(descriptor_));
        }
    }

    /** @brief 禁止复制 FD ownership / FD ownership cannot be copied. */
    ReplayOwnedFd(const ReplayOwnedFd&) = delete;
    /** @brief 禁止复制赋值 FD ownership / FD ownership cannot be copy-assigned. */
    ReplayOwnedFd& operator=(const ReplayOwnedFd&) = delete;

    /**
     * @brief 移动 FD ownership / Move FD ownership.
     * @param other 被移动的 FD owner / FD owner being moved.
     */
    ReplayOwnedFd(ReplayOwnedFd&& other) noexcept
        : descriptor_(std::exchange(other.descriptor_, -1)) {}

    /**
     * @brief 移动赋值 FD ownership / Move-assign FD ownership.
     * @param other 被移动的 FD owner / FD owner being moved.
     * @return 当前 owner / This owner.
     */
    ReplayOwnedFd& operator=(ReplayOwnedFd&& other) noexcept {
        if (this != &other) {
            if (descriptor_ >= 0) {
                static_cast<void>(close(descriptor_));
            }
            descriptor_ = std::exchange(other.descriptor_, -1);
        }
        return *this;
    }

    /**
     * @brief 取得借用 FD / Get the borrowed FD.
     * @return 借用 FD / Borrowed FD.
     */
    [[nodiscard]] int get() const noexcept { return descriptor_; }

private:
    /** @brief 被拥有的 FD / Owned FD. */
    int descriptor_;
};

/**
 * @brief 将一段 SHA-256 bytes 渲染为小写 hex / Render a SHA-256 byte sequence as lowercase hex.
 * @param digest SHA-256 digest bytes / SHA-256 digest bytes.
 * @return 64-character lowercase hexadecimal digest / 64-character lowercase hexadecimal digest.
 */
[[nodiscard]] std::string
render_sha256_hex(const std::array<unsigned char, SHA256_DIGEST_LENGTH>& digest) {
    /** @brief hex digit table / Hex digit table. */
    constexpr std::string_view kDigits{"0123456789abcdef"};
    /** @brief rendered digest / Rendered digest. */
    std::string rendered;
    rendered.reserve(digest.size() * 2U);
    for (const unsigned char byte : digest) {
        rendered.push_back(kDigits[(byte >> 4U) & 0x0fU]);
        rendered.push_back(kDigits[byte & 0x0fU]);
    }
    return rendered;
}

} // namespace

Result<PayloadResult> resolve_payload_replay_receipt(const Journal& journal,
                                                     const PayloadReplayRequest& request) {
    const auto existing = journal.lookup(request.runtime_key, request.request_id);
    if (!existing) {
        if (existing.error().code == ErrorCode::not_found) {
            return std::unexpected(make_error(ErrorCode::not_found,
                                              "no durable file ingress receipt exists for replay"));
        }
        return std::unexpected(existing.error());
    }
    if (!existing->has_value()) {
        return std::unexpected(
            make_error(ErrorCode::not_found, "no durable file ingress receipt exists for replay"));
    }
    /** @brief durable journal record / Durable journal record. */
    const JournalRecord& record = **existing;
    if (record.operation != JournalOperation::payload ||
        record.request_hash != request.request_hash ||
        record.payload_hash != canonical_payload_hash(request)) {
        return std::unexpected(
            make_error(ErrorCode::journal_conflict,
                       "file replay metadata does not match the durable ingress receipt"));
    }
    if (record.state == JournalState::pending) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "file replay found a pending ingress publish whose durable outcome is unknown"));
    }
    if (!record.payload_result.has_value()) {
        return std::unexpected(make_error(ErrorCode::journal_conflict,
                                          "completed file ingress journal has no receipt"));
    }
    /** @brief completed receipt copied before replay marking / Completed receipt copied before
     * replay marking. */
    PayloadResult replay = *record.payload_result;
    /** @brief canonical in-workspace payload path / Canonical in-workspace payload path. */
    const std::string expected_path = "/workspace/uploads/" + request.opaque_id + "/payload";
    if (replay.request_id != request.request_id || replay.path != expected_path ||
        replay.byte_size != request.byte_size || replay.sha256 != request.sha256) {
        return std::unexpected(
            make_error(ErrorCode::journal_conflict,
                       "completed file ingress receipt does not match replay metadata"));
    }
    replay.replayed = true;
    return replay;
}

Result<void> verify_replayable_payload_object(const RuntimeQuotaBinding& binding,
                                              const PayloadReplayRequest& request,
                                              const PayloadResult& receipt) {
    /** @brief canonical in-workspace payload path / Canonical in-workspace payload path. */
    const std::string expected_path = "/workspace/uploads/" + request.opaque_id + "/payload";
    if (!receipt.replayed || receipt.request_id != request.request_id ||
        receipt.path != expected_path || receipt.byte_size != request.byte_size ||
        receipt.sha256 != request.sha256) {
        return std::unexpected(
            make_error(ErrorCode::journal_conflict,
                       "replay receipt changed before persistent object verification"));
    }
    /** @brief persistent workspace upper root / Persistent workspace upper root. */
    ReplayOwnedFd upper_fd(open((binding.workspace_dir / "upper").c_str(),
                                O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (upper_fd.get() < 0) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "completed file receipt has no readable persistent workspace upper root"));
    }
    /** @brief upper-root metadata / Upper-root metadata. */
    struct stat upper_metadata {};
    if (fstat(upper_fd.get(), &upper_metadata) != 0 || !S_ISDIR(upper_metadata.st_mode)) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "persistent workspace upper root changed during replay verification"));
    }
    /** @brief uploads directory FD / Uploads-directory FD. */
    ReplayOwnedFd uploads_fd(
        openat(upper_fd.get(), "uploads", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (uploads_fd.get() < 0) {
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt,
                                          "completed file receipt payload directory is missing"));
    }
    /** @brief uploads metadata / Uploads metadata. */
    struct stat uploads_metadata {};
    if (fstat(uploads_fd.get(), &uploads_metadata) != 0 || !S_ISDIR(uploads_metadata.st_mode) ||
        uploads_metadata.st_dev != upper_metadata.st_dev) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "completed file receipt uploads directory is not recoverable"));
    }
    /** @brief opaque payload directory FD / Opaque payload-directory FD. */
    ReplayOwnedFd payload_directory_fd(openat(uploads_fd.get(), request.opaque_id.c_str(),
                                              O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW));
    if (payload_directory_fd.get() < 0) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "completed file receipt opaque payload directory is missing"));
    }
    /** @brief opaque payload-directory metadata / Opaque payload-directory metadata. */
    struct stat payload_directory_metadata {};
    if (fstat(payload_directory_fd.get(), &payload_directory_metadata) != 0 ||
        !S_ISDIR(payload_directory_metadata.st_mode) ||
        payload_directory_metadata.st_dev != upper_metadata.st_dev) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "completed file receipt opaque payload directory is not recoverable"));
    }
    /** @brief no-follow payload file FD / No-follow payload-file FD. */
    ReplayOwnedFd payload_fd(
        openat(payload_directory_fd.get(), "payload", O_RDONLY | O_CLOEXEC | O_NOFOLLOW));
    if (payload_fd.get() < 0) {
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt,
                                          "completed file receipt payload object is missing"));
    }
    /** @brief payload metadata before digest / Payload metadata before digest. */
    struct stat before_digest {};
    if (fstat(payload_fd.get(), &before_digest) != 0 || !S_ISREG(before_digest.st_mode) ||
        before_digest.st_dev != upper_metadata.st_dev || before_digest.st_size < 0 ||
        static_cast<std::uintmax_t>(before_digest.st_size) != request.byte_size) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "completed file receipt payload object does not match its persisted size"));
    }
    /** @brief streaming SHA-256 context / Streaming SHA-256 context. */
    std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> digest_context(EVP_MD_CTX_new(),
                                                                           &EVP_MD_CTX_free);
    if (!digest_context || EVP_DigestInit_ex(digest_context.get(), EVP_sha256(), nullptr) != 1) {
        return std::unexpected(make_error(ErrorCode::io_failure,
                                          "initialize persistent payload SHA-256 verification"));
    }
    /** @brief bounded streaming read buffer / Bounded streaming read buffer. */
    std::array<std::byte, 64U * 1024U> buffer{};
    /** @brief total bytes read through the opened object FD / Total bytes read through the opened
     * object FD. */
    std::size_t read_bytes{0U};
    for (;;) {
        /** @brief current read count / Current read count. */
        const ssize_t count = read(payload_fd.get(), buffer.data(), buffer.size());
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            return std::unexpected(
                make_error(ErrorCode::invocation_in_doubt,
                           "cannot prove the completed payload object is readable"));
        }
        if (count == 0) {
            break;
        }
        /** @brief safely converted current chunk size / Safely converted current chunk size. */
        const std::size_t chunk_size = static_cast<std::size_t>(count);
        if (chunk_size > request.byte_size - read_bytes ||
            EVP_DigestUpdate(digest_context.get(), buffer.data(), chunk_size) != 1) {
            return std::unexpected(
                make_error(ErrorCode::invocation_in_doubt,
                           "completed payload object changed while being verified"));
        }
        read_bytes += chunk_size;
    }
    /** @brief final SHA-256 digest / Final SHA-256 digest. */
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    /** @brief OpenSSL-reported digest size / OpenSSL-reported digest size. */
    unsigned int digest_size{0U};
    if (read_bytes != request.byte_size ||
        EVP_DigestFinal_ex(digest_context.get(), digest.data(), &digest_size) != 1 ||
        digest_size != digest.size()) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "completed payload object size or digest finalization is indeterminate"));
    }
    /** @brief payload metadata after digest / Payload metadata after digest. */
    struct stat after_digest {};
    if (fstat(payload_fd.get(), &after_digest) != 0 ||
        after_digest.st_dev != before_digest.st_dev ||
        after_digest.st_ino != before_digest.st_ino ||
        after_digest.st_size != before_digest.st_size ||
        after_digest.st_mtim.tv_sec != before_digest.st_mtim.tv_sec ||
        after_digest.st_mtim.tv_nsec != before_digest.st_mtim.tv_nsec ||
        after_digest.st_ctim.tv_sec != before_digest.st_ctim.tv_sec ||
        after_digest.st_ctim.tv_nsec != before_digest.st_ctim.tv_nsec ||
        render_sha256_hex(digest) != request.sha256) {
        return std::unexpected(
            make_error(ErrorCode::invocation_in_doubt,
                       "completed payload object changed or no longer matches its durable digest"));
    }
    return {};
}

} // namespace wspctl::detail
