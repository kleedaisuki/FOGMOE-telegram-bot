"""@brief wspctl 部署边界静态契约测试 / Static deployment-boundary contract tests for wspctl."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief Docker 构建定义路径 / Docker build definition path.
DOCKERFILE_PATH = REPOSITORY_ROOT / "Dockerfile"
#: @brief wspctl CMake 定义路径 / wspctl CMake definition path.
WSPCTL_CMAKE_PATH = REPOSITORY_ROOT / "src" / "wspctl" / "CMakeLists.txt"
#: @brief 顶层 CMake 定义路径 / Top-level CMake definition path.
ROOT_CMAKE_PATH = REPOSITORY_ROOT / "CMakeLists.txt"
#: @brief Python 构建配置路径 / Python build configuration path.
PYPROJECT_PATH = REPOSITORY_ROOT / "pyproject.toml"
#: @brief Compose 定义路径 / Compose definition path.
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.yml"
#: @brief workspace runtime OCI 构建定义 / Workspace-runtime OCI build definition.
WSPCTL_CONTAINERFILE_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "image" / "Containerfile"
)
#: @brief workspace runtime builder-tool lock / Workspace-runtime builder-tool lock.
WSPCTL_BUILD_TOOLS_LOCK_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "image" / "build-tools.lock"
)
#: @brief 固定 runtime hostname 文件 / Fixed runtime hostname file.
WSPCTL_HOSTNAME_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "image" / "etc" / "hostname"
)
#: @brief 固定 runtime hosts 文件 / Fixed runtime hosts file.
WSPCTL_HOSTS_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "image" / "etc" / "hosts"
)
#: @brief 离线 runtime resolver 文件 / Offline runtime resolver file.
WSPCTL_RESOLV_CONF_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "image" / "etc" / "resolv.conf"
)
#: @brief native runtime image contract 实现 / Native runtime-image contract implementation.
WSPCTL_IMAGE_CONTRACT_SOURCE_PATH = (
    REPOSITORY_ROOT / "src" / "wspctl" / "src" / "infrastructure" / "image.cpp"
)
#: @brief workspace PID 1 入口 / Workspace PID 1 entry point.
WSPCTL_SUPERVISOR_MAIN_PATH = (
    REPOSITORY_ROOT
    / "src"
    / "wspctl"
    / "src"
    / "presentation"
    / "systemd"
    / "main.cpp"
)
#: @brief workspace supervisor 实现 / Workspace supervisor implementation.
WSPCTL_SUPERVISOR_PATH = (
    REPOSITORY_ROOT
    / "src"
    / "wspctl"
    / "src"
    / "infrastructure"
    / "supervisor.cpp"
)
#: @brief wspctl-scoped host deployment CMake 路径 / wspctl-scoped host-deployment CMake path.
DEPLOYMENT_CMAKE_PATH = REPOSITORY_ROOT / "deploy" / "wspctl" / "CMakeLists.txt"
#: @brief host broker systemd unit 模板路径 / Host-broker systemd unit template path.
SYSTEMD_UNIT_TEMPLATE_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "systemd" / "wspctld.service.in"
)
#: @brief wspctl 专用 LXCFS unit 模板路径 / Dedicated wspctl LXCFS unit-template path.
LXCFS_SYSTEMD_UNIT_TEMPLATE_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "systemd" / "wspctl-lxcfs.service.in"
)
#: @brief host broker 环境文件模板路径 / Host-broker environment-template path.
SYSTEMD_ENVIRONMENT_TEMPLATE_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "systemd" / "wspctld.env.example.in"
)
#: @brief 部署说明路径 / Deployment guide path.
DEPLOYMENT_GUIDE_PATH = REPOSITORY_ROOT / "docs" / "wspctl-deployment.md"
#: @brief XFS project quota 容量契约路径 / XFS project-quota capacity contract path.
XFS_QUOTA_GUIDE_PATH = REPOSITORY_ROOT / "docs" / "wspctl-xfs-project-quota.md"


def test_bot_image_builds_native_client_but_excludes_host_broker_programs() -> None:
    """@brief wheel 由 builder 编译，最终镜像不能保留 broker 程序 / Build client wheel in builder but exclude broker programs from final image.

    @return None / None.
    """

    dockerfile = DOCKERFILE_PATH.read_text(encoding="utf-8")
    cmake = WSPCTL_CMAKE_PATH.read_text(encoding="utf-8")
    root_cmake = ROOT_CMAKE_PATH.read_text(encoding="utf-8")
    with PYPROJECT_PATH.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)
    wheel_packages = pyproject["tool"]["scikit-build"]["wheel"]["packages"]
    assert isinstance(wheel_packages, list)
    assert "AS wheel-builder" in dockerfile
    assert '"cmake>=3.28"' in dockerfile
    assert "libseccomp-dev" in dockerfile
    assert "libcap-dev" in dockerfile
    assert "--config-settings=cmake.define.WSPCTL_INSTALL_HOST_TOOLS=OFF" in dockerfile
    assert (
        re.search(
            r'option\(WSPCTL_INSTALL_HOST_TOOLS\s+"[^"]+"\s+OFF\)',
            cmake,
        )
        is not None
    )
    assert "src/wspctl" not in wheel_packages
    assert "AS bot-runtime" in dockerfile
    assert (
        "if(WSPCTL_INSTALL_HOST_TOOLS)\n"
        "    add_subdirectory(deploy/wspctl)\n"
        "endif()" in root_cmake
    )
    assert re.search(
        r"install\(\s+TARGETS wspctld wspctl\s+RUNTIME DESTINATION bin\s+COMPONENT WspctlHost",
        cmake,
    )
    assert re.search(
        r"install\(\s+TARGETS wspctl-image\s+RUNTIME DESTINATION bin\s+COMPONENT WspctlPublisher",
        cmake,
    )
    assert "install(TARGETS wsp-systemd" not in cmake
    assert "add_library(wspctl_image_infrastructure STATIC" in cmake
    assert (
        "target_link_libraries(wspctl-image PRIVATE wspctl_image_infrastructure)"
        in cmake
    )
    assert "USER 65532:65532" in dockerfile
    assert 'CMD ["fogmoe-bot", "--config", "/app/config.json"]' in dockerfile


def test_compose_exposes_only_a_nonprivileged_bot_client_to_the_socket() -> None:
    """@brief Compose 不得声明 broker，且只读连接 socket / Compose must not declare the broker and connects to the socket readonly.

    @return None / None.
    """

    compose = COMPOSE_PATH.read_text(encoding="utf-8")
    assert re.search(r"(?m)^  wspctld:\s*$", compose) is None
    assert 'user: "65532:65532"' in compose
    assert "read_only: true" in compose
    assert "privileged: false" in compose
    assert "- ALL" in compose
    assert "cap_add:" not in compose
    assert "- no-new-privileges:true" in compose
    assert "source: ./.wspctl/run/bot" in compose
    assert "target: /app/.wspctl/run/bot" in compose
    assert "source: ./.wspctl/run/operator" not in compose
    assert "target: /app/.wspctl/run/operator" not in compose
    assert "/var/run/docker.sock" not in compose
    assert "/run/containerd/containerd.sock" not in compose
    assert "/sys/fs/cgroup" not in compose
    assert "/lib/modules" not in compose
    assert "create_host_path: false" in compose


def test_workspace_runtime_is_a_digest_pinned_explicit_oci_build() -> None:
    """@brief workspace runtime 必须由独立、固定输入的 OCI 配方构建 / Workspace runtime must be built by a separate OCI recipe with pinned inputs.

    @return None / None.
    """

    containerfile = WSPCTL_CONTAINERFILE_PATH.read_text(encoding="utf-8")
    build_tools_lock = WSPCTL_BUILD_TOOLS_LOCK_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"docker\.io/library/python:3\.14-slim-bookworm@sha256:[0-9a-f]{64}",
        containerfile,
    )
    assert "AS supervisor-builder" in containerfile
    assert "AS wspctl-runtime" in containerfile
    assert "DEBIAN_SNAPSHOT=" in containerfile
    assert "snapshot.debian.org" in containerfile
    assert "COPY --from=wspctl-build-tools" in containerfile
    assert "--no-index" in containerfile
    assert "--no-deps" in containerfile
    assert re.search(r"(?m)^[0-9a-f]{64}  cmake-", build_tools_lock)
    assert re.search(r"(?m)^[0-9a-f]{64}  ninja-", build_tools_lock)
    assert re.search(r"(?m)^[0-9a-f]{64}  node-v24\.", build_tools_lock)
    assert re.search(r"(?m)^[0-9a-f]{64}  pnpm-11\.", build_tools_lock)
    assert "-DWSPCTL_BUILD_PYTHON_BINDINGS=OFF" in containerfile
    assert "cmake --build /build --target wsp-systemd" in containerfile
    assert "libcap2" in containerfile
    assert "libseccomp2" in containerfile
    assert "libssl3" in containerfile
    assert "site-packages" in containerfile
    assert 'io.fogmoe.wspctl.contract="3"' in containerfile
    assert "WSPCTL_AGENT_UID=65533" in containerfile
    assert "WSPCTL_AGENT_GID=65533" in containerfile
    for executable in (
        "/usr/local/bin/python3",
        "/usr/bin/curl",
        "/usr/bin/wget",
        "/usr/bin/git",
        "/usr/bin/jq",
        "/usr/bin/gcc",
        "/usr/bin/g++",
        "/usr/local/bin/node",
        "/usr/local/bin/pnpm",
        "/usr/bin/ffmpeg",
        "/usr/bin/convert",
        "/usr/bin/sqlite3",
        "/usr/bin/htop",
        "/usr/bin/hostname",
        "/usr/bin/domainname",
        "/usr/bin/tree",
        "/usr/bin/neofetch",
        "/usr/bin/java",
        "/usr/bin/javac",
    ):
        assert f"test -x {executable}" in containerfile
    assert 'ENTRYPOINT ["/usr/local/libexec/wspctl/wsp-systemd"]' in containerfile
    assert "COPY --from=supervisor-builder" in containerfile
    assert "find / -xdev -type f -perm /6000" in containerfile
    assert "COPY .venv" not in containerfile
    assert "readelf" not in containerfile
    assert "ldconfig" not in containerfile


def test_workspace_runtime_files_match_the_fixed_offline_uts_identity() -> None:
    """@brief 镜像静态主机文件必须与 UTS 固定身份及无 IP 网络合同一致 / Image host files must match the fixed UTS identity and no-IP-network contract.

    @return None / None.
    """

    containerfile = WSPCTL_CONTAINERFILE_PATH.read_text(encoding="utf-8")
    hostname = WSPCTL_HOSTNAME_PATH.read_text(encoding="utf-8")
    hosts = WSPCTL_HOSTS_PATH.read_text(encoding="utf-8")
    resolv_conf = WSPCTL_RESOLV_CONF_PATH.read_text(encoding="utf-8")
    native_contract = WSPCTL_IMAGE_CONTRACT_SOURCE_PATH.read_text(encoding="utf-8")
    assert hostname == "workspace\n"
    assert "127.0.0.1 localhost\n" in hosts
    assert "127.0.1.1 workspace.localdomain workspace\n" in hosts
    assert "::1 localhost ip6-localhost ip6-loopback\n" in hosts
    assert "nameserver" not in resolv_conf
    assert "COPY --chown=0:0 --chmod=0644 deploy/wspctl/image/etc/ /etc/" in containerfile
    assert "hosts:          files" in containerfile
    final_validation = containerfile.index("RUN test -z")
    host_files_copy = containerfile.index(
        "COPY --chown=0:0 --chmod=0644 deploy/wspctl/image/etc/ /etc/"
    )
    assert final_validation < host_files_copy < containerfile.index("ENTRYPOINT")
    for contract_path in (
        "etc/hostname",
        "etc/hosts",
        "etc/resolv.conf",
        "etc/nsswitch.conf",
        "usr/bin/hostname",
        "usr/bin/domainname",
    ):
        assert f'"{contract_path}"' in native_contract


def test_supervisor_pins_private_workspace_before_dropping_dac_override() -> None:
    """@brief PID 1 必须在降权前固定打开 Agent 私有 workspace /
    PID 1 must pin the Agent-private workspace before dropping DAC override.

    @return None / None.
    """

    supervisor = WSPCTL_SUPERVISOR_MAIN_PATH.read_text(encoding="utf-8")
    open_workspace = supervisor.index('config.workspace_fd = open(')
    drop_privileges = supervisor.index("wspctl::harden_supervisor()")
    assert open_workspace < drop_privileges
    assert "O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW" in supervisor
    hardening_failure = supervisor.index(
        "wsp-systemd: cannot reduce supervisor privileges"
    )
    close_workspace = supervisor.index(
        "close(config.workspace_fd)",
        hardening_failure,
    )
    assert hardening_failure < close_workspace


def test_task_resolves_cwd_as_agent_from_the_pinned_workspace_fd() -> None:
    """@brief task 必须降权后通过固定 FD 安全解析 cwd /
    Tasks must securely resolve cwd from the pinned FD after dropping identity.

    @return None / None.
    """

    supervisor = WSPCTL_SUPERVISOR_PATH.read_text(encoding="utf-8")
    drop_identity = supervisor.index(
        "harden_task(config.sandbox_uid, config.sandbox_gid)"
    )
    resolve_cwd = supervisor.index(
        "open_workspace_cwd_for_task(config, request.cwd)"
    )
    close_control_fds = supervisor.index(
        "close_non_stdio_fds()",
        resolve_cwd,
    )
    assert drop_identity < resolve_cwd < close_control_fds
    assert "fchdir(*cwd_fd)" in supervisor
    assert (
        "RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | "
        "RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV"
    ) in " ".join(supervisor.split())
    assert "chdir(std::string(effective_cwd)" not in supervisor


def test_host_unit_requires_exact_socket_uid_and_readonly_image_mount() -> None:
    """@brief host unit 必须固定 peer UID、工作根并在启动前验证只读 image / Host unit fixes peer UID, work root, and verifies the readonly image before start.

    @return None / None.
    """

    unit = SYSTEMD_UNIT_TEMPLATE_PATH.read_text(encoding="utf-8")
    lxcfs_unit = LXCFS_SYSTEMD_UNIT_TEMPLATE_PATH.read_text(encoding="utf-8")
    environment = SYSTEMD_ENVIRONMENT_TEMPLATE_PATH.read_text(encoding="utf-8")
    deployment_cmake = DEPLOYMENT_CMAKE_PATH.read_text(encoding="utf-8")
    deployment_guide = DEPLOYMENT_GUIDE_PATH.read_text(encoding="utf-8")
    quota_guide = XFS_QUOTA_GUIDE_PATH.read_text(encoding="utf-8")
    assert not (REPOSITORY_ROOT / "systemd").exists()
    assert "Delegate=cpuset cpu memory pids io" in unit
    assert "Requires=wspctl-lxcfs.service" in unit
    assert "BindsTo=wspctl-lxcfs.service" in unit
    assert "After=local-fs.target wspctl-lxcfs.service" in unit
    assert "Type=notify" in unit
    assert "Type=simple" not in unit
    assert "NotifyAccess=main" in unit
    assert "TimeoutStartSec=30s" in unit
    assert "--systemd-notify" in unit
    assert "RestartPreventExitStatus=78" in unit
    assert "CAP_MKNOD" in unit
    assert "CAP_SETPCAP" in unit
    capability_line = next(
        line for line in unit.splitlines() if line.startswith("CapabilityBoundingSet=")
    )
    assert set(capability_line.removeprefix("CapabilityBoundingSet=").split()) == {
        "CAP_SYS_ADMIN",
        "CAP_SYS_CHROOT",
        "CAP_SETUID",
        "CAP_SETGID",
        "CAP_SETPCAP",
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_FOWNER",
        "CAP_KILL",
        "CAP_MKNOD",
    }
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "RuntimeDirectory=" not in unit
    assert "StateDirectory=" not in unit
    assert "EnvironmentFile=@WSPCTL_HOST_ENVIRONMENT_PATH@" in unit
    assert "ExecStart=@WSPCTL_HOST_BROKER_EXECUTABLE@" in unit
    assert "RequiresMountsFor=@WSPCTL_HOST_STATE_ROOT@" in unit
    assert "ConditionPathIsMountPoint=@WSPCTL_HOST_STATE_ROOT@" in unit
    assert "--mode=0700 @WSPCTL_HOST_STATE_ROOT@" not in unit
    assert "ReadWritePaths=@WSPCTL_HOST_WORKDIR@ /sys/fs/cgroup" in unit
    assert "@WSPCTL_HOST_SOCKET_ROOT_DIRECTORY@" in unit
    assert "@WSPCTL_HOST_SOCKET_DIRECTORY@" in unit
    assert "@WSPCTL_HOST_OPERATOR_SOCKET_DIRECTORY@" in unit
    assert "WSPCTL_BASE_ROOT" not in unit
    assert "--image-store ${WSPCTL_IMAGES_ROOT}" in unit
    assert "--image-digest ${WSPCTL_IMAGE_DIGEST}" in unit
    assert "--client-uid ${WSPCTL_CLIENT_UID}" in unit
    assert "--operator-socket ${WSPCTL_OPERATOR_SOCKET}" in unit
    assert "--operator-uid ${WSPCTL_OPERATOR_UID}" in unit
    assert "--cpu-max-us ${WSPCTL_CPU_MAX_US}" in unit
    assert "--cpu-period-us ${WSPCTL_CPU_PERIOD_US}" in unit
    assert "--memory-high ${WSPCTL_MEMORY_HIGH}" in unit
    assert "--memory-swap-max ${WSPCTL_MEMORY_SWAP_MAX}" in unit
    assert "--tmp-size-bytes ${WSPCTL_TMP_SIZE_BYTES}" in unit
    assert "--io-weight ${WSPCTL_IO_WEIGHT}" in unit
    assert "--lxcfs-root @WSPCTL_HOST_LXCFS_ROOT@" in unit
    assert "--quota-backend ${WSPCTL_QUOTA_BACKEND}" in unit
    assert "--xfs-quota-mount ${WSPCTL_XFS_QUOTA_MOUNT}" in unit
    assert "--xfs-project-id-min ${WSPCTL_XFS_PROJECT_ID_MIN}" in unit
    assert "--xfs-project-id-max ${WSPCTL_XFS_PROJECT_ID_MAX}" in unit
    assert "--runtime-control-hard-bytes ${WSPCTL_RUNTIME_CONTROL_HARD_BYTES}" in unit
    assert (
        "--runtime-workspace-hard-inodes ${WSPCTL_RUNTIME_WORKSPACE_HARD_INODES}"
        in unit
    )
    assert "--xfs-global-admission-bytes ${WSPCTL_XFS_GLOBAL_ADMISSION_BYTES}" in unit
    assert "--xfs-system-reserve-inodes ${WSPCTL_XFS_SYSTEM_RESERVE_INODES}" in unit
    assert "WSPCTL_SOCKET=@WSPCTL_HOST_SOCKET_PATH@" in environment
    assert "WSPCTL_OPERATOR_SOCKET=@WSPCTL_HOST_OPERATOR_SOCKET_PATH@" in environment
    assert "WSPCTL_OPERATOR_UID=0" in environment
    assert "WSPCTL_STATE_ROOT=@WSPCTL_HOST_STATE_ROOT@" in environment
    assert "WSPCTL_IMAGES_ROOT=@WSPCTL_HOST_IMAGES_ROOT@" in environment
    assert "WSPCTL_XFS_QUOTA_MOUNT=@WSPCTL_HOST_STATE_ROOT@" in environment
    assert "WSPCTL_QUOTA_BACKEND=xfs_project_v1" in environment
    assert "WSPCTL_XFS_PROJECT_ID_MIN=100000" in environment
    assert "WSPCTL_XFS_PROJECT_ID_MAX=199999" in environment
    assert "WSPCTL_RUNTIME_CONTROL_HARD_BYTES=16777216" in environment
    assert "WSPCTL_RUNTIME_WORKSPACE_HARD_BYTES=4294967296" in environment
    assert "WSPCTL_RUNTIME_WORKSPACE_HARD_INODES=131072" in environment
    assert "WSPCTL_XFS_GLOBAL_ADMISSION_BYTES=53687091200" in environment
    assert "WSPCTL_XFS_SYSTEM_RESERVE_INODES=262144" in environment
    assert "WSPCTL_CLIENT_UID=65532" in environment
    assert "WSPCTL_SANDBOX_UID=65533" in environment
    assert "WSPCTL_SANDBOX_GID=65533" in environment
    assert "WSPCTL_OPERATOR_UID=0" in environment
    assert "WSPCTL_BASE_ROOT" not in environment
    assert "WSPCTL_SUPERVISOR" not in environment
    assert "WSPCTL_IMAGE_DIGEST=sha256:REPLACE_WITH_64_LOWERCASE_HEX" in environment
    assert "WSPCTL_CPU_MAX_US=200000" in environment
    assert "WSPCTL_CPU_PERIOD_US=100000" in environment
    assert "WSPCTL_MEMORY_MAX=4294967296" in environment
    assert "WSPCTL_MEMORY_HIGH=4294967296" in environment
    assert "WSPCTL_MEMORY_SWAP_MAX=2147483648" in environment
    assert "WSPCTL_TMP_SIZE_BYTES=1073741824" in environment
    assert "WSPCTL_IO_WEIGHT=100" in environment
    assert "Authoritative identity" in environment
    assert "per-runtime" in unit
    assert "XFS project quota" in deployment_guide
    assert "wspctl-xfs-project-quota.md" in deployment_guide
    assert "WSPCTL_QUOTA_BACKEND=xfs_project_v1" in quota_guide
    assert "PROJINHERIT" in quota_guide
    assert "bhard" in quota_guide
    assert "ihard" in quota_guide
    assert "wspctl.xfs_project_quota" in deployment_guide
    assert "WSPCTL_REQUIRE_XFS_QUOTA_TESTS=1" in deployment_guide
    assert "WSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT" in deployment_cmake
    assert "--allow-insecure-dev-root" in deployment_cmake
    assert "WSPCTL_HOST_WORKDIR is mandatory" in deployment_cmake
    assert "WSPCTL_HOST_ENVIRONMENT_PATH" in deployment_cmake
    assert "WSPCTL_HOST_LXCFS_ROOT" in deployment_cmake
    assert "wspctl-lxcfs.service.in" in deployment_cmake
    assert "/usr/bin/lxcfs --enable-loadavg --enable-cfs --enable-pidfd" in lxcfs_unit
    assert "RuntimeDirectory=fogmoe-wspctl-lxcfs" in lxcfs_unit
    assert "ExecStopPost=-/usr/bin/fusermount3 --unmount" in lxcfs_unit
    assert "ConditionPathIsExecutable" not in lxcfs_unit
    assert (
        'set(WSPCTL_HOST_BROKER_EXECUTABLE "${CMAKE_INSTALL_FULL_BINDIR}/wspctld")'
        in deployment_cmake
    )
    assert (
        'DESTINATION "${CMAKE_INSTALL_DATADIR}/fogmoe-wspctl/systemd"'
        in deployment_cmake
    )
    assert "tools/publish_wspctl_image.py" in deployment_cmake
    assert 'DESTINATION "${CMAKE_INSTALL_LIBEXECDIR}/wspctl"' in deployment_cmake
    assert "local developer 纳入 trusted control plane" in deployment_guide
    assert "WSPCTL_OPERATOR_SOCKET" in deployment_guide
    assert "workspace ls" in deployment_guide
    assert "OverlayFS upper layer" in deployment_guide
    assert "run/bot" in deployment_guide
    assert "run/operator" in deployment_guide
    assert "target_compile_definitions(\n    wspctl" in deployment_cmake
    assert "WSPCTL_DEFAULT_OPERATOR_SOCKET" in deployment_cmake
    assert (
        'set(WSPCTL_HOST_SOCKET_DIRECTORY "${WSPCTL_HOST_SOCKET_ROOT_DIRECTORY}/bot")'
        in deployment_cmake
    )
    assert (
        'set(WSPCTL_HOST_OPERATOR_SOCKET_DIRECTORY "${WSPCTL_HOST_SOCKET_ROOT_DIRECTORY}/operator")'
        in deployment_cmake
    )


def _run_contract_tests() -> None:
    """@brief 以 CTest 直接执行静态契约测试 / Execute static contract tests directly under CTest.

    @return None / None.
    """

    test_bot_image_builds_native_client_but_excludes_host_broker_programs()
    test_compose_exposes_only_a_nonprivileged_bot_client_to_the_socket()
    test_workspace_runtime_is_a_digest_pinned_explicit_oci_build()
    test_host_unit_requires_exact_socket_uid_and_readonly_image_mount()


if __name__ == "__main__":
    _run_contract_tests()
