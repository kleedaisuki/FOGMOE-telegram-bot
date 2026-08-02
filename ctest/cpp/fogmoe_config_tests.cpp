/**
 * @file fogmoe_config_tests.cpp
 * @brief 严格 JSONC 静态库测试 / Tests for the strict JSONC static library.
 */

#include "fogmoe_config/jsonc.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <string>
#include <string_view>
#include <unistd.h>

namespace {

/**
 * @brief 测试失败计数 / Test failure counter.
 */
unsigned int g_failures = 0U;

/**
 * @brief 断言条件 / Assert a condition.
 * @param condition 条件 / Condition.
 * @param message 失败说明 / Failure message.
 */
void expect(const bool condition, const std::string_view message) {
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL: " << message << '\n';
    }
}

/**
 * @brief 查找对象成员 / Find an object member.
 * @param object JSON 对象 / JSON object.
 * @param key ASCII 键 / ASCII key.
 * @return 成员值或空值 / Member value or no value.
 */
[[nodiscard]] const fogmoe::config::Value* find_member(
    const fogmoe::config::Value::Object& object, const std::u32string_view key) {
    for (const auto& [member_key, value] : object) {
        if (member_key == key) {
            return &value;
        }
    }
    return nullptr;
}

/**
 * @brief 写入测试文件 / Write a test file.
 * @param path 文件路径 / File path.
 * @param source 文件内容 / File content.
 */
void write_file(const std::filesystem::path& path, const std::string_view source) {
    std::ofstream output(path, std::ios::binary | std::ios::trunc);
    output << source;
    expect(output.good(), "write JSONC test file");
}

/**
 * @brief 验证严格 JSONC 基本语义 / Verify core strict-JSONC semantics.
 */
void test_strict_jsonc_semantics() {
    const fogmoe::config::Value value = fogmoe::config::parse_jsonc(
        R"({
            "url": "https://example.test//models/*stable*/",
            /* comment */ "enabled": true,
            "items": [1, 2.5, null]
        })");
    const auto& object = std::get<fogmoe::config::Value::Object>(value.data());
    expect(find_member(object, U"url") != nullptr, "preserve strings containing comment markers");
    expect(find_member(object, U"enabled") != nullptr, "parse line and block comments");
    expect(find_member(object, U"items") != nullptr, "parse arrays and numbers");

    try {
        (void)fogmoe::config::parse_jsonc(R"({"mode": 1, "mode": 2})");
        expect(false, "reject duplicate object keys");
    } catch (const fogmoe::config::Error& error) {
        expect(error.code() == fogmoe::config::ErrorCode::syntax,
               "duplicate key is a syntax error");
        expect(std::string{error.what()}.find("duplicate object key 'mode'") != std::string::npos,
               "duplicate key reports the key name");
    }

    try {
        (void)fogmoe::config::parse_jsonc(R"({"value": 1e999})");
        expect(false, "reject non-finite JSON numbers");
    } catch (const fogmoe::config::Error& error) {
        expect(std::string{error.what()}.find("non-finite JSON numeric value") != std::string::npos,
               "non-finite number reports a stable error");
    }

    try {
        (void)fogmoe::config::parse_jsonc("[]");
        expect(false, "reject non-object root values");
    } catch (const fogmoe::config::Error& error) {
        expect(std::string{error.what()}.find("top-level JSONC value must be an object") !=
                   std::string::npos,
               "non-object root reports the public contract");
    }
}

/**
 * @brief 验证递归 JSONC include / Verify recursive JSONC includes.
 */
void test_file_includes() {
    const std::filesystem::path root = std::filesystem::temp_directory_path() /
                                       ("fogmoe-jsonc-test-" + std::to_string(getpid()));
    std::error_code cleanup_error;
    std::filesystem::remove_all(root, cleanup_error);
    std::filesystem::create_directories(root / "nested", cleanup_error);
    expect(!cleanup_error, "create JSONC include test directory");
    if (cleanup_error) {
        return;
    }

    write_file(root / "parts.jsonc", R"({"name": "雾萌" // JSONC child
})");
    write_file(root / "items.json", R"([1, 2, 3])");
    write_file(root / "nested" / "leaf.jsonc", "true");
    write_file(root / "root.jsonc",
               R"({
                   "object": "$<parts.jsonc>",
                   "array": "$<items.json>",
                   "scalar": "$<nested/leaf.jsonc>",
                   "literal": "prefix $<parts.jsonc>"
               })");

    try {
        const fogmoe::config::Value value = fogmoe::config::load_jsonc(root / "root.jsonc");
        const auto& object = std::get<fogmoe::config::Value::Object>(value.data());
        const fogmoe::config::Value* included_object = find_member(object, U"object");
        const fogmoe::config::Value* included_array = find_member(object, U"array");
        const fogmoe::config::Value* included_scalar = find_member(object, U"scalar");
        const fogmoe::config::Value* literal = find_member(object, U"literal");
        expect(included_object != nullptr &&
                   std::holds_alternative<fogmoe::config::Value::Object>(included_object->data()),
               "inline an object fragment");
        expect(included_array != nullptr &&
                   std::holds_alternative<fogmoe::config::Value::Array>(included_array->data()),
               "inline an array fragment");
        expect(included_scalar != nullptr &&
                   std::holds_alternative<bool>(included_scalar->data()) &&
                   std::get<bool>(included_scalar->data()),
               "inline a scalar fragment");
        expect(literal != nullptr && std::holds_alternative<fogmoe::config::String>(literal->data()),
               "only replace an exact include string");
    } catch (const fogmoe::config::Error& error) {
        expect(false, std::string{"recursive include unexpectedly failed: "} + error.what());
    }

    write_file(root / "cycle-a.jsonc", R"({"next": "$<cycle-b.jsonc>"})");
    write_file(root / "cycle-b.jsonc", R"({"next": "$<cycle-a.jsonc>"})");
    try {
        (void)fogmoe::config::load_jsonc(root / "cycle-a.jsonc");
        expect(false, "reject include cycles");
    } catch (const fogmoe::config::Error& error) {
        expect(error.code() == fogmoe::config::ErrorCode::include_cycle,
               "include cycle has a dedicated error code");
    }

    try {
        (void)fogmoe::config::parse_jsonc(R"({"child": "$<child.json>"})");
        expect(false, "reject includes without a source path");
    } catch (const fogmoe::config::Error& error) {
        expect(error.code() == fogmoe::config::ErrorCode::include,
               "pathless include has a dedicated error code");
    }

    std::filesystem::remove_all(root, cleanup_error);
    expect(!cleanup_error, "remove JSONC include test directory");
}

}  // namespace

/**
 * @brief 运行 JSONC 静态库测试 / Run JSONC static-library tests.
 * @return 成功返回 0，失败返回 1 / Return 0 on success and 1 on failure.
 */
int main() {
    test_strict_jsonc_semantics();
    test_file_includes();
    return g_failures == 0U ? 0 : 1;
}
