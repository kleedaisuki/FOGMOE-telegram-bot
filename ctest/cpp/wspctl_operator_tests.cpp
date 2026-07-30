/**
 * @file wspctl_operator_tests.cpp
 * @brief 独立 operator shell DDD/协议/文件系统测试 / Independent operator-shell DDD, protocol, and
 * filesystem tests.
 */

#include "wspctl/application/operator_workspace.hpp"
#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/operator_endpoint.hpp"
#include "wspctl/infrastructure/operator_protocol.hpp"
#include "wspctl/infrastructure/operator_workspace_reader.hpp"
#include "wspctl/infrastructure/protocol.hpp"
#include "wspctl/presentation/operator_cli.hpp"

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <string>
#include <string_view>
#include <sys/stat.h>
#include <unistd.h>
#include <utility>
#include <variant>
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
[[nodiscard]] wspctl::domain::OperatorWorkspaceStatus
ready_status(const wspctl::domain::RuntimeId& runtime) {
    const auto status = wspctl::domain::OperatorWorkspaceStatus::create(
        runtime, wspctl::domain::WorkspacePersistence::ready,
        wspctl::domain::WorkspaceActivity::inactive, ready_quota());
    expect(status.has_value(), "create ready operator status");
    return *status;
}

/**
 * @brief 构造默认测试 runtime 的 ready status / Construct a ready status for the default test
 * runtime.
 * @return allowlisted operator status / Allowlisted operator status.
 */
[[nodiscard]] wspctl::domain::OperatorWorkspaceStatus ready_status() {
    return ready_status(test_runtime());
}

/**
 * @brief 计算移除 ANSI SGR 后的单行可见宽度 / Compute one line's visible width after removing
 * ANSI SGR.
 * @param line 待检查行 / Line to inspect.
 * @return 可见 ASCII 列数 / Visible ASCII column count.
 */
[[nodiscard]] std::size_t visible_width(const std::string_view line) {
    /** @brief 可见字符数 / Visible character count. */
    std::size_t width{0U};
    /** @brief 当前扫描位置 / Current scan position. */
    std::size_t index{0U};
    while (index < line.size()) {
        if (line[index] != '\x1b') {
            ++width;
            ++index;
            continue;
        }
        ++index;
        if (index < line.size() && line[index] == '[') {
            ++index;
            while (index < line.size() && line[index] != 'm') {
                ++index;
            }
            if (index < line.size()) {
                ++index;
            }
        }
    }
    return width;
}

/**
 * @brief 验证每个输出行不超过给定可见宽度 / Verify every output line fits a visible width.
 * @param rendered 完整渲染输出 / Complete rendered output.
 * @param columns 最大列数 / Maximum columns.
 * @return 是否全部行均满足宽度 / Whether every line fits.
 */
[[nodiscard]] bool all_lines_fit(const std::string_view rendered, const std::size_t columns) {
    /** @brief 当前行开始位置 / Current line start. */
    std::size_t begin{0U};
    while (begin < rendered.size()) {
        /** @brief 当前行结束位置 / Current line end. */
        const std::size_t end = rendered.find('\n', begin);
        /** @brief 当前行 view / Current line view. */
        const std::string_view line = rendered.substr(
            begin, end == std::string_view::npos ? rendered.size() - begin : end - begin);
        if (visible_width(line) > columns) {
            return false;
        }
        if (end == std::string_view::npos) {
            break;
        }
        begin = end + 1U;
    }
    return true;
}

/**
 * @brief 测试类型化 CLI 路由拒绝非法 option 状态 / Test typed CLI routing rejects invalid option
 * states.
 */
void test_operator_cli_typed_routing() {
    /** @brief CLI presentation 命名空间别名 / CLI presentation namespace alias. */
    namespace cli = wspctl::presentation::operator_cli;
    /** @brief 测试 endpoint / Test endpoint. */
    constexpr std::string_view kSocket{"/tmp/wspctl-operator.sock"};
    /** @brief 测试 UUID / Test UUID. */
    constexpr std::string_view kRuntime{"123e4567-e89b-12d3-a456-426614174000"};

    /** @brief 合法 dashboard watch 参数 / Valid dashboard-watch arguments. */
    const std::vector<std::string_view> watch_arguments{
        "--socket", kSocket, "dashboard", "--runtime", kRuntime, "--watch", "--refresh", "7",
    };
    /** @brief 已解析 watch 调用 / Parsed watch invocation. */
    const auto watch = cli::parse_arguments(watch_arguments, "");
    /** @brief watch 命令 view / Watch-command view. */
    const auto* const watch_command =
        watch ? std::get_if<cli::DashboardCommand>(&watch->command) : nullptr;
    expect(watch_command != nullptr && watch_command->watch &&
               watch_command->refresh == std::chrono::seconds{7},
           "route dashboard watch into a complete typed command");

    /** @brief 合法一次性 dashboard 参数 / Valid one-shot dashboard arguments. */
    const std::vector<std::string_view> snapshot_arguments{
        "--plain", "--socket", kSocket, "dashboard", "--runtime", kRuntime,
    };
    /** @brief 已解析 snapshot 调用 / Parsed snapshot invocation. */
    const auto snapshot = cli::parse_arguments(snapshot_arguments, "");
    /** @brief snapshot 命令 view / Snapshot-command view. */
    const auto* const snapshot_command =
        snapshot ? std::get_if<cli::DashboardCommand>(&snapshot->command) : nullptr;
    expect(snapshot_command != nullptr && !snapshot_command->watch &&
               snapshot->color == cli::ColorMode::never,
           "route a plain one-shot dashboard without an implicit watch");

    /** @brief 缺少 watch 的 refresh 参数 / Refresh arguments missing watch. */
    const std::vector<std::string_view> orphan_refresh{
        "--socket", kSocket, "dashboard", "--runtime", kRuntime, "--refresh", "2",
    };
    expect(!cli::parse_arguments(orphan_refresh, "").has_value(),
           "reject refresh without watch before any gateway call");

    /** @brief 重复 runtime 的 status 参数 / Status arguments with duplicate runtime. */
    const std::vector<std::string_view> duplicate_runtime{
        "--socket", kSocket, "status", "--runtime", kRuntime, "--runtime", kRuntime,
    };
    expect(!cli::parse_arguments(duplicate_runtime, "").has_value(),
           "reject duplicate runtime before any gateway call");

    /** @brief 合法 workspace 参数 / Valid workspace arguments. */
    const std::vector<std::string_view> listing_arguments{
        "--socket", kSocket, "workspace", "ls", "--path", "/workspace/logs", "--runtime", kRuntime,
    };
    /** @brief 已解析 listing 调用 / Parsed listing invocation. */
    const auto listing = cli::parse_arguments(listing_arguments, "");
    /** @brief listing 命令 view / Listing-command view. */
    const auto* const listing_command =
        listing ? std::get_if<cli::WorkspaceListCommand>(&listing->command) : nullptr;
    expect(listing_command != nullptr && listing_command->path.value() == "/workspace/logs",
           "route workspace ls with a validated logical path");

    /** @brief 无 endpoint 的子命令 help / Subcommand help without an endpoint. */
    const std::vector<std::string_view> subcommand_help{"workspace", "ls", "--help"};
    /** @brief 已解析子命令 help / Parsed subcommand help. */
    const auto help = cli::parse_arguments(subcommand_help, "");
    expect(help.has_value() && std::holds_alternative<cli::HelpCommand>(help->command),
           "subcommand help never requires an operator endpoint");
}

/**
 * @brief 测试 TTY、NO_COLOR 与显式色彩策略 / Test TTY, NO_COLOR, and explicit color policies.
 */
void test_operator_cli_color_policy() {
    /** @brief CLI presentation 命名空间别名 / CLI presentation namespace alias. */
    namespace cli = wspctl::presentation::operator_cli;
    /** @brief 支持 ANSI 的 TTY / ANSI-capable TTY. */
    const cli::TerminalProfile tty{
        .is_tty = true,
        .no_color = false,
        .dumb_terminal = false,
        .columns = 80U,
    };
    /** @brief 设置 NO_COLOR 的 TTY / TTY with NO_COLOR set. */
    const cli::TerminalProfile no_color{
        .is_tty = true,
        .no_color = true,
        .dumb_terminal = false,
        .columns = 80U,
    };
    /** @brief 非 TTY pipe / Non-TTY pipe. */
    const cli::TerminalProfile pipe{
        .is_tty = false,
        .no_color = false,
        .dumb_terminal = false,
        .columns = 80U,
    };
    expect(tty.use_color(cli::ColorMode::automatic),
           "automatic color is enabled only for a capable TTY");
    expect(!no_color.use_color(cli::ColorMode::automatic) &&
               !pipe.use_color(cli::ColorMode::automatic),
           "NO_COLOR and non-TTY output disable automatic ANSI");
    expect(no_color.use_color(cli::ColorMode::always),
           "explicit --color always overrides environment auto-detection");
    expect(!tty.use_color(cli::ColorMode::never), "explicit plain mode disables ANSI on a TTY");
}

/**
 * @brief 测试 dashboard ANSI、窄终端与缺失 quota 渲染 / Test dashboard ANSI, narrow-terminal,
 * and missing-quota rendering.
 */
void test_operator_dashboard_rendering() {
    /** @brief CLI presentation 命名空间别名 / CLI presentation namespace alias. */
    namespace cli = wspctl::presentation::operator_cli;
    /** @brief ready dashboard 状态 / Ready dashboard status. */
    const auto ready = ready_status();
    /** @brief 彩色宽屏 dashboard / Colored wide dashboard. */
    const std::string colored = cli::render_dashboard(ready, cli::RenderOptions{
                                                                 .color = true,
                                                                 .interactive = true,
                                                                 .columns = 88U,
                                                                 .watching = false,
                                                             });
    /** @brief 纯文本宽屏 dashboard / Plain wide dashboard. */
    const std::string plain = cli::render_dashboard(ready, cli::RenderOptions{
                                                               .color = false,
                                                               .interactive = false,
                                                               .columns = 88U,
                                                               .watching = false,
                                                           });
    expect(colored.find("\x1b[") != std::string::npos, "colored dashboard contains ANSI SGR");
    expect(plain.find('\x1b') == std::string::npos &&
               plain.find("READ-ONLY SNAPSHOT") != std::string::npos,
           "plain dashboard contains no ANSI and labels snapshot semantics");
    expect(all_lines_fit(colored, 88U), "wide colored dashboard respects visible terminal width");

    /** @brief 32-column dashboard / 32-column dashboard. */
    const std::string narrow = cli::render_dashboard(ready, cli::RenderOptions{
                                                                .color = false,
                                                                .interactive = true,
                                                                .columns = 32U,
                                                                .watching = true,
                                                            });
    expect(narrow.find("READ-ONLY WATCH") != std::string::npos && all_lines_fit(narrow, 32U),
           "narrow dashboard switches layout and keeps every line bounded");

    /** @brief 极窄 dashboard / Very narrow dashboard. */
    const std::string very_narrow = cli::render_dashboard(ready, cli::RenderOptions{
                                                                     .color = false,
                                                                     .interactive = true,
                                                                     .columns = 16U,
                                                                     .watching = false,
                                                                 });
    expect(all_lines_fit(very_narrow, 16U),
           "very narrow dashboard never exceeds the reported terminal width");

    /** @brief 超额 quota fixture / Over-limit quota fixture. */
    const auto over_limit_quota =
        wspctl::domain::WorkspaceQuotaUsage::create(4'097U, 4'096U, 2U, 64U);
    expect(over_limit_quota.has_value(), "create over-limit quota fixture");
    if (over_limit_quota.has_value()) {
        /** @brief 超额 quota 状态 / Over-limit quota status. */
        const auto over_limit = wspctl::domain::OperatorWorkspaceStatus::create(
            test_runtime(), wspctl::domain::WorkspacePersistence::ready,
            wspctl::domain::WorkspaceActivity::ready, *over_limit_quota);
        expect(over_limit.has_value(), "create over-limit dashboard fixture");
        if (over_limit.has_value()) {
            /** @brief 超额 quota dashboard / Over-limit quota dashboard. */
            const std::string over_limit_page =
                cli::render_dashboard(*over_limit, cli::RenderOptions{
                                                       .color = false,
                                                       .interactive = false,
                                                       .columns = 80U,
                                                       .watching = false,
                                                   });
            expect(over_limit_page.find("DEGRADED") != std::string::npos &&
                       over_limit_page.find('!') != std::string::npos,
                   "quota overage changes aggregate health and uses a non-color cue");
        }
    }

    /** @brief 尚无 workspace 的合法状态 / Valid status without a workspace. */
    const auto absent = wspctl::domain::OperatorWorkspaceStatus::create(
        test_runtime(), wspctl::domain::WorkspacePersistence::absent,
        wspctl::domain::WorkspaceActivity::inactive, std::nullopt);
    expect(absent.has_value(), "create missing-workspace dashboard fixture");
    if (absent.has_value()) {
        /** @brief 缺失 quota dashboard / Missing-quota dashboard. */
        const std::string missing = cli::render_dashboard(*absent, cli::RenderOptions{
                                                                       .color = false,
                                                                       .interactive = false,
                                                                       .columns = 80U,
                                                                       .watching = false,
                                                                   });
        expect(missing.find("unavailable") != std::string::npos &&
                   missing.find("EMPTY") != std::string::npos,
               "missing workspace is informative rather than rendered as zero usage");
    }

    /** @brief 非交互 help / Non-interactive help. */
    const std::string piped_help = cli::render_help(cli::RenderOptions{
        .color = false,
        .interactive = false,
        .columns = 80U,
        .watching = false,
    });
    expect(piped_help.find("______") == std::string::npos &&
               piped_help.find('\x1b') == std::string::npos,
           "non-interactive help omits ASCII logo and ANSI");

    /** @brief 显式 plain 的交互 help / Explicitly plain interactive help. */
    const std::string plain_tty_help = cli::render_help(cli::RenderOptions{
        .color = false,
        .interactive = true,
        .columns = 80U,
        .watching = false,
    });
    expect(plain_tty_help.find("______") == std::string::npos &&
               plain_tty_help.find('\x1b') == std::string::npos,
           "plain interactive help omits decorative logo for accessibility");

    /** @brief 彩色交互 help / Colored interactive help. */
    const std::string colored_tty_help = cli::render_help(cli::RenderOptions{
        .color = true,
        .interactive = true,
        .columns = 80U,
        .watching = false,
    });
    expect(colored_tty_help.find("______") != std::string::npos &&
               colored_tty_help.find("\x1b[") != std::string::npos,
           "capable interactive TTY receives the compact colored ASCII logo");

    /** @brief 空 listing 路径 / Empty-listing path. */
    const auto root_path = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    expect(root_path.has_value(), "parse empty-listing fixture path");
    if (root_path.has_value()) {
        /** @brief 空 workspace listing / Empty workspace listing. */
        const wspctl::domain::WorkspaceListing empty_listing{
            .path = *root_path,
            .entries = {},
            .truncated = false,
        };
        /** @brief 空 listing 页面 / Empty-listing page. */
        const std::string empty_page =
            cli::render_listing_page(empty_listing, cli::RenderOptions{
                                                        .color = false,
                                                        .interactive = false,
                                                        .columns = 16U,
                                                        .watching = false,
                                                    });
        expect(empty_page.find("No entries") != std::string::npos &&
                   empty_page.find('\x1b') == std::string::npos && all_lines_fit(empty_page, 16U),
               "empty listing is explicit, plain, and narrow-terminal safe");
    }
}

/**
 * @brief 测试稳定退出码和 terminal-safe 可操作错误 / Test stable exit codes and terminal-safe,
 * actionable errors.
 */
void test_operator_cli_failure_contract() {
    /** @brief CLI presentation 命名空间别名 / CLI presentation namespace alias. */
    namespace cli = wspctl::presentation::operator_cli;
    expect(cli::exit_code_for(wspctl::make_error(wspctl::ErrorCode::not_found, "missing")) ==
                   cli::ExitCode::not_found &&
               cli::exit_code_for(wspctl::make_error(wspctl::ErrorCode::authentication_failed,
                                                     "denied")) == cli::ExitCode::permission &&
               cli::exit_code_for(wspctl::make_error(wspctl::ErrorCode::io_failure, "down")) ==
                   cli::ExitCode::unavailable,
           "map important operator failures to stable documented exit codes");
    /** @brief 带控制字符的模拟错误 / Simulated error carrying control characters. */
    std::string hostile_message{"down\n"};
    hostile_message.push_back('\x1b');
    hostile_message.append("[31m");
    /** @brief 已安全渲染错误 / Safely rendered error. */
    const std::string rendered =
        cli::render_failure("dashboard query", wspctl::make_error(wspctl::ErrorCode::io_failure,
                                                                  std::move(hostile_message)));
    expect(rendered.find('\x1b') == std::string::npos &&
               rendered.find("%0A%1B[31m") != std::string::npos &&
               rendered.find("hint:") != std::string::npos,
           "failure diagnostics encode controls and provide a next action");
}

/** @brief 测试 workspace logical-path 与 filename 编码领域约束 / Test workspace logical-path and
 * filename-encoding domain constraints. */
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
    expect(wspctl::domain::WorkspaceEntry::create(
               *encoded, wspctl::domain::WorkspaceEntryKind::regular_file, 1U)
               .has_value(),
           "accept canonical safe filename encoding");
    expect(!wspctl::domain::WorkspaceEntry::create(
                "line\n", wspctl::domain::WorkspaceEntryKind::regular_file, 1U)
                .has_value(),
           "reject raw terminal-control filename bytes");
    expect(!wspctl::domain::WorkspaceEntry::create(
                "%41", wspctl::domain::WorkspaceEntryKind::regular_file, 1U)
                .has_value(),
           "reject non-canonical percent encoding of a display-safe byte");
    expect(!wspctl::domain::WorkspaceEntry::create(
                ".", wspctl::domain::WorkspaceEntryKind::directory, 0U)
                   .has_value() &&
               !wspctl::domain::WorkspaceEntry::create(
                    "..", wspctl::domain::WorkspaceEntryKind::directory, 0U)
                    .has_value(),
           "reject POSIX dot names even though they are display-safe bytes");
}

/** @brief 测试 operator endpoint UID/path 隔离策略 / Test operator endpoint UID/path separation
 * policy. */
void test_endpoint_separation_policy() {
    expect(wspctl::validate_operator_endpoint_separation("/run/wspctl/bot/broker.sock", 65532U,
                                                         "/run/wspctl/operator/broker.sock", 0U)
               .has_value(),
           "accept distinct Bot and root operator endpoints");
    expect(!wspctl::validate_operator_endpoint_separation(
                "/run/wspctl/bot/broker.sock", 65532U, "/run/wspctl/operator/broker.sock", 65532U)
                .has_value(),
           "reject an operator UID equal to Bot UID");
    expect(!wspctl::validate_operator_endpoint_separation("/run/wspctl/same.sock", 65532U,
                                                          "/run/wspctl/same.sock", 0U)
                .has_value(),
           "reject shared Bot and operator socket path");
    expect(!wspctl::validate_operator_endpoint_separation(
                "/run/wspctl/bot/broker.sock", 65532U, "/run/wspctl/bot/operator/broker.sock", 0U)
                .has_value(),
           "reject an operator endpoint nested in the Bot socket directory view");
    expect(!wspctl::validate_operator_endpoint_separation("/run/wspctl/bot/operator/broker.sock",
                                                          65532U, "/run/wspctl/bot/broker.sock", 0U)
                .has_value(),
           "reject a Bot endpoint nested in the operator socket directory view");
    expect(wspctl::is_authorized_operator_peer(0U, 0U) &&
               !wspctl::is_authorized_operator_peer(65532U, 0U),
           "authorize only the exact operator SO_PEERCRED UID");
}

/** @brief 测试 operator wire 与 Bot wire 的魔数隔离 / Test magic isolation between operator and Bot
 * wires. */
void test_protocol_isolation_and_round_trip() {
    namespace op = wspctl::operator_protocol;
    const auto status_payload =
        op::encode_status_response(op::StatusResponse{.status = ready_status()});
    expect(status_payload.has_value(), "encode operator status response");
    const auto status_frame =
        op::encode_operator_frame(op::OperatorMessageKind::status_response, *status_payload);
    expect(status_frame.has_value(), "frame operator status response");
    const auto decoded_operator_frame = op::decode_operator_frame(*status_frame);
    expect(decoded_operator_frame.has_value(), "decode operator status frame");
    const auto decoded_status = op::decode_status_response(decoded_operator_frame->payload);
    expect(decoded_status.has_value() && decoded_status->status.quota().has_value() &&
               decoded_status->status.quota()->hard_bytes() == 4096U,
           "round trip operator status quota fields");

    expect(!wspctl::domain::OperatorWorkspaceStatus::create(
                test_runtime(), wspctl::domain::WorkspacePersistence::ready,
                static_cast<wspctl::domain::WorkspaceActivity>(255U), ready_quota())
                .has_value(),
           "domain status rejects an unknown activity enum before serialization");
    expect(!wspctl::domain::WorkspaceQuotaUsage::create(1024U, 0U, 2U, 64U).has_value(),
           "domain quota rejects a zero byte hard limit before serialization");
    expect(!wspctl::domain::OperatorWorkspaceStatus::create(
                test_runtime(), wspctl::domain::WorkspacePersistence::ready,
                wspctl::domain::WorkspaceActivity::inactive, std::nullopt)
                .has_value(),
           "domain status rejects a ready workspace without a quota snapshot");
    expect(!wspctl::domain::OperatorWorkspaceStatus::create(
                test_runtime(), wspctl::domain::WorkspacePersistence::absent,
                wspctl::domain::WorkspaceActivity::inactive, ready_quota())
                .has_value(),
           "domain status rejects an absent workspace with a quota snapshot");
    auto malformed_quota_payload = *status_payload;
    constexpr std::size_t kStatusPrefixBytes = sizeof(std::uint32_t) + 36U + 3U;
    constexpr std::size_t kHardBytesOffset = kStatusPrefixBytes + sizeof(std::uint64_t);
    std::fill_n(malformed_quota_payload.begin() + static_cast<std::ptrdiff_t>(kHardBytesOffset),
                sizeof(std::uint64_t), std::byte{0U});
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
    const auto entry = wspctl::domain::WorkspaceEntry::create(
        "safe%0Aname", wspctl::domain::WorkspaceEntryKind::regular_file, 7U);
    expect(entry.has_value(), "create safe list entry");
    const auto list_payload =
        op::encode_list_response(op::ListResponse{.listing = wspctl::domain::WorkspaceListing{
                                                      .path = *path,
                                                      .entries = {*entry},
                                                      .truncated = false,
                                                  }});
    const auto decoded_list =
        list_payload ? op::decode_list_response(*list_payload)
                     : wspctl::Result<op::ListResponse>{std::unexpected(list_payload.error())};
    expect(decoded_list.has_value() &&
               decoded_list->listing.entries.front().encoded_name() == "safe%0Aname",
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
    [[nodiscard]] wspctl::application::OperatorWorkspaceQueryResult<
        wspctl::domain::OperatorWorkspaceStatus>
    status(const wspctl::domain::RuntimeId& runtime) const override {
        ++status_calls;
        return ready_status(runtime);
    }

    /**
     * @brief 记录 list 调用 / Record a list call.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param path 被列举 path / Listed path.
     * @return 固定空 listing / Fixed empty listing.
     */
    [[nodiscard]] wspctl::application::OperatorWorkspaceQueryResult<
        wspctl::domain::WorkspaceListing>
    list(const wspctl::domain::RuntimeId& runtime,
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

/** @brief 测试 application read use case 不含 activation 副作用 / Test the application read use
 * case contains no activation side effect. */
void test_application_read_only_boundary() {
    RecordingOperatorPort port;
    wspctl::application::OperatorWorkspaceQueryService service;
    const auto runtime = test_runtime();
    const auto path = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    const auto status = service.status(runtime, port);
    const auto listing = service.list(runtime, *path, port);
    expect(status.has_value() && listing.has_value() && port.status_calls == 1U &&
               port.list_calls == 1U,
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
 * @brief 测试 upper-dirfd 遍历与安全 filename 输出 / Test upper-dirfd traversal and safe filename
 * output.
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
    const bool found_encoded_hostile =
        root_listing.has_value() &&
        std::ranges::any_of(root_listing->entries,
                            [&](const wspctl::domain::WorkspaceEntry& entry) {
                                return entry.encoded_name() == *encoded_hostile;
                            });
    expect(found_encoded_hostile, "reader returns only safely encoded hostile filename");
    const auto escape_path = wspctl::domain::OperatorWorkspacePath::parse("/workspace/escape");
    expect(!reader.list(binding, *escape_path).has_value(),
           "reader fails closed instead of traversing an upper-layer symlink");
    const auto missing_path = wspctl::domain::OperatorWorkspacePath::parse("/workspace/missing");
    const auto missing_listing = reader.list(binding, *missing_path);
    expect(!missing_listing.has_value() &&
               missing_listing.error().code == wspctl::ErrorCode::not_found,
           "reader preserves a missing logical directory as the domain not-found result");
    std::filesystem::remove_all(temporary_root, error);
    expect(!error, "clean up self-created temporary reader root");
}

} // namespace

/**
 * @brief operator CTest 入口 / Operator CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_operator_cli_typed_routing();
    test_operator_cli_color_policy();
    test_operator_dashboard_rendering();
    test_operator_cli_failure_contract();
    test_domain_path_and_filename_encoding();
    test_endpoint_separation_policy();
    test_protocol_isolation_and_round_trip();
    test_application_read_only_boundary();
    test_reader_uses_upper_dirfd_and_refuses_symlink_traversal();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
