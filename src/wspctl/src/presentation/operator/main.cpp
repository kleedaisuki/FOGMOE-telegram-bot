#include "wspctl/presentation/operator_cli.hpp"
#include "wspctl/presentation/operator_gateway.hpp"

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <string>
#include <string_view>
#include <time.h>
#include <unistd.h>
#include <variant>
#include <vector>

#ifndef WSPCTL_DEFAULT_OPERATOR_SOCKET
/** @brief 非 host 安装构建不提供默认 endpoint / Non-host-install builds provide no default
 * endpoint. */
#define WSPCTL_DEFAULT_OPERATOR_SOCKET ""
#endif

namespace {

/** @brief operator CLI presentation 命名空间别名 / Operator CLI presentation namespace alias. */
namespace cli = wspctl::presentation::operator_cli;

/** @brief watch 是否收到 SIGINT / Whether watch has received SIGINT. */
volatile std::sig_atomic_t g_interrupted{0};

/** @brief dashboard refresh timer 的等待结果 / Wait result for the dashboard refresh timer. */
enum class RefreshWait : unsigned char {
    /** @brief 周期正常结束 / Refresh period elapsed normally. */
    elapsed = 0,
    /** @brief 用户发出 SIGINT / User sent SIGINT. */
    interrupted = 1,
    /** @brief timer syscall 失败 / Timer syscall failed. */
    failed = 2,
};

/**
 * @brief 将 SIGINT 转换为可观测 watch 状态 / Convert SIGINT into observable watch state.
 * @param signal_number 收到的 signal 编号 / Received signal number.
 */
extern "C" void handle_interrupt(const int signal_number) noexcept {
    if (signal_number == SIGINT) {
        g_interrupted = 1;
    }
}

/**
 * @brief 将完整字符串写入 stdio stream / Write a complete string to a stdio stream.
 * @param stream 目标 stream / Destination stream.
 * @param value 待写字符串 / String to write.
 */
void write_text(FILE* const stream, const std::string& value) {
    static_cast<void>(std::fwrite(value.data(), sizeof(char), value.size(), stream));
}

/**
 * @brief 构造当前 stdout 的确定性渲染选项 / Build deterministic render options for current stdout.
 * @param profile stdout 终端能力 / Stdout terminal capabilities.
 * @param color 用户色彩策略 / User color policy.
 * @param watching 是否为 watch dashboard / Whether this is a watch dashboard.
 * @return 完整渲染选项 / Complete render options.
 */
[[nodiscard]] cli::RenderOptions make_render_options(const cli::TerminalProfile& profile,
                                                     const cli::ColorMode color,
                                                     const bool watching) noexcept {
    return cli::RenderOptions{
        .color = profile.use_color(color),
        .interactive = profile.is_tty,
        .columns = profile.columns,
        .watching = watching,
    };
}

/**
 * @brief 安装 watch 的 SIGINT handler / Install the SIGINT handler for watch mode.
 * @return 是否成功安装 / Whether installation succeeded.
 */
[[nodiscard]] bool install_interrupt_handler() noexcept {
    /** @brief 新 SIGINT action / New SIGINT action. */
    struct sigaction action {};
    action.sa_handler = handle_interrupt;
    if (sigemptyset(&action.sa_mask) != 0) {
        return false;
    }
    action.sa_flags = 0;
    return sigaction(SIGINT, &action, nullptr) == 0;
}

/**
 * @brief 等待下一次 dashboard refresh 或 SIGINT / Wait for the next dashboard refresh or SIGINT.
 * @param interval 刷新周期 / Refresh period.
 * @return elapsed、interrupted 或 failed / Elapsed, interrupted, or failed.
 */
[[nodiscard]] RefreshWait wait_for_refresh(const std::chrono::seconds interval) noexcept {
    /** @brief 尚需等待的 timespec / Remaining timespec to wait. */
    timespec remaining{
        .tv_sec = static_cast<time_t>(interval.count()),
        .tv_nsec = 0,
    };
    while (g_interrupted == 0) {
        /** @brief 被 signal 打断时内核返回的剩余时间 / Kernel-returned remaining time after a
         * signal. */
        timespec next{};
        if (nanosleep(&remaining, &next) == 0) {
            return g_interrupted == 0 ? RefreshWait::elapsed : RefreshWait::interrupted;
        }
        if (errno != EINTR) {
            return RefreshWait::failed;
        }
        remaining = next;
    }
    return RefreshWait::interrupted;
}

/**
 * @brief 输出一个结构化 native 失败并返回稳定退出码 / Print one structured native failure and
 * return its stable exit code.
 * @param action 失败操作名 / Failed action name.
 * @param error native 错误 / Native error.
 * @return 稳定 CLI 退出码 / Stable CLI exit code.
 */
[[nodiscard]] int report_failure(const std::string_view action, const wspctl::Error& error) {
    write_text(stderr, cli::render_failure(action, error));
    return static_cast<int>(cli::exit_code_for(error));
}

/**
 * @brief 执行 status 只读查询 / Execute a read-only status query.
 * @param command 类型化 status 命令 / Typed status command.
 * @param invocation 完整 CLI 调用 / Complete CLI invocation.
 * @param profile stdout 终端能力 / Stdout terminal capabilities.
 * @return 稳定 CLI 退出码 / Stable CLI exit code.
 */
[[nodiscard]] int run_status(const cli::StatusCommand& command, const cli::Invocation& invocation,
                             const cli::TerminalProfile& profile) {
    /** @brief operator gateway / Operator gateway. */
    const wspctl::presentation::OperatorGatewayClient client(invocation.socket_path);
    /** @brief allowlisted 状态结果 / Allowlisted status result. */
    const auto status = client.status(command.runtime);
    if (!status) {
        return report_failure("status query", status.error());
    }
    if (profile.is_tty) {
        write_text(stdout, cli::render_dashboard(
                               *status, make_render_options(profile, invocation.color, false)));
    } else {
        write_text(stdout, cli::render_status_record(*status));
    }
    return static_cast<int>(cli::ExitCode::success);
}

/**
 * @brief 执行 workspace ls 只读查询 / Execute a read-only workspace ls query.
 * @param command 类型化 listing 命令 / Typed listing command.
 * @param invocation 完整 CLI 调用 / Complete CLI invocation.
 * @param profile stdout 终端能力 / Stdout terminal capabilities.
 * @return 稳定 CLI 退出码 / Stable CLI exit code.
 */
[[nodiscard]] int run_listing(const cli::WorkspaceListCommand& command,
                              const cli::Invocation& invocation,
                              const cli::TerminalProfile& profile) {
    /** @brief operator gateway / Operator gateway. */
    const wspctl::presentation::OperatorGatewayClient client(invocation.socket_path);
    /** @brief 有界 listing 结果 / Bounded listing result. */
    const auto listing = client.list(command.runtime, command.path);
    if (!listing) {
        return report_failure("workspace listing", listing.error());
    }
    if (profile.is_tty) {
        write_text(stdout, cli::render_listing_page(
                               *listing, make_render_options(profile, invocation.color, false)));
    } else {
        write_text(stdout, cli::render_listing_record(*listing));
    }
    return static_cast<int>(cli::ExitCode::success);
}

/**
 * @brief 执行一次性或 watch dashboard / Execute a one-shot or watch dashboard.
 * @param command 类型化 dashboard 命令 / Typed dashboard command.
 * @param invocation 完整 CLI 调用 / Complete CLI invocation.
 * @param profile stdout 终端能力 / Stdout terminal capabilities.
 * @return 稳定 CLI 退出码 / Stable CLI exit code.
 */
[[nodiscard]] int run_dashboard(const cli::DashboardCommand& command,
                                const cli::Invocation& invocation,
                                const cli::TerminalProfile& profile) {
    if (command.watch && !profile.is_tty) {
        std::fputs("wspctl: dashboard --watch requires stdout connected to a TTY\n"
                   "hint: omit --watch for one pipe-friendly snapshot\n",
                   stderr);
        return static_cast<int>(cli::ExitCode::usage);
    }
    if (command.watch && !install_interrupt_handler()) {
        std::fputs("wspctl: dashboard watch failed: cannot install SIGINT handler\n", stderr);
        return static_cast<int>(cli::ExitCode::software);
    }
    /** @brief operator gateway / Operator gateway. */
    const wspctl::presentation::OperatorGatewayClient client(invocation.socket_path);
    /** @brief dashboard 渲染选项 / Dashboard render options. */
    const cli::RenderOptions options =
        make_render_options(profile, invocation.color, command.watch);
    /** @brief 是否正在输出第一帧 / Whether the first frame is being emitted. */
    bool first_frame{true};
    while (g_interrupted == 0) {
        /** @brief 最新 allowlisted 快照 / Latest allowlisted snapshot. */
        const auto status = client.status(command.runtime);
        if (!status) {
            return report_failure("dashboard query", status.error());
        }
        if (!first_frame) {
            if (options.color) {
                std::fputs("\x1b[H\x1b[2J", stdout);
            } else {
                std::fputs("\n--- refresh ---\n", stdout);
            }
        }
        write_text(stdout, cli::render_dashboard(*status, options));
        static_cast<void>(std::fflush(stdout));
        first_frame = false;
        if (!command.watch) {
            return static_cast<int>(cli::ExitCode::success);
        }
        /** @brief refresh timer 等待结果 / Refresh-timer wait result. */
        const RefreshWait waited = wait_for_refresh(command.refresh);
        if (waited == RefreshWait::failed) {
            std::fputs("wspctl: dashboard watch failed: refresh timer could not be scheduled\n"
                       "hint: retry the one-shot dashboard without --watch\n",
                       stderr);
            return static_cast<int>(cli::ExitCode::software);
        }
        if (waited == RefreshWait::interrupted) {
            break;
        }
    }
    std::fputs("\nwatch stopped\n", stderr);
    return static_cast<int>(cli::ExitCode::interrupted);
}

} // namespace

/**
 * @brief wspctl operator inspection 入口 / wspctl operator-inspection entry point.
 * @param argc 参数数 / Argument count.
 * @param argv 参数数组 / Argument vector.
 * @return 稳定 POSIX/sysexits 风格退出码 / Stable POSIX/sysexits-style exit code.
 */
int main(const int argc, char* argv[]) {
    /** @brief 不含 argv[0] 的只读参数 view / Read-only argument view excluding argv[0]. */
    std::vector<std::string_view> arguments;
    arguments.reserve(argc > 1 ? static_cast<std::size_t>(argc - 1) : 0U);
    for (int index = 1; index < argc; ++index) {
        arguments.emplace_back(argv[index]);
    }
    /** @brief 类型化命令解析结果 / Typed command parse result. */
    const auto invocation =
        cli::parse_arguments(arguments, std::string_view{WSPCTL_DEFAULT_OPERATOR_SOCKET});
    if (!invocation) {
        std::fputs("wspctl: ", stderr);
        write_text(stderr, invocation.error().message);
        std::fputc('\n', stderr);
        if (invocation.error().show_usage) {
            std::fputs("hint: run wspctl --help\n", stderr);
        }
        return static_cast<int>(cli::ExitCode::usage);
    }

    /** @brief stdout 终端能力快照 / Stdout terminal-capability snapshot. */
    const cli::TerminalProfile profile = cli::detect_terminal_profile(STDOUT_FILENO);
    if (std::holds_alternative<cli::HelpCommand>(invocation->command)) {
        write_text(stdout,
                   cli::render_help(make_render_options(profile, invocation->color, false)));
        return static_cast<int>(cli::ExitCode::success);
    }
    if (const auto* const command = std::get_if<cli::StatusCommand>(&invocation->command);
        command != nullptr) {
        return run_status(*command, *invocation, profile);
    }
    if (const auto* const command = std::get_if<cli::WorkspaceListCommand>(&invocation->command);
        command != nullptr) {
        return run_listing(*command, *invocation, profile);
    }
    if (const auto* const command = std::get_if<cli::DashboardCommand>(&invocation->command);
        command != nullptr) {
        return run_dashboard(*command, *invocation, profile);
    }
    std::fputs("wspctl: internal command routing error\n", stderr);
    return static_cast<int>(cli::ExitCode::software);
}
