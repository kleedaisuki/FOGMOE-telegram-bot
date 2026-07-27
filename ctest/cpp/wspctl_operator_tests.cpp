/**
 * @file wspctl_operator_tests.cpp
 * @brief 独立 operator shell DDD/协议/文件系统测试 / Independent operator-shell DDD, protocol, and filesystem tests.
 */

#include "wspctl/application/operator_workspace.hpp"
#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/operator_endpoint.hpp"
#include "wspctl/infrastructure/operator_protocol.hpp"
#include "wspctl/infrastructure/operator_workspace_reader.hpp"
#include "wspctl/infrastructure/protocol.hpp"

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <vector>

namespace {

/** @brief 测试失败计数 / Test failure count. */
unsigned int g_failures{0U};

/**
 * @brief 断言一个条件 / Assert one condition.
 * @param condition 待断言条件 / Condition to assert.
 * @param message 失败说明 / Failure description.
 */
void expect(const bool condition, const std::string& message) {
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL: " << message << '\n';
    }
}

/**
 * @brief 构造 canonical 测试 runtime / Construct a canonical test runtime.
 * @return 已验证 runtime / Validated runtime.
 */
[[nodiscard]] wspctl::domain::RuntimeId test_runtime() {
    const auto runtime = wspctl::domain::RuntimeId::parse("123e4567-e89b-12d3-a456-426614174000");
    expect(runtime.has_value(), "parse operator test runtime");
    return *runtime;
}

/**
 * @brief 构造安全的 kernel quota 值对象 / Construct a safe kernel-quota value object.
 * @return 已验证 quota / Validated quota.
 */
[[nodiscard]] wspctl::domain::WorkspaceQuotaUsage ready_quota() {
    const auto quota = wspctl::domain::WorkspaceQuotaUsage::create(1024U, 4096U, 2U, 64U);
    expect(quota.has_value(), "create ready operator quota");
    return *quota;
}

/**
 * @brief 构造具有安全 quota 的 ready status / Construct a ready status with a safe quota.
 * @param runtime status 所属 runtime / Runtime owned by the status.
 * @return allowlisted operator status / Allowlisted operator status.
 */
[[nodiscard]] wspctl::domain::OperatorWorkspaceStatus ready_status(const wspctl::domain::RuntimeId& runtime) {
    const auto status = wspctl::domain::OperatorWorkspaceStatus::create(
        runtime,
        wspctl::domain::WorkspacePersistence::ready,
        wspctl::domain::WorkspaceActivity::inactive,
        ready_quota());
    expect(status.has_value(), "create ready operator status");
    return *status;
}

/**
 * @brief 构造默认测试 runtime 的 ready status / Construct a ready status for the default test runtime.
 * @return allowlisted operator status / Allowlisted operator status.
 */
[[nodiscard]] wspctl::domain::OperatorWorkspaceStatus ready_status() {
    return ready_status(test_runtime());
}

/** @brief 测试 workspace logical-path 与 filename 编码领域约束 / Test workspace logical-path and filename-encoding domain constraints. */
void test_domain_path_and_filename_encoding() {
    expect(wspctl::domain::OperatorWorkspacePath::parse("/workspace/a/b").has_value(),
           "accept normalized workspace path");
    expect(!wspctl::domain::OperatorWorkspacePath::parse("/workspace/../host").has_value(),
           "reject workspace traversal");
    expect(!wspctl::domain::OperatorWorkspacePath::parse("/workspace/a/").has_value(),
           "reject trailing workspace separator");
    std::string nul_path{"/workspace/a"};
    nul_path.push_back('\0');
    expect(!wspctl::domain::OperatorWorkspacePath::parse(nul_path).has_value(),
           "reject NUL in workspace path");

    std::string raw_name{"line\n"};
    raw_name.push_back(static_cast<char>(0x1b));
    raw_name += "[31m";
    const auto encoded = wspctl::domain::encode_workspace_entry_name(raw_name);
    expect(encoded.has_value() && *encoded == "line%0A%1B%5B31m",
           "percent encode newline and ANSI escape bytes reversibly");
    expect(wspctl::domain::WorkspaceEntry::create(*encoded, wspctl::domain::WorkspaceEntryKind::regular_file, 1U).has_value(),
           "accept canonical safe filename encoding");
    expect(!wspctl::domain::WorkspaceEntry::create("line\n", wspctl::domain::WorkspaceEntryKind::regular_file, 1U).has_value(),
           "reject raw terminal-control filename bytes");
    expect(!wspctl::domain::WorkspaceEntry::create("%41", wspctl::domain::WorkspaceEntryKind::regular_file, 1U).has_value(),
           "reject non-canonical percent encoding of a display-safe byte");
    expect(!wspctl::domain::WorkspaceEntry::create(".", wspctl::domain::WorkspaceEntryKind::directory, 0U).has_value() &&
               !wspctl::domain::WorkspaceEntry::create("..", wspctl::domain::WorkspaceEntryKind::directory, 0U).has_value(),
           "reject POSIX dot names even though they are display-safe bytes");
}

/** @brief 测试 operator endpoint UID/path 隔离策略 / Test operator endpoint UID/path separation policy. */
void test_endpoint_separation_policy() {
    expect(wspctl::validate_operator_endpoint_separation(
               "/run/wspctl/bot/broker.sock", 65532U, "/run/wspctl/operator/broker.sock", 0U)
               .has_value(),
           "accept distinct Bot and root operator endpoints");
    expect(!wspctl::validate_operator_endpoint_separation(
                "/run/wspctl/bot/broker.sock", 65532U, "/run/wspctl/operator/broker.sock", 65532U)
                .has_value(),
           "reject an operator UID equal to Bot UID");
    expect(!wspctl::validate_operator_endpoint_separation(
                "/run/wspctl/same.sock", 65532U, "/run/wspctl/same.sock", 0U)
                .has_value(),
           "reject shared Bot and operator socket path");
    expect(!wspctl::validate_operator_endpoint_separation(
                "/run/wspctl/bot/broker.sock", 65532U, "/run/wspctl/bot/operator/broker.sock", 0U)
                .has_value(),
           "reject an operator endpoint nested in the Bot socket directory view");
    expect(!wspctl::validate_operator_endpoint_separation(
                "/run/wspctl/bot/operator/broker.sock", 65532U, "/run/wspctl/bot/broker.sock", 0U)
                .has_value(),
           "reject a Bot endpoint nested in the operator socket directory view");
    expect(wspctl::is_authorized_operator_peer(0U, 0U) && !wspctl::is_authorized_operator_peer(65532U, 0U),
           "authorize only the exact operator SO_PEERCRED UID");
}

/** @brief 测试 operator wire 与 Bot wire 的魔数隔离 / Test magic isolation between operator and Bot wires. */
void test_protocol_isolation_and_round_trip() {
    namespace op = wspctl::operator_protocol;
    const auto status_payload = op::encode_status_response(op::StatusResponse{.status = ready_status()});
    expect(status_payload.has_value(), "encode operator status response");
    const auto status_frame = op::encode_operator_frame(op::OperatorMessageKind::status_response, *status_payload);
    expect(status_frame.has_value(), "frame operator status response");
    const auto decoded_operator_frame = op::decode_operator_frame(*status_frame);
    expect(decoded_operator_frame.has_value(), "decode operator status frame");
    const auto decoded_status = op::decode_status_response(decoded_operator_frame->payload);
    expect(decoded_status.has_value() && decoded_status->status.quota().has_value() &&
               decoded_status->status.quota()->hard_bytes() == 4096U,
           "round trip operator status quota fields");

    expect(!wspctl::domain::OperatorWorkspaceStatus::create(
                test_runtime(),
                wspctl::domain::WorkspacePersistence::ready,
                static_cast<wspctl::domain::WorkspaceActivity>(255U),
                ready_quota())
                .has_value(),
           "domain status rejects an unknown activity enum before serialization");
    expect(!wspctl::domain::WorkspaceQuotaUsage::create(1024U, 0U, 2U, 64U).has_value(),
           "domain quota rejects a zero byte hard limit before serialization");
    expect(!wspctl::domain::OperatorWorkspaceStatus::create(
                test_runtime(),
                wspctl::domain::WorkspacePersistence::ready,
                wspctl::domain::WorkspaceActivity::inactive,
                std::nullopt)
                .has_value(),
           "domain status rejects a ready workspace without a quota snapshot");
    expect(!wspctl::domain::OperatorWorkspaceStatus::create(
                test_runtime(),
                wspctl::domain::WorkspacePersistence::absent,
                wspctl::domain::WorkspaceActivity::inactive,
                ready_quota())
                .has_value(),
           "domain status rejects an absent workspace with a quota snapshot");
    auto malformed_quota_payload = *status_payload;
    constexpr std::size_t kStatusPrefixBytes = sizeof(std::uint32_t) + 36U + 3U;
    constexpr std::size_t kHardBytesOffset = kStatusPrefixBytes + sizeof(std::uint64_t);
    std::fill_n(
        malformed_quota_payload.begin() + static_cast<std::ptrdiff_t>(kHardBytesOffset),
        sizeof(std::uint64_t),
        std::byte{0U});
    expect(!op::decode_status_response(malformed_quota_payload).has_value(),
           "reject a wire status response with a zero byte hard limit");
    auto malformed_activity_payload = *status_payload;
    constexpr std::size_t kActivityOffset = sizeof(std::uint32_t) + 36U + 1U;
    malformed_activity_payload[kActivityOffset] = std::byte{255U};
    expect(!op::decode_status_response(malformed_activity_payload).has_value(),
           "delegate unknown wire activity rejection to the domain status factory");
    expect(!wspctl::decode_frame(*status_frame).has_value(),
           "Bot protocol rejects the independent operator magic");

    const auto bot_frame = wspctl::encode_frame(wspctl::MessageKind::shutdown, {});
    expect(bot_frame.has_value() && !op::decode_operator_frame(*bot_frame).has_value(),
           "operator protocol rejects Bot protocol magic");

    const auto path = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    expect(path.has_value(), "parse root path for list round trip");
    const auto entry = wspctl::domain::WorkspaceEntry::create("safe%0Aname", wspctl::domain::WorkspaceEntryKind::regular_file, 7U);
    expect(entry.has_value(), "create safe list entry");
    const auto list_payload = op::encode_list_response(op::ListResponse{.listing = wspctl::domain::WorkspaceListing{
        .path = *path,
        .entries = {*entry},
        .truncated = false,
    }});
    const auto decoded_list = list_payload ? op::decode_list_response(*list_payload)
                                           : wspctl::Result<op::ListResponse>{std::unexpected(list_payload.error())};
    expect(decoded_list.has_value() && decoded_list->listing.entries.front().encoded_name() == "safe%0Aname",
           "round trip safely encoded operator list entry");
}

/** @brief 只读 operator 端口 fake / Read-only fake operator port. */
class RecordingOperatorPort final : public wspctl::application::OperatorWorkspaceReadPort {
public:
    /**
     * @brief 记录 status 调用 / Record a status call.
     * @param runtime 查询 runtime / Queried runtime.
     * @return 固定 ready status / Fixed ready status.
     */
    [[nodiscard]] wspctl::application::OperatorWorkspaceQueryResult<wspctl::domain::OperatorWorkspaceStatus> status(
        const wspctl::domain::RuntimeId& runtime) const override {
        ++status_calls;
        return ready_status(runtime);
    }

    /**
     * @brief 记录 list 调用 / Record a list call.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param path 被列举 path / Listed path.
     * @return 固定空 listing / Fixed empty listing.
     */
    [[nodiscard]] wspctl::application::OperatorWorkspaceQueryResult<wspctl::domain::WorkspaceListing> list(
        const wspctl::domain::RuntimeId& runtime,
        const wspctl::domain::OperatorWorkspacePath& path) const override {
        static_cast<void>(runtime);
        ++list_calls;
        return wspctl::domain::WorkspaceListing{.path = path, .entries = {}, .truncated = false};
    }

    /** @brief status 调用数 / Status-call count. */
    mutable unsigned int status_calls{0U};
    /** @brief list 调用数 / List-call count. */
    mutable unsigned int list_calls{0U};
};

/** @brief 测试 application read use case 不含 activation 副作用 / Test the application read use case contains no activation side effect. */
void test_application_read_only_boundary() {
    RecordingOperatorPort port;
    wspctl::application::OperatorWorkspaceQueryService service;
    const auto runtime = test_runtime();
    const auto path = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    const auto status = service.status(runtime, port);
    const auto listing = service.list(runtime, *path, port);
    expect(status.has_value() && listing.has_value() && port.status_calls == 1U && port.list_calls == 1U,
           "application service invokes only read-model port methods");
}

/**
 * @brief 创建自有临时测试根 / Create a self-owned temporary test root.
 * @return 临时根或空字符串 / Temporary root or an empty string.
 */
[[nodiscard]] std::filesystem::path make_temporary_root() {
    std::string material{"/tmp/wspctl-operator-tests-XXXXXX"};
    std::vector<char> buffer(material.begin(), material.end());
    buffer.push_back('\0');
    char* const created = mkdtemp(buffer.data());
    if (created == nullptr) {
        return {};
    }
    return std::filesystem::path(created);
}

/**
 * @brief 测试 upper-dirfd 遍历与安全 filename 输出 / Test upper-dirfd traversal and safe filename output.
 */
void test_reader_uses_upper_dirfd_and_refuses_symlink_traversal() {
    const std::filesystem::path temporary_root = make_temporary_root();
    expect(!temporary_root.empty(), "create temporary operator reader root");
    if (temporary_root.empty()) {
        return;
    }
    const std::filesystem::path workspace = temporary_root / "workspace";
    const std::filesystem::path upper = workspace / "upper";
    std::error_code error;
    std::filesystem::create_directories(upper / "safe", error);
    expect(!error, "create temporary upper directory");
    {
        std::ofstream file(upper / "safe" / "plain.txt", std::ios::binary);
        file << "ok";
    }
    const std::string hostile_name{"line\nname"};
    {
        std::ofstream file(upper / hostile_name, std::ios::binary);
        file << "x";
    }
    const std::filesystem::path escape = upper / "escape";
    expect(symlink("/tmp", escape.c_str()) == 0, "create hostile escape symlink");

    const wspctl::RuntimeQuotaBinding binding{
        .runtime_dir = temporary_root / "runtime",
        .control_dir = temporary_root / "control",
        .workspace_dir = workspace,
        .control_project_id = 1U,
        .workspace_project_id = 2U,
    };
    wspctl::OperatorWorkspaceReader reader;
    const auto root_path = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    const auto root_listing = reader.list(binding, *root_path);
    const auto encoded_hostile = wspctl::domain::encode_workspace_entry_name(hostile_name);
    const bool found_encoded_hostile = root_listing.has_value() && std::ranges::any_of(
        root_listing->entries,
        [&](const wspctl::domain::WorkspaceEntry& entry) { return entry.encoded_name() == *encoded_hostile; });
    expect(found_encoded_hostile, "reader returns only safely encoded hostile filename");
    const auto escape_path = wspctl::domain::OperatorWorkspacePath::parse("/workspace/escape");
    expect(!reader.list(binding, *escape_path).has_value(),
           "reader fails closed instead of traversing an upper-layer symlink");
    const auto missing_path = wspctl::domain::OperatorWorkspacePath::parse("/workspace/missing");
    const auto missing_listing = reader.list(binding, *missing_path);
    expect(!missing_listing.has_value() && missing_listing.error().code == wspctl::ErrorCode::not_found,
           "reader preserves a missing logical directory as the domain not-found result");
    std::filesystem::remove_all(temporary_root, error);
    expect(!error, "clean up self-created temporary reader root");
}

}  // namespace

/**
 * @brief operator CTest 入口 / Operator CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_domain_path_and_filename_encoding();
    test_endpoint_separation_policy();
    test_protocol_isolation_and_round_trip();
    test_application_read_only_boundary();
    test_reader_uses_upper_dirfd_and_refuses_symlink_traversal();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
