/**
 * @file wspctl_operator_cli_tests.cpp
 * @brief wspctl operator CLI 参数契约测试 / wspctl operator CLI argument-contract tests.
 */

#include <cerrno>
#include <cstdlib>
#include <iostream>
#include <string>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace {

/** @brief 测试失败计数 / Test failure count. */
unsigned int g_failures{0U};

/**
 * @brief 一次 CLI 子进程的可观测结果 / Observable result of one CLI child process.
 */
struct CliResult final {
    /** @brief 普通退出码；启动失败为 -1 / Normal exit code; -1 on launch failure. */
    int exit_code{-1};
    /** @brief 合并后的 stdout/stderr / Combined stdout and stderr. */
    std::string output;
};

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
 * @brief 以精确 argv 运行 CLI / Run the CLI with an exact argv.
 * @param executable 已构建 wspctl 的绝对路径 / Absolute path of the built wspctl.
 * @param arguments 不含 argv[0] 的参数 / Arguments excluding argv[0].
 * @param no_color 是否为 child 设置非空 NO_COLOR / Whether to set non-empty NO_COLOR in child.
 * @return child 的退出码与输出 / Child exit code and output.
 */
[[nodiscard]] CliResult run_cli(const std::string& executable,
                                const std::vector<std::string>& arguments,
                                const bool no_color = false) {
    /** @brief 捕获 stdout/stderr 的 pipe / Pipe capturing stdout/stderr. */
    int captured[2]{-1, -1};
    if (pipe(captured) != 0) {
        return {};
    }
    /** @brief exec 参数的自有 storage / Owning storage for exec arguments. */
    std::vector<std::string> storage;
    storage.reserve(arguments.size() + 1U);
    storage.push_back(executable);
    storage.insert(storage.end(), arguments.begin(), arguments.end());
    /** @brief exec argv view / Exec argv view. */
    std::vector<char*> argv;
    argv.reserve(storage.size() + 1U);
    for (std::string& argument : storage) {
        argv.push_back(argument.data());
    }
    argv.push_back(nullptr);
    /** @brief CLI child PID / CLI child PID. */
    const pid_t child = fork();
    if (child < 0) {
        static_cast<void>(close(captured[0]));
        static_cast<void>(close(captured[1]));
        return {};
    }
    if (child == 0) {
        static_cast<void>(close(captured[0]));
        if (dup2(captured[1], STDOUT_FILENO) < 0 || dup2(captured[1], STDERR_FILENO) < 0) {
            _exit(126);
        }
        static_cast<void>(close(captured[1]));
        if (no_color) {
            static_cast<void>(setenv("NO_COLOR", "1", 1));
        } else {
            static_cast<void>(unsetenv("NO_COLOR"));
        }
        execv(executable.c_str(), argv.data());
        _exit(127);
    }
    static_cast<void>(close(captured[1]));
    /** @brief 捕获结果 / Captured result. */
    CliResult result;
    /** @brief 单次读取 buffer / Per-read buffer. */
    char buffer[4096]{};
    while (true) {
        /** @brief 当前读取字节数 / Bytes read in this iteration. */
        const ssize_t count = read(captured[0], buffer, sizeof(buffer));
        if (count > 0) {
            result.output.append(buffer, static_cast<std::size_t>(count));
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        break;
    }
    static_cast<void>(close(captured[0]));
    /** @brief waitpid 状态 / waitpid status. */
    int status{0};
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) {
        return result;
    }
    result.exit_code = WEXITSTATUS(status);
    return result;
}

} // namespace

/**
 * @brief operator CLI CTest 入口 / Operator CLI CTest entry point.
 * @param argc 参数数 / Argument count.
 * @param argv 参数数组 / Argument vector.
 * @return 成功为 0 / Zero on success.
 */
int main(const int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "FAIL: expected built wspctl executable path\n";
        return EXIT_FAILURE;
    }
    /** @brief 已构建 CLI 路径 / Built CLI path. */
    const std::string executable(argv[1]);
    /** @brief 非 TTY help 结果 / Non-TTY help result. */
    const CliResult help = run_cli(executable, {"--help"});
    expect(help.exit_code == 0, "wspctl --help succeeds without an endpoint");
    expect(help.output.find('\x1b') == std::string::npos &&
               help.output.find("______") == std::string::npos,
           "non-TTY help is plain and omits the interactive ASCII logo");

    /** @brief 强制色彩 help 结果 / Force-colored help result. */
    const CliResult colored_help = run_cli(executable, {"--color", "always", "--help"});
    expect(colored_help.exit_code == 0 && colored_help.output.find("\x1b[") != std::string::npos,
           "explicit color override emits ANSI while remaining non-interactive");

    /** @brief NO_COLOR help 结果 / NO_COLOR help result. */
    const CliResult no_color_help = run_cli(executable, {"--help"}, true);
    expect(no_color_help.exit_code == 0 && no_color_help.output.find('\x1b') == std::string::npos,
           "NO_COLOR suppresses automatic ANSI");

    /** @brief NO_COLOR 上的显式覆盖 / Explicit override over NO_COLOR. */
    const CliResult overridden_no_color =
        run_cli(executable, {"--color", "always", "--help"}, true);
    expect(overridden_no_color.exit_code == 0 &&
               overridden_no_color.output.find("\x1b[") != std::string::npos,
           "explicit --color always overrides NO_COLOR");

    /** @brief 重复 runtime 结果 / Duplicate-runtime result. */
    const CliResult duplicate_runtime =
        run_cli(executable, {"--socket", "/tmp/wspctl-operator-test.sock", "status", "--runtime",
                             "123e4567-e89b-12d3-a456-426614174000", "--runtime",
                             "123e4567-e89b-12d3-a456-426614174001"});
    expect(duplicate_runtime.exit_code == 64,
           "wspctl rejects duplicate --runtime before connecting");

    /** @brief 相对 socket 结果 / Relative-socket result. */
    const CliResult relative_socket =
        run_cli(executable, {"--socket", "relative.sock", "status", "--runtime",
                             "123e4567-e89b-12d3-a456-426614174000"});
    expect(relative_socket.exit_code == 64,
           "wspctl rejects a relative operator endpoint before connecting");

    /** @brief 非 TTY watch 结果 / Non-TTY watch result. */
    const CliResult piped_watch =
        run_cli(executable, {"--socket", "/tmp/wspctl-operator-test.sock", "dashboard", "--runtime",
                             "123e4567-e89b-12d3-a456-426614174000", "--watch"});
    expect(piped_watch.exit_code == 64 &&
               piped_watch.output.find("requires stdout connected to a TTY") != std::string::npos,
           "dashboard watch fails fast and informatively off TTY");

    /** @brief 无 endpoint status 结果 / Status result with an unavailable endpoint. */
    const CliResult unavailable =
        run_cli(executable, {"--socket", "/tmp/wspctl-definitely-missing.sock", "status",
                             "--runtime", "123e4567-e89b-12d3-a456-426614174000"});
    expect(unavailable.exit_code == 69 && unavailable.output.find("hint:") != std::string::npos,
           "unavailable endpoint has a stable exit code and next action");
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
