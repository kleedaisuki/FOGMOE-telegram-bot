# 优先使用项目已固定依赖的 virtual environment（虚拟环境），避免 CMake 在开发机上
# 随机选中另一个满足版本号却没有 pybind11 的 Python。wheel build 会显式传入其
# isolation interpreter；因此这个 fallback 只服务于源码树的本地/host 构建。
# Prefer the project virtual environment so a host CMake invocation does not arbitrarily
# discover a version-compatible Python without pybind11. Wheel builds explicitly provide
# their isolated interpreter, so this fallback only applies to source-tree local/host builds.
option(WSPCTL_BUILD_PYTHON_BINDINGS "构建 pybind11 client / Build the pybind11 client" ON)

if(WSPCTL_BUILD_PYTHON_BINDINGS)
    set(WSPCTL_PROJECT_VENV_PYTHON "${CMAKE_SOURCE_DIR}/.venv/bin/python")
    if((NOT DEFINED Python_EXECUTABLE OR Python_EXECUTABLE STREQUAL "") AND EXISTS "${WSPCTL_PROJECT_VENV_PYTHON}")
        set(
            Python_EXECUTABLE
            "${WSPCTL_PROJECT_VENV_PYTHON}"
            CACHE FILEPATH
            "Python interpreter selected from the project .venv for wspctl"
        )
        message(STATUS "wspctl: using project virtual-environment Python: ${Python_EXECUTABLE}")
    endif()
    find_package(Python 3.14 REQUIRED COMPONENTS Interpreter Development.Module)
    find_package(pybind11 CONFIG QUIET)
    if(NOT pybind11_FOUND)
        execute_process(
            COMMAND "${Python_EXECUTABLE}" -m pybind11 --cmakedir
            OUTPUT_VARIABLE WSPCTL_PYBIND11_CMAKE_DIR
            OUTPUT_STRIP_TRAILING_WHITESPACE
            RESULT_VARIABLE WSPCTL_PYBIND11_RESULT
        )
        if(NOT WSPCTL_PYBIND11_RESULT EQUAL 0 OR WSPCTL_PYBIND11_CMAKE_DIR STREQUAL "")
            message(FATAL_ERROR "pybind11 CMake package was not found for ${Python_EXECUTABLE}")
        endif()
        list(PREPEND CMAKE_PREFIX_PATH "${WSPCTL_PYBIND11_CMAKE_DIR}")
        find_package(pybind11 CONFIG REQUIRED)
    endif()
endif()
find_package(OpenSSL REQUIRED COMPONENTS Crypto)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(SECCOMP REQUIRED IMPORTED_TARGET libseccomp)
pkg_check_modules(LIBCAP REQUIRED IMPORTED_TARGET libcap)

option(WSPCTL_INSTALL_HOST_TOOLS "安装特权 host tools 与 systemd unit / Install privileged host tools and systemd unit" OFF)

# 为原生 target 应用严格编译告警 / Apply strict compiler warnings to a native target.
function(wspctl_enable_warnings target)
    target_compile_features(${target} PUBLIC cxx_std_23)
    target_compile_options(${target} PRIVATE
        -Wall
        -Wextra
        -Wpedantic
        -Wconversion
        -Wshadow
        -Wformat=2
        -Werror=return-type
    )
endfunction()

# Dependency direction is intentionally one way: domain -> application -> infrastructure -> presentation.
# The domain target is pure C++ and must never acquire host, Linux, filesystem, socket, or pybind headers.
add_library(wspctl_domain STATIC
    src/domain/operator_workspace.cpp
    src/domain/runtime.cpp
)
set_target_properties(wspctl_domain PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(wspctl_domain
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
wspctl_enable_warnings(wspctl_domain)

add_library(wspctl_application STATIC
    src/application/operator_workspace.cpp
    src/application/runtime_activation.cpp
    src/application/runtime_status.cpp
)
set_target_properties(wspctl_application PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(wspctl_application
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_link_libraries(wspctl_application
    PUBLIC
        wspctl_domain
)
wspctl_enable_warnings(wspctl_application)

# Image publication is an independent infrastructure capability.  Keeping it in its own target
# prevents the privileged publisher from linking broker, quota, journal, and sandbox machinery.
add_library(wspctl_image_infrastructure STATIC
    src/infrastructure/image.cpp
)
set_target_properties(wspctl_image_infrastructure PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(wspctl_image_infrastructure
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_link_libraries(wspctl_image_infrastructure
    PUBLIC
        OpenSSL::Crypto
)
wspctl_enable_warnings(wspctl_image_infrastructure)

add_library(wspctl_infrastructure STATIC
    src/infrastructure/broker.cpp
    src/infrastructure/journal.cpp
    src/infrastructure/operator_endpoint.cpp
    src/infrastructure/operator_protocol.cpp
    src/infrastructure/operator_workspace_reader.cpp
    src/infrastructure/payload_replay.cpp
    src/infrastructure/pidfd_control.cpp
    src/infrastructure/protocol.cpp
    src/infrastructure/runtime_gate.cpp
    src/infrastructure/sandbox.cpp
    src/infrastructure/supervisor.cpp
    src/infrastructure/xfs_project_quota.cpp
)
set_target_properties(wspctl_infrastructure PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(wspctl_infrastructure
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_link_libraries(wspctl_infrastructure
    PUBLIC
        wspctl_domain
        wspctl_image_infrastructure
    PRIVATE
        wspctl_application
    PUBLIC
        Threads::Threads
        PkgConfig::SECCOMP
        PkgConfig::LIBCAP
)
wspctl_enable_warnings(wspctl_infrastructure)

# Presentation owns process entry points and Python/Unix-facing adapters.  It only depends downwards.
add_library(wspctl_presentation STATIC
    src/presentation/operator_gateway.cpp
    src/presentation/unix_gateway.cpp
)
set_target_properties(wspctl_presentation PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(wspctl_presentation
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
)
target_link_libraries(wspctl_presentation
    PUBLIC
        wspctl_application
        wspctl_infrastructure
)
wspctl_enable_warnings(wspctl_presentation)

add_executable(wspctld src/presentation/broker/main.cpp)
target_link_libraries(wspctld PRIVATE wspctl_presentation)
wspctl_enable_warnings(wspctld)

add_executable(wsp-systemd src/presentation/systemd/main.cpp)
target_link_libraries(wsp-systemd PRIVATE wspctl_presentation)
wspctl_enable_warnings(wsp-systemd)

add_executable(wspctl-image src/presentation/image/main.cpp)
target_link_libraries(wspctl-image PRIVATE wspctl_image_infrastructure)
wspctl_enable_warnings(wspctl-image)

add_executable(wspctl src/presentation/operator/main.cpp)
target_link_libraries(wspctl PRIVATE wspctl_presentation)
wspctl_enable_warnings(wspctl)

if(WSPCTL_BUILD_PYTHON_BINDINGS)
    pybind11_add_module(_native MODULE src/presentation/python/bindings.cpp)
    target_link_libraries(_native PRIVATE wspctl_presentation)
    target_compile_options(_native PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)

    set(WSPCTL_PYTHON_PACKAGE_DIR "${CMAKE_BINARY_DIR}/python/wspctl")
    file(MAKE_DIRECTORY "${WSPCTL_PYTHON_PACKAGE_DIR}")
    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/__init__.py"
        "${WSPCTL_PYTHON_PACKAGE_DIR}/__init__.py"
        COPYONLY
    )
    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/_native.pyi"
        "${WSPCTL_PYTHON_PACKAGE_DIR}/_native.pyi"
        COPYONLY
    )
    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/py.typed"
        "${WSPCTL_PYTHON_PACKAGE_DIR}/py.typed"
        COPYONLY
    )
    set_target_properties(_native PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY "${WSPCTL_PYTHON_PACKAGE_DIR}"
    )

    install(TARGETS _native LIBRARY DESTINATION wspctl)
    install(FILES __init__.py _native.pyi py.typed DESTINATION wspctl)
endif()

if(WSPCTL_INSTALL_HOST_TOOLS)
    install(
        TARGETS wspctld wspctl
        RUNTIME DESTINATION bin
        COMPONENT WspctlHost
    )
    install(
        TARGETS wspctl-image
        RUNTIME DESTINATION bin
        COMPONENT WspctlPublisher
    )
endif()
