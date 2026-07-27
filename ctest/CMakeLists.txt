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

file(GLOB WSPCTL_PYTHON_TESTS CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/python/test_*.py")
foreach(WSPCTL_PYTHON_TEST IN LISTS WSPCTL_PYTHON_TESTS)
    get_filename_component(WSPCTL_PYTHON_TEST_NAME "${WSPCTL_PYTHON_TEST}" NAME_WE)
    add_test(
        NAME "wspctl.python.${WSPCTL_PYTHON_TEST_NAME}"
        COMMAND "${Python_EXECUTABLE}" "${WSPCTL_PYTHON_TEST}"
    )
    set(WSPCTL_PYTHONPATH "${CMAKE_SOURCE_DIR}/src")
    if(WSPCTL_PYTHON_TEST_NAME STREQUAL "test_native_binding")
        # This is intentionally the built package first: a source-only import would not prove
        # that pybind11 exports the documented exception ABI.
        set(WSPCTL_PYTHONPATH "${CMAKE_BINARY_DIR}/python:${CMAKE_SOURCE_DIR}/src")
    endif()
    set_tests_properties(
        "wspctl.python.${WSPCTL_PYTHON_TEST_NAME}"
        PROPERTIES
            WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
            ENVIRONMENT "PYTHONPATH=${WSPCTL_PYTHONPATH}"
    )
endforeach()
