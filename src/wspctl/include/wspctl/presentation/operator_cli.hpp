#pragma once

#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/common.hpp"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <expected>
#include <span>
#include <string>
#include <string_view>
#include <variant>

namespace wspctl::presentation::operator_cli {

/** @brief dashboard watch 的默认刷新周期 / Default refresh period for dashboard watch mode. */
inline constexpr std::chrono::seconds kDefaultRefreshInterval{2};

/**
 * @brief operator CLI 的稳定进程退出码 / Stable process exit codes for the operator CLI.
 * @note 数值遵循 sysexits(3)；SIGINT 使用 shell 惯例 128 + SIGINT。
 *       Values follow sysexits(3); SIGINT uses the shell convention 128 + SIGINT.
 */
enum class ExitCode : int {
    /** @brief 命令成功 / Command succeeded. */
    success = 0,
    /** @brief 命令行用法错误 / Command-line usage error. */
    usage = 64,
    /** @brief runtime 或 workspace 不存在 / Runtime or workspace does not exist. */
    not_found = 66,
    /** @brief operator endpoint 暂时不可用 / Operator endpoint is temporarily unavailable. */
    unavailable = 69,
    /** @brief 协议或内部软件错误 / Protocol or internal software error. */
    software = 70,
    /** @brief operator endpoint 拒绝权限 / Operator endpoint rejected permission. */
    permission = 77,
    /** @brief 用户通过 SIGINT 中断 watch / User interrupted watch with SIGINT. */
    interrupted = 130,
};

/** @brief ANSI 色彩选择策略 / ANSI color-selection policy. */
enum class ColorMode : std::uint8_t {
    /** @brief 仅在兼容 TTY 上着色 / Color only on a compatible TTY. */
    automatic = 0,
    /** @brief 显式强制 ANSI 色彩 / Explicitly force ANSI color. */
    always = 1,
    /** @brief 显式禁用所有 ANSI 序列 / Explicitly disable every ANSI sequence. */
    never = 2,
};

/**
 * @brief 一次渲染所需的终端能力快照 / Terminal-capability snapshot for one render.
 *
 * 该值对象把不稳定的进程环境转换成确定输入，使渲染器可在无 PTY 的单元测试中验证。
 * This value object turns unstable process environment into deterministic input, making renderers
 * testable without a PTY.
 */
struct TerminalProfile final {
    /** @brief stdout 是否连接到 TTY / Whether stdout is connected to a TTY. */
    bool is_tty{false};
    /** @brief `NO_COLOR` 是否为非空 / Whether `NO_COLOR` is non-empty. */
    bool no_color{false};
    /** @brief `TERM` 是否声明为 dumb / Whether `TERM` declares a dumb terminal. */
    bool dumb_terminal{false};
    /** @brief 可用终端列数 / Available terminal columns. */
    std::size_t columns{80U};

    /**
     * @brief 按显式策略解析最终 ANSI 能力 / Resolve final ANSI capability from explicit policy.
     * @param mode 用户选择的色彩策略 / User-selected color policy.
     * @return 是否允许 ANSI 样式 / Whether ANSI styling is allowed.
     * @note `always` 是用户对环境自动探测的显式覆盖；`never` 总是优先。
     *       `always` explicitly overrides environment auto-detection; `never` always wins.
     */
    [[nodiscard]] bool use_color(ColorMode mode) const noexcept;
};

/**
 * @brief status 子命令的完整类型化参数 / Complete typed arguments for the status subcommand.
 */
struct StatusCommand final {
    /** @brief 待查询 runtime / Runtime to query. */
    domain::RuntimeId runtime;
};

/**
 * @brief workspace ls 子命令的完整类型化参数 / Complete typed arguments for workspace ls.
 */
struct WorkspaceListCommand final {
    /** @brief 待查询 runtime / Runtime to query. */
    domain::RuntimeId runtime;
    /** @brief 待列举逻辑 workspace 路径 / Logical workspace path to list. */
    domain::OperatorWorkspacePath path;
};

/**
 * @brief dashboard 子命令的完整类型化参数 / Complete typed arguments for the dashboard
 * subcommand.
 */
struct DashboardCommand final {
    /** @brief 待观测 runtime / Runtime to observe. */
    domain::RuntimeId runtime;
    /** @brief 是否持续刷新 / Whether to refresh continuously. */
    bool watch{false};
    /** @brief watch 刷新周期 / Watch refresh period. */
    std::chrono::seconds refresh{kDefaultRefreshInterval};
};

/** @brief help 路由的显式类型 / Explicit type for the help route. */
struct HelpCommand final {};

/** @brief 所有可执行 operator CLI 命令的封闭和类型 / Closed sum type of executable commands. */
using Command = std::variant<HelpCommand, StatusCommand, WorkspaceListCommand, DashboardCommand>;

/**
 * @brief 已完成语义校验的 CLI 调用 / Semantically validated CLI invocation.
 */
struct Invocation final {
    /** @brief 已验证 operator socket；help 路由允许为空 / Validated operator socket; help may be
     * empty. */
    std::string socket_path;
    /** @brief 最终用户色彩策略 / Final user color policy. */
    ColorMode color{ColorMode::automatic};
    /** @brief 已解析命令 / Parsed command. */
    Command command;
};

/**
 * @brief 参数解析失败值 / Argument-parsing failure value.
 */
struct ParseError final {
    /** @brief 面向用户且不含控制字符的诊断 / User-facing diagnostic without control characters. */
    std::string message;
    /** @brief 是否应在诊断后显示简短用法 / Whether concise usage should follow the diagnostic. */
    bool show_usage{true};
};

/** @brief CLI 参数解析结果 / CLI argument-parsing result. */
using ParseResult = std::expected<Invocation, ParseError>;

/**
 * @brief 人类视图的确定性渲染参数 / Deterministic render options for human views.
 */
struct RenderOptions final {
    /** @brief 是否输出 ANSI SGR / Whether to emit ANSI SGR. */
    bool color{false};
    /** @brief 输出是否面向交互式 TTY / Whether output targets an interactive TTY. */
    bool interactive{false};
    /** @brief 目标可见列宽 / Target visible column width. */
    std::size_t columns{80U};
    /** @brief dashboard 是否处于 watch 模式 / Whether the dashboard is in watch mode. */
    bool watching{false};
};

/**
 * @brief 读取指定输出 FD 的终端能力 / Read terminal capabilities for an output FD.
 * @param descriptor 待探测输出 FD / Output file descriptor to inspect.
 * @return 一次性能力快照 / One-shot capability snapshot.
 */
[[nodiscard]] TerminalProfile detect_terminal_profile(int descriptor) noexcept;

/**
 * @brief 将 argv 解析成无非法状态的命令和类型 / Parse argv into commands with no invalid states.
 * @param arguments 不含 argv[0] 的参数 / Arguments excluding argv[0].
 * @param default_socket 构建时默认 operator endpoint；可为空 /
 *        Build-time default operator endpoint; may be empty.
 * @return 完整调用或具体用法错误 / Complete invocation or a specific usage error.
 */
[[nodiscard]] ParseResult parse_arguments(std::span<const std::string_view> arguments,
                                          std::string_view default_socket);

/**
 * @brief 渲染 operator CLI 帮助页面 / Render the operator CLI help page.
 * @param options 确定性渲染选项 / Deterministic render options.
 * @return 仅含 ASCII 与可选 ANSI SGR 的页面 / Page containing ASCII and optional ANSI SGR only.
 */
[[nodiscard]] std::string render_help(const RenderOptions& options);

/**
 * @brief 渲染稳定、可管道处理的 status 记录 / Render a stable, pipe-friendly status record.
 * @param status allowlisted runtime 状态 / Allowlisted runtime status.
 * @return 无 ANSI 的 `key=value` 记录 / ANSI-free `key=value` record.
 */
[[nodiscard]] std::string render_status_record(const domain::OperatorWorkspaceStatus& status);

/**
 * @brief 渲染单 runtime operator dashboard / Render a single-runtime operator dashboard.
 * @param status allowlisted runtime 状态快照 / Allowlisted runtime-status snapshot.
 * @param options 确定性渲染选项 / Deterministic render options.
 * @return 宽屏或窄屏自适应 ASCII dashboard / Wide or narrow adaptive ASCII dashboard.
 */
[[nodiscard]] std::string render_dashboard(const domain::OperatorWorkspaceStatus& status,
                                           const RenderOptions& options);

/**
 * @brief 渲染稳定、可管道处理的 workspace listing / Render a stable, pipe-friendly workspace
 * listing.
 * @param listing 有界安全目录列举 / Bounded safe directory listing.
 * @return 无 ANSI 的 tabular 记录 / ANSI-free tabular record.
 */
[[nodiscard]] std::string render_listing_record(const domain::WorkspaceListing& listing);

/**
 * @brief 渲染面向人的 workspace listing 页面 / Render a human-oriented workspace listing page.
 * @param listing 有界安全目录列举 / Bounded safe directory listing.
 * @param options 确定性渲染选项 / Deterministic render options.
 * @return 宽度自适应 ASCII 页面 / Width-adaptive ASCII page.
 */
[[nodiscard]] std::string render_listing_page(const domain::WorkspaceListing& listing,
                                              const RenderOptions& options);

/**
 * @brief 将 native 错误映射为稳定 CLI 退出码 / Map a native error to a stable CLI exit code.
 * @param error native 结构化错误 / Native structured error.
 * @return 稳定 sysexits 风格退出码 / Stable sysexits-style exit code.
 */
[[nodiscard]] ExitCode exit_code_for(const Error& error) noexcept;

/**
 * @brief 渲染可继续操作的失败说明 / Render an actionable failure diagnostic.
 * @param action 失败的只读操作名 / Failed read-only action name.
 * @param error native 结构化错误 / Native structured error.
 * @return terminal-safe、无 ANSI 的多行诊断 / Terminal-safe, ANSI-free multi-line diagnostic.
 */
[[nodiscard]] std::string render_failure(std::string_view action, const Error& error);

} // namespace wspctl::presentation::operator_cli
