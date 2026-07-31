cmake_minimum_required(VERSION 3.28)

project(fogmoe_wspctl VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(WSPCTL_BUILD_TESTING "构建 wspctl 的 CTest 测试 / Build wspctl CTest suites" ON)

include(CTest)

add_subdirectory(src/wspctl)

# Host deployment assets are deliberately absent from the Bot wheel and publisher-only
# configuration. They are generated only with the explicitly requested broker/operator runtime.
if(WSPCTL_BUILD_HOST_RUNTIME)
    add_subdirectory(deploy/wspctl)
endif()

if(BUILD_TESTING AND WSPCTL_BUILD_TESTING)
    add_subdirectory(ctest)
endif()
