/**
 * @file python_bindings.cpp
 * @brief JSONC 静态库的 Python 薄绑定 / Thin Python binding for the JSONC static library.
 */

#include "fogmoe_config/jsonc.hpp"

#include <pybind11/pybind11.h>

#include <Python.h>

#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <vector>

namespace py = pybind11;
namespace fogmoe::config {
namespace {

/**
 * @brief 将 UTF-32 字符串转换为 Python Unicode / Convert a UTF-32 string to Python Unicode.
 * @param value C++ Unicode 字符串 / C++ Unicode string.
 * @return Python 字符串对象 / Python string object.
 */
[[nodiscard]] py::str python_string(const String& value) {
    std::vector<Py_UCS4> code_points;
    code_points.reserve(value.size());
    for (const char32_t code_point : value) {
        code_points.push_back(static_cast<Py_UCS4>(code_point));
    }
    PyObject* result = PyUnicode_FromKindAndData(
        PyUnicode_4BYTE_KIND,
        code_points.empty() ? nullptr : static_cast<const void*>(code_points.data()),
        static_cast<Py_ssize_t>(code_points.size()));
    if (result == nullptr) {
        throw py::error_already_set();
    }
    return py::reinterpret_steal<py::str>(result);
}

/**
 * @brief 将 C++ 数字转换为 Python 数字 / Convert a C++ number to a Python number.
 * @param number C++ JSON 数字 / C++ JSON number.
 * @return Python 整数或浮点数 / Python integer or floating-point number.
 */
[[nodiscard]] py::object python_number(const Number& number) {
    if (number.kind == Number::Kind::integer) {
        char* end = nullptr;
        PyObject* result = PyLong_FromString(number.lexeme.c_str(), &end, 10);
        if (result == nullptr) {
            throw py::error_already_set();
        }
        if (end == nullptr || *end != '\0') {
            Py_DECREF(result);
            throw std::runtime_error("internal JSON integer conversion failed");
        }
        return py::reinterpret_steal<py::object>(result);
    }

    char* end = nullptr;
    const double result = std::strtod(number.lexeme.c_str(), &end);
    if (end == nullptr || *end != '\0' || !std::isfinite(result)) {
        throw std::runtime_error("internal JSON floating-point conversion failed");
    }
    return py::float_(result);
}

/**
 * @brief 递归转换 JSON 值为 Python 值 / Recursively convert a JSON value to a Python value.
 * @param value C++ JSON 值 / C++ JSON value.
 * @return Python 原生值 / Native Python value.
 */
[[nodiscard]] py::object python_value(const Value& value) {
    const Value::Data& data = value.data();
    if (std::holds_alternative<std::nullptr_t>(data)) {
        return py::none();
    }
    if (const auto* boolean = std::get_if<bool>(&data); boolean != nullptr) {
        return py::bool_(*boolean);
    }
    if (const auto* number = std::get_if<Number>(&data); number != nullptr) {
        return python_number(*number);
    }
    if (const auto* string = std::get_if<String>(&data); string != nullptr) {
        return python_string(*string);
    }
    if (const auto* array = std::get_if<Value::Array>(&data); array != nullptr) {
        py::list result;
        for (const Value& item : *array) {
            result.append(python_value(item));
        }
        return result;
    }

    const auto* object = std::get_if<Value::Object>(&data);
    if (object == nullptr) {
        throw std::runtime_error("internal JSON value conversion failed");
    }
    py::dict result;
    for (const auto& [key, item] : *object) {
        result[python_string(key)] = python_value(item);
    }
    return result;
}

} // namespace
} // namespace fogmoe::config

/**
 * @brief 注册 fogmoe_config 原生模块 / Register the fogmoe_config native module.
 * @param module Python 模块对象 / Python module object.
 */
PYBIND11_MODULE(_native, module) {
    module.doc() = "FOGMOE strict JSONC native parser";
    py::register_exception<fogmoe::config::Error>(module, "NativeJsoncError");

    module.def(
        "parse_jsonc",
        [](const py::str& source, const py::object& source_path) -> py::object {
            const std::string source_utf8 = source.cast<std::string>();
            fogmoe::config::Value value;
            if (source_path.is_none()) {
                value = fogmoe::config::parse_jsonc(source_utf8);
            } else {
                value = fogmoe::config::parse_jsonc(
                    source_utf8, std::filesystem::path{source_path.cast<std::string>()});
            }
            return fogmoe::config::python_value(value);
        },
        py::arg("source"), py::arg("source_path") = py::none());

    module.def(
        "load_jsonc",
        [](const py::str& path) -> py::object {
            const fogmoe::config::Value value =
                fogmoe::config::load_jsonc(std::filesystem::path{path.cast<std::string>()});
            return fogmoe::config::python_value(value);
        },
        py::arg("path"));
}
