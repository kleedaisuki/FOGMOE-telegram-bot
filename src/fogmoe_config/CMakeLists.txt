# 严格 JSONC 的独立静态库与 Python 适配层 / Independent strict-JSONC static library and Python adapter.

option(
    FOGMOE_CONFIG_BUILD_PYTHON_BINDINGS
    "构建 fogmoe_config 的 pybind11 适配层 / Build the fogmoe_config pybind11 adapter"
    ON
)

add_library(fogmoe_config STATIC
    src/jsonc.cpp
)
set_target_properties(fogmoe_config PROPERTIES POSITION_INDEPENDENT_CODE ON)
target_include_directories(fogmoe_config
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:include>
)
target_compile_features(fogmoe_config PUBLIC cxx_std_23)
target_compile_options(fogmoe_config PRIVATE
    -Wall
    -Wextra
    -Wpedantic
    -Wconversion
    -Wshadow
    -Wformat=2
    -Werror=return-type
)

install(TARGETS fogmoe_config
    ARCHIVE DESTINATION lib
)
install(DIRECTORY include/ DESTINATION include)

if(BUILD_TESTING)
    add_executable(fogmoe_config_tests
        ../../ctest/cpp/fogmoe_config_tests.cpp
    )
    target_link_libraries(fogmoe_config_tests PRIVATE fogmoe_config)
    target_compile_options(fogmoe_config_tests PRIVATE
        -Wall
        -Wextra
        -Wpedantic
        -Wconversion
        -Wshadow
        -Wformat=2
        -Werror=return-type
    )
    add_test(NAME fogmoe_config COMMAND fogmoe_config_tests)
endif()

if(FOGMOE_CONFIG_BUILD_PYTHON_BINDINGS)
    set(FOGMOE_CONFIG_PROJECT_VENV_PYTHON "${CMAKE_SOURCE_DIR}/.venv/bin/python")
    if((NOT DEFINED Python_EXECUTABLE OR Python_EXECUTABLE STREQUAL "") AND
       EXISTS "${FOGMOE_CONFIG_PROJECT_VENV_PYTHON}")
        set(
            Python_EXECUTABLE
            "${FOGMOE_CONFIG_PROJECT_VENV_PYTHON}"
            CACHE FILEPATH
            "Python interpreter selected from the project .venv for fogmoe_config"
        )
        message(STATUS "fogmoe_config: using project virtual-environment Python: ${Python_EXECUTABLE}")
    endif()

    find_package(Python 3.14 REQUIRED COMPONENTS Interpreter Development.Module)
    find_package(pybind11 CONFIG QUIET)
    if(NOT pybind11_FOUND)
        execute_process(
            COMMAND "${Python_EXECUTABLE}" -m pybind11 --cmakedir
            OUTPUT_VARIABLE FOGMOE_CONFIG_PYBIND11_CMAKE_DIR
            OUTPUT_STRIP_TRAILING_WHITESPACE
            RESULT_VARIABLE FOGMOE_CONFIG_PYBIND11_RESULT
        )
        if(NOT FOGMOE_CONFIG_PYBIND11_RESULT EQUAL 0 OR
           FOGMOE_CONFIG_PYBIND11_CMAKE_DIR STREQUAL "")
            message(FATAL_ERROR "pybind11 CMake package was not found for ${Python_EXECUTABLE}")
        endif()
        list(PREPEND CMAKE_PREFIX_PATH "${FOGMOE_CONFIG_PYBIND11_CMAKE_DIR}")
        find_package(pybind11 CONFIG REQUIRED)
    endif()

    pybind11_add_module(fogmoe_config_native MODULE src/python_bindings.cpp)
    target_link_libraries(fogmoe_config_native PRIVATE fogmoe_config)
    target_compile_options(fogmoe_config_native PRIVATE
        -Wall
        -Wextra
        -Wpedantic
        -Wconversion
        -Wshadow
        -Wformat=2
        -Werror=return-type
    )
    set_target_properties(fogmoe_config_native PROPERTIES
        OUTPUT_NAME _native
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/python/fogmoe_config"
    )

    set(FOGMOE_CONFIG_PYTHON_PACKAGE_DIR "${CMAKE_BINARY_DIR}/python/fogmoe_config")
    file(MAKE_DIRECTORY "${FOGMOE_CONFIG_PYTHON_PACKAGE_DIR}")
    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/__init__.py"
        "${FOGMOE_CONFIG_PYTHON_PACKAGE_DIR}/__init__.py"
        COPYONLY
    )
    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/py.typed"
        "${FOGMOE_CONFIG_PYTHON_PACKAGE_DIR}/py.typed"
        COPYONLY
    )
    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/_native.pyi"
        "${FOGMOE_CONFIG_PYTHON_PACKAGE_DIR}/_native.pyi"
        COPYONLY
    )
    configure_file(
        "${CMAKE_CURRENT_SOURCE_DIR}/jsonc.py"
        "${FOGMOE_CONFIG_PYTHON_PACKAGE_DIR}/jsonc.py"
        COPYONLY
    )

    install(TARGETS fogmoe_config_native LIBRARY DESTINATION fogmoe_config)
endif()
