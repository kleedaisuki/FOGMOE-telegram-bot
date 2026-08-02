/**
 * @file jsonc.cpp
 * @brief 严格 JSONC 解析与文件内联实现 / Implementation of strict JSONC parsing and file inlining.
 */

#include "fogmoe_config/jsonc.hpp"

#include <cerrno>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <system_error>
#include <utility>

namespace fogmoe::config {
namespace {

/**
 * @brief include 最大递归深度 / Maximum include recursion depth.
 */
constexpr std::size_t kMaxIncludeDepth = 64U;

/**
 * @brief 判断码点是否为高代理项 / Check whether a code point is a high surrogate.
 * @param code_point 待检查码点 / Code point to check.
 * @return 是否为高代理项 / Whether it is a high surrogate.
 */
[[nodiscard]] bool is_high_surrogate(const char32_t code_point) noexcept {
    return code_point >= 0xd800U && code_point <= 0xdbffU;
}

/**
 * @brief 判断码点是否为低代理项 / Check whether a code point is a low surrogate.
 * @param code_point 待检查码点 / Code point to check.
 * @return 是否为低代理项 / Whether it is a low surrogate.
 */
[[nodiscard]] bool is_low_surrogate(const char32_t code_point) noexcept {
    return code_point >= 0xdc00U && code_point <= 0xdfffU;
}

/**
 * @brief 将 Unicode 码点追加为 UTF-8 / Append one Unicode code point as UTF-8.
 * @param code_point 要编码的码点 / Code point to encode.
 * @param output UTF-8 输出缓冲区 / UTF-8 output buffer.
 * @note 孤立代理项只用于错误文本，使用替代符号而不生成非法 UTF-8。/ Lone surrogates are
 *       used only in error text and are rendered as the replacement character rather than
 *       invalid UTF-8.
 */
void append_utf8(const char32_t code_point, std::string& output) {
    const char32_t safe_code_point =
        (code_point > 0x10ffffU || (code_point >= 0xd800U && code_point <= 0xdfffU))
            ? 0xfffdU
            : code_point;
    if (safe_code_point <= 0x7fU) {
        output.push_back(static_cast<char>(safe_code_point));
    } else if (safe_code_point <= 0x7ffU) {
        output.push_back(static_cast<char>(0xc0U | (safe_code_point >> 6U)));
        output.push_back(static_cast<char>(0x80U | (safe_code_point & 0x3fU)));
    } else if (safe_code_point <= 0xffffU) {
        output.push_back(static_cast<char>(0xe0U | (safe_code_point >> 12U)));
        output.push_back(static_cast<char>(0x80U | ((safe_code_point >> 6U) & 0x3fU)));
        output.push_back(static_cast<char>(0x80U | (safe_code_point & 0x3fU)));
    } else {
        output.push_back(static_cast<char>(0xf0U | (safe_code_point >> 18U)));
        output.push_back(static_cast<char>(0x80U | ((safe_code_point >> 12U) & 0x3fU)));
        output.push_back(static_cast<char>(0x80U | ((safe_code_point >> 6U) & 0x3fU)));
        output.push_back(static_cast<char>(0x80U | (safe_code_point & 0x3fU)));
    }
}

/**
 * @brief 将 UTF-32 字符串编码为 UTF-8 / Encode a UTF-32 string as UTF-8.
 * @param value UTF-32 字符串 / UTF-32 string.
 * @return UTF-8 字符串 / UTF-8 string.
 */
[[nodiscard]] std::string utf8_from_string(const String& value) {
    std::string output;
    output.reserve(value.size());
    for (const char32_t code_point : value) {
        append_utf8(code_point, output);
    }
    return output;
}

/**
 * @brief 将路径格式化为稳定文本 / Render a path as stable text.
 * @param path 文件路径 / File path.
 * @return 通用路径文本 / Generic path text.
 */
[[nodiscard]] std::string path_text(const std::filesystem::path& path) {
    return path.generic_string();
}

/**
 * @brief 将路径规范化为绝对路径 / Normalize a path into an absolute path.
 * @param path 待规范化路径 / Path to normalize.
 * @return 尽可能解析符号链接后的绝对路径 / Absolute path with symlinks resolved when possible.
 */
[[nodiscard]] std::filesystem::path normalized_path(const std::filesystem::path& path) {
    std::error_code error;
    const std::filesystem::path canonical = std::filesystem::weakly_canonical(path, error);
    if (!error) {
        return canonical;
    }
    error.clear();
    const std::filesystem::path absolute = std::filesystem::absolute(path, error);
    if (!error) {
        return absolute.lexically_normal();
    }
    return path.lexically_normal();
}

/**
 * @brief 读取文件原始字节 / Read raw file bytes.
 * @param path 文件路径 / File path.
 * @param included 是否为 include 文件 / Whether this is an included file.
 * @return 文件字节 / File bytes.
 * @throw Error 文件无法读取时抛出 / Thrown when the file cannot be read.
 */
[[nodiscard]] std::string read_file(const std::filesystem::path& path,
                                    const bool included) {
    std::ifstream input(path, std::ios::binary);
    if (!input) {
        const std::string prefix = included ? "cannot read included JSONC file "
                                            : "cannot read JSONC file ";
        throw Error(ErrorCode::io, prefix + path_text(path) + ": cannot open file");
    }
    std::ostringstream buffer;
    buffer << input.rdbuf();
    if (!input.good() && !input.eof()) {
        const std::string prefix = included ? "cannot read included JSONC file "
                                            : "cannot read JSONC file ";
        throw Error(ErrorCode::io, prefix + path_text(path) + ": read failed");
    }
    return buffer.str();
}

/**
 * @brief 将源位置格式化到 JSON 错误文本 / Format a source location into JSON error text.
 * @param location 一基源位置 / One-based source location.
 * @param detail 错误细节 / Error detail.
 * @return 稳定错误文本 / Stable error text.
 */
[[nodiscard]] std::string json_error_text(const SourceLocation location,
                                          const std::string_view detail) {
    return "invalid JSON at line " + std::to_string(location.line) + ", column " +
           std::to_string(location.column) + ": " + std::string{detail};
}

/**
 * @brief 严格 JSONC 文件上下文 / Strict JSONC file context.
 *
 * 该类型拥有 include 栈，解析器只负责语法；文件读取、路径解析与循环检测集中在这里。/
 * This type owns the include stack; the parser handles syntax while this context centralizes
 * file I/O, path resolution, and cycle detection.
 */
class IncludeContext;

/**
 * @brief 单个 JSONC 文本解析器 / Parser for one JSONC text.
 */
class Parser final {
public:
    /**
     * @brief 构造文本解析器 / Construct a text parser.
     * @param source 原始 UTF-8 文本 / Original UTF-8 text.
     * @param source_path 可选当前文件路径 / Optional current file path.
     * @param context include 上下文 / Include context.
     */
    Parser(std::string_view source, std::optional<std::filesystem::path> source_path,
           IncludeContext& context)
        : source_(source), source_path_(std::move(source_path)), context_(context) {}

    /**
     * @brief 解析一个 JSON 值 / Parse one JSON value.
     * @param require_object 是否要求顶层对象 / Whether the top-level value must be an object.
     * @return JSON 值 / JSON value.
     */
    [[nodiscard]] Value parse_document(bool require_object);

private:
    /**
     * @brief 解析 JSON 值 / Parse a JSON value.
     * @return JSON 值 / JSON value.
     */
    [[nodiscard]] Value parse_value();

    /**
     * @brief 解析 JSON 对象 / Parse a JSON object.
     * @return JSON 对象 / JSON object.
     */
    [[nodiscard]] Value parse_object();

    /**
     * @brief 解析 JSON 数组 / Parse a JSON array.
     * @return JSON 数组 / JSON array.
     */
    [[nodiscard]] Value parse_array();

    /**
     * @brief 解析 JSON 字符串 / Parse a JSON string.
     * @return Unicode 字符串 / Unicode string.
     */
    [[nodiscard]] String parse_string();

    /**
     * @brief 解析 JSON 数字 / Parse a JSON number.
     * @return 数字值 / Number value.
     */
    [[nodiscard]] Value parse_number();

    /**
     * @brief 解析 JSON literal / Parse a JSON literal.
     * @param literal 期望的 literal / Expected literal.
     * @param value 对应 JSON 值 / Corresponding JSON value.
     * @return JSON 值 / JSON value.
     */
    [[nodiscard]] Value parse_literal(std::string_view literal, Value value);

    /**
     * @brief 跳过空白与 JSONC 注释 / Skip whitespace and JSONC comments.
     */
    void skip_space_and_comments();

    /**
     * @brief 验证整个输入的 UTF-8 / Validate UTF-8 across the entire input.
     */
    void validate_utf8();

    /**
     * @brief 解码一个 UTF-8 码点 / Decode one UTF-8 code point.
     * @return 码点及其字节宽度 / Code point and its byte width.
     */
    [[nodiscard]] std::pair<char32_t, std::size_t> decode_utf8() const;

    /**
     * @brief 解析四位十六进制 Unicode escape / Parse a four-digit hexadecimal Unicode escape.
     * @return UTF-16 code unit / UTF-16 code unit.
     */
    [[nodiscard]] char32_t parse_unicode_escape();

    /**
     * @brief 解析可能的 include 值 / Expand a possible include value.
     * @param value 已解析字符串 / Parsed string.
     * @param location include 字符串位置 / Location of the include string.
     * @return 原字符串或被 include 的值 / Original string or included value.
     */
    [[nodiscard]] Value expand_include(String value, SourceLocation location);

    /**
     * @brief 计算源位置 / Compute a source location.
     * @param position 字节偏移 / Byte offset.
     * @return 一基行列位置 / One-based line-and-column location.
     */
    [[nodiscard]] SourceLocation location_at(std::size_t position) const noexcept;

    /**
     * @brief 查看当前字符 / Peek at the current character.
     * @return 当前字符或空字符 / Current character or the empty character.
     */
    [[nodiscard]] char peek() const noexcept;

    /**
     * @brief 查看前缀 / Check a source prefix.
     * @param prefix 待检查前缀 / Prefix to check.
     * @return 是否匹配 / Whether it matches.
     */
    [[nodiscard]] bool starts_with(std::string_view prefix) const noexcept;

    /**
     * @brief 生成语法错误 / Raise a syntax error.
     * @param detail 错误细节 / Error detail.
     * @param position 错误位置 / Error position.
     */
    [[noreturn]] void fail_json(std::string_view detail, std::size_t position) const;

    /**
     * @brief 原始 JSONC 文本 / Original JSONC text.
     */
    std::string_view source_;

    /**
     * @brief 当前解析文件路径 / Current parsed file path.
     */
    std::optional<std::filesystem::path> source_path_;

    /**
     * @brief include 上下文引用 / Reference to the include context.
     */
    IncludeContext& context_;

    /**
     * @brief 当前字节偏移 / Current byte offset.
     */
    std::size_t position_{0U};
};

/**
 * @brief 管理递归文件加载与 include 栈 / Manage recursive file loading and the include stack.
 */
class IncludeContext final {
public:
    /**
     * @brief 解析带路径上下文的内存文本 / Parse in-memory text with a file-path context.
     * @param source 原始 UTF-8 文本 / Original UTF-8 text.
     * @param path 虚拟源文件路径 / Virtual source-file path.
     * @return 展开后的顶层对象 / Expanded top-level object.
     */
    [[nodiscard]] Value parse_source(std::string_view source,
                                     const std::filesystem::path& path) {
        const std::filesystem::path normalized = normalized_path(path);
        stack_.push_back(normalized);
        try {
            Parser parser(source, normalized, *this);
            Value value = parser.parse_document(true);
            stack_.pop_back();
            return value;
        } catch (...) {
            stack_.pop_back();
            throw;
        }
    }

    /**
     * @brief 读取根文件 / Load the root file.
     * @param path 根文件路径 / Root file path.
     * @return 展开后的根对象 / Expanded root object.
     */
    [[nodiscard]] Value load_root(const std::filesystem::path& path) {
        const std::filesystem::path normalized = normalized_path(path);
        const std::string source = read_file(normalized, false);
        stack_.push_back(normalized);
        try {
            Parser parser(source, normalized, *this);
            Value value = parser.parse_document(true);
            stack_.pop_back();
            return value;
        } catch (...) {
            stack_.pop_back();
            throw;
        }
    }

    /**
     * @brief 读取 include 文件 / Load an included file.
     * @param path include 目标路径 / Include target path.
     * @param location include 表达式位置 / Location of the include expression.
     * @return 展开后的 JSON 值 / Expanded JSON value.
     */
    [[nodiscard]] Value load_include(const std::filesystem::path& path,
                                     const SourceLocation location) {
        if (stack_.size() >= kMaxIncludeDepth) {
            throw Error(ErrorCode::include_depth,
                        "JSONC include depth exceeds " + std::to_string(kMaxIncludeDepth),
                        location);
        }

        const std::filesystem::path normalized = normalized_path(path);
        for (const std::filesystem::path& active : stack_) {
            if (active == normalized) {
                throw Error(ErrorCode::include_cycle,
                            "JSONC include cycle detected at " + path_text(normalized),
                            location);
            }
        }

        const std::string source = read_file(normalized, true);
        stack_.push_back(normalized);
        try {
            Parser parser(source, normalized, *this);
            Value value = parser.parse_document(false);
            stack_.pop_back();
            return value;
        } catch (const Error& error) {
            stack_.pop_back();
            if (error.code() == ErrorCode::io || error.code() == ErrorCode::include_cycle ||
                error.code() == ErrorCode::include_depth) {
                throw;
            }
            throw Error(error.code(),
                        "invalid included JSONC file " + path_text(normalized) + ": " +
                            error.what(),
                        error.location());
        } catch (...) {
            stack_.pop_back();
            throw;
        }
    }

private:
    /**
     * @brief 当前活动 include 文件栈 / Active include-file stack.
     */
    std::vector<std::filesystem::path> stack_;
};

/**
 * @brief 判断字节是否为 UTF-8 continuation byte / Check for a UTF-8 continuation byte.
 * @param byte 待检查字节 / Byte to check.
 * @return 是否为 continuation byte / Whether it is a continuation byte.
 */
[[nodiscard]] bool is_utf8_continuation(const unsigned char byte) noexcept {
    return (byte & 0xc0U) == 0x80U;
}

/**
 * @brief 判断字符串是否为 include 占位符 / Check whether a string is an include placeholder.
 * @param value 已解析字符串 / Parsed string.
 * @return include 路径或空值 / Include path or no value.
 */
[[nodiscard]] std::optional<std::string> include_path(const String& value) {
    if (value.size() < 3U || value[0] != U'$' || value[1] != U'<' || value.back() != U'>') {
        return std::nullopt;
    }
    const String path_value(value.begin() + 2, value.end() - 1);
    if (path_value.empty()) {
        return std::string{};
    }
    for (const char32_t code_point : path_value) {
        if (code_point == U'\0' || code_point > 0x10ffffU ||
            (code_point >= 0xd800U && code_point <= 0xdfffU)) {
            return std::string{};
        }
    }
    return utf8_from_string(path_value);
}

}  // namespace

Value::Value(Data data) : data_(std::move(data)) {}

Value::Value() : data_(nullptr) {}

const Value::Data& Value::data() const noexcept {
    return data_;
}

Error::Error(const ErrorCode code, std::string message, const SourceLocation location)
    : std::runtime_error(std::move(message)), code_(code), location_(location) {}

ErrorCode Error::code() const noexcept {
    return code_;
}

SourceLocation Error::location() const noexcept {
    return location_;
}

Value Parser::parse_document(const bool require_object) {
    validate_utf8();
    skip_space_and_comments();
    const std::size_t document_start = position_;
    Value value = parse_value();
    skip_space_and_comments();
    if (position_ != source_.size()) {
        fail_json("Extra data", position_);
    }
    if (require_object && !std::holds_alternative<Value::Object>(value.data())) {
        throw Error(ErrorCode::syntax, "the top-level JSONC value must be an object",
                    location_at(document_start));
    }
    return value;
}

Value Parser::parse_value() {
    skip_space_and_comments();
    const std::size_t value_start = position_;
    if (position_ >= source_.size()) {
        fail_json("Expecting value", position_);
    }

    Value value;
    switch (peek()) {
    case '{':
        value = parse_object();
        break;
    case '[':
        value = parse_array();
        break;
    case '"':
        value = expand_include(parse_string(), location_at(value_start));
        break;
    case 't':
        value = parse_literal("true", Value{true});
        break;
    case 'f':
        value = parse_literal("false", Value{false});
        break;
    case 'n':
        value = parse_literal("null", Value{nullptr});
        break;
    case 'N':
        if (starts_with("NaN")) {
            throw Error(ErrorCode::syntax,
                        "non-standard JSON numeric constant 'NaN' is not allowed",
                        location_at(value_start));
        }
        fail_json("Expecting value", value_start);
        break;
    case 'I':
        if (starts_with("Infinity")) {
            throw Error(ErrorCode::syntax,
                        "non-standard JSON numeric constant 'Infinity' is not allowed",
                        location_at(value_start));
        }
        fail_json("Expecting value", value_start);
        break;
    case '-':
        if (starts_with("-Infinity")) {
            throw Error(ErrorCode::syntax,
                        "non-standard JSON numeric constant '-Infinity' is not allowed",
                        location_at(value_start));
        }
        value = parse_number();
        break;
    default:
        if (peek() >= '0' && peek() <= '9') {
            value = parse_number();
        } else {
            fail_json("Expecting value", value_start);
        }
        break;
    }
    return value;
}

Value Parser::parse_object() {
    ++position_;
    Value::Object object;
    skip_space_and_comments();
    if (peek() == '}') {
        ++position_;
        return Value{std::move(object)};
    }

    while (true) {
        skip_space_and_comments();
        if (peek() != '"') {
            fail_json("Expecting property name enclosed in double quotes", position_);
        }
        const String key = parse_string();
        skip_space_and_comments();
        if (peek() != ':') {
            fail_json("Expecting ':' delimiter", position_);
        }
        ++position_;
        Value value = parse_value();

        for (const auto& member : object) {
            if (member.first == key) {
                throw Error(ErrorCode::syntax, "duplicate object key '" + utf8_from_string(key) +
                                                  "'");
            }
        }
        object.emplace_back(key, std::move(value));

        skip_space_and_comments();
        if (peek() == '}') {
            ++position_;
            return Value{std::move(object)};
        }
        if (peek() != ',') {
            fail_json("Expecting ',' delimiter", position_);
        }
        ++position_;
        skip_space_and_comments();
        if (peek() == '}') {
            fail_json("Expecting property name enclosed in double quotes", position_);
        }
    }
}

Value Parser::parse_array() {
    ++position_;
    Value::Array array;
    skip_space_and_comments();
    if (peek() == ']') {
        ++position_;
        return Value{std::move(array)};
    }

    while (true) {
        array.emplace_back(parse_value());
        skip_space_and_comments();
        if (peek() == ']') {
            ++position_;
            return Value{std::move(array)};
        }
        if (peek() != ',') {
            fail_json("Expecting ',' delimiter", position_);
        }
        ++position_;
        skip_space_and_comments();
        if (peek() == ']') {
            fail_json("Expecting value", position_);
        }
    }
}

String Parser::parse_string() {
    if (peek() != '"') {
        fail_json("Expecting property name enclosed in double quotes", position_);
    }
    ++position_;
    String value;
    while (position_ < source_.size()) {
        const unsigned char byte = static_cast<unsigned char>(source_[position_]);
        if (byte == '"') {
            ++position_;
            return value;
        }
        if (byte == '\\') {
            ++position_;
            if (position_ >= source_.size()) {
                fail_json("Unterminated string", position_);
            }
            const char escape = source_[position_++];
            switch (escape) {
            case '"':
                value.push_back(U'"');
                break;
            case '\\':
                value.push_back(U'\\');
                break;
            case '/':
                value.push_back(U'/');
                break;
            case 'b':
                value.push_back(U'\b');
                break;
            case 'f':
                value.push_back(U'\f');
                break;
            case 'n':
                value.push_back(U'\n');
                break;
            case 'r':
                value.push_back(U'\r');
                break;
            case 't':
                value.push_back(U'\t');
                break;
            case 'u': {
                const char32_t first = parse_unicode_escape();
                if (is_high_surrogate(first) && starts_with("\\u")) {
                    const std::size_t second_start = position_;
                    position_ += 2U;
                    const char32_t second = parse_unicode_escape();
                    if (is_low_surrogate(second)) {
                        value.push_back(
                            static_cast<char32_t>(0x10000U + ((first - 0xd800U) << 10U) +
                                                  (second - 0xdc00U)));
                    } else {
                        value.push_back(first);
                        position_ = second_start;
                    }
                } else {
                    value.push_back(first);
                }
                break;
            }
            default:
                fail_json("Invalid \\escape", position_ - 1U);
            }
            continue;
        }
        if (byte < 0x20U) {
            fail_json("Invalid control character", position_);
        }
        if (byte < 0x80U) {
            value.push_back(static_cast<char32_t>(byte));
            ++position_;
            continue;
        }

        try {
            const auto [code_point, width] = decode_utf8();
            value.push_back(code_point);
            position_ += width;
        } catch (const Error&) {
            throw;
        }
    }
    fail_json("Unterminated string", position_);
}

Value Parser::parse_number() {
    const std::size_t start = position_;
    if (peek() == '-') {
        ++position_;
    }
    if (peek() == '0') {
        ++position_;
    } else if (peek() >= '1' && peek() <= '9') {
        while (peek() >= '0' && peek() <= '9') {
            ++position_;
        }
    } else {
        fail_json("Expecting value", start);
    }

    bool real = false;
    if (peek() == '.') {
        real = true;
        ++position_;
        if (peek() < '0' || peek() > '9') {
            fail_json("Expecting value", position_);
        }
        while (peek() >= '0' && peek() <= '9') {
            ++position_;
        }
    }
    if (peek() == 'e' || peek() == 'E') {
        real = true;
        ++position_;
        if (peek() == '+' || peek() == '-') {
            ++position_;
        }
        if (peek() < '0' || peek() > '9') {
            fail_json("Expecting value", position_);
        }
        while (peek() >= '0' && peek() <= '9') {
            ++position_;
        }
    }

    std::string lexeme(source_.substr(start, position_ - start));
    if (real) {
        char* end = nullptr;
        errno = 0;
        const double parsed = std::strtod(lexeme.c_str(), &end);
        if (end == nullptr || *end != '\0' || !std::isfinite(parsed)) {
            throw Error(ErrorCode::syntax,
                        "non-finite JSON numeric value '" + lexeme + "' is not allowed",
                        location_at(start));
        }
        return Value{Number{Number::Kind::real, std::move(lexeme)}};
    }
    return Value{Number{Number::Kind::integer, std::move(lexeme)}};
}

Value Parser::parse_literal(const std::string_view literal, Value value) {
    if (!starts_with(literal)) {
        fail_json("Expecting value", position_);
    }
    position_ += literal.size();
    return value;
}

void Parser::skip_space_and_comments() {
    while (position_ < source_.size()) {
        const char current = source_[position_];
        if (current == ' ' || current == '\t' || current == '\r' || current == '\n') {
            ++position_;
            continue;
        }
        if (current != '/' || position_ + 1U >= source_.size() ||
            (source_[position_ + 1U] != '/' && source_[position_ + 1U] != '*')) {
            return;
        }
        if (source_[position_ + 1U] == '/') {
            position_ += 2U;
            while (position_ < source_.size() && source_[position_] != '\r' &&
                   source_[position_] != '\n') {
                ++position_;
            }
            continue;
        }

        const std::size_t comment_start = position_;
        position_ += 2U;
        bool terminated = false;
        while (position_ < source_.size()) {
            if (source_[position_] == '*' && position_ + 1U < source_.size() &&
                source_[position_ + 1U] == '/') {
                position_ += 2U;
                terminated = true;
                break;
            }
            ++position_;
        }
        if (!terminated) {
            const SourceLocation location = location_at(comment_start);
            throw Error(ErrorCode::syntax,
                        "unterminated block comment at line " +
                            std::to_string(location.line) + ", column " +
                            std::to_string(location.column),
                        location);
        }
    }
}

void Parser::validate_utf8() {
    const std::size_t saved_position = position_;
    position_ = 0U;
    while (position_ < source_.size()) {
        const unsigned char byte = static_cast<unsigned char>(source_[position_]);
        if (byte < 0x80U) {
            ++position_;
            continue;
        }
        const auto [unused_code_point, width] = decode_utf8();
        static_cast<void>(unused_code_point);
        position_ += width;
    }
    position_ = saved_position;
}

std::pair<char32_t, std::size_t> Parser::decode_utf8() const {
    const std::size_t start = position_;
    const unsigned char first = static_cast<unsigned char>(source_[start]);
    std::size_t width = 0U;
    char32_t code_point = 0U;
    char32_t minimum = 0U;
    if (first >= 0xc2U && first <= 0xdfU) {
        width = 2U;
        code_point = static_cast<char32_t>(first & 0x1fU);
        minimum = 0x80U;
    } else if (first >= 0xe0U && first <= 0xefU) {
        width = 3U;
        code_point = static_cast<char32_t>(first & 0x0fU);
        minimum = 0x800U;
    } else if (first >= 0xf0U && first <= 0xf4U) {
        width = 4U;
        code_point = static_cast<char32_t>(first & 0x07U);
        minimum = 0x10000U;
    } else {
        throw Error(ErrorCode::syntax,
                    json_error_text(location_at(start), "invalid UTF-8 sequence"),
                    location_at(start));
    }

    if (start + width > source_.size()) {
        throw Error(ErrorCode::syntax,
                    json_error_text(location_at(start), "invalid UTF-8 sequence"),
                    location_at(start));
    }
    for (std::size_t offset = 1U; offset < width; ++offset) {
        const unsigned char continuation =
            static_cast<unsigned char>(source_[start + offset]);
        if (!is_utf8_continuation(continuation)) {
            throw Error(ErrorCode::syntax,
                        json_error_text(location_at(start), "invalid UTF-8 sequence"),
                        location_at(start));
        }
        code_point = static_cast<char32_t>((code_point << 6U) | (continuation & 0x3fU));
    }
    if (code_point < minimum || code_point > 0x10ffffU ||
        (code_point >= 0xd800U && code_point <= 0xdfffU)) {
        throw Error(ErrorCode::syntax,
                    json_error_text(location_at(start), "invalid UTF-8 sequence"),
                    location_at(start));
    }
    return {code_point, width};
}

char32_t Parser::parse_unicode_escape() {
    if (position_ + 4U > source_.size()) {
        fail_json("Invalid \\uXXXX escape", position_);
    }
    char32_t value = 0U;
    for (std::size_t index = 0U; index < 4U; ++index) {
        const char character = source_[position_++];
        value <<= 4U;
        if (character >= '0' && character <= '9') {
            value |= static_cast<char32_t>(character - '0');
        } else if (character >= 'a' && character <= 'f') {
            value |= static_cast<char32_t>(character - 'a' + 10);
        } else if (character >= 'A' && character <= 'F') {
            value |= static_cast<char32_t>(character - 'A' + 10);
        } else {
            fail_json("Invalid \\uXXXX escape", position_ - 1U);
        }
    }
    return value;
}

Value Parser::expand_include(String value, const SourceLocation location) {
    const std::optional<std::string> target_text = include_path(value);
    if (!target_text.has_value()) {
        return Value{std::move(value)};
    }
    if (target_text->empty()) {
        throw Error(ErrorCode::include, "JSONC include path must not be empty", location);
    }
    if (!source_path_.has_value()) {
        throw Error(ErrorCode::include, "JSONC include requires a source file path", location);
    }
    std::filesystem::path target{*target_text};
    if (target.is_relative()) {
        target = source_path_->parent_path() / target;
    }
    return context_.load_include(target, location);
}

SourceLocation Parser::location_at(const std::size_t position) const noexcept {
    SourceLocation location{};
    const std::size_t bounded = position < source_.size() ? position : source_.size();
    for (std::size_t index = 0U; index < bounded; ++index) {
        if (source_[index] == '\n') {
            ++location.line;
            location.column = 1U;
        } else {
            ++location.column;
        }
    }
    return location;
}

char Parser::peek() const noexcept {
    return position_ < source_.size() ? source_[position_] : '\0';
}

bool Parser::starts_with(const std::string_view prefix) const noexcept {
    return position_ + prefix.size() <= source_.size() &&
           source_.compare(position_, prefix.size(), prefix) == 0;
}

void Parser::fail_json(const std::string_view detail, const std::size_t position) const {
    const SourceLocation location = location_at(position);
    throw Error(ErrorCode::syntax, json_error_text(location, detail), location);
}

Value parse_jsonc(const std::string_view source) {
    IncludeContext context;
    Parser parser(source, std::nullopt, context);
    return parser.parse_document(true);
}

Value parse_jsonc(const std::string_view source, const std::filesystem::path& source_path) {
    IncludeContext context;
    return context.parse_source(source, source_path);
}

Value load_jsonc(const std::filesystem::path& path) {
    IncludeContext context;
    return context.load_root(path);
}

}  // namespace fogmoe::config
