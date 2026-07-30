/**
 * @file wspctl_operator_cli_tests.cpp
 * @brief wspctl operator CLI 参数契约测试 / wspctl operator CLI argument-contract tests.
 */

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
 * @return child 的普通退出码；exec/wait 失败为 -1 / Normal child exit code; -1 for exec/wait
 * failure.
 */
[[nodiscard]] int run_cli(const std::string& executable,
                          const std::vector<std::string>& arguments) {
    std::vector<std::string> storage;
    storage.reserve(arguments.size() + 1U);
    storage.push_back(executable);
    storage.insert(storage.end(), arguments.begin(), arguments.end());
    std::vector<char*> argv;
    argv.reserve(storage.size() + 1U);
    for (std::string& argument : storage) {
        argv.push_back(argument.data());
    }
    argv.push_back(nullptr);
    const pid_t child = fork();
    if (child < 0) {
        return -1;
    }
    if (child == 0) {
        execv(executable.c_str(), argv.data());
        _exit(127);
    }
    int status{0};
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status)) {
        return -1;
    }
    return WEXITSTATUS(status);
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
    const std::string executable(argv[1]);
    expect(run_cli(executable, {"--help"}) == 0, "wspctl --help succeeds without an endpoint");
    expect(run_cli(executable, {"--socket", "/tmp/wspctl-operator-test.sock", "status", "--runtime",
                                "123e4567-e89b-12d3-a456-426614174000", "--runtime",
                                "123e4567-e89b-12d3-a456-426614174001"}) == 64,
           "wspctl rejects duplicate --runtime before connecting");
    expect(run_cli(executable, {"--socket", "relative.sock", "status", "--runtime",
                                "123e4567-e89b-12d3-a456-426614174000"}) == 64,
           "wspctl rejects a relative operator endpoint before connecting");
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
