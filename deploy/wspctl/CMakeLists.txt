# @brief wspctl 宿主部署资产生成 / Generate wspctl host deployment assets.
#
# 这里的路径是 host control plane，而非 Bot 配置；CMake 在生成时将唯一的工作根固定进
# systemd unit 的 namespace/hardening 字段。/
# These paths are host control plane rather than Bot configuration; CMake fixes the sole
# work root into the systemd unit's namespace and hardening fields at generation time.

#: @brief 是否显式允许不安全的 checkout 内开发根 / Whether to explicitly allow an unsafe checkout-local development root.
#:
#: user-writable checkout 的祖先目录可在 root daemon 路径解析期间被替换，因此它绝不等价于
# production-safe root-owned tree。开启此项只表示本机开发者被纳入 trusted control plane。/
# A user-writable checkout ancestor may be replaced while a root daemon resolves paths, so it
# is never equivalent to a production-safe root-owned tree. Enabling this option means only
# that the local developer joins the trusted control plane.
option(
    WSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT
    "Pass the explicit local-development opt-in for a checkout-local wspctl root"
    OFF
)

#: @brief wspctl 宿主运行时工作根 / wspctl host runtime work root.
#:
#: production-safe 构建必须显式给出一个祖先均为 root-owned 且不可写的绝对路径。只有
# local-development opt-in 开启时，才将源码 checkout 的 ``.wspctl`` 用作默认值。/
# A production-safe build must explicitly provide an absolute path whose ancestors are all
# root-owned and non-writable. Only when the local-development opt-in is enabled does the
# source checkout's ``.wspctl`` become the default.
set(
    WSPCTL_HOST_WORKDIR
    ""
    CACHE PATH
    "Absolute host work root for wspctld; mandatory unless insecure local development is explicitly enabled"
)
if(WSPCTL_HOST_WORKDIR STREQUAL "")
    if(WSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT)
        set(
            WSPCTL_HOST_WORKDIR
            "${CMAKE_SOURCE_DIR}/.wspctl"
            CACHE PATH
            "Absolute host work root for wspctld; development-only checkout default"
            FORCE
        )
    else()
        message(
            FATAL_ERROR
            "WSPCTL_HOST_WORKDIR is mandatory for host installation. Set an absolute root-owned production path, or explicitly set WSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON for a trusted local checkout."
        )
    endif()
endif()

if(NOT IS_ABSOLUTE "${WSPCTL_HOST_WORKDIR}")
    message(FATAL_ERROR "WSPCTL_HOST_WORKDIR must be an absolute path")
endif()
if(WSPCTL_HOST_WORKDIR MATCHES "[ \t\r\n]")
    message(FATAL_ERROR "WSPCTL_HOST_WORKDIR must not contain whitespace")
endif()

#: @brief 规范化后的 host 工作根 / Normalized host work root.
get_filename_component(WSPCTL_HOST_WORKDIR "${WSPCTL_HOST_WORKDIR}" ABSOLUTE)
#: @brief host socket 根目录 / Host socket-root directory.
set(WSPCTL_HOST_SOCKET_ROOT_DIRECTORY "${WSPCTL_HOST_WORKDIR}/run")
#: @brief Bot 专属 host socket 目录 / Bot-exclusive host socket directory.
#:
#: 此目录是唯一允许 bind mount 到 Bot container 的 host object view。/
#: This is the sole host-object view permitted to be bind-mounted into the Bot container.
set(WSPCTL_HOST_SOCKET_DIRECTORY "${WSPCTL_HOST_SOCKET_ROOT_DIRECTORY}/bot")
#: @brief host broker socket 路径 / Host broker socket path.
set(WSPCTL_HOST_SOCKET_PATH "${WSPCTL_HOST_SOCKET_DIRECTORY}/wspctld.sock")
#: @brief operator 专属 host socket 目录 / Operator-exclusive host socket directory.
#:
#: 它与 Bot view 处于不同目录，因此即使 container 获得 Bot socket directory 的只读 mount，也
#: 无法枚举或引用 operator endpoint。/ It is a different directory from the Bot view, so even a
#: read-only mount of the Bot socket directory cannot enumerate or reference the operator endpoint.
set(WSPCTL_HOST_OPERATOR_SOCKET_DIRECTORY "${WSPCTL_HOST_SOCKET_ROOT_DIRECTORY}/operator")
#: @brief host operator broker socket 路径 / Host operator broker socket path.
#:
#: 此路径绝不被 Bot 配置或容器 bind mount 引用；它是 root/operator 专属的独立 control-plane
#: endpoint。/ This path is never referenced by Bot configuration or a container bind mount; it is
#: an independent control-plane endpoint exclusive to root/the operator.
set(WSPCTL_HOST_OPERATOR_SOCKET_PATH "${WSPCTL_HOST_OPERATOR_SOCKET_DIRECTORY}/wspctld.sock")
#: @brief 安装后的 operator CLI 默认 endpoint / Default endpoint compiled into the installed operator CLI.
#:
#: 这是 configure-time 固定的 host control-plane path；CLI 可以以 `--socket` 显式覆盖，但绝不
#: 从 cwd、Bot 配置或环境变量猜测 endpoint。/ This is a configure-time fixed host-control-plane
#: path. The CLI may explicitly override it with `--socket`, but never guesses an endpoint from a
#: cwd, Bot configuration, or environment variable.
target_compile_definitions(
    wspctl
    PRIVATE
        WSPCTL_DEFAULT_OPERATOR_SOCKET="${WSPCTL_HOST_OPERATOR_SOCKET_PATH}"
)
#: @brief root-owned host broker 环境文件路径 / Root-owned host broker environment-file path.
set(WSPCTL_HOST_ENVIRONMENT_PATH "${WSPCTL_HOST_WORKDIR}/wspctld.env")
#: @brief 持久 Overlay/journal 状态根 / Persistent Overlay/journal state root.
set(WSPCTL_HOST_STATE_ROOT "${WSPCTL_HOST_WORKDIR}/state")
#: @brief 只读 OCI image 发布根 / Read-only OCI image publication root.
set(WSPCTL_HOST_IMAGES_ROOT "${WSPCTL_HOST_WORKDIR}/images")
#: @brief 专用 LXCFS 的 host runtime directory / Host runtime directory for the dedicated LXCFS instance.
set(WSPCTL_HOST_LXCFS_RUNTIME_DIRECTORY "/run/fogmoe-wspctl-lxcfs")
#: @brief 专用 LXCFS mount root / Dedicated LXCFS mount root.
set(WSPCTL_HOST_LXCFS_ROOT "${WSPCTL_HOST_LXCFS_RUNTIME_DIRECTORY}/root")
#: @brief 专用 LXCFS pidfile / Dedicated LXCFS pidfile.
set(WSPCTL_HOST_LXCFS_PID_FILE "${WSPCTL_HOST_LXCFS_RUNTIME_DIRECTORY}/lxcfs.pid")
#: @brief native broker 的本地开发显式授权参数 / Explicit local-development authorization argument for the native broker.
set(WSPCTL_INSECURE_DEVELOPMENT_ROOT_ARGUMENT "")
if(WSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT)
    set(WSPCTL_INSECURE_DEVELOPMENT_ROOT_ARGUMENT "--allow-insecure-dev-root")
endif()

#: @brief GNU 安装目录定义 / GNU installation-directory definitions.
#:
#: 必须在配置 service unit 前加载，以便 ``CMAKE_INSTALL_FULL_BINDIR`` 已经按 configure
#: 阶段的 ``CMAKE_INSTALL_PREFIX`` 展开。/ This must be loaded before configuring the
#: service unit so ``CMAKE_INSTALL_FULL_BINDIR`` has expanded according to the
#: configure-time ``CMAKE_INSTALL_PREFIX``.
include(GNUInstallDirs)
#: @brief 已安装 host broker 的绝对路径 / Absolute path of the installed host broker.
#:
#: systemd 的 ``ExecStart=`` 不经过 shell，也不能依赖 ``PATH``；将此值由 CMake 的安装
#: prefix 单一定义，防止 unit 与安装的 binary 漂移。/ systemd's ``ExecStart=`` does not
#: pass through a shell and must not rely on ``PATH``. Defining this from CMake's sole install
#: prefix prevents the unit from drifting away from the installed binary.
set(WSPCTL_HOST_BROKER_EXECUTABLE "${CMAKE_INSTALL_FULL_BINDIR}/wspctld")

#: @brief CMake 生成的 systemd 资产目录 / CMake-generated systemd asset directory.
set(WSPCTL_SYSTEMD_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/systemd")
file(MAKE_DIRECTORY "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}")
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/systemd/wspctld.service.in"
    "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.service"
    @ONLY
)
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/systemd/wspctl-lxcfs.service.in"
    "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctl-lxcfs.service"
    @ONLY
)
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/systemd/wspctld.env.example.in"
    "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.env.example"
    @ONLY
)

install(
    FILES
        "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.service"
        "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctl-lxcfs.service"
        "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.env.example"
    DESTINATION "${CMAKE_INSTALL_DATADIR}/fogmoe-wspctl/systemd"
    COMPONENT WspctlHost
)
