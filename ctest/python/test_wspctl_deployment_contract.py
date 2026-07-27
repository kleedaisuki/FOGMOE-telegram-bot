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
#: @brief wspctl-scoped host deployment CMake 路径 / wspctl-scoped host-deployment CMake path.
DEPLOYMENT_CMAKE_PATH = REPOSITORY_ROOT / "deploy" / "wspctl" / "CMakeLists.txt"
#: @brief host broker systemd unit 模板路径 / Host-broker systemd unit template path.
SYSTEMD_UNIT_TEMPLATE_PATH = (
    REPOSITORY_ROOT / "deploy" / "wspctl" / "systemd" / "wspctld.service.in"
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
    assert (
        re.search(
            r"install\(TARGETS\s+wspctld\s+wspctl-image\s+RUNTIME DESTINATION bin\)",
            cmake,
        )
        is not None
    )
    assert (
        re.search(
            r"install\(TARGETS\s+wsp-systemd\s+RUNTIME DESTINATION libexec/wspctl\)",
            cmake,
        )
        is not None
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
    assert "source: ./.wspctl/run" in compose
    assert "target: /app/.wspctl/run" in compose
    assert "/var/run/docker.sock" not in compose
    assert "/run/containerd/containerd.sock" not in compose
    assert "/sys/fs/cgroup" not in compose
    assert "/lib/modules" not in compose
    assert "create_host_path: false" in compose


def test_host_unit_requires_exact_socket_uid_and_readonly_generation_mount() -> None:
    """@brief host unit 必须固定 peer UID、工作根并在启动前验证只读 generation / Host unit fixes peer UID, work root, and verifies readonly generation before start.

    @return None / None.
    """

    unit = SYSTEMD_UNIT_TEMPLATE_PATH.read_text(encoding="utf-8")
    environment = SYSTEMD_ENVIRONMENT_TEMPLATE_PATH.read_text(encoding="utf-8")
    deployment_cmake = DEPLOYMENT_CMAKE_PATH.read_text(encoding="utf-8")
    deployment_guide = DEPLOYMENT_GUIDE_PATH.read_text(encoding="utf-8")
    quota_guide = XFS_QUOTA_GUIDE_PATH.read_text(encoding="utf-8")
    assert not (REPOSITORY_ROOT / "systemd").exists()
    assert "Delegate=cpu memory pids io" in unit
    assert "CAP_MKNOD" in unit
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
    assert "@WSPCTL_HOST_SOCKET_DIRECTORY@" in unit
    assert "ExecStartPre=/usr/bin/test -d ${WSPCTL_BASE_ROOT}" in unit
    assert "findmnt --noheadings --output OPTIONS --target" in unit
    assert "grep --extended-regexp --quiet" in unit
    assert "--client-uid ${WSPCTL_CLIENT_UID}" in unit
    assert "--cpu-max-us ${WSPCTL_CPU_MAX_US}" in unit
    assert "--cpu-period-us ${WSPCTL_CPU_PERIOD_US}" in unit
    assert "--memory-high ${WSPCTL_MEMORY_HIGH}" in unit
    assert "--memory-swap-max ${WSPCTL_MEMORY_SWAP_MAX}" in unit
    assert "--io-weight ${WSPCTL_IO_WEIGHT}" in unit
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
    assert "WSPCTL_STATE_ROOT=@WSPCTL_HOST_STATE_ROOT@" in environment
    assert "WSPCTL_IMAGES_ROOT=@WSPCTL_HOST_IMAGES_ROOT@" in environment
    assert "WSPCTL_XFS_QUOTA_MOUNT=@WSPCTL_HOST_STATE_ROOT@" in environment
    assert "WSPCTL_QUOTA_BACKEND=xfs_project_v1" in environment
    assert "WSPCTL_XFS_PROJECT_ID_MIN=100000" in environment
    assert "WSPCTL_XFS_PROJECT_ID_MAX=199999" in environment
    assert "WSPCTL_RUNTIME_CONTROL_HARD_BYTES=16777216" in environment
    assert "WSPCTL_RUNTIME_WORKSPACE_HARD_INODES=131072" in environment
    assert "WSPCTL_XFS_GLOBAL_ADMISSION_BYTES=53687091200" in environment
    assert "WSPCTL_XFS_SYSTEM_RESERVE_INODES=262144" in environment
    assert "WSPCTL_CLIENT_UID=65532" in environment
    assert "WSPCTL_SUPERVISOR=/usr/local/libexec/wspctl/wsp-systemd" in environment
    assert "WSPCTL_CPU_MAX_US=50000" in environment
    assert "WSPCTL_CPU_PERIOD_US=100000" in environment
    assert "WSPCTL_MEMORY_HIGH=536870912" in environment
    assert "WSPCTL_MEMORY_SWAP_MAX=0" in environment
    assert "WSPCTL_IO_WEIGHT=100" in environment
    assert "must be a mount with the `ro` VFS option" in environment
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
    assert (
        'set(WSPCTL_HOST_BROKER_EXECUTABLE "${CMAKE_INSTALL_FULL_BINDIR}/wspctld")'
        in deployment_cmake
    )
    assert (
        'DESTINATION "${CMAKE_INSTALL_DATADIR}/fogmoe-wspctl/systemd"'
        in deployment_cmake
    )
    assert "local developer 纳入 trusted control plane" in deployment_guide


def _run_contract_tests() -> None:
    """@brief 以 CTest 直接执行静态契约测试 / Execute static contract tests directly under CTest.

    @return None / None.
    """

    test_bot_image_builds_native_client_but_excludes_host_broker_programs()
    test_compose_exposes_only_a_nonprivileged_bot_client_to_the_socket()
    test_host_unit_requires_exact_socket_uid_and_readonly_generation_mount()


if __name__ == "__main__":
    _run_contract_tests()
