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
#: @brief host socket 目录 / Host socket directory.
set(WSPCTL_HOST_SOCKET_DIRECTORY "${WSPCTL_HOST_WORKDIR}/run")
#: @brief host broker socket 路径 / Host broker socket path.
set(WSPCTL_HOST_SOCKET_PATH "${WSPCTL_HOST_SOCKET_DIRECTORY}/wspctld.sock")
#: @brief root-owned host broker 环境文件路径 / Root-owned host broker environment-file path.
set(WSPCTL_HOST_ENVIRONMENT_PATH "${WSPCTL_HOST_WORKDIR}/wspctld.env")
#: @brief 持久 Overlay/journal 状态根 / Persistent Overlay/journal state root.
set(WSPCTL_HOST_STATE_ROOT "${WSPCTL_HOST_WORKDIR}/state")
#: @brief 只读 generation 发布根 / Read-only generation publication root.
set(WSPCTL_HOST_IMAGES_ROOT "${WSPCTL_HOST_WORKDIR}/images")
#: @brief native broker 的本地开发显式授权参数 / Explicit local-development authorization argument for the native broker.
set(WSPCTL_INSECURE_DEVELOPMENT_ROOT_ARGUMENT "")
if(WSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT)
    set(WSPCTL_INSECURE_DEVELOPMENT_ROOT_ARGUMENT "--allow-insecure-dev-root")
endif()

#: @brief CMake 生成的 systemd 资产目录 / CMake-generated systemd asset directory.
set(WSPCTL_SYSTEMD_OUTPUT_DIRECTORY "${CMAKE_CURRENT_BINARY_DIR}/systemd")
file(MAKE_DIRECTORY "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}")
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/systemd/wspctld.service.in"
    "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.service"
    @ONLY
)
configure_file(
    "${CMAKE_CURRENT_SOURCE_DIR}/systemd/wspctld.env.example.in"
    "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.env.example"
    @ONLY
)

include(GNUInstallDirs)
install(
    FILES
        "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.service"
        "${WSPCTL_SYSTEMD_OUTPUT_DIRECTORY}/wspctld.env.example"
    DESTINATION "${CMAKE_INSTALL_DATADIR}/fogmoe-wspctl/systemd"
)
