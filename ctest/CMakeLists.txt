find_package(Python 3.14 REQUIRED COMPONENTS Interpreter)

add_executable(wspctl_core_tests cpp/wspctl_core_tests.cpp)
target_link_libraries(wspctl_core_tests PRIVATE wspctl_infrastructure)
target_compile_options(wspctl_core_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)

add_test(NAME wspctl.core COMMAND wspctl_core_tests)

# 此测试会在子进程内永久降权并加载 seccomp，不能在普通开发 CTest 中假装通过。
# It irreversibly drops privilege and loads seccomp in a child, so ordinary development CTest
# explicitly reports a skip while privileged CI can require a real execution.
add_executable(wspctl_privileged_security_tests cpp/wspctl_privileged_security_tests.cpp)
target_link_libraries(wspctl_privileged_security_tests PRIVATE wspctl_infrastructure)
target_compile_options(wspctl_privileged_security_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
add_test(NAME wspctl.privileged_security COMMAND wspctl_privileged_security_tests)
set_tests_properties(
    wspctl.privileged_security
    PROPERTIES
        SKIP_RETURN_CODE 77
        LABELS "privileged;security"
)

if(WSPCTL_BUILD_HOST_RUNTIME)
    # 这是唯一真正启动 broker、namespace PID 1、OverlayFS 与 XFS quota 的验收路径。它从不把
    # 普通开发机的 skip 当作通过；operator/CI 必须显式提供 disposable 的特权环境。
    # This is the sole acceptance path that actually starts the broker, namespace PID 1, OverlayFS,
    # and XFS quota. A normal-development skip is never treated as success; operator/CI must provide
    # an explicit disposable privileged environment.
    add_executable(wspctl_privileged_e2e_tests cpp/wspctl_privileged_e2e_tests.cpp)
    target_link_libraries(wspctl_privileged_e2e_tests PRIVATE wspctl_presentation)
    target_compile_options(wspctl_privileged_e2e_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
    add_dependencies(wspctl_privileged_e2e_tests wspctld)
    add_test(
        NAME wspctl.privileged_e2e
        COMMAND wspctl_privileged_e2e_tests $<TARGET_FILE:wspctld>
    )
    set_tests_properties(
        wspctl.privileged_e2e
        PROPERTIES
            SKIP_RETURN_CODE 77
            LABELS "privileged;e2e;namespace;overlayfs;cgroup;xfs"
    )
endif()

# XFS project quota has no meaningful unprivileged mock: its acceptance criterion is the
# kernel returning EDQUOT for both inode and block hard limits on a disposable dedicated XFS mount.
add_executable(wspctl_xfs_project_quota_tests cpp/wspctl_xfs_project_quota_tests.cpp)
target_link_libraries(wspctl_xfs_project_quota_tests PRIVATE wspctl_infrastructure)
target_compile_options(wspctl_xfs_project_quota_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
add_test(NAME wspctl.xfs_project_quota COMMAND wspctl_xfs_project_quota_tests)
set_tests_properties(
    wspctl.xfs_project_quota
    PROPERTIES
        SKIP_RETURN_CODE 77
        LABELS "privileged;xfs;quota"
)

add_executable(wspctl_domain_tests cpp/wspctl_domain_tests.cpp)
target_link_libraries(wspctl_domain_tests PRIVATE wspctl_domain)
target_compile_options(wspctl_domain_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
add_test(NAME wspctl.domain COMMAND wspctl_domain_tests)

add_executable(wspctl_application_tests cpp/wspctl_application_tests.cpp)
target_link_libraries(wspctl_application_tests PRIVATE wspctl_application)
target_compile_options(wspctl_application_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
add_test(NAME wspctl.application COMMAND wspctl_application_tests)

add_executable(wspctl_presentation_tests cpp/wspctl_presentation_tests.cpp)
target_link_libraries(wspctl_presentation_tests PRIVATE wspctl_presentation)
target_compile_options(wspctl_presentation_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
add_test(NAME wspctl.presentation COMMAND wspctl_presentation_tests)

add_executable(wspctl_operator_tests cpp/wspctl_operator_tests.cpp)
target_link_libraries(wspctl_operator_tests PRIVATE wspctl_presentation)
target_compile_options(wspctl_operator_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
add_test(NAME wspctl.operator COMMAND wspctl_operator_tests)

if(WSPCTL_BUILD_HOST_RUNTIME)
    add_executable(wspctl_operator_cli_tests cpp/wspctl_operator_cli_tests.cpp)
    target_compile_options(wspctl_operator_cli_tests PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow -Wformat=2 -Werror=return-type)
    add_dependencies(wspctl_operator_cli_tests wspctl)
    add_test(NAME wspctl.operator_cli COMMAND wspctl_operator_cli_tests $<TARGET_FILE:wspctl>)
endif()

file(GLOB WSPCTL_PYTHON_TESTS CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/python/test_*.py")
foreach(WSPCTL_PYTHON_TEST IN LISTS WSPCTL_PYTHON_TESTS)
    get_filename_component(WSPCTL_PYTHON_TEST_NAME "${WSPCTL_PYTHON_TEST}" NAME_WE)
    if(FOGMOE_CONFIG_BUILD AND FOGMOE_CONFIG_BUILD_PYTHON_BINDINGS)
        set(WSPCTL_PYTHONPATH "${CMAKE_BINARY_DIR}/python:${CMAKE_SOURCE_DIR}/src")
    else()
        set(WSPCTL_PYTHONPATH "${CMAKE_SOURCE_DIR}/src")
    endif()
    set(WSPCTL_PYTHON_COMMAND "${Python_EXECUTABLE}" "${WSPCTL_PYTHON_TEST}")
    if(WSPCTL_PYTHON_TEST_NAME STREQUAL "test_native_binding")
        # Run without site initialization: a scikit-build editable finder can otherwise intercept
        # ``wspctl`` ahead of PYTHONPATH and silently load an older installed extension.  The ABI
        # contract needs to exercise exactly the freshly built package and no source/site fallback.
        set(WSPCTL_PYTHONPATH "${CMAKE_BINARY_DIR}/python")
        set(WSPCTL_PYTHON_COMMAND "${Python_EXECUTABLE}" "-S" "${WSPCTL_PYTHON_TEST}")
    elseif(WSPCTL_PYTHON_TEST_NAME STREQUAL "test_wspctl_oci_publisher")
        # 此 suite 使用 pytest fixtures 驱动 fake OCI toolchain；必须通过 pytest collection
        # 执行全部 publication 状态机测试，不能依赖手写 __main__ 列表。/
        # This suite uses pytest fixtures to drive a fake OCI toolchain. Run its complete
        # publication state machine through pytest collection, never a hand-maintained __main__ list.
        set(
            WSPCTL_PYTHON_COMMAND
            "${Python_EXECUTABLE}" "-m" "pytest" "-q" "${WSPCTL_PYTHON_TEST}"
        )
    endif()
    add_test(
        NAME "wspctl.python.${WSPCTL_PYTHON_TEST_NAME}"
        COMMAND ${WSPCTL_PYTHON_COMMAND}
    )
    set_tests_properties(
        "wspctl.python.${WSPCTL_PYTHON_TEST_NAME}"
        PROPERTIES
            WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
            ENVIRONMENT "PYTHONPATH=${WSPCTL_PYTHONPATH}"
    )
    if(WSPCTL_PYTHON_TEST_NAME STREQUAL "test_workspace_attachment_receipt_postgres")
        # 真实 migration/trigger 验证只在显式提供私有 PostgreSQL 16 + pgvector 环境时执行；
        # 普通开发 CTest 的 77 是 skip，而不是把静态检查伪装成运行时通过。
        set_tests_properties(
            "wspctl.python.${WSPCTL_PYTHON_TEST_NAME}"
            PROPERTIES
                SKIP_RETURN_CODE 77
                LABELS "integration;postgres;workspace"
        )
    endif()
endforeach()
