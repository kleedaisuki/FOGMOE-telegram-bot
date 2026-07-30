#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/domain/runtime.hpp"
#include "wspctl/presentation/operator_gateway.hpp"

#include <cstdio>
#include <string>
#include <string_view>

#ifndef WSPCTL_DEFAULT_OPERATOR_SOCKET
/** @brief 非 host 安装构建不提供默认 endpoint / Non-host-install builds provide no default
 * endpoint. */
#define WSPCTL_DEFAULT_OPERATOR_SOCKET ""
#endif

namespace {

/**
 * @brief 输出 `wspctl` operator inspection 用法 / Print `wspctl` operator-inspection usage.
 * @param stream 输出流 / Output stream.
 */
void print_usage(FILE* const stream) {
    std::fputs(
        "usage:\n"
        "  wspctl [--socket ABSOLUTE_SOCKET] status --runtime RUNTIME_UUID\n"
        "  wspctl [--socket ABSOLUTE_SOCKET] workspace ls --runtime RUNTIME_UUID [--path "
        "/workspace]\n"
        "\n"
        "wspctl is a read-only operator inspection client. It never starts a runtime, executes\n"
        "a command, writes a workspace, or reads file contents. workspace ls shows the persistent\n"
        "OverlayFS upper layer rather than a synthetic merged lower+upper filesystem. Operator\n"
        "sockets normally belong to root; installed host builds default to their CMake-fixed\n"
        "operator endpoint, so invoke `sudo wspctl ...` unless deployment explicitly configures\n"
        "another trusted operator UID. Non-host builds require --socket.\n",
        stream);
}

/**
 * @brief 将任意 bytes 渲染为 terminal-safe ASCII / Render arbitrary bytes as terminal-safe ASCII.
 * @param value 待渲染 bytes / Bytes to render.
 * @param preserve_slash 是否保留 `/` / Whether to preserve `/`.
 * @return 只含安全 ASCII 与 `%HH` 的文本 / Text containing only safe ASCII and `%HH`.
 */
[[nodiscard]] std::string terminal_safe_text(const std::string_view value,
                                             const bool preserve_slash) {
    constexpr std::string_view kDigits{"0123456789ABCDEF"};
    std::string rendered;
    rendered.reserve(value.size() * 3U);
    for (const unsigned char byte : value) {
        const bool safe =
            (byte >= static_cast<unsigned char>('a') && byte <= static_cast<unsigned char>('z')) ||
            (byte >= static_cast<unsigned char>('A') && byte <= static_cast<unsigned char>('Z')) ||
            (byte >= static_cast<unsigned char>('0') && byte <= static_cast<unsigned char>('9')) ||
            byte == static_cast<unsigned char>('.') || byte == static_cast<unsigned char>('_') ||
            byte == static_cast<unsigned char>('-') ||
            (preserve_slash && byte == static_cast<unsigned char>('/'));
        if (safe) {
            rendered.push_back(static_cast<char>(byte));
            continue;
        }
        rendered.push_back('%');
        rendered.push_back(kDigits[(byte >> 4U) & 0x0fU]);
        rendered.push_back(kDigits[byte & 0x0fU]);
    }
    return rendered;
}

/**
 * @brief 将 persistence 枚举写为稳定文本 / Render a persistence enum as stable text.
 * @param persistence 待渲染 persistence / Persistence to render.
 * @return 稳定 ASCII 名称 / Stable ASCII name.
 */
[[nodiscard]] std::string_view
persistence_name(const wspctl::domain::WorkspacePersistence persistence) noexcept {
    switch (persistence) {
    case wspctl::domain::WorkspacePersistence::absent:
        return "absent";
    case wspctl::domain::WorkspacePersistence::ready:
        return "ready";
    }
    return "unknown";
}

/**
 * @brief 将 activity 枚举写为稳定文本 / Render an activity enum as stable text.
 * @param activity 待渲染 activity / Activity to render.
 * @return 稳定 ASCII 名称 / Stable ASCII name.
 */
[[nodiscard]] std::string_view
activity_name(const wspctl::domain::WorkspaceActivity activity) noexcept {
    switch (activity) {
    case wspctl::domain::WorkspaceActivity::inactive:
        return "inactive";
    case wspctl::domain::WorkspaceActivity::activating:
        return "activating";
    case wspctl::domain::WorkspaceActivity::ready:
        return "ready";
    case wspctl::domain::WorkspaceActivity::executing:
        return "executing";
    case wspctl::domain::WorkspaceActivity::retiring:
        return "retiring";
    case wspctl::domain::WorkspaceActivity::failed:
        return "failed";
    }
    return "unknown";
}

/**
 * @brief 将 entry kind 渲染为单字母文本 / Render an entry kind as one-letter text.
 * @param kind 待渲染 entry kind / Entry kind to render.
 * @return 单字母稳定类型文本 / One-letter stable type text.
 */
[[nodiscard]] char entry_kind_name(const wspctl::domain::WorkspaceEntryKind kind) noexcept {
    switch (kind) {
    case wspctl::domain::WorkspaceEntryKind::regular_file:
        return 'f';
    case wspctl::domain::WorkspaceEntryKind::directory:
        return 'd';
    case wspctl::domain::WorkspaceEntryKind::symbolic_link:
        return 'l';
    }
    return '?';
}

/**
 * @brief 输出只读 runtime 状态 / Print a read-only runtime status.
 * @param status allowlisted runtime 状态 / Allowlisted runtime status.
 */
void print_status(const wspctl::domain::OperatorWorkspaceStatus& status) {
    std::printf("runtime=%s\n", status.runtime().value().c_str());
    std::printf("persistence=%s\n", persistence_name(status.persistence()).data());
    std::printf("activity=%s\n", activity_name(status.activity()).data());
    if (!status.quota().has_value()) {
        std::fputs(
            "quota_used_bytes=-\nquota_hard_bytes=-\nquota_used_inodes=-\nquota_hard_inodes=-\n",
            stdout);
        return;
    }
    std::printf("quota_used_bytes=%llu\n",
                static_cast<unsigned long long>(status.quota()->used_bytes()));
    std::printf("quota_hard_bytes=%llu\n",
                static_cast<unsigned long long>(status.quota()->hard_bytes()));
    std::printf("quota_used_inodes=%llu\n",
                static_cast<unsigned long long>(status.quota()->used_inodes()));
    std::printf("quota_hard_inodes=%llu\n",
                static_cast<unsigned long long>(status.quota()->hard_inodes()));
}

/**
 * @brief 输出一个安全编码的 workspace listing / Print one safely encoded workspace listing.
 * @param listing 有界 workspace listing / Bounded workspace listing.
 */
void print_listing(const wspctl::domain::WorkspaceListing& listing) {
    const std::string safe_path = terminal_safe_text(listing.path.value(), true);
    std::printf("path=%s\n", safe_path.c_str());
    std::printf("truncated=%s\n", listing.truncated ? "true" : "false");
    std::fputs("kind\tsize_bytes\tencoded_name\n", stdout);
    for (const wspctl::domain::WorkspaceEntry& entry : listing.entries) {
        std::printf("%c\t%llu\t%s\n", entry_kind_name(entry.kind()),
                    static_cast<unsigned long long>(entry.size_bytes()),
                    entry.encoded_name().c_str());
    }
}

/**
 * @brief 从命令行取出一个唯一 option 值 / Extract one unique option value from command-line
 * arguments.
 * @param argc 参数数 / Argument count.
 * @param argv 参数数组 / Argument vector.
 * @param begin 开始搜索的 index / Index at which to start searching.
 * @param option 所需 option 名 / Required option name.
 * @param output 输出值 / Output value.
 * @return 是否恰好取到一个值 / Whether exactly one value was found.
 */
[[nodiscard]] bool take_unique_option(const int argc, char* const argv[], const int begin,
                                      const std::string_view option, std::string& output) {
    bool found{false};
    for (int index = begin; index < argc; ++index) {
        if (std::string_view(argv[index]) != option) {
            continue;
        }
        if (index + 1 >= argc || found) {
            return false;
        }
        output = argv[index + 1];
        found = true;
        ++index;
    }
    return found;
}

/**
 * @brief 检查 argv 区间只由已知二值 option 组成 / Check an argv range contains only known binary
 * options.
 * @param argc 参数数 / Argument count.
 * @param argv 参数数组 / Argument vector.
 * @param begin 开始 index / Start index.
 * @param first 允许的第一个 option / First allowed option.
 * @param second 允许的第二个 option；空字符串表示无第二项 / Second allowed option; empty means
 * none.
 * @return 参数形状是否合法 / Whether the argument shape is valid.
 */
[[nodiscard]] bool contains_only_binary_options(const int argc, char* const argv[], int begin,
                                                const std::string_view first,
                                                const std::string_view second) {
    while (begin < argc) {
        const std::string_view option(argv[begin]);
        if ((option != first && option != second) || begin + 1 >= argc) {
            return false;
        }
        begin += 2;
    }
    return true;
}

} // namespace

/**
 * @brief wspctl operator inspection 入口 / wspctl operator-inspection entry point.
 * @param argc 参数数 / Argument count.
 * @param argv 参数数组 / Argument vector.
 * @return POSIX 退出码 / POSIX exit code.
 */
int main(const int argc, char* argv[]) {
    if (argc == 2 && (std::string_view(argv[1]) == "--help" || std::string_view(argv[1]) == "-h")) {
        print_usage(stdout);
        return 0;
    }
    int command_index{1};
    std::string socket_path{WSPCTL_DEFAULT_OPERATOR_SOCKET};
    if (argc > 1 && std::string_view(argv[1]) == "--socket") {
        if (argc < 4) {
            print_usage(stderr);
            return 64;
        }
        socket_path = argv[2];
        command_index = 3;
    }
    if (command_index >= argc || socket_path.empty()) {
        print_usage(stderr);
        return 64;
    }
    if (!wspctl::presentation::OperatorGatewayClient::validate_socket_path(socket_path)) {
        std::fputs("wspctl: --socket must be an absolute AF_UNIX endpoint\n", stderr);
        return 64;
    }
    const std::string_view command(argv[command_index]);
    wspctl::presentation::OperatorGatewayClient client(socket_path);
    if (command == "status") {
        std::string runtime_text;
        if (!contains_only_binary_options(argc, argv, command_index + 1, "--runtime", "") ||
            !take_unique_option(argc, argv, command_index + 1, "--runtime", runtime_text)) {
            print_usage(stderr);
            return 64;
        }
        const auto runtime = wspctl::domain::RuntimeId::parse(runtime_text);
        if (!runtime) {
            std::fputs("wspctl: --runtime must be a canonical lowercase UUID\n", stderr);
            return 64;
        }
        const auto status = client.status(*runtime);
        if (!status) {
            std::fputs("wspctl: operator status query failed\n", stderr);
            return status.error().code == wspctl::ErrorCode::not_found ? 66 : 69;
        }
        print_status(*status);
        return 0;
    }
    if (command == "workspace" && argc >= command_index + 4 &&
        std::string_view(argv[command_index + 1]) == "ls") {
        std::string runtime_text;
        std::string path_text{"/workspace"};
        if (!contains_only_binary_options(argc, argv, command_index + 2, "--runtime", "--path") ||
            !take_unique_option(argc, argv, command_index + 2, "--runtime", runtime_text)) {
            print_usage(stderr);
            return 64;
        }
        bool seen_path{false};
        for (int index = command_index + 2; index < argc; index += 2) {
            if (std::string_view(argv[index]) == "--path") {
                if (seen_path) {
                    print_usage(stderr);
                    return 64;
                }
                path_text = argv[index + 1];
                seen_path = true;
            }
        }
        const auto runtime = wspctl::domain::RuntimeId::parse(runtime_text);
        const auto path = wspctl::domain::OperatorWorkspacePath::parse(path_text);
        if (!runtime || !path) {
            std::fputs("wspctl: --runtime or --path is invalid\n", stderr);
            return 64;
        }
        const auto listing = client.list(*runtime, *path);
        if (!listing) {
            std::fputs("wspctl: operator workspace listing failed\n", stderr);
            return listing.error().code == wspctl::ErrorCode::not_found ? 66 : 69;
        }
        print_listing(*listing);
        return 0;
    }
    print_usage(stderr);
    return 64;
}
