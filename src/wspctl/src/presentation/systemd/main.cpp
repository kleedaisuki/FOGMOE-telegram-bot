#include "wspctl/infrastructure/supervisor.hpp"
#include "wspctl/infrastructure/sandbox.hpp"

#include <charconv>
#include <cstdio>
#include <cstring>
#include <string_view>
#include <fcntl.h>
#include <sys/prctl.h>
#include <unistd.h>

namespace {

/**
 * @brief 严格解析无符号整数参数 / Strictly parse an unsigned integer option.
 * @param text 参数文本 / Option text.
 * @param output 解析结果 / Parsed result.
 * @return 成功与否 / Whether parsing succeeded.
 */
template <typename Value>
[[nodiscard]] bool parse_unsigned(const std::string_view text, Value& output) {
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), output);
    return error == std::errc{} && end == text.data() + text.size();
}

}  // namespace

/**
 * @brief wsp-systemd 入口 / wsp-systemd entry point.
 * @param argc 参数数 / Argument count.
 * @param argv 参数值 / Argument values.
 * @return POSIX 退出码 / POSIX exit code.
 */
int main(const int argc, char* argv[]) {
    if (getpid() != 1) {
        std::fputs("wsp-systemd: must be PID 1 of a PID namespace\n", stderr);
        return 70;
    }
    // PID 1 retains broker control FDs until it forks a task. Do not rely on a host-wide
    // fs.suid_dumpable policy to keep same-UID payload processes from inspecting them via /proc.
    if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) {
        std::fputs("wsp-systemd: cannot disable dumpability\n", stderr);
        return 70;
    }
    wspctl::SupervisorConfig config;
    bool have_control = false;
    bool have_procs = false;
    bool have_kill = false;
    bool have_events = false;
    bool have_uid = false;
    bool have_gid = false;
    for (int index = 1; index < argc; index += 2) {
        if (index + 1 >= argc) {
            std::fputs("wsp-systemd: option value missing\n", stderr);
            return 64;
        }
        const std::string_view option{argv[index]};
        const std::string_view value{argv[index + 1]};
        if (option == "--control-fd") {
            have_control = parse_unsigned(value, config.control_fd);
        } else if (option == "--task-cgroup-procs-fd") {
            have_procs = parse_unsigned(value, config.task_cgroup_procs_fd);
        } else if (option == "--task-cgroup-kill-fd") {
            have_kill = parse_unsigned(value, config.task_cgroup_kill_fd);
        } else if (option == "--task-cgroup-events-fd") {
            have_events = parse_unsigned(value, config.task_cgroup_events_fd);
        } else if (option == "--sandbox-uid") {
            have_uid = parse_unsigned(value, config.sandbox_uid);
        } else if (option == "--sandbox-gid") {
            have_gid = parse_unsigned(value, config.sandbox_gid);
        } else {
            std::fputs("wsp-systemd: unknown option\n", stderr);
            return 64;
        }
    }
    if (!have_control || !have_procs || !have_kill || !have_events || !have_uid || !have_gid || config.control_fd < 3 ||
        config.task_cgroup_procs_fd < 3 || config.task_cgroup_kill_fd < 3 || config.task_cgroup_events_fd < 3 || config.sandbox_uid == 0U ||
        config.sandbox_gid == 0U) {
        std::fputs("wsp-systemd: mandatory secure control configuration missing\n", stderr);
        return 64;
    }
    // This executes after trusted argument/FD validation, but before PID 1 accepts any broker
    // command or forks a task.  Keeping it after exec preserves exactly the three capabilities
    // needed by the child-side identity drop and PID1 task-tree cleanup.
    if (const auto hardened = wspctl::harden_supervisor(); !hardened) {
        std::fputs("wsp-systemd: cannot reduce supervisor privileges\n", stderr);
        return 70;
    }
    config.workspace_fd = open("/workspace", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (config.workspace_fd < 0) {
        std::fputs("wsp-systemd: cannot open immutable workspace mount\n", stderr);
        return 70;
    }
    wspctl::Supervisor supervisor(config);
    const auto served = supervisor.serve();
    if (!served) {
        std::fputs("wsp-systemd: supervisor failed\n", stderr);
        static_cast<void>(close(config.workspace_fd));
        return 70;
    }
    static_cast<void>(close(config.workspace_fd));
    return 0;
}
