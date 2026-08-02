#pragma once

/**
 * @file jsonc.hpp
 * @brief 严格 JSONC 静态库公共接口 / Public interface for the strict JSONC static library.
 */

#include <cstddef>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

namespace fogmoe::config {

/**
 * @brief JSON 字符串的 Unicode 码点序列 / Unicode-code-point sequence for a JSON string.
 *
 * @note 使用 UTF-32 是为了保留 Python JSON 解码器允许的孤立代理项（lone surrogate），
 *       同时让静态库不依赖 Python 的字符串对象。/ UTF-32 preserves lone surrogates accepted
 *       by Python's JSON decoder while keeping the static library independent of Python objects.
 */
using String = std::u32string;

/**
 * @brief JSON 数字的词法表示 / Lexical representation of a JSON number.
 *
 * @note 整数保留原始十进制词法，以避免把 Python 任意精度整数错误截断为 64 位。/
 *       Integer lexemes are retained to avoid truncating Python's arbitrary-precision integers
 *       to 64 bits.
 */
struct Number final {
    /**
     * @brief 数字类别 / Number category.
     */
    enum class Kind {
        integer,
        real,
    };

    /**
     * @brief 数字类别 / Number category.
     */
    Kind kind;

    /**
     * @brief 原始十进制词法 / Original decimal lexeme.
     */
    std::string lexeme;
};

/**
 * @brief 可递归的 JSON 值 / Recursive JSON value.
 *
 * @note Object 使用 vector 保留成员顺序；重复键在解析阶段拒绝。/ Object uses a vector to
 *       preserve member order; duplicate keys are rejected during parsing.
 */
class Value final {
public:
    /**
     * @brief JSON 数组 / JSON array.
     */
    using Array = std::vector<Value>;

    /**
     * @brief JSON 对象成员 / JSON object member.
     */
    using Member = std::pair<String, Value>;

    /**
     * @brief JSON 对象 / JSON object.
     */
    using Object = std::vector<Member>;

    /**
     * @brief JSON 值存储类型 / Storage type for a JSON value.
     */
    using Data = std::variant<std::nullptr_t, bool, Number, String, Array, Object>;

    /**
     * @brief 构造 JSON 值 / Construct a JSON value.
     * @param data 完整的 JSON 值 / Complete JSON value.
     */
    explicit Value(Data data);

    /**
     * @brief 默认构造 JSON null / Construct JSON null by default.
     */
    Value();

    /**
     * @brief 取得只读 JSON 数据 / Get read-only JSON data.
     * @return JSON 值存储 / JSON value storage.
     */
    [[nodiscard]] const Data& data() const noexcept;

private:
    /**
     * @brief JSON 值存储 / JSON value storage.
     */
    Data data_;
};

/**
 * @brief JSONC 错误类别 / JSONC error category.
 */
enum class ErrorCode {
    syntax,
    io,
    include,
    include_cycle,
    include_depth,
};

/**
 * @brief JSONC 源位置 / JSONC source location.
 */
struct SourceLocation final {
    /**
     * @brief 一基行号 / One-based line number.
     */
    std::size_t line{1U};

    /**
     * @brief 一基列号 / One-based column number.
     */
    std::size_t column{1U};
};

/**
 * @brief JSONC 解析异常 / JSONC parse exception.
 *
 * @note 错误文本不包含配置值，调用方可以安全地将其记录到日志。/ Error text does not include
 *       configuration values, so callers may safely record it in logs.
 */
class Error final : public std::runtime_error {
public:
    /**
     * @brief 构造 JSONC 错误 / Construct a JSONC error.
     * @param code 错误类别 / Error category.
     * @param message 稳定的人类可读错误文本 / Stable human-readable error text.
     * @param location 可选源位置 / Optional source location.
     */
    Error(ErrorCode code, std::string message, SourceLocation location = SourceLocation{});

    /**
     * @brief 取得错误类别 / Get the error category.
     * @return 错误类别 / Error category.
     */
    [[nodiscard]] ErrorCode code() const noexcept;

    /**
     * @brief 取得错误源位置 / Get the error source location.
     * @return 一基行列位置 / One-based line-and-column location.
     */
    [[nodiscard]] SourceLocation location() const noexcept;

private:
    /**
     * @brief 错误类别 / Error category.
     */
    ErrorCode code_;

    /**
     * @brief 错误源位置 / Error source location.
     */
    SourceLocation location_;
};

/**
 * @brief 解析内存中的严格 JSONC 文本 / Parse strict JSONC text in memory.
 *
 * @param source UTF-8 JSONC 文本 / UTF-8 JSONC text.
 * @return 顶层 JSON 对象 / Top-level JSON object.
 * @throw Error 文本无效、包含 include 却没有文件上下文时抛出 /
 *        Thrown for invalid text or an include without file context.
 * @note 只允许 JSON 注释扩展；顶层值必须是 object。/ Only JSON comments are extended; the
 *       top-level value must be an object.
 */
[[nodiscard]] Value parse_jsonc(std::string_view source);

/**
 * @brief 解析带源文件上下文的严格 JSONC 文本 / Parse strict JSONC text with source-file context.
 *
 * @param source UTF-8 JSONC 文本 / UTF-8 JSONC text.
 * @param source_path 当前虚拟源文件路径 / Current virtual source-file path.
 * @return 顶层 JSON 对象 / Top-level JSON object.
 * @throw Error 文本无效或 include 无法读取时抛出 / Thrown for invalid text or an unreadable
 *        include.
 * @note include 路径相对于 source_path 的父目录解析。/ Include paths are resolved relative to
 *       the parent directory of source_path.
 */
[[nodiscard]] Value parse_jsonc(std::string_view source, const std::filesystem::path& source_path);

/**
 * @brief 从文件读取并解析严格 JSONC / Read and parse strict JSONC from a file.
 *
 * @param path 根 JSONC 或 JSON 文件路径 / Root JSONC or JSON file path.
 * @return 展开 include 后的顶层 JSON 对象 / Top-level JSON object after include expansion.
 * @throw Error 文件无法读取、语法无效或 include 循环时抛出 / Thrown for I/O, syntax, or
 *        include-cycle failures.
 * @note ``"$<file_path>"`` 是精确字符串值替换；被 include 的文件可以返回任意 JSON 值，
 *       但根文件最终必须是 object。/ ``"$<file_path>"`` is an exact string-value
 *       replacement; an included file may contain any JSON value, but the root file must
 *       ultimately be an object.
 */
[[nodiscard]] Value load_jsonc(const std::filesystem::path& path);

} // namespace fogmoe::config
