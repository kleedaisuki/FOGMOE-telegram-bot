"""@brief wspctl 分层与 proc 隔离的静态契约测试 / Static contracts for wspctl layers and proc isolation."""

from __future__ import annotations

from pathlib import Path


#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief 纯领域头路径 / Pure-domain header path.
DOMAIN_HEADER = (
    REPOSITORY_ROOT / "src" / "wspctl" / "include" / "wspctl" / "domain" / "runtime.hpp"
)
#: @brief 领域实现路径 / Domain implementation path.
DOMAIN_SOURCE = REPOSITORY_ROOT / "src" / "wspctl" / "src" / "domain" / "runtime.cpp"
#: @brief 应用用例头路径 / Application use-case header path.
APPLICATION_HEADER = (
    REPOSITORY_ROOT
    / "src"
    / "wspctl"
    / "include"
    / "wspctl"
    / "application"
    / "runtime_activation.hpp"
)
#: @brief sandbox 实现路径 / Sandbox implementation path.
SANDBOX_SOURCE = (
    REPOSITORY_ROOT / "src" / "wspctl" / "src" / "infrastructure" / "sandbox.cpp"
)
#: @brief broker 实现路径 / Broker implementation path.
BROKER_SOURCE = (
    REPOSITORY_ROOT / "src" / "wspctl" / "src" / "infrastructure" / "broker.cpp"
)
#: @brief XFS quota 实现路径 / XFS quota implementation path.
XFS_QUOTA_SOURCE = (
    REPOSITORY_ROOT
    / "src"
    / "wspctl"
    / "src"
    / "infrastructure"
    / "xfs_project_quota.cpp"
)
#: @brief 只读 payload replay verifier 实现路径 / Read-only payload replay verifier implementation path.
PAYLOAD_REPLAY_SOURCE = (
    REPOSITORY_ROOT / "src" / "wspctl" / "src" / "infrastructure" / "payload_replay.cpp"
)
#: @brief pidfd ownership helper 头路径 / pidfd ownership-helper header path.
PIDFD_CONTROL_HEADER = (
    REPOSITORY_ROOT
    / "src"
    / "wspctl"
    / "include"
    / "wspctl"
    / "infrastructure"
    / "detail"
    / "pidfd_control.hpp"
)
#: @brief pidfd ownership helper 实现路径 / pidfd ownership-helper implementation path.
PIDFD_CONTROL_SOURCE = (
    REPOSITORY_ROOT / "src" / "wspctl" / "src" / "infrastructure" / "pidfd_control.cpp"
)
#: @brief supervisor 实现路径 / Supervisor implementation path.
SUPERVISOR_SOURCE = (
    REPOSITORY_ROOT / "src" / "wspctl" / "src" / "infrastructure" / "supervisor.cpp"
)
#: @brief presentation Unix gateway 头路径 / Presentation Unix gateway header path.
PRESENTATION_GATEWAY_HEADER = (
    REPOSITORY_ROOT
    / "src"
    / "wspctl"
    / "include"
    / "wspctl"
    / "presentation"
    / "unix_gateway.hpp"
)
#: @brief presentation Unix gateway 实现路径 / Presentation Unix gateway implementation path.
PRESENTATION_GATEWAY_SOURCE = (
    REPOSITORY_ROOT / "src" / "wspctl" / "src" / "presentation" / "unix_gateway.cpp"
)
#: @brief CMake 路径 / CMake path.
WSPCTL_CMAKE = REPOSITORY_ROOT / "src" / "wspctl" / "CMakeLists.txt"


def test_domain_has_no_host_or_transport_dependencies() -> None:
    """@brief domain 不能依赖 host、filesystem、socket、pybind 或 common transport / Domain must not depend on host, filesystem, sockets, pybind, or common transport.

    @return None / None.
    """

    combined = DOMAIN_HEADER.read_text(encoding="utf-8") + DOMAIN_SOURCE.read_text(
        encoding="utf-8"
    )
    prohibited = (
        '"wspctl/common.hpp"',
        "<filesystem>",
        "<sys/",
        "<linux/",
        "<socket",
        "pybind",
    )
    for token in prohibited:
        assert token not in combined, f"domain must not depend on {token}"
    assert "class Sha256Digest" in combined
    assert "std::optional<ActivationId> active_activation_" in combined


def test_application_declares_a_port_and_use_case() -> None:
    """@brief application 通过 port 编排外设而非泄漏 Linux 细节 / Application orchestrates effects through a port without leaking Linux details.

    @return None / None.
    """

    text = APPLICATION_HEADER.read_text(encoding="utf-8")
    assert "class RuntimeActivationPort" in text
    assert "class RuntimeActivationService" in text
    assert "establish(" in text
    assert "retire(" in text
    assert "<sys/" not in text
    assert "<filesystem>" not in text


def test_proc_mount_hides_peers_and_fails_closed() -> None:
    """@brief runtime proc mount 必须启用 hidepid=2 和 subset=pid / Runtime proc mount must enable hidepid=2 and subset=pid.

    @return None / None.
    """

    text = SANDBOX_SOURCE.read_text(encoding="utf-8")
    assert (
        'mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, "hidepid=2,subset=pid")'
        in text
    )


def test_workspace_overlay_remains_executable_for_untrusted_payload_scripts() -> None:
    """@brief workspace overlay 不得带 noexec；不可信脚本受 namespace/cgroup/seccomp 限制而不是被误挂载禁用 / Workspace overlay must not be noexec; untrusted scripts are constrained by namespace/cgroup/seccomp rather than a mistaken data-mount prohibition.

    @return None / None.
    """

    text = SANDBOX_SOURCE.read_text(encoding="utf-8")
    overlay_mount = next(
        line
        for line in text.splitlines()
        if 'mount("overlay", layer.merged_dir.c_str(), "overlay"' in line
    )
    assert "MS_NOSUID | MS_NODEV" in overlay_mount
    assert "MS_NOEXEC" not in overlay_mount


def test_task_seccomp_closes_io_uring_socket_bypass() -> None:
    """@brief io_uring 入口必须被拒绝，避免 IORING_OP_SOCKET 绕过 AF_UNIX-only socket(2) 规则 / io_uring entries must be denied so IORING_OP_SOCKET cannot bypass the AF_UNIX-only socket(2) rule.

    @return None / None.
    """

    text = SANDBOX_SOURCE.read_text(encoding="utf-8")
    for syscall in ("io_uring_setup", "io_uring_enter", "io_uring_register"):
        assert f"SCMP_SYS({syscall})" in text
        assert f"__NR_{syscall}" in text


def test_task_seccomp_denies_global_keyring_and_module_operations() -> None:
    """@brief 不随 runtime namespace 隔离的 keyring/module syscall 必须被拒绝 / Keyring and module syscalls not isolated by the runtime namespaces must be denied.

    @return None / None.
    """

    text = SANDBOX_SOURCE.read_text(encoding="utf-8")
    for syscall in (
        "keyctl",
        "add_key",
        "request_key",
        "init_module",
        "finit_module",
        "delete_module",
    ):
        assert f"SCMP_SYS({syscall})" in text


def test_task_seccomp_preserves_broker_owned_xfs_project_quota() -> None:
    """@brief payload 不能通过 FS_IOC_FSSETXATTR 改写 XFS project quota 元数据 / Payload must not rewrite XFS project-quota metadata through FS_IOC_FSSETXATTR.

    @return None / None.
    """

    text = SANDBOX_SOURCE.read_text(encoding="utf-8")
    assert "#include <linux/fs.h>" in text
    assert "SCMP_SYS(ioctl)" in text
    for request in ("FS_IOC_FSSETXATTR", "FS_IOC_SETFLAGS", "FS_IOC32_SETFLAGS"):
        assert f"add_deny_ioctl_request_rule(filter, {request})" in text


def test_payload_resource_limits_are_irreversible_and_scoped() -> None:
    """@brief payload 必须在 exec 前限制 NOFILE/core，但不能抢先伪造 workspace FSIZE 配额 / Payload must constrain NOFILE/core before exec without inventing a workspace FSIZE quota.

    @return None / None.
    """

    text = SUPERVISOR_SOURCE.read_text(encoding="utf-8")
    assert "constexpr rlim_t kTaskNofileLimit{256U}" in text
    assert "setrlimit(RLIMIT_NOFILE, &nofile)" in text
    assert "setrlimit(RLIMIT_CORE, &core)" in text
    assert "RLIMIT_FSIZE is deliberately not set here" in text
    assert text.index("install_task_rlimits()") < text.index(
        "harden_task(config.sandbox_uid, config.sandbox_gid)"
    )


def test_broker_uses_application_lifecycle_port_in_real_paths() -> None:
    """@brief 特权 broker 必须通过 application use case 驱动真实 activation/retire，而非旁路 domain 状态机 / The privileged broker must drive real activation/retirement through the application use case rather than bypassing the domain state machine.

    @return None / None.
    """

    broker = BROKER_SOURCE.read_text(encoding="utf-8")
    cmake = WSPCTL_CMAKE.read_text(encoding="utf-8")
    assert '#include "wspctl/application/runtime_activation.hpp"' in broker
    assert "class BrokerRuntimeActivationPort" in broker
    assert "lifecycle.activate(created->runtime, created->activation, port)" in broker
    assert "lifecycle.retire(session->runtime, session->activation, port)" in broker
    assert "lifecycle.abort(session->runtime, session->activation, port)" in broker
    assert ".runtime.begin_activation(" not in broker
    assert ".runtime.mark_ready(" not in broker
    assert ".runtime.begin_retirement(" not in broker
    assert ".runtime.finish_retirement(" not in broker
    infrastructure_block = cmake[
        cmake.index("target_link_libraries(wspctl_infrastructure") : cmake.index(
            "wspctl_enable_warnings(wspctl_infrastructure)"
        )
    ]
    assert "PRIVATE\n        wspctl_application" in infrastructure_block


def test_payload_replay_never_activates_or_provisions_state() -> None:
    """@brief attachment replay 只能读取既有回执/binding/object，绝不能激活 Runtime 或 provision XFS 状态 / Attachment replay may only read existing receipt/binding/object; it must never activate a Runtime or provision XFS state.

    @return None / None.
    """

    broker = BROKER_SOURCE.read_text(encoding="utf-8")
    quota = XFS_QUOTA_SOURCE.read_text(encoding="utf-8")
    verifier = PAYLOAD_REPLAY_SOURCE.read_text(encoding="utf-8")
    replay_start = broker.index("Result<PayloadResult> Broker::replay_payload(")
    replay_end = broker.index(
        "Result<void> Broker::dispatch_payload_stream(", replay_start
    )
    replay_block = broker[replay_start:replay_end]
    assert "detail::resolve_payload_replay_receipt(journal_, request)" in replay_block
    assert "execution_gate_.try_acquire(admission->runtime.value())" in replay_block
    assert "quota_.find_ready_runtime(admission->runtime.value())" in replay_block
    for forbidden in (
        "acquire_session(",
        "ensure_runtime(",
        "journal_.begin",
        "journal_.begin_payload",
        "dispatch_payload_stream(",
    ):
        assert forbidden not in replay_block, f"replay must not perform {forbidden}"
    lookup_start = quota.index(
        "Result<RuntimeQuotaBinding> XfsProjectQuota::find_ready_runtime("
    )
    lookup_end = quota.index(
        "Result<RuntimeActivationLease> XfsProjectQuota::acquire_activation_lease(",
        lookup_start,
    )
    lookup_block = quota[lookup_start:lookup_end]
    assert "lock_existing_registry_shared(registry_root)" in lookup_block
    for forbidden in (
        "ensure_registry_directories(",
        "lock_registry(",
        "O_CREAT",
        "fchmod",
        "fchown",
    ):
        assert forbidden not in lookup_block, (
            f"read-only binding lookup must not perform {forbidden}"
        )
    assert "O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW" in verifier
    assert "ErrorCode::invocation_in_doubt" in verifier
    assert "O_CREAT" not in verifier


def test_helper_cleanup_uses_pidfd_not_reusable_pid1_number() -> None:
    """@brief fork helper 必须以 PID1 pidfd SIGKILL 终止 namespace，并消费 descriptor / The fork helper must terminate a namespace with PID1 pidfd SIGKILL and consume the descriptor.

    @return None / None.
    """

    broker = BROKER_SOURCE.read_text(encoding="utf-8")
    helper_header = PIDFD_CONTROL_HEADER.read_text(encoding="utf-8")
    helper = PIDFD_CONTROL_SOURCE.read_text(encoding="utf-8")
    assert (
        "int pid1_pidfd = static_cast<int>(syscall(SYS_pidfd_open, *pid1, 0U))"
        in broker
    )
    assert '#include "wspctl/infrastructure/detail/pidfd_control.hpp"' in broker
    assert "detail::signal_and_close_pidfd(record.pid1_pidfd, SIGKILL)" in broker
    assert (
        "signal_and_close_pidfd(int& owned_pidfd, int signal) noexcept" in helper_header
    )
    assert "const int pidfd = std::exchange(owned_pidfd, -1);" in helper
    assert "errno == ESRCH" in helper
    assert "static_cast<void>(close(pidfd));" in helper
    assert "kill(record.pid1_pid" not in broker


def test_launcher_self_joins_supervisor_cgroup_before_forking_pid1() -> None:
    """@brief launcher 必须以 cgroup ``0`` 自加入 supervisor，broker 不能再按 PID1 数字 placement / Launcher must self-join supervisor through cgroup ``0`` and the broker must not place PID1 by number.

    @return None / None.
    """

    broker = BROKER_SOURCE.read_text(encoding="utf-8")
    sandbox = SANDBOX_SOURCE.read_text(encoding="utf-8")
    assert 'constexpr std::string_view kSelf{"0"};' in broker
    assert (
        "join_cgroup_self(static_cast<int>(LaunchFd::supervisor_cgroup_procs))"
        in broker
    )
    assert "cgroup.supervisor_procs_fd" in broker
    assert ".supervisor_procs_fd = supervisor_procs_fd" in sandbox
    assert "place_in_runtime_cgroup" not in broker
    assert "place_in_runtime_cgroup" not in sandbox


def test_physical_src_layout_has_only_layered_roots() -> None:
    """@brief 物理树必须按 include/src 的四层收敛 / The physical tree must converge on four layers under include/src.

    @return None / None.
    """

    wspctl_root = REPOSITORY_ROOT / "src" / "wspctl"
    for layer in ("domain", "application", "infrastructure", "presentation"):
        assert (wspctl_root / "include" / "wspctl" / layer).is_dir()
        assert (wspctl_root / "src" / layer).is_dir()
    for retired_root in (
        "domain",
        "application",
        "infrastructure",
        "presentation",
        "image",
        "python",
        "systemd",
        "broker",
    ):
        assert not (wspctl_root / retired_root).exists(), (
            f"legacy flat root remains: {retired_root}"
        )


def test_presentation_exposes_a_real_unix_gateway_adapter() -> None:
    """@brief presentation target 必须拥有实际 Bot gateway，而非空壳 INTERFACE target / Presentation target must own a real Bot gateway rather than an empty INTERFACE target.

    @return None / None.
    """

    header = PRESENTATION_GATEWAY_HEADER.read_text(encoding="utf-8")
    source = PRESENTATION_GATEWAY_SOURCE.read_text(encoding="utf-8")
    cmake = WSPCTL_CMAKE.read_text(encoding="utf-8")
    assert "class UnixGatewayClient" in header
    assert "struct ClientExecuteRequest" in header
    assert "UnixGatewayClient::execute" in source
    assert "socket_path.find('\\0')" in source
    assert "add_library(wspctl_presentation STATIC" in cmake


def test_cmake_declares_one_way_layer_targets() -> None:
    """@brief CMake 必须显式声明 domain/application/infrastructure/presentation 顺序 / CMake must explicitly declare domain/application/infrastructure/presentation order.

    @return None / None.
    """

    text = WSPCTL_CMAKE.read_text(encoding="utf-8")
    domain_index = text.index("add_library(wspctl_domain")
    application_index = text.index("add_library(wspctl_application")
    infrastructure_index = text.index("add_library(wspctl_infrastructure")
    presentation_index = text.index("add_library(wspctl_presentation")
    assert domain_index < application_index < infrastructure_index < presentation_index


def _run_contract_tests() -> None:
    """@brief 以 CTest 直接执行静态契约测试 / Execute static contract tests directly under CTest.

    @return None / None.
    """

    test_domain_has_no_host_or_transport_dependencies()
    test_application_declares_a_port_and_use_case()
    test_proc_mount_hides_peers_and_fails_closed()
    test_workspace_overlay_remains_executable_for_untrusted_payload_scripts()
    test_task_seccomp_closes_io_uring_socket_bypass()
    test_task_seccomp_denies_global_keyring_and_module_operations()
    test_task_seccomp_preserves_broker_owned_xfs_project_quota()
    test_payload_resource_limits_are_irreversible_and_scoped()
    test_broker_uses_application_lifecycle_port_in_real_paths()
    test_payload_replay_never_activates_or_provisions_state()
    test_helper_cleanup_uses_pidfd_not_reusable_pid1_number()
    test_launcher_self_joins_supervisor_cgroup_before_forking_pid1()
    test_physical_src_layout_has_only_layered_roots()
    test_presentation_exposes_a_real_unix_gateway_adapter()
    test_cmake_declares_one_way_layer_targets()


if __name__ == "__main__":
    _run_contract_tests()
