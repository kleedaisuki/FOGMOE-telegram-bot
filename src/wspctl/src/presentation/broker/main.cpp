#include "wspctl/infrastructure/broker.hpp"

#include <charconv>
#include <cstdio>
#include <string_view>

namespace
{

    /**
     * @brief 严格解析无符号 CLI 数字 / Strictly parse an unsigned CLI number.
     * @param text 文本 / Text.
     * @param output 输出值 / Output value.
     * @return 是否完整成功 / Whether parsing completely succeeded.
     */
    template <typename Value>
    [[nodiscard]] bool parse_unsigned(const std::string_view text, Value &output)
    {
        const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), output);
        return error == std::errc{} && end == text.data() + text.size();
    }

} // namespace

/**
 * @brief wspctld 入口 / wspctld entry point.
 * @param argc 参数数 / Argument count.
 * @param argv 参数数组 / Argument array.
 * @return POSIX 退出码 / POSIX exit code.
 */
int main(const int argc, char *argv[])
{
    wspctl::BrokerConfig config;
    bool have_socket = false;
    bool have_operator_socket = false;
    bool have_state = false;
    bool have_image_store = false;
    bool have_image_digest = false;
    bool have_client_uid = false;
    bool have_operator_uid = false;
    bool have_cgroup = false;
    bool have_sandbox_uid = false;
    bool have_sandbox_gid = false;
    bool have_memory = false;
    bool have_cpu_quota = false;
    bool have_cpu_period = false;
    bool have_pids = false;
    bool have_idle = false;
    bool have_quota_backend = false;
    bool have_quota_mount = false;
    bool have_project_id_min = false;
    bool have_project_id_max = false;
    bool have_control_hard_bytes = false;
    bool have_control_hard_inodes = false;
    bool have_workspace_hard_bytes = false;
    bool have_workspace_hard_inodes = false;
    bool have_admission_bytes = false;
    bool have_admission_inodes = false;
    bool have_system_reserve_bytes = false;
    bool have_system_reserve_inodes = false;
    for (int index = 1; index < argc;)
    {
        const std::string_view option{argv[index]};
        if (option == "--allow-insecure-dev-root")
        {
            config.allow_insecure_dev_root = true;
            ++index;
            continue;
        }
        if (index + 1 >= argc)
        {
            std::fputs("wspctld: option value missing\n", stderr);
            return 64;
        }
        const std::string_view value{argv[index + 1]};
        if (option == "--socket")
        {
            config.socket_path = value;
            have_socket = true;
        }
        else if (option == "--operator-socket")
        {
            config.operator_socket_path = value;
            have_operator_socket = true;
        }
        else if (option == "--state-root")
        {
            config.sandbox.state_root = value;
            have_state = true;
        }
        else if (option == "--image-store")
        {
            config.sandbox.images_root = value;
            have_image_store = true;
        }
        else if (option == "--image-digest")
        {
            const auto digest = wspctl::OciImageDigest::parse(value);
            if (!digest)
            {
                std::fputs("wspctld: invalid --image-digest\n", stderr);
                return 64;
            }
            config.sandbox.image_digest = *digest;
            have_image_digest = true;
        }
        else if (option == "--client-uid")
        {
            have_client_uid = parse_unsigned(value, config.client_uid);
        }
        else if (option == "--operator-uid")
        {
            have_operator_uid = parse_unsigned(value, config.operator_uid);
        }
        else if (option == "--cgroup-root")
        {
            config.sandbox.cgroup_root = value;
            have_cgroup = true;
        }
        else if (option == "--sandbox-uid")
        {
            have_sandbox_uid = parse_unsigned(value, config.sandbox.sandbox_uid);
        }
        else if (option == "--sandbox-gid")
        {
            have_sandbox_gid = parse_unsigned(value, config.sandbox.sandbox_gid);
        }
        else if (option == "--memory-max")
        {
            have_memory = parse_unsigned(value, config.sandbox.memory_max_bytes);
        }
        else if (option == "--memory-high")
        {
            if (!parse_unsigned(value, config.sandbox.memory_high_bytes))
            {
                std::fputs("wspctld: invalid --memory-high\n", stderr);
                return 64;
            }
        }
        else if (option == "--memory-swap-max")
        {
            if (!parse_unsigned(value, config.sandbox.memory_swap_max_bytes))
            {
                std::fputs("wspctld: invalid --memory-swap-max\n", stderr);
                return 64;
            }
        }
        else if (option == "--cpu-max-us")
        {
            have_cpu_quota = parse_unsigned(value, config.sandbox.cpu_max_quota_us);
        }
        else if (option == "--cpu-period-us")
        {
            have_cpu_period = parse_unsigned(value, config.sandbox.cpu_max_period_us);
        }
        else if (option == "--pids-max")
        {
            have_pids = parse_unsigned(value, config.sandbox.pids_max);
        }
        else if (option == "--io-weight")
        {
            if (!parse_unsigned(value, config.sandbox.io_weight))
            {
                std::fputs("wspctld: invalid --io-weight\n", stderr);
                return 64;
            }
        }
        else if (option == "--idle-minutes")
        {
            unsigned long long minutes = 0;
            have_idle = parse_unsigned(value, minutes);
            if (have_idle)
            {
                config.idle_ttl = std::chrono::minutes(minutes);
            }
        }
        else if (option == "--quota-backend")
        {
            if (value != "xfs_project_v1")
            {
                std::fputs("wspctld: only --quota-backend xfs_project_v1 is supported\n", stderr);
                return 64;
            }
            have_quota_backend = true;
        }
        else if (option == "--xfs-quota-mount")
        {
            config.sandbox.xfs_project_quota.mount_path = value;
            have_quota_mount = true;
        }
        else if (option == "--xfs-project-id-min")
        {
            have_project_id_min = parse_unsigned(value, config.sandbox.xfs_project_quota.project_id_min);
        }
        else if (option == "--xfs-project-id-max")
        {
            have_project_id_max = parse_unsigned(value, config.sandbox.xfs_project_quota.project_id_max);
        }
        else if (option == "--runtime-control-hard-bytes")
        {
            have_control_hard_bytes = parse_unsigned(value, config.sandbox.xfs_project_quota.control_hard_bytes);
        }
        else if (option == "--runtime-control-hard-inodes")
        {
            have_control_hard_inodes = parse_unsigned(value, config.sandbox.xfs_project_quota.control_hard_inodes);
        }
        else if (option == "--runtime-workspace-hard-bytes")
        {
            have_workspace_hard_bytes = parse_unsigned(value, config.sandbox.xfs_project_quota.workspace_hard_bytes);
        }
        else if (option == "--runtime-workspace-hard-inodes")
        {
            have_workspace_hard_inodes = parse_unsigned(value, config.sandbox.xfs_project_quota.workspace_hard_inodes);
        }
        else if (option == "--xfs-global-admission-bytes")
        {
            have_admission_bytes = parse_unsigned(value, config.sandbox.xfs_project_quota.global_admission_bytes);
        }
        else if (option == "--xfs-global-admission-inodes")
        {
            have_admission_inodes = parse_unsigned(value, config.sandbox.xfs_project_quota.global_admission_inodes);
        }
        else if (option == "--xfs-system-reserve-bytes")
        {
            have_system_reserve_bytes = parse_unsigned(value, config.sandbox.xfs_project_quota.system_reserve_bytes);
        }
        else if (option == "--xfs-system-reserve-inodes")
        {
            have_system_reserve_inodes = parse_unsigned(value, config.sandbox.xfs_project_quota.system_reserve_inodes);
        }
        else
        {
            std::fputs("wspctld: unknown option\n", stderr);
            return 64;
        }
        index += 2;
    }
    if (!have_socket || !have_operator_socket || !have_state || !have_image_store ||
        !have_image_digest || !have_client_uid ||
        !have_operator_uid || !have_cgroup ||
        !have_sandbox_uid || !have_sandbox_gid || !have_memory || !have_cpu_quota || !have_cpu_period ||
        !have_pids || !have_idle || !have_quota_backend || !have_quota_mount || !have_project_id_min || !have_project_id_max ||
        !have_control_hard_bytes || !have_control_hard_inodes || !have_workspace_hard_bytes || !have_workspace_hard_inodes ||
        !have_admission_bytes || !have_admission_inodes || !have_system_reserve_bytes || !have_system_reserve_inodes)
    {
        std::fputs("wspctld: secure configuration is mandatory\n", stderr);
        return 64;
    }
    auto broker = wspctl::Broker::create(std::move(config));
    if (!broker)
    {
        std::fputs("wspctld: fail-closed preflight rejected configuration\n", stderr);
        return 78;
    }
    const auto served = broker->serve_forever();
    if (!served)
    {
        std::fputs("wspctld: listener failure\n", stderr);
        return 70;
    }
    return 0;
}
