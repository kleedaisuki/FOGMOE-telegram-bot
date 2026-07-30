#include "wspctl/infrastructure/journal.hpp"
#include "wspctl/infrastructure/image.hpp"
#include "wspctl/infrastructure/detail/launcher_transport.hpp"
#include "wspctl/infrastructure/detail/payload_replay.hpp"
#include "wspctl/infrastructure/detail/pidfd_control.hpp"
#include "wspctl/infrastructure/protocol.hpp"
#include "wspctl/infrastructure/runtime_gate.hpp"
#include "wspctl/infrastructure/sandbox.hpp"
#include "wspctl/infrastructure/supervisor.hpp"
#include "wspctl/infrastructure/xfs_project_quota.hpp"

#include <openssl/sha.h>

#include <chrono>
#include <array>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <sys/wait.h>

#include <signal.h>

namespace {

/** @brief 测试失败计数 / Test failure counter. */
unsigned int g_failures = 0;

/**
 * @brief 断言条件 / Assert a condition.
 * @param condition 条件 / Condition.
 * @param message 失败说明 / Failure message.
 */
void expect(bool condition, const std::string& message);

/**
 * @brief 渲染 SHA-256 小写十六进制 / Render a SHA-256 digest in lowercase hexadecimal.
 * @param value 要哈希的文字 / Text to hash.
 * @return 64-character lowercase digest / 64-character lowercase digest.
 */
[[nodiscard]] std::string sha256_hex(const std::string_view value) {
    /** @brief OpenSSL digest bytes / OpenSSL digest bytes. */
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest{};
    SHA256(reinterpret_cast<const unsigned char*>(value.data()), value.size(), digest.data());
    /** @brief hex digits / Hex digits. */
    constexpr std::string_view kDigits{"0123456789abcdef"};
    /** @brief rendered digest / Rendered digest. */
    std::string output;
    output.reserve(digest.size() * 2U);
    for (const unsigned char byte : digest) {
        output.push_back(kDigits[(byte >> 4U) & 0x0fU]);
        output.push_back(kDigits[byte & 0x0fU]);
    }
    return output;
}

/**
 * @brief 创建 quota service 应已提供的 per-runtime journal layout / Create the per-runtime journal layout that the quota service normally provides.
 * @param state_root 测试 state root / Test state root.
 * @param runtime_key 已验证测试 runtime / Validated test runtime.
 * @return None / None.
 * @note Journal 不再创建 global ``state_root/journal``；这个 helper 令无特权 unit test
 *       精确模拟 ready runtime 的 control project layout。 Journal no longer creates global
 *       ``state_root/journal``; this helper precisely simulates a ready runtime control-project layout for the unprivileged unit test.
 */
void prepare_runtime_journal(const std::filesystem::path& state_root, const std::string_view runtime_key) {
    std::error_code error;
    std::filesystem::create_directories(
        state_root / "runtimes" / sha256_hex(runtime_key) / "control" / "journal",
        error);
    expect(!error, "create per-runtime control journal layout");
}

/**
 * @brief 断言条件 / Assert a condition.
 * @param condition 条件 / Condition.
 * @param message 失败说明 / Failure message.
 */
void expect(const bool condition, const std::string& message) {
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL: " << message << '\n';
    }
}

/**
 * @brief 验证有界 cgroup 元数据读取不会依赖 eofbit / Verify bounded cgroup metadata reads do not depend on eofbit.
 * @note ``istreambuf_iterator`` 消耗全部内容后不保证设置 ``eofbit``；此回归测试通过公开的
 *       cgroup drain API 覆盖真实读取路径。/ ``istreambuf_iterator`` does not guarantee
 *       setting ``eofbit`` after consuming all content; this regression test covers the real read
 *       path through the public cgroup-drain API.
 */
void test_cgroup_metadata_read() {
    /** @brief mkdtemp 输入与结果缓冲区 / Input and result buffer for mkdtemp. */
    char template_path[] = "/tmp/wspctl-cgroup-read-XXXXXX";
    /** @brief 本测试独占的临时根目录 / Temporary root owned exclusively by this test. */
    char* const directory = mkdtemp(template_path);
    expect(directory != nullptr, "create cgroup metadata test root");
    if (directory == nullptr) {
        return;
    }

    /** @brief 测试 runtime 标识 / Test runtime identifier. */
    constexpr std::string_view kRuntimeKey = "cgroup-metadata-read";
    /** @brief 模拟的 runtime cgroup 目录 / Simulated runtime cgroup directory. */
    const std::filesystem::path runtime_cgroup =
        std::filesystem::path{directory} / "wspctl" / sha256_hex(kRuntimeKey);
    std::error_code error;
    std::filesystem::create_directories(runtime_cgroup, error);
    expect(!error, "create simulated runtime cgroup");
    if (error) {
        std::filesystem::remove_all(directory);
        return;
    }

    {
        /** @brief 模拟的 kernel cgroup.events 文件 / Simulated kernel cgroup.events file. */
        std::ofstream events(runtime_cgroup / "cgroup.events");
        events << "populated 0\n";
        expect(events.good(), "write simulated cgroup.events");
    }

    /** @brief 仅需 cgroup_root 的最小 sandbox 配置 / Minimal sandbox configuration requiring only cgroup_root. */
    wspctl::SandboxConfig config;
    config.cgroup_root = directory;
    const auto drained =
        wspctl::wait_runtime_cgroup_empty(config, std::string{kRuntimeKey});
    expect(drained.has_value(), "read a complete cgroup.events file without requiring eofbit");
    std::filesystem::remove_all(directory);
}

/**
 * @brief 验证首次 runtime 激活把缺失 cgroup 当作创建条件 / Verify first runtime activation treats a missing cgroup as a creation condition.
 * @note 普通文件系统只模拟创建状态机；真实 cgroup 控制文件语义由 privileged E2E 覆盖。/
 *       A regular filesystem only simulates the creation state machine; privileged E2E covers real cgroup control-file semantics.
 */
void test_missing_runtime_cgroup_is_created() {
    /** @brief mkdtemp 输入与结果缓冲区 / Input and result buffer for mkdtemp. */
    char template_path[] = "/tmp/wspctl-cgroup-create-XXXXXX";
    /** @brief 本测试独占的临时 cgroup 根 / Temporary cgroup root owned exclusively by this test. */
    char* const directory = mkdtemp(template_path);
    expect(directory != nullptr, "create missing-runtime cgroup test root");
    if (directory == nullptr) {
        return;
    }

    /** @brief 测试 runtime 标识 / Test runtime identifier. */
    constexpr std::string_view kRuntimeKey = "missing-runtime-cgroup";
    /** @brief 模拟的 wspctl cgroup 父节点 / Simulated wspctl cgroup parent. */
    const std::filesystem::path wspctl_cgroup =
        std::filesystem::path{directory} / "wspctl";
    /** @brief 模拟的首次 runtime cgroup 路径 / Simulated first-runtime cgroup path. */
    const std::filesystem::path runtime_cgroup =
        wspctl_cgroup / sha256_hex(kRuntimeKey);
    /** @brief 文件系统准备错误 / Filesystem preparation error. */
    std::error_code error;
    std::filesystem::create_directories(wspctl_cgroup, error);
    expect(!error, "create simulated wspctl cgroup parent");
    if (error) {
        std::filesystem::remove_all(directory);
        return;
    }
    {
        /** @brief 模拟已启用 controller 的父控制文件 / Parent control file simulating enabled controllers. */
        std::ofstream controllers(wspctl_cgroup / "cgroup.subtree_control");
        controllers << "cpu memory pids\n";
        expect(controllers.good(), "write simulated parent cgroup controllers");
    }

    /** @brief 仅需 cgroup_root 的最小 sandbox 配置 / Minimal sandbox configuration requiring only cgroup_root. */
    wspctl::SandboxConfig config;
    config.cgroup_root = directory;
    config.io_weight = 0U;
    /** @brief 首次 runtime cgroup 准备结果 / First-runtime cgroup preparation result. */
    const auto prepared =
        wspctl::prepare_runtime_cgroup(config, std::string{kRuntimeKey});
    expect(!prepared.has_value(), "regular filesystem cannot emulate kernel-created cgroup control files");
    expect(
        std::filesystem::is_directory(runtime_cgroup),
        "missing runtime cgroup advances through the normal creation path");
    if (!prepared) {
        expect(
            !prepared.error().message.starts_with("inspect existing runtime cgroup"),
            "missing runtime cgroup is not reported as an inspection failure");
    }
    std::filesystem::remove_all(directory);
}

/** @brief 验证 OCI identity 的类型与路径派生 / Verify OCI identity typing and path derivation. */
void test_oci_image_identity() {
    const std::string digest_text = "sha256-" + std::string(64U, 'a');
    expect(
        !wspctl::OciImageDigest::parse(digest_text).has_value(),
        "reject path-like image generations instead of accepting them as OCI digests");
    const auto digest =
        wspctl::OciImageDigest::parse("sha256:" + std::string(64U, 'a'));
    expect(digest.has_value(), "accept one canonical OCI sha256 digest");
    if (!digest) {
        return;
    }
    wspctl::SandboxConfig config;
    config.images_root = "/srv/fogmoe/images";
    config.image_digest = *digest;
    const auto root = wspctl::image_root(config);
    expect(
        root.has_value() &&
            *root ==
                std::filesystem::path{"/srv/fogmoe/images/sha256"} /
                    std::string(64U, 'a') / "rootfs",
        "derive the sole image path from the typed digest");
}

/** @brief 构造有效请求 / Construct a valid request. */
[[nodiscard]] wspctl::ExecuteRequest request() {
    wspctl::ExecuteRequest value{
        .runtime_key = "123e4567-e89b-12d3-a456-426614174000",
        .activation_id = "activation-test",
        .request_id = "request-test",
        .request_hash = std::string(64U, 'a'),
        .argv = {"/bin/sh", "-c", "printf ok"},
        .stdin_data = "",
        .cwd = "/workspace",
        .timeout = std::chrono::milliseconds(1'000),
        .output_limit = 4'096U,
    };
    return value;
}

/**
 * @brief 将 ASCII 测试文本转换为原始 bytes / Convert ASCII test text to raw bytes.
 * @param value 测试文本 / Test text.
 * @return 等值 raw bytes / Equivalent raw bytes.
 */
[[nodiscard]] std::vector<std::byte> bytes_from_text(const std::string_view value) {
    std::vector<std::byte> bytes;
    bytes.reserve(value.size());
    for (const unsigned char character : value) {
        bytes.push_back(static_cast<std::byte>(character));
    }
    return bytes;
}

/** @brief 构造有效文件写入请求 / Construct a valid file-ingress request. */
[[nodiscard]] wspctl::PayloadBeginRequest file_request() {
    return wspctl::PayloadBeginRequest{
        .runtime_key = request().runtime_key,
        .activation_id = "activation-file-test",
        .request_id = "file-request-test",
        .request_hash = std::string(64U, 'b'),
        .opaque_id = "ingress-file-test",
        .byte_size = 11U,
        .sha256 = sha256_hex("hello world"),
    };
}

/** @brief 构造与文件写入元数据严格相同的只读恢复查询 / Construct a read-only replay query with exactly the same file-ingress metadata. */
[[nodiscard]] wspctl::PayloadReplayRequest file_replay_request() {
    const wspctl::PayloadBeginRequest begin = file_request();
    return wspctl::PayloadReplayRequest{
        .runtime_key = begin.runtime_key,
        .request_id = begin.request_id,
        .request_hash = begin.request_hash,
        .opaque_id = begin.opaque_id,
        .byte_size = begin.byte_size,
        .sha256 = begin.sha256,
    };
}

/**
 * @brief 追加 little-endian u16 / Append a little-endian u16.
 * @param bytes 输出 bytes / Output bytes.
 * @param value 待写入数值 / Value to write.
 */
void append_u16(std::vector<std::byte>& bytes, const std::uint16_t value) {
    bytes.push_back(static_cast<std::byte>(value & 0xffU));
    bytes.push_back(static_cast<std::byte>((value >> 8U) & 0xffU));
}

/**
 * @brief 追加 little-endian u32 / Append a little-endian u32.
 * @param bytes 输出 bytes / Output bytes.
 * @param value 待写入数值 / Value to write.
 */
void append_u32(std::vector<std::byte>& bytes, const std::uint32_t value) {
    for (unsigned int index = 0U; index < 4U; ++index) {
        bytes.push_back(static_cast<std::byte>((value >> (index * 8U)) & 0xffU));
    }
}

/**
 * @brief 追加 v1 journal 长度前缀字符串 / Append a v1-journal length-prefixed string.
 * @param bytes 输出 bytes / Output bytes.
 * @param value 待写入文本 / Text to write.
 */
void append_journal_string(std::vector<std::byte>& bytes, const std::string_view value) {
    append_u32(bytes, static_cast<std::uint32_t>(value.size()));
    for (const unsigned char character : value) {
        bytes.push_back(static_cast<std::byte>(character));
    }
}

/**
 * @brief 写入一个既有 v1 completed execution journal / Write one existing v1 completed execution journal.
 * @param state_root 测试 state root / Test state root.
 * @param command 旧版本 command 请求 / Legacy command request.
 * @param result 旧版本完成结果 / Legacy completed result.
 * @return 成功时 true / True on success.
 * @note 这不是 production writer；它专门固定旧布局，确保 v2 reader 的无损迁移承诺可测试。
 *       This is not a production writer; it fixes the old layout solely to test v2 reader's lossless migration promise.
 */
[[nodiscard]] bool write_legacy_completed_execution_journal(
    const std::filesystem::path& state_root,
    const wspctl::ExecuteRequest& command,
    const wspctl::ExecutionResult& result) {
    const auto encoded_result = wspctl::encode_execution_result(result);
    if (!encoded_result) {
        return false;
    }
    std::vector<std::byte> bytes;
    bytes.reserve(256U + encoded_result->size());
    bytes.push_back(std::byte{'W'});
    bytes.push_back(std::byte{'S'});
    bytes.push_back(std::byte{'P'});
    bytes.push_back(std::byte{'J'});
    append_u16(bytes, 1U);
    bytes.push_back(static_cast<std::byte>(wspctl::JournalState::completed));
    bytes.push_back(std::byte{0});
    append_journal_string(bytes, command.request_hash);
    append_journal_string(bytes, wspctl::canonical_request_hash(command));
    append_u32(bytes, static_cast<std::uint32_t>(encoded_result->size()));
    bytes.insert(bytes.end(), encoded_result->begin(), encoded_result->end());
    std::string record_material = command.runtime_key;
    record_material.push_back('\0');
    record_material += command.request_id;
    const std::filesystem::path record_path = state_root / "runtimes" / sha256_hex(command.runtime_key) /
        "control" / "journal" / sha256_hex(record_material);
    std::ofstream output(record_path, std::ios::binary | std::ios::trunc);
    if (!output.is_open()) {
        return false;
    }
    output.write(
        reinterpret_cast<const char*>(bytes.data()),
        static_cast<std::streamsize>(bytes.size()));
    return output.good();
}

/** @brief 测试协议 round-trip 与截断拒绝 / Test protocol round-trip and truncation rejection. */
void test_protocol() {
    const wspctl::ExecuteRequest original = request();
    const auto payload = wspctl::encode_execute_request(original);
    expect(payload.has_value(), "encode execute request");
    if (!payload) {
        return;
    }
    const auto frame = wspctl::encode_frame(wspctl::MessageKind::execute, *payload);
    expect(frame.has_value(), "encode frame");
    if (!frame) {
        return;
    }
    const auto parsed_frame = wspctl::decode_frame(*frame);
    expect(parsed_frame.has_value(), "decode frame");
    if (parsed_frame) {
        const auto parsed_request = wspctl::decode_execute_request(parsed_frame->payload);
        expect(parsed_request.has_value() && parsed_request->argv == original.argv, "decode request argv");
    }
    std::vector<std::byte> truncated = *frame;
    truncated.pop_back();
    expect(!wspctl::decode_frame(truncated).has_value(), "reject truncated frame");

    const wspctl::RuntimeStatusRequest status_request{
        .runtime_key = original.runtime_key,
        .activation_id = "activation-status-test",
    };
    const auto encoded_status_request = wspctl::encode_runtime_status_request(status_request);
    expect(encoded_status_request.has_value(), "encode side-effect-free runtime status request");
    if (encoded_status_request) {
        const auto decoded_status_request = wspctl::decode_runtime_status_request(*encoded_status_request);
        expect(decoded_status_request.has_value() &&
                   decoded_status_request->runtime_key == status_request.runtime_key &&
                   decoded_status_request->activation_id == status_request.activation_id,
               "round-trip side-effect-free runtime status request");
    }
    const wspctl::RuntimeStatusResult status_result{
        .runtime_key = original.runtime_key,
        .state = wspctl::domain::RuntimeState::ready,
        .active_activation_id = status_request.activation_id,
        .handle_activation_matches = true,
        .supervisor_alive = true,
        .idle_for = std::chrono::milliseconds(42),
        .idle_ttl = std::chrono::minutes(15),
        .borrowed_dispatches = 2U,
        .cleanup_pending = false,
    };
    const auto encoded_status_result = wspctl::encode_runtime_status_result(status_result);
    expect(encoded_status_result.has_value(), "encode allowlisted runtime status result");
    if (encoded_status_result) {
        const auto decoded_status_result = wspctl::decode_runtime_status_result(*encoded_status_result);
        expect(decoded_status_result.has_value() &&
                   decoded_status_result->state == wspctl::domain::RuntimeState::ready &&
                   decoded_status_result->active_activation_id == status_result.active_activation_id &&
                   decoded_status_result->handle_activation_matches &&
                   decoded_status_result->supervisor_alive &&
                   decoded_status_result->idle_for == status_result.idle_for &&
                   decoded_status_result->idle_ttl == status_result.idle_ttl &&
                   decoded_status_result->borrowed_dispatches == 2U &&
                   !decoded_status_result->cleanup_pending,
               "round-trip fixed allowlisted runtime status result");
        const auto status_frame = wspctl::encode_frame(wspctl::MessageKind::runtime_status_result, *encoded_status_result);
        const auto decoded_status_frame = status_frame ? wspctl::decode_frame(*status_frame)
                                                       : wspctl::Result<wspctl::Frame>{std::unexpected(status_frame.error())};
        expect(decoded_status_frame.has_value() &&
                   decoded_status_frame->kind == wspctl::MessageKind::runtime_status_result,
               "recognize runtime status result frame kind");
    }
    wspctl::RuntimeStatusResult invalid_status = status_result;
    invalid_status.state = wspctl::domain::RuntimeState::failed;
    invalid_status.active_activation_id.reset();
    expect(!wspctl::validate_runtime_status_result(invalid_status).has_value(),
           "reject failed runtime claiming a live reusable supervisor");

    const wspctl::PayloadBeginRequest file = file_request();
    const auto encoded_file_begin = wspctl::encode_payload_begin_request(file);
    expect(encoded_file_begin.has_value(), "encode file begin request");
    if (encoded_file_begin) {
        const auto decoded_file_begin = wspctl::decode_payload_begin_request(*encoded_file_begin);
        expect(decoded_file_begin.has_value() && decoded_file_begin->opaque_id == file.opaque_id &&
                   decoded_file_begin->sha256 == file.sha256,
               "round-trip file begin request");
    }
    const wspctl::PayloadReplayRequest replay = file_replay_request();
    const auto encoded_replay = wspctl::encode_payload_replay_request(replay);
    expect(encoded_replay.has_value(), "encode activation-free file replay request");
    if (encoded_replay) {
        const auto decoded_replay = wspctl::decode_payload_replay_request(*encoded_replay);
        expect(decoded_replay.has_value() && decoded_replay->runtime_key == replay.runtime_key &&
                   decoded_replay->request_id == replay.request_id && decoded_replay->opaque_id == replay.opaque_id &&
                   decoded_replay->byte_size == replay.byte_size && decoded_replay->sha256 == replay.sha256,
               "round-trip activation-free file replay request");
        const auto replay_frame = wspctl::encode_frame(wspctl::MessageKind::payload_replay, *encoded_replay);
        const auto decoded_replay_frame = replay_frame ? wspctl::decode_frame(*replay_frame)
                                                        : wspctl::Result<wspctl::Frame>{std::unexpected(replay_frame.error())};
        expect(decoded_replay_frame.has_value() && decoded_replay_frame->kind == wspctl::MessageKind::payload_replay,
               "recognize read-only file replay frame kind");
    }
    expect(
        wspctl::canonical_payload_hash(file) == wspctl::canonical_payload_hash(replay),
        "file replay canonical metadata hash matches original ingress without activation");
    const wspctl::PayloadChunk file_chunk{
        .request_id = file.request_id,
        .bytes = bytes_from_text("hello world"),
    };
    const auto encoded_file_chunk = wspctl::encode_payload_chunk(file_chunk);
    expect(encoded_file_chunk.has_value(), "encode file chunk");
    if (encoded_file_chunk) {
        const auto decoded_file_chunk = wspctl::decode_payload_chunk(*encoded_file_chunk);
        expect(decoded_file_chunk.has_value() && decoded_file_chunk->bytes == file_chunk.bytes,
               "round-trip file chunk");
    }
    const wspctl::PayloadAck acknowledgement{
        .request_id = file.request_id,
        .stage = wspctl::PayloadAckStage::sealed,
        .received_bytes = file.byte_size,
    };
    const auto encoded_acknowledgement = wspctl::encode_payload_ack(acknowledgement);
    expect(encoded_acknowledgement.has_value(), "encode file acknowledgement");
    if (encoded_acknowledgement) {
        const auto decoded_acknowledgement = wspctl::decode_payload_ack(*encoded_acknowledgement);
        expect(decoded_acknowledgement.has_value() && decoded_acknowledgement->stage == acknowledgement.stage &&
                   decoded_acknowledgement->received_bytes == acknowledgement.received_bytes,
               "round-trip file acknowledgement");
    }
    wspctl::PayloadBeginRequest unsafe_file = file;
    unsafe_file.opaque_id = "../escape";
    expect(!wspctl::validate_payload_begin_request(unsafe_file).has_value(), "reject traversal-shaped opaque file identifier");
    wspctl::PayloadReplayRequest unsafe_replay = replay;
    unsafe_replay.opaque_id = "../escape";
    expect(!wspctl::validate_payload_replay_request(unsafe_replay).has_value(), "reject traversal-shaped opaque replay identifier");
    std::array<int, 2> pair{-1, -1};
    const int passed_fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
    expect(socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, pair.data()) == 0 && passed_fd >= 0,
           "create ancillary-data protocol test channel");
    if (pair[0] >= 0 && pair[1] >= 0 && passed_fd >= 0) {
        iovec vector{.iov_base = const_cast<std::byte*>(frame->data()), .iov_len = frame->size()};
        std::array<std::byte, CMSG_SPACE(sizeof(int))> control{};
        msghdr message{};
        message.msg_iov = &vector;
        message.msg_iovlen = 1U;
        message.msg_control = control.data();
        message.msg_controllen = control.size();
        cmsghdr* header = CMSG_FIRSTHDR(&message);
        header->cmsg_level = SOL_SOCKET;
        header->cmsg_type = SCM_RIGHTS;
        header->cmsg_len = CMSG_LEN(sizeof(int));
        std::memcpy(CMSG_DATA(header), &passed_fd, sizeof(passed_fd));
        expect(sendmsg(pair[0], &message, MSG_NOSIGNAL) == static_cast<ssize_t>(frame->size()),
               "send frame carrying forbidden SCM_RIGHTS");
        expect(!wspctl::receive_frame(pair[1]).has_value(), "reject and close SCM_RIGHTS on public control frame");
    }
    if (passed_fd >= 0) {
        close(passed_fd);
    }
    if (pair[0] >= 0) {
        close(pair[0]);
    }
    if (pair[1] >= 0) {
        close(pair[1]);
    }
}

/**
 * @brief 绕过受测 transport 构造一个原始 SCM_RIGHTS 数据报 / Construct a raw SCM_RIGHTS datagram bypassing the transport under test.
 * @param socket_fd 发送端 SOCK_SEQPACKET FD / Sending SOCK_SEQPACKET FD.
 * @param bytes 非空 wire bytes / Non-empty wire bytes.
 * @param fds 要附带的 FD / FDs to attach.
 * @return 内核接受完整数据报时为真 / True when the kernel accepted the complete datagram.
 * @note 此 helper 只用于构造超过 production 上限的 hostile peer 输入。 This helper exists only
 *       to construct hostile peer input that exceeds the production limit.
 */
[[nodiscard]] bool send_raw_scm_rights_packet(
    const int socket_fd,
    const std::span<const std::byte> bytes,
    const std::span<const int> fds) {
    if (socket_fd < 0 || bytes.empty() || fds.empty()) {
        return false;
    }
    std::vector<std::byte> control(CMSG_SPACE(fds.size() * sizeof(int)));
    iovec vector{.iov_base = const_cast<std::byte*>(bytes.data()), .iov_len = bytes.size()};
    msghdr message{};
    message.msg_iov = &vector;
    message.msg_iovlen = 1U;
    message.msg_control = control.data();
    message.msg_controllen = control.size();
    cmsghdr* const header = CMSG_FIRSTHDR(&message);
    if (header == nullptr) {
        return false;
    }
    header->cmsg_level = SOL_SOCKET;
    header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(static_cast<unsigned int>(fds.size() * sizeof(int)));
    std::memcpy(CMSG_DATA(header), fds.data(), fds.size() * sizeof(int));
    return sendmsg(socket_fd, &message, MSG_NOSIGNAL) == static_cast<ssize_t>(bytes.size());
}

/**
 * @brief 测试 fork-server SCM_RIGHTS 的五 FD 精确边界 / Test the fork-server SCM_RIGHTS exact five-FD boundary.
 *
 * 该测试直接调用生产 launcher transport：五个 FD 必须 round-trip，六个 FD 必须在发送端拒绝，
 * hostile peer 原始发送的六个 FD 也必须在接收端 fail closed。/ This test calls the production
 * launcher transport directly: five FDs must round-trip, six must be rejected at send time, and
 * a hostile peer's raw six-FD datagram must also fail closed at receive time.
 */
void test_launcher_scm_rights_contract() {
    namespace transport = wspctl::detail::launcher_transport;
    /** @brief 用于 transport round-trip 的最小非空 wire / Minimal non-empty wire for the transport round-trip. */
    constexpr std::array<std::byte, 1U> kWire{static_cast<std::byte>(0x42U)};
    /** @brief 生产上限加一，以覆盖拒绝边界 / Production limit plus one to cover the rejection boundary. */
    std::array<int, transport::kMaxFileDescriptors + 1U> descriptors{};
    descriptors.fill(-1);
    bool descriptors_ready = true;
    for (int& descriptor : descriptors) {
        descriptor = open("/dev/null", O_RDONLY | O_CLOEXEC);
        descriptors_ready = descriptors_ready && descriptor >= 0;
    }
    expect(descriptors_ready, "open six SCM_RIGHTS test descriptors");

    /** @brief 正常 production send/receive 使用的 socketpair / Socketpair used by the normal production send/receive path. */
    std::array<int, 2U> accepted_pair{-1, -1};
    const bool accepted_pair_ready = socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, accepted_pair.data()) == 0;
    expect(accepted_pair_ready, "create five-FD launcher SCM_RIGHTS socketpair");
    if (descriptors_ready && accepted_pair_ready) {
        const std::span<const int> five_descriptors{descriptors.data(), transport::kMaxFileDescriptors};
        const auto sent = transport::send_launcher_packet(accepted_pair[0], kWire, five_descriptors);
        expect(sent.has_value(), "send exactly five launcher SCM_RIGHTS FDs");
        if (sent) {
            auto received = transport::receive_launcher_packet(accepted_pair[1]);
            expect(received.has_value(), "receive exactly five launcher SCM_RIGHTS FDs");
            if (received) {
                expect(received->fd_count == transport::kMaxFileDescriptors &&
                           received->bytes.size() == kWire.size() && received->bytes.front() == kWire.front(),
                       "five launcher SCM_RIGHTS FDs and wire bytes round-trip intact");
                bool received_close_on_exec = true;
                for (std::size_t index = 0U; index < received->fd_count; ++index) {
                    const int flags = fcntl(received->fds[index], F_GETFD);
                    received_close_on_exec = received_close_on_exec && flags >= 0 && (flags & FD_CLOEXEC) != 0;
                }
                expect(received_close_on_exec, "received launcher SCM_RIGHTS FDs have FD_CLOEXEC");
                transport::close_launcher_packet_fds(*received);
            }
        }
        const std::span<const int> six_descriptors{descriptors};
        const auto rejected_send = transport::send_launcher_packet(accepted_pair[0], kWire, six_descriptors);
        expect(!rejected_send && rejected_send.error().code == wspctl::ErrorCode::invalid_argument,
               "reject a sixth launcher SCM_RIGHTS FD before sendmsg");
    }

    /** @brief hostile peer 溢出接收边界时使用的独立 socketpair / Separate socketpair for hostile-peer receive-boundary overflow. */
    std::array<int, 2U> rejected_pair{-1, -1};
    const bool rejected_pair_ready = socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, rejected_pair.data()) == 0;
    expect(rejected_pair_ready, "create six-FD hostile launcher SCM_RIGHTS socketpair");
    if (descriptors_ready && rejected_pair_ready) {
        const std::span<const int> six_descriptors{descriptors};
        const bool raw_sent = send_raw_scm_rights_packet(rejected_pair[0], kWire, six_descriptors);
        expect(raw_sent, "raw hostile peer sends six launcher SCM_RIGHTS FDs");
        if (raw_sent) {
            auto rejected_receive = transport::receive_launcher_packet(rejected_pair[1]);
            expect(!rejected_receive.has_value(), "fail closed when hostile peer sends a sixth launcher SCM_RIGHTS FD");
            if (rejected_receive) {
                transport::close_launcher_packet_fds(*rejected_receive);
            }
        }
    }

    for (int& descriptor : descriptors) {
        if (descriptor >= 0) {
            close(descriptor);
            descriptor = -1;
        }
    }
    for (int& descriptor : accepted_pair) {
        if (descriptor >= 0) {
            close(descriptor);
            descriptor = -1;
        }
    }
    for (int& descriptor : rejected_pair) {
        if (descriptor >= 0) {
            close(descriptor);
            descriptor = -1;
        }
    }
}

/**
 * @brief 测试 helper 的 pidfd 终止与所有权清理 / Test helper pidfd termination and owned-FD cleanup.
 *
 * 这是不需要 namespace 或 root 的单元测试：它验证 broker recovery 使用的 production helper
 * 会以 identity-stable pidfd 送达 ``SIGKILL``，并在成功、target 已退出和 syscall 失败时均消费
 * descriptor。 This rootless unit test verifies that the production helper used by broker
 * recovery delivers ``SIGKILL`` through an identity-stable pidfd and consumes the descriptor
 * on success, after target exit, and on syscall failure.
 */
void test_pidfd_terminal_signal_consumes_owned_fd() {
#ifndef SYS_pidfd_open
    // The broker itself fails closed without pidfd_open; keep the ordinary unit suite buildable
    // on old libc headers while privileged E2E remains unavailable there.
    return;
#else
    /** @brief 持续等待 SIGKILL 的 direct child / Direct child waiting until SIGKILL. */
    const pid_t live_child = fork();
    expect(live_child >= 0, "fork live child for pidfd terminal-signal test");
    if (live_child > 0) {
        /** @brief 由 production helper 消费的 live child pidfd / Live-child pidfd consumed by the production helper. */
        int owned_pidfd = static_cast<int>(syscall(SYS_pidfd_open, live_child, 0U));
        expect(owned_pidfd >= 0, "open identity-stable pidfd for live child");
        /** @brief 用于验证实际 close 的原始 descriptor number / Original descriptor number used to verify the actual close. */
        const int original_pidfd = owned_pidfd;
        /** @brief 通过 pidfd SIGKILL 得到的 terminal 判定 / Terminal result from pidfd SIGKILL. */
        const bool terminal = owned_pidfd >= 0 && wspctl::detail::signal_and_close_pidfd(owned_pidfd, SIGKILL);
        expect(terminal, "deliver SIGKILL through the owned pidfd");
        expect(owned_pidfd == -1, "successful pidfd termination consumes the owned FD");
        if (original_pidfd >= 0) {
            errno = 0;
            expect(fcntl(original_pidfd, F_GETFD) == -1 && errno == EBADF,
                   "successful pidfd termination closes the original descriptor exactly once");
        }
        if (!terminal) {
            static_cast<void>(kill(live_child, SIGKILL));
        }
        /** @brief live child 的 wait status / Wait status of the live child. */
        int live_status{};
        /** @brief live child 的 waitpid 返回值 / waitpid return value for the live child. */
        const pid_t live_waited = waitpid(live_child, &live_status, 0);
        expect(live_waited == live_child && WIFSIGNALED(live_status) && WTERMSIG(live_status) == SIGKILL,
               "pidfd terminal-signal child exits by SIGKILL");
    } else if (live_child == 0) {
        for (;;) {
            pause();
        }
    }

    /** @brief 已退出 child，用于 ESRCH 可接受终态 / Exited child for the accepted ESRCH terminal state. */
    const pid_t exited_child = fork();
    expect(exited_child >= 0, "fork exited child for pidfd ESRCH test");
    if (exited_child > 0) {
        /** @brief child 尚可 wait 时取得的 pidfd / pidfd acquired while the child is still waitable. */
        int exited_pidfd = static_cast<int>(syscall(SYS_pidfd_open, exited_child, 0U));
        expect(exited_pidfd >= 0, "open pidfd before reaping exited child");
        /** @brief exited child 的 wait status / Wait status of the exited child. */
        int exited_status{};
        /** @brief exited child 的 waitpid 返回值 / waitpid return value for the exited child. */
        const pid_t exited_waited = waitpid(exited_child, &exited_status, 0);
        expect(exited_waited == exited_child && WIFEXITED(exited_status), "reap exited pidfd test child");
        /** @brief 用于验证 ESRCH path close 的 descriptor number / Descriptor number used to verify close on the ESRCH path. */
        const int original_pidfd = exited_pidfd;
        /** @brief 已退出 target 的 terminal 判定 / Terminal result for the exited target. */
        const bool already_terminal = exited_pidfd >= 0 && wspctl::detail::signal_and_close_pidfd(exited_pidfd, SIGKILL);
        expect(already_terminal, "treat ESRCH for an already reaped pidfd target as terminal");
        expect(exited_pidfd == -1, "ESRCH pidfd path consumes the owned FD");
        if (original_pidfd >= 0) {
            errno = 0;
            expect(fcntl(original_pidfd, F_GETFD) == -1 && errno == EBADF,
                   "ESRCH pidfd path closes the original descriptor");
        }
    } else if (exited_child == 0) {
        _exit(0);
    }

    /** @brief 被外部关闭的 pidfd，覆盖 syscall 失败仍消费所有权 / Externally closed pidfd covering ownership consumption after syscall failure. */
    const pid_t invalid_child = fork();
    expect(invalid_child >= 0, "fork child for invalid-pidfd error path");
    if (invalid_child > 0) {
        /** @brief 故意在 helper 前关闭的 child pidfd / Child pidfd deliberately closed before the helper. */
        int invalid_pidfd = static_cast<int>(syscall(SYS_pidfd_open, invalid_child, 0U));
        expect(invalid_pidfd >= 0, "open pidfd for invalid-descriptor error path");
        if (invalid_pidfd >= 0) {
            /** @brief 用于验证 error path 无泄漏的原 descriptor number / Original descriptor number used to verify no error-path leak. */
            const int original_pidfd = invalid_pidfd;
            static_cast<void>(close(invalid_pidfd));
            expect(!wspctl::detail::signal_and_close_pidfd(invalid_pidfd, SIGKILL) && invalid_pidfd == -1,
                   "invalid pidfd error path still consumes its owned FD slot");
            errno = 0;
            expect(fcntl(original_pidfd, F_GETFD) == -1 && errno == EBADF,
                   "invalid pidfd error path never resurrects or leaks the descriptor");
        }
        static_cast<void>(kill(invalid_child, SIGKILL));
        /** @brief invalid-pidfd child 的 wait status / Wait status of the invalid-pidfd child. */
        int invalid_status{};
        /** @brief invalid-pidfd child 的 waitpid 返回值 / waitpid return value for the invalid-pidfd child. */
        const pid_t invalid_waited = waitpid(invalid_child, &invalid_status, 0);
        expect(invalid_waited == invalid_child && WIFSIGNALED(invalid_status) && WTERMSIG(invalid_status) == SIGKILL,
               "invalid pidfd error path never signals an unrelated process");
    } else if (invalid_child == 0) {
        for (;;) {
            pause();
        }
    }
#endif
}

/** @brief 测试 journal pending/completed/conflict 语义 / Test journal pending/completed/conflict semantics. */
void test_journal() {
    char template_path[] = "/tmp/wspctl-journal-XXXXXX";
    char* directory = mkdtemp(template_path);
    expect(directory != nullptr, "create journal temp directory");
    if (directory == nullptr) {
        return;
    }
    wspctl::ExecuteRequest command = request();
    prepare_runtime_journal(directory, command.runtime_key);
    wspctl::Journal journal(directory);
    expect(journal.begin(command).has_value(), "persist pending journal");
    const auto pending = journal.lookup(command.runtime_key, command.request_id);
    expect(pending.has_value() && pending->has_value() && (*pending)->state == wspctl::JournalState::pending, "read pending journal");
    wspctl::ExecutionResult result{
        .request_id = command.request_id,
        .exit_code = 0,
        .timed_out = false,
        .truncated = false,
        .replayed = false,
        .stdout_data = "ok",
        .stderr_data = "",
    };
    expect(journal.complete(command, result).has_value(), "persist completed journal");
    const auto completed = journal.lookup(command.runtime_key, command.request_id);
    expect(completed.has_value() && completed->has_value() && (*completed)->operation == wspctl::JournalOperation::execution &&
               (*completed)->execution_result.has_value(),
           "read completed execution journal");
    wspctl::ExecuteRequest mismatched_execution = command;
    mismatched_execution.request_id = "mismatched-execution-receipt";
    expect(journal.begin(mismatched_execution).has_value(), "create pending journal for mismatched execution receipt");
    wspctl::ExecutionResult wrong_execution_receipt = result;
    wrong_execution_receipt.request_id = "another-execution-request";
    expect(!journal.complete(mismatched_execution, wrong_execution_receipt).has_value(),
           "reject execution receipt whose request ID differs from journal filename identity");
    wspctl::ExecuteRequest replayed_execution = command;
    replayed_execution.request_id = "replayed-execution-receipt";
    expect(journal.begin(replayed_execution).has_value(), "create pending journal for replayed execution receipt");
    wspctl::ExecutionResult replayed_execution_receipt = result;
    replayed_execution_receipt.request_id = replayed_execution.request_id;
    replayed_execution_receipt.replayed = true;
    expect(!journal.complete(replayed_execution, replayed_execution_receipt).has_value(),
           "reject a replay-marked execution receipt from durable persistence");
    wspctl::ExecuteRequest conflicting = command;
    conflicting.request_hash = std::string(64U, 'b');
    expect((*completed)->request_hash != conflicting.request_hash, "detect differing semantic hash");

    wspctl::ExecuteRequest legacy_command = command;
    legacy_command.request_id = "legacy-execution-test";
    legacy_command.request_hash = std::string(64U, 'c');
    wspctl::ExecutionResult legacy_result = result;
    legacy_result.request_id = legacy_command.request_id;
    legacy_result.stdout_data = "legacy";
    expect(write_legacy_completed_execution_journal(directory, legacy_command, legacy_result),
           "write a durable v1 execution journal fixture");
    const auto migrated_legacy = journal.lookup(legacy_command.runtime_key, legacy_command.request_id);
    expect(migrated_legacy.has_value() && migrated_legacy->has_value() &&
               (*migrated_legacy)->operation == wspctl::JournalOperation::execution &&
               (*migrated_legacy)->execution_result.has_value() &&
               (*migrated_legacy)->execution_result->stdout_data == "legacy",
           "read existing v1 execution journal as v2 execution record");
    wspctl::ExecuteRequest malformed_legacy_command = command;
    malformed_legacy_command.request_id = "legacy-wrong-receipt";
    malformed_legacy_command.request_hash = std::string(64U, 'd');
    wspctl::ExecutionResult malformed_legacy_result = result;
    malformed_legacy_result.request_id = "legacy-other-request";
    expect(write_legacy_completed_execution_journal(directory, malformed_legacy_command, malformed_legacy_result),
           "write a v1 fixture with mismatched receipt identity");
    expect(!journal.lookup(malformed_legacy_command.runtime_key, malformed_legacy_command.request_id).has_value(),
           "fail closed when a v1 completed receipt differs from its filename request identity");

    wspctl::PayloadBeginRequest file = file_request();
    expect(journal.begin_payload(file).has_value(), "persist pending file journal after seal");
    const auto pending_file = journal.lookup(file.runtime_key, file.request_id);
    expect(pending_file.has_value() && pending_file->has_value() &&
               (*pending_file)->operation == wspctl::JournalOperation::payload &&
               (*pending_file)->state == wspctl::JournalState::pending,
           "read pending file journal");
    const wspctl::PayloadResult file_result{
        .request_id = file.request_id,
        .replayed = false,
        .path = "/workspace/uploads/" + file.opaque_id + "/payload",
        .byte_size = file.byte_size,
        .sha256 = file.sha256,
    };
    expect(journal.complete_payload(file, file_result).has_value(), "persist completed file journal");
    const auto completed_file = journal.lookup(file.runtime_key, file.request_id);
    expect(completed_file.has_value() && completed_file->has_value() &&
               (*completed_file)->operation == wspctl::JournalOperation::payload &&
               (*completed_file)->payload_result.has_value() &&
               (*completed_file)->payload_result->path == file_result.path,
           "read completed file journal receipt");
    wspctl::PayloadBeginRequest replayed_file = file;
    replayed_file.request_id = "replayed-file-receipt";
    replayed_file.opaque_id = "replayed-file-ingress";
    expect(journal.begin_payload(replayed_file).has_value(), "create pending journal for replayed file receipt");
    wspctl::PayloadResult replayed_file_receipt{
        .request_id = replayed_file.request_id,
        .replayed = true,
        .path = "/workspace/uploads/" + replayed_file.opaque_id + "/payload",
        .byte_size = replayed_file.byte_size,
        .sha256 = replayed_file.sha256,
    };
    expect(!journal.complete_payload(replayed_file, replayed_file_receipt).has_value(),
           "reject a replay-marked file receipt from durable persistence");
    wspctl::PayloadBeginRequest race_file = file;
    race_file.request_id = "gate-race-file";
    race_file.opaque_id = "gate-race-ingress";
    const auto observed_absent = journal.lookup(race_file.runtime_key, race_file.request_id);
    expect(observed_absent.has_value() && !observed_absent->has_value(),
           "first contender observes an absent file journal");
    expect(journal.begin_payload(race_file).has_value(), "second contender persists a pending file journal");
    const auto observed_pending = journal.lookup(race_file.runtime_key, race_file.request_id);
    expect(observed_pending.has_value() && observed_pending->has_value() &&
               (*observed_pending)->state == wspctl::JournalState::pending,
           "gate-side reread observes pending and must return invocation-in-doubt");
    const wspctl::PayloadResult race_receipt{
        .request_id = race_file.request_id,
        .replayed = false,
        .path = "/workspace/uploads/" + race_file.opaque_id + "/payload",
        .byte_size = race_file.byte_size,
        .sha256 = race_file.sha256,
    };
    expect(journal.complete_payload(race_file, race_receipt).has_value(), "complete raced file journal");
    const auto observed_completed = journal.lookup(race_file.runtime_key, race_file.request_id);
    expect(observed_completed.has_value() && observed_completed->has_value() &&
               (*observed_completed)->payload_result.has_value(),
           "gate-side reread observes completed receipt and must replay");
    wspctl::ExecuteRequest cross_operation = command;
    cross_operation.request_id = file.request_id;
    expect(!journal.begin(cross_operation).has_value(), "reject journal operation reuse under one request ID");
    std::filesystem::remove_all(directory);
}

/**
 * @brief 测试只读 attachment replay 的无状态缺失与对象证明语义 / Test attachment replay's stateless miss and object-proof semantics.
 *
 * 本测试刻意不构造 Broker、quota 或 RuntimeSession：它覆盖 replay 的两个纯 durable 边界。
 * 首先，缺失 record 只能报告 ``not_found`` 且不得创建 ``runtimes``；其次，已完成回执所指
 * payload 的 SHA-256 不匹配必须是 ``invocation_in_doubt``，绝不能把损坏对象当作可重新下载的
 * 正常 miss。 This deliberately constructs no Broker, quota, or RuntimeSession: it covers the
 * two pure durable boundaries of replay. First, a missing record must report ``not_found`` without
 * creating ``runtimes``; second, a SHA-256 mismatch in the object named by a completed receipt
 * must be ``invocation_in_doubt``, never a normal downloadable miss.
 */
void test_payload_replay_recovery_contract() {
    /** @brief isolated durable-state test root template / Isolated durable-state test-root template. */
    char template_path[] = "/tmp/wspctl-payload-replay-XXXXXX";
    /** @brief mkdtemp-created test root / Test root created by mkdtemp. */
    char* const directory = mkdtemp(template_path);
    expect(directory != nullptr, "create payload replay temp directory");
    if (directory == nullptr) {
        return;
    }
    /** @brief replay request with the same durable identity as file ingress / Replay request sharing the durable identity of file ingress. */
    const wspctl::PayloadReplayRequest replay = file_replay_request();
    /** @brief journal with no runtime tree or record / Journal with no runtime tree or record. */
    const wspctl::Journal empty_journal(directory);
    const auto absent = wspctl::detail::resolve_payload_replay_receipt(empty_journal, replay);
    expect(!absent && absent.error().code == wspctl::ErrorCode::not_found,
           "no durable replay record is the sole read-only not-found result");
    /** @brief no-write postcondition query error / No-write postcondition query error. */
    std::error_code no_write_error;
    expect(!std::filesystem::exists(std::filesystem::path(directory) / "runtimes", no_write_error) && !no_write_error,
           "missing replay receipt does not create journal/runtime state");

    prepare_runtime_journal(directory, replay.runtime_key);
    /** @brief journal backed by the explicitly provisioned test layout / Journal backed by the explicitly provisioned test layout. */
    const wspctl::Journal journal(directory);
    const wspctl::PayloadBeginRequest begin = file_request();
    expect(journal.begin_payload(begin).has_value(), "persist completed-replay payload pending record");
    /** @brief durable non-replayed ingress receipt / Durable non-replayed ingress receipt. */
    const wspctl::PayloadResult stored_receipt{
        .request_id = replay.request_id,
        .replayed = false,
        .path = "/workspace/uploads/" + replay.opaque_id + "/payload",
        .byte_size = replay.byte_size,
        .sha256 = replay.sha256,
    };
    expect(journal.complete_payload(begin, stored_receipt).has_value(), "complete replay payload journal record");
    const auto resolved = wspctl::detail::resolve_payload_replay_receipt(journal, replay);
    expect(resolved.has_value() && resolved->replayed && resolved->request_id == replay.request_id,
           "completed payload journal resolves to an explicitly replayed receipt");
    if (!resolved) {
        std::filesystem::remove_all(directory);
        return;
    }

    /** @brief test-only runtime binding whose workspace needs no XFS for no-follow digest verification / Test-only runtime binding whose workspace needs no XFS for no-follow digest verification. */
    const wspctl::RuntimeQuotaBinding binding{
        .runtime_dir = std::filesystem::path(directory) / "runtime",
        .control_dir = std::filesystem::path(directory) / "runtime" / "control",
        .workspace_dir = std::filesystem::path(directory) / "runtime" / "workspace",
        .control_project_id = 2U,
        .workspace_project_id = 3U,
    };
    /** @brief fixed persistent payload parent / Fixed persistent payload parent. */
    const std::filesystem::path payload_parent = binding.workspace_dir / "upper" / "uploads" / replay.opaque_id;
    /** @brief directory-creation error / Directory-creation error. */
    std::error_code create_error;
    std::filesystem::create_directories(payload_parent, create_error);
    expect(!create_error, "create persistent payload path for replay verifier");
    /** @brief fixed persistent payload object path / Fixed persistent payload object path. */
    const std::filesystem::path payload_path = payload_parent / "payload";
    {
        /** @brief matching payload output stream / Matching payload output stream. */
        std::ofstream output(payload_path, std::ios::binary | std::ios::trunc);
        output << "hello world";
        expect(output.good(), "write matching persistent replay payload");
    }
    expect(wspctl::detail::verify_replayable_payload_object(binding, replay, *resolved).has_value(),
           "matching persistent payload verifies read-only");
    {
        /** @brief deliberately digest-mismatched payload output stream / Deliberately digest-mismatched payload output stream. */
        std::ofstream output(payload_path, std::ios::binary | std::ios::trunc);
        output << "HELLO WORLD";
        expect(output.good(), "write same-size digest-mismatched replay payload");
    }
    const auto mismatched_object = wspctl::detail::verify_replayable_payload_object(binding, replay, *resolved);
    expect(!mismatched_object && mismatched_object.error().code == wspctl::ErrorCode::invocation_in_doubt,
           "persistent replay object SHA-256 mismatch is invocation-in-doubt");
    std::filesystem::remove_all(directory);
}

/** @brief 测试 runtime gate 拒绝共享 workspace 并发 / Test runtime gate rejects concurrent shared-workspace use. */
void test_runtime_gate() {
    wspctl::RuntimeExecutionGate gate;
    const auto first = gate.try_acquire("123e4567-e89b-12d3-a456-426614174000");
    expect(first.has_value(), "acquire first runtime lease");
    const auto second = gate.try_acquire("123e4567-e89b-12d3-a456-426614174000");
    expect(!second.has_value() && second.error().code == wspctl::ErrorCode::busy, "reject concurrent runtime lease");
}

/** @brief 测试 quota 配置拒绝 generic/root filesystem fallback / Test quota configuration rejects generic/root-filesystem fallback. */
void test_xfs_quota_fail_closed_contract() {
    wspctl::XfsProjectQuotaConfig malformed{
        .mount_path = "/",
        .project_id_min = 2U,
        .project_id_max = 3U,
        .control_hard_bytes = 513U,
        .control_hard_inodes = 1U,
        .workspace_hard_bytes = 512U,
        .workspace_hard_inodes = 1U,
        .global_admission_bytes = 1'025U,
        .global_admission_inodes = 2U,
        .system_reserve_bytes = 0U,
        .system_reserve_inodes = 0U,
    };
    const auto malformed_result = wspctl::preflight_xfs_project_quota(malformed, "/tmp");
    expect(!malformed_result && malformed_result.error().code == wspctl::ErrorCode::invalid_argument,
           "reject XFS byte hard limit that is not an exact 512-byte quota block count");

    wspctl::XfsProjectQuotaConfig root_filesystem = malformed;
    root_filesystem.control_hard_bytes = 512U;
    root_filesystem.global_admission_bytes = 1'024U;
    const auto root_result = wspctl::preflight_xfs_project_quota(root_filesystem, "/tmp");
    expect(!root_result && root_result.error().code == wspctl::ErrorCode::sandbox_preflight_failed,
           "refuse host root filesystem instead of falling back to generic disk accounting");
}

/** @brief 测试 timeout、输出截断和信号退出码 / Test timeout, output truncation, and signal exit code. */
void test_supervisor() {
    char template_path[] = "/tmp/wspctl-workspace-XXXXXX";
    char* workspace = mkdtemp(template_path);
    expect(workspace != nullptr, "create supervisor workspace");
    if (workspace == nullptr) {
        return;
    }
    wspctl::SupervisorConfig config;
    config.control_fd = -1;
    config.test_workspace_root = workspace;
    config.sandbox_uid = getuid();
    config.sandbox_gid = getgid();
    wspctl::Supervisor supervisor(std::move(config));
    wspctl::ExecuteRequest environment_request = request();
    environment_request.argv = {
        "/bin/sh",
        "-c",
        "printf '%s\\n' \"$HOME\" \"$USER\" \"$LOGNAME\" \"$SHELL\" \"$PATH\" \"$TMPDIR\" \"$LANG\" \"$LC_ALL\" \"$XDG_CACHE_HOME\"",
    };
    const auto environment_result = supervisor.execute_once(environment_request);
    expect(
        environment_result.has_value() &&
            environment_result->stdout_data ==
                "/workspace\nagent\nagent\n/bin/bash\n/usr/local/bin:/usr/bin:/bin\n/tmp\nC.UTF-8\nC.UTF-8\n/workspace/.cache\n",
        "expose the fixed named-Agent environment without inheriting broker state");
    /** @brief 合法嵌套 cwd / Valid nested cwd. */
    const std::filesystem::path nested_cwd =
        std::filesystem::path(workspace) / "project" / "nested";
    expect(
        std::filesystem::create_directories(nested_cwd),
        "create nested supervisor cwd");
    wspctl::ExecuteRequest nested_cwd_request = request();
    nested_cwd_request.cwd = "/workspace/project/nested";
    nested_cwd_request.argv = {"/bin/pwd"};
    const auto nested_cwd_result = supervisor.execute_once(nested_cwd_request);
    expect(
        nested_cwd_result.has_value() &&
            nested_cwd_result->exit_code == 0 &&
            nested_cwd_result->stdout_data == nested_cwd.string() + "\n",
        "resolve nested cwd beneath the pinned workspace FD");
    /** @brief 指向 workspace 内目录的 cwd symlink / Cwd symlink pointing to a directory inside the workspace. */
    const std::filesystem::path cwd_symlink =
        std::filesystem::path(workspace) / "cwd-link";
    expect(
        symlink(nested_cwd.c_str(), cwd_symlink.c_str()) == 0,
        "create supervisor cwd symlink");
    wspctl::ExecuteRequest symlink_cwd_request = request();
    symlink_cwd_request.cwd = "/workspace/cwd-link";
    symlink_cwd_request.argv = {"/bin/true"};
    const auto symlink_cwd_result = supervisor.execute_once(symlink_cwd_request);
    expect(
        symlink_cwd_result.has_value() &&
            symlink_cwd_result->exit_code == 126 &&
            symlink_cwd_result->stderr_data == "wsp-systemd: chdir failed\n",
        "reject cwd symlinks even when their target remains inside workspace");
    wspctl::ExecuteRequest missing_cwd_request = request();
    missing_cwd_request.cwd = "/workspace/does-not-exist";
    missing_cwd_request.argv = {"/bin/true"};
    const auto missing_cwd_result = supervisor.execute_once(missing_cwd_request);
    expect(
        missing_cwd_result.has_value() &&
            missing_cwd_result->exit_code == 126 &&
            missing_cwd_result->stderr_data == "wsp-systemd: chdir failed\n",
        "reject a missing cwd without falling back to workspace root");
    wspctl::ExecuteRequest timeout_request = request();
    timeout_request.argv = {"/bin/sh", "-c", "sleep 1"};
    timeout_request.timeout = std::chrono::milliseconds(30);
    const auto timeout_result = supervisor.execute_once(timeout_request);
    expect(timeout_result.has_value() && timeout_result->timed_out && !timeout_result->exit_code.has_value(), "enforce timeout with null exit code");
    wspctl::ExecuteRequest output_request = request();
    output_request.argv = {"/bin/sh", "-c", "yes x | head -c 4096"};
    output_request.output_limit = 64U;
    const auto output_result = supervisor.execute_once(output_request);
    expect(output_result.has_value() && output_result->truncated &&
               output_result->stdout_data.size() + output_result->stderr_data.size() <= output_request.output_limit,
           "truncate combined output");
    wspctl::ExecuteRequest noisy_timeout_request = request();
    noisy_timeout_request.argv = {"/bin/sh", "-c", "yes x"};
    noisy_timeout_request.timeout = std::chrono::milliseconds(30);
    noisy_timeout_request.output_limit = 64U;
    const auto noisy_started = std::chrono::steady_clock::now();
    const auto noisy_timeout_result = supervisor.execute_once(noisy_timeout_request);
    const auto noisy_elapsed = std::chrono::steady_clock::now() - noisy_started;
    expect(noisy_timeout_result.has_value() && noisy_timeout_result->timed_out &&
               noisy_elapsed < std::chrono::seconds(2),
           "enforce timeout even while stdout remains continuously readable");
    wspctl::ExecuteRequest signal_request = request();
    signal_request.argv = {"/bin/sh", "-c", "kill -KILL $$"};
    const auto signal_result = supervisor.execute_once(signal_request);
    expect(signal_result.has_value() && signal_result->exit_code.has_value() && *signal_result->exit_code == 137,
           "map SIGKILL to conventional exit code");
    wspctl::ExecuteRequest malformed_output_request = request();
    malformed_output_request.argv = {"/bin/sh", "-c", "printf '\\000\\377'"};
    const auto malformed_output = supervisor.execute_once(malformed_output_request);
    expect(malformed_output.has_value() && malformed_output->stdout_data == "??" &&
               malformed_output->stdout_data.find('\0') == std::string::npos,
           "normalize NUL and invalid UTF-8 output without leaving raw bytes");
    if (malformed_output) {
        const auto encoded = wspctl::encode_execution_result(*malformed_output);
        expect(encoded.has_value(), "encode normalized output");
        if (encoded) {
            const auto decoded = wspctl::decode_execution_result(*encoded);
            expect(decoded.has_value() && decoded->stdout_data == "??", "normalized output round-trips through protocol");
        }
        char journal_template[] = "/tmp/wspctl-sanitized-journal-XXXXXX";
        char* journal_directory = mkdtemp(journal_template);
        expect(journal_directory != nullptr, "create sanitized-output journal directory");
        if (journal_directory != nullptr) {
            prepare_runtime_journal(journal_directory, malformed_output_request.runtime_key);
            wspctl::Journal journal(journal_directory);
            expect(journal.begin(malformed_output_request).has_value(), "persist sanitized-output pending journal");
            expect(journal.complete(malformed_output_request, *malformed_output).has_value(), "persist sanitized-output completed journal");
            const auto replay = journal.lookup(malformed_output_request.runtime_key, malformed_output_request.request_id);
            expect(replay.has_value() && replay->has_value() && (*replay)->execution_result.has_value() &&
                       (*replay)->execution_result->stdout_data == "??",
                   "replay sanitized output instead of leaving journal pending");
            std::filesystem::remove_all(journal_directory);
        }
    }
    wspctl::ExecuteRequest unicode_boundary_request = request();
    unicode_boundary_request.argv = {"/bin/sh", "-c", "printf '\\303\\251'"};
    unicode_boundary_request.output_limit = 1U;
    const auto unicode_boundary = supervisor.execute_once(unicode_boundary_request);
    expect(unicode_boundary.has_value() && unicode_boundary->truncated && unicode_boundary->stdout_data.empty(),
           "never split a UTF-8 scalar at the final output-byte boundary");
    wspctl::ExecuteRequest malformed_sequence_request = request();
    malformed_sequence_request.argv = {"/bin/sh", "-c", "printf '\\300\\257\\342(\\241'"};
    const auto malformed_sequence = supervisor.execute_once(malformed_sequence_request);
    const std::string malformed_sequence_expected = std::string{"???"} + "(?";
    expect(malformed_sequence.has_value() && malformed_sequence->stdout_data == malformed_sequence_expected,
           "replace overlong and invalid-continuation bytes deterministically");
    wspctl::ExecuteRequest cross_read_request = request();
    cross_read_request.argv = {
        "/bin/sh",
        "-c",
        "head -c 16383 /dev/zero | tr '\\000' a; printf '\\303'; sleep 0.02; printf '\\251'",
    };
    cross_read_request.output_limit = 20'000U;
    const auto cross_read = supervisor.execute_once(cross_read_request);
    const std::string cross_read_expected = std::string(16'383U, 'a') + "\xc3\xa9";
    expect(cross_read.has_value() && cross_read->stdout_data == cross_read_expected,
           "preserve a complete UTF-8 scalar split across a 16KiB output-drain boundary");

    const wspctl::PayloadBeginRequest file = file_request();
    const auto file_begin = supervisor.begin_payload(file);
    expect(file_begin.has_value() && file_begin->stage == wspctl::PayloadAckStage::begun,
           "begin a task-owned streaming file ingress");
    const wspctl::PayloadChunk file_chunk{
        .request_id = file.request_id,
        .bytes = bytes_from_text("hello world"),
    };
    const auto file_appended = supervisor.append_payload(file_chunk);
    expect(file_appended.has_value() && file_appended->received_bytes == file.byte_size,
           "append one bounded file chunk");
    const wspctl::PayloadControlRequest file_control{.request_id = file.request_id};
    const auto file_sealed = supervisor.seal_payload(file_control);
    expect(file_sealed.has_value() && file_sealed->stage == wspctl::PayloadAckStage::sealed,
           "seal after exact bytes and SHA-256");
    const auto file_published = supervisor.publish_payload(file_control);
    expect(file_published.has_value() && file_published->path == "/workspace/uploads/" + file.opaque_id + "/payload",
           "atomically publish sealed file to fixed runtime path");
    const std::filesystem::path published_path =
        std::filesystem::path(workspace) / "uploads" / file.opaque_id / "payload";
    std::ifstream published_file(published_path, std::ios::binary);
    const std::string published_content{
        std::istreambuf_iterator<char>(published_file),
        std::istreambuf_iterator<char>()};
    expect(published_file.good() || published_file.eof(), "open published file");
    expect(published_content == "hello world", "published file preserves streamed bytes");
    struct stat published_metadata {};
    expect(stat(published_path.c_str(), &published_metadata) == 0 && S_ISREG(published_metadata.st_mode) &&
               (published_metadata.st_mode & 0777) == 0600,
           "published file is a private regular task-owned artifact");

    wspctl::PayloadBeginRequest aborted_file = file;
    aborted_file.request_id = "file-abort-test";
    aborted_file.opaque_id = "ingress-abort-test";
    aborted_file.byte_size = 5U;
    aborted_file.sha256 = sha256_hex("abort");
    const std::filesystem::path uploads_path = std::filesystem::path(workspace) / "uploads";
    expect(chmod(uploads_path.c_str(), 0777) == 0, "make a pre-existing uploads directory intentionally permissive");
    const auto aborted_begin = supervisor.begin_payload(aborted_file);
    expect(aborted_begin.has_value(), "begin abortable file ingress");
    struct stat uploads_metadata {};
    expect(stat(uploads_path.c_str(), &uploads_metadata) == 0 && (uploads_metadata.st_mode & 0777) == 0700,
           "normalize pre-existing uploads directory back to task-private mode");
    const wspctl::PayloadChunk aborted_chunk{
        .request_id = aborted_file.request_id,
        .bytes = bytes_from_text("abort"),
    };
    expect(supervisor.append_payload(aborted_chunk).has_value(), "write abortable file chunk");
    const wspctl::PayloadControlRequest abort_control{.request_id = aborted_file.request_id};
    const auto aborted = supervisor.abort_payload(abort_control);
    expect(aborted.has_value() && aborted->stage == wspctl::PayloadAckStage::aborted,
           "abort removes active PID1 file temporary");
    expect(!std::filesystem::exists(std::filesystem::path(workspace) / "uploads" / aborted_file.opaque_id / "payload"),
           "abort never publishes a final file path");
    std::filesystem::remove_all(workspace);
}

}  // namespace

/**
 * @brief CTest 入口 / CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_cgroup_metadata_read();
    test_missing_runtime_cgroup_is_created();
    test_oci_image_identity();
    test_protocol();
    test_launcher_scm_rights_contract();
    test_pidfd_terminal_signal_consumes_owned_fd();
    test_journal();
    test_payload_replay_recovery_contract();
    test_runtime_gate();
    test_xfs_quota_fail_closed_contract();
    test_supervisor();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
