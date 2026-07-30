#include "wspctl/presentation/operator_cli.hpp"

#include "wspctl/presentation/operator_gateway.hpp"

#include <algorithm>
#include <charconv>
#include <cstdlib>
#include <iomanip>
#include <optional>
#include <sstream>
#include <sys/ioctl.h>
#include <unistd.h>
#include <utility>

namespace wspctl::presentation::operator_cli {
namespace {

/** @brief dashboard 使用的最小安全渲染宽度 / Minimum safe dashboard render width. */
constexpr std::size_t kMinimumColumns{16U};
/** @brief dashboard 使用的最大渲染宽度 / Maximum dashboard render width. */
constexpr std::size_t kMaximumColumns{120U};
/** @brief 切换宽屏 dashboard 的阈值 / Threshold for switching to the wide dashboard. */
constexpr std::size_t kWideDashboardColumns{72U};
/** @brief watch 允许的最短刷新秒数 / Minimum allowed watch refresh seconds. */
constexpr unsigned int kMinimumRefreshSeconds{1U};
/** @brief watch 允许的最长刷新秒数 / Maximum allowed watch refresh seconds. */
constexpr unsigned int kMaximumRefreshSeconds{3'600U};

/** @brief ANSI 语义色调 / Semantic ANSI tones. */
enum class Tone : std::uint8_t {
    /** @brief 默认文本 / Default text. */
    normal = 0,
    /** @brief 标题 cyan / Cyan title. */
    title = 1,
    /** @brief 健康 green / Green healthy state. */
    healthy = 2,
    /** @brief 过渡 yellow / Yellow transitional state. */
    warning = 3,
    /** @brief 失败 red / Red failed state. */
    danger = 4,
    /** @brief 次要 dim 文本 / Dim secondary text. */
    muted = 5,
    /** @brief 数据 blue / Blue data text. */
    data = 6,
};

/** @brief dashboard 综合健康度 / Aggregate dashboard health. */
enum class DashboardHealth : std::uint8_t {
    /** @brief runtime 与 workspace 正常 / Runtime and workspace are healthy. */
    healthy = 0,
    /** @brief 当前正处于生命周期转换 / A lifecycle transition is in progress. */
    transitioning = 1,
    /** @brief 尚无持久 workspace / No persistent workspace exists yet. */
    empty = 2,
    /** @brief runtime 已失败 / Runtime has failed. */
    failed = 3,
    /** @brief quota 快照已超过 hard limit / Quota snapshot exceeds a hard limit. */
    degraded = 4,
};

/**
 * @brief 构造一个解析失败 / Construct a parse failure.
 * @param message 面向用户的诊断 / User-facing diagnostic.
 * @return 可直接返回的 expected error / Expected error ready to return.
 */
[[nodiscard]] ParseResult parse_failure(std::string message) {
    return std::unexpected(ParseError{.message = std::move(message), .show_usage = true});
}

/**
 * @brief 检查参数是否等于短或长 help option / Check whether an argument is a short or long help
 * option.
 * @param argument 待检查参数 / Argument to inspect.
 * @return 是否为 help option / Whether this is a help option.
 */
[[nodiscard]] bool is_help(const std::string_view argument) noexcept {
    return argument == "-h" || argument == "--help";
}

/**
 * @brief 解析显式色彩策略 / Parse an explicit color policy.
 * @param value option 值 / Option value.
 * @return 已知策略；未知值为空 / Known policy, or empty for an unknown value.
 */
[[nodiscard]] std::optional<ColorMode> parse_color_mode(const std::string_view value) noexcept {
    if (value == "auto") {
        return ColorMode::automatic;
    }
    if (value == "always") {
        return ColorMode::always;
    }
    if (value == "never") {
        return ColorMode::never;
    }
    return std::nullopt;
}

/**
 * @brief 解析有界的整数刷新周期 / Parse a bounded integral refresh period.
 * @param value 十进制秒数 / Decimal seconds.
 * @return 合法秒数；失败为空 / Valid seconds, or empty on failure.
 */
[[nodiscard]] std::optional<std::chrono::seconds>
parse_refresh_interval(const std::string_view value) noexcept {
    /** @brief 已解析秒数 / Parsed seconds. */
    unsigned int seconds{0U};
    /** @brief 无 locale 整数解析结果 / Locale-free integer parse result. */
    const auto parsed = std::from_chars(value.data(), value.data() + value.size(), seconds);
    if (value.empty() || parsed.ec != std::errc{} || parsed.ptr != value.data() + value.size() ||
        seconds < kMinimumRefreshSeconds || seconds > kMaximumRefreshSeconds) {
        return std::nullopt;
    }
    return std::chrono::seconds{seconds};
}

/**
 * @brief 将不可任意信任的 bytes 编码为 terminal-safe ASCII / Encode untrusted bytes as
 * terminal-safe ASCII.
 * @param value 待编码 bytes / Bytes to encode.
 * @param preserve_slash 是否保留 `/` / Whether to preserve `/`.
 * @param preserve_space 是否保留可打印 ASCII 空格与标点 /
 *        Whether to preserve printable ASCII spaces and punctuation.
 * @return 不含控制字符的 ASCII / ASCII without control characters.
 */
[[nodiscard]] std::string terminal_safe_text(const std::string_view value,
                                             const bool preserve_slash, const bool preserve_space) {
    /** @brief 十六进制数字表 / Hexadecimal digit table. */
    constexpr std::string_view kDigits{"0123456789ABCDEF"};
    /** @brief 编码结果 / Encoded result. */
    std::string rendered;
    rendered.reserve(value.size() * 3U);
    for (const unsigned char byte : value) {
        /** @brief 当前 byte 是否可以原样输出 / Whether the current byte is safe verbatim. */
        const bool alphanumeric =
            (byte >= static_cast<unsigned char>('a') && byte <= static_cast<unsigned char>('z')) ||
            (byte >= static_cast<unsigned char>('A') && byte <= static_cast<unsigned char>('Z')) ||
            (byte >= static_cast<unsigned char>('0') && byte <= static_cast<unsigned char>('9'));
        /** @brief 当前 byte 是否属于安全基础标点 / Whether the byte is safe base punctuation. */
        const bool base_punctuation = byte == static_cast<unsigned char>('.') ||
                                      byte == static_cast<unsigned char>('_') ||
                                      byte == static_cast<unsigned char>('-');
        /** @brief 当前 byte 是否属于允许的可打印 ASCII / Whether the byte is allowed printable
         * ASCII. */
        const bool printable = preserve_space && byte >= static_cast<unsigned char>(' ') &&
                               byte <= static_cast<unsigned char>('~') &&
                               byte != static_cast<unsigned char>('\x1b');
        if (alphanumeric || base_punctuation ||
            (preserve_slash && byte == static_cast<unsigned char>('/')) || printable) {
            rendered.push_back(static_cast<char>(byte));
            continue;
        }
        rendered.push_back('%');
        rendered.push_back(kDigits[(byte >> 4U) & 0x0fU]);
        rendered.push_back(kDigits[byte & 0x0fU]);
    }
    return rendered;
}

/**
 * @brief 取得 persistence 的稳定英文名 / Get the stable English name of persistence.
 * @param persistence workspace persistence / Workspace persistence.
 * @return 稳定 ASCII 名称 / Stable ASCII name.
 */
[[nodiscard]] std::string_view
persistence_name(const domain::WorkspacePersistence persistence) noexcept {
    switch (persistence) {
    case domain::WorkspacePersistence::absent:
        return "absent";
    case domain::WorkspacePersistence::ready:
        return "ready";
    }
    return "unknown";
}

/**
 * @brief 取得 activity 的稳定英文名 / Get the stable English name of activity.
 * @param activity runtime activity / Runtime activity.
 * @return 稳定 ASCII 名称 / Stable ASCII name.
 */
[[nodiscard]] std::string_view activity_name(const domain::WorkspaceActivity activity) noexcept {
    switch (activity) {
    case domain::WorkspaceActivity::inactive:
        return "inactive";
    case domain::WorkspaceActivity::activating:
        return "activating";
    case domain::WorkspaceActivity::ready:
        return "ready";
    case domain::WorkspaceActivity::executing:
        return "executing";
    case domain::WorkspaceActivity::retiring:
        return "retiring";
    case domain::WorkspaceActivity::failed:
        return "failed";
    }
    return "unknown";
}

/**
 * @brief 取得目录项类型的稳定短名 / Get the stable short name of a directory-entry kind.
 * @param kind 目录项类型 / Directory-entry kind.
 * @return 稳定 ASCII 名称 / Stable ASCII name.
 */
[[nodiscard]] std::string_view entry_kind_name(const domain::WorkspaceEntryKind kind) noexcept {
    switch (kind) {
    case domain::WorkspaceEntryKind::regular_file:
        return "file";
    case domain::WorkspaceEntryKind::directory:
        return "dir";
    case domain::WorkspaceEntryKind::symbolic_link:
        return "link";
    }
    return "unknown";
}

/**
 * @brief 取得目录项类型的稳定单字母名 / Get the stable one-letter name of a directory-entry kind.
 * @param kind 目录项类型 / Directory-entry kind.
 * @return 稳定单字母 ASCII / Stable one-letter ASCII.
 */
[[nodiscard]] char entry_kind_letter(const domain::WorkspaceEntryKind kind) noexcept {
    switch (kind) {
    case domain::WorkspaceEntryKind::regular_file:
        return 'f';
    case domain::WorkspaceEntryKind::directory:
        return 'd';
    case domain::WorkspaceEntryKind::symbolic_link:
        return 'l';
    }
    return '?';
}

/**
 * @brief 从状态推导 operator 最关心的综合健康度 / Derive operator-oriented aggregate health
 * from status.
 * @param status allowlisted 状态 / Allowlisted status.
 * @return 综合健康度 / Aggregate health.
 */
[[nodiscard]] DashboardHealth
dashboard_health(const domain::OperatorWorkspaceStatus& status) noexcept {
    if (status.activity() == domain::WorkspaceActivity::failed) {
        return DashboardHealth::failed;
    }
    if (status.quota().has_value() &&
        (status.quota()->used_bytes() > status.quota()->hard_bytes() ||
         status.quota()->used_inodes() > status.quota()->hard_inodes())) {
        return DashboardHealth::degraded;
    }
    if (status.activity() == domain::WorkspaceActivity::activating ||
        status.activity() == domain::WorkspaceActivity::retiring) {
        return DashboardHealth::transitioning;
    }
    if (status.persistence() == domain::WorkspacePersistence::absent) {
        return DashboardHealth::empty;
    }
    return DashboardHealth::healthy;
}

/**
 * @brief 取得综合健康度标签 / Get an aggregate-health label.
 * @param health 综合健康度 / Aggregate health.
 * @return 稳定大写标签 / Stable uppercase label.
 */
[[nodiscard]] std::string_view health_name(const DashboardHealth health) noexcept {
    switch (health) {
    case DashboardHealth::healthy:
        return "HEALTHY";
    case DashboardHealth::transitioning:
        return "TRANSITION";
    case DashboardHealth::empty:
        return "EMPTY";
    case DashboardHealth::failed:
        return "FAILED";
    case DashboardHealth::degraded:
        return "DEGRADED";
    }
    return "UNKNOWN";
}

/**
 * @brief 将综合健康度映射到语义色调 / Map aggregate health to a semantic tone.
 * @param health 综合健康度 / Aggregate health.
 * @return 对应色调 / Corresponding tone.
 */
[[nodiscard]] Tone health_tone(const DashboardHealth health) noexcept {
    switch (health) {
    case DashboardHealth::healthy:
        return Tone::healthy;
    case DashboardHealth::transitioning:
    case DashboardHealth::empty:
        return Tone::warning;
    case DashboardHealth::failed:
    case DashboardHealth::degraded:
        return Tone::danger;
    }
    return Tone::normal;
}

/**
 * @brief 用可选 ANSI SGR 装饰一段安全 ASCII / Decorate safe ASCII with optional ANSI SGR.
 * @param value 安全 ASCII / Safe ASCII.
 * @param tone 语义色调 / Semantic tone.
 * @param enabled 是否启用 ANSI / Whether ANSI is enabled.
 * @return 装饰后文本 / Decorated text.
 */
[[nodiscard]] std::string decorate(const std::string_view value, const Tone tone,
                                   const bool enabled) {
    if (!enabled || tone == Tone::normal) {
        return std::string(value);
    }
    /** @brief 当前语义色调的 SGR prefix / SGR prefix for the semantic tone. */
    std::string_view prefix;
    switch (tone) {
    case Tone::title:
        prefix = "\x1b[1;36m";
        break;
    case Tone::healthy:
        prefix = "\x1b[1;32m";
        break;
    case Tone::warning:
        prefix = "\x1b[1;33m";
        break;
    case Tone::danger:
        prefix = "\x1b[1;31m";
        break;
    case Tone::muted:
        prefix = "\x1b[2m";
        break;
    case Tone::data:
        prefix = "\x1b[1;34m";
        break;
    case Tone::normal:
        break;
    }
    return std::string(prefix) + std::string(value) + "\x1b[0m";
}

/**
 * @brief 将渲染宽度限制到安全区间 / Clamp render width to a safe interval.
 * @param columns 请求列数 / Requested columns.
 * @return 安全列数 / Safe column count.
 */
[[nodiscard]] std::size_t safe_columns(const std::size_t columns) noexcept {
    return std::clamp(columns, kMinimumColumns, kMaximumColumns);
}

/**
 * @brief 将文本截断到固定可见宽度 / Truncate text to a fixed visible width.
 * @param value 待截断 ASCII / ASCII to truncate.
 * @param width 最大可见宽度 / Maximum visible width.
 * @return 不超过宽度的文本 / Text no wider than the limit.
 */
[[nodiscard]] std::string truncate_ascii(const std::string_view value, const std::size_t width) {
    if (value.size() <= width) {
        return std::string(value);
    }
    if (width == 0U) {
        return {};
    }
    if (width == 1U) {
        return "~";
    }
    return std::string(value.substr(0U, width - 1U)) + "~";
}

/**
 * @brief 以单词边界将段落追加到输出 / Append a paragraph using word-boundary wrapping.
 * @param output 输出 buffer / Output buffer.
 * @param indent 首行与续行缩进 / First and continuation indentation.
 * @param text 待换行纯 ASCII / Plain ASCII to wrap.
 * @param columns 最大可见列数 / Maximum visible columns.
 * @param tone 正文语义色调 / Semantic tone for the body.
 * @param color 是否启用 ANSI / Whether ANSI is enabled.
 */
void append_wrapped(std::string& output, const std::string_view indent, const std::string_view text,
                    const std::size_t columns, const Tone tone = Tone::normal,
                    const bool color = false) {
    /** @brief 每行可用于正文的宽度 / Width available to paragraph text. */
    const std::size_t available = columns > indent.size() ? columns - indent.size() : 1U;
    /** @brief 当前扫描位置 / Current scan position. */
    std::size_t cursor{0U};
    while (cursor < text.size()) {
        /** @brief 当前行最多可取的 bytes / Maximum bytes for the current line. */
        const std::size_t remaining = text.size() - cursor;
        /** @brief 当前行初始长度 / Initial current-line length. */
        std::size_t length = std::min(available, remaining);
        if (length < remaining) {
            /** @brief 当前窗口内最后一个空格 / Last space in the current window. */
            const std::size_t break_at = text.rfind(' ', cursor + length);
            if (break_at != std::string_view::npos && break_at >= cursor) {
                length = break_at - cursor;
            }
        }
        if (length == 0U) {
            ++cursor;
            continue;
        }
        output.append(indent);
        output.append(decorate(text.substr(cursor, length), tone, color));
        output.push_back('\n');
        cursor += length;
        while (cursor < text.size() && text[cursor] == ' ') {
            ++cursor;
        }
    }
    if (text.empty()) {
        output.append(indent);
        output.push_back('\n');
    }
}

/**
 * @brief 按固定 byte 块追加不可含空格的值 / Append a no-space value in fixed byte chunks.
 * @param output 输出 buffer / Output buffer.
 * @param value 待切分安全 ASCII / Safe ASCII to split.
 * @param columns 最大可见列数 / Maximum visible columns.
 * @param tone 语义色调 / Semantic tone.
 * @param color 是否启用 ANSI / Whether ANSI is enabled.
 */
void append_chunked_value(std::string& output, const std::string_view value,
                          const std::size_t columns, const Tone tone, const bool color) {
    /** @brief 值行缩进 / Value-line indentation. */
    constexpr std::string_view kIndent{"  "};
    /** @brief 每行值宽度 / Per-line value width. */
    const std::size_t available = std::max<std::size_t>(1U, columns - kIndent.size());
    /** @brief 当前切分位置 / Current split position. */
    std::size_t cursor{0U};
    while (cursor < value.size()) {
        /** @brief 当前安全块 / Current safe chunk. */
        const std::string_view chunk = value.substr(cursor, available);
        output.append(kIndent);
        output.append(decorate(chunk, tone, color));
        output.push_back('\n');
        cursor += chunk.size();
    }
}

/**
 * @brief 格式化 IEC byte 数 / Format an IEC byte count.
 * @param bytes 原始 bytes / Raw bytes.
 * @return 紧凑人类可读值 / Compact human-readable value.
 */
[[nodiscard]] std::string format_bytes(const std::uint64_t bytes) {
    /** @brief IEC 单位表 / IEC unit table. */
    constexpr std::string_view kUnits[]{"B", "KiB", "MiB", "GiB", "TiB", "PiB"};
    /** @brief 当前缩放值 / Current scaled value. */
    long double value = static_cast<long double>(bytes);
    /** @brief 当前单位 index / Current unit index. */
    std::size_t unit{0U};
    while (value >= 1024.0L && unit + 1U < std::size(kUnits)) {
        value /= 1024.0L;
        ++unit;
    }
    /** @brief 格式化流 / Formatting stream. */
    std::ostringstream output;
    if (unit == 0U) {
        output << bytes << ' ' << kUnits[unit];
    } else {
        output << std::fixed << std::setprecision(value >= 100.0L ? 0 : 1)
               << static_cast<double>(value) << ' ' << kUnits[unit];
    }
    return output.str();
}

/**
 * @brief 格式化 quota 比例 / Format a quota ratio.
 * @param used 当前使用量 / Current usage.
 * @param hard hard limit / Hard limit.
 * @return 一位小数百分比 / Percentage with one decimal.
 */
[[nodiscard]] std::string format_percent(const std::uint64_t used, const std::uint64_t hard) {
    /** @brief 百分比值 / Percentage value. */
    const long double percentage =
        (static_cast<long double>(used) * 100.0L) / static_cast<long double>(hard);
    /** @brief 格式化流 / Formatting stream. */
    std::ostringstream output;
    output << std::fixed << std::setprecision(1) << static_cast<double>(percentage) << '%';
    return output.str();
}

/**
 * @brief 构造固定宽度 ASCII quota bar / Build a fixed-width ASCII quota bar.
 * @param used 当前使用量 / Current usage.
 * @param hard hard limit / Hard limit.
 * @param width bar 内部字符数 / Bar interior character count.
 * @return `[###...]` 形式 bar / Bar in `[###...]` form.
 */
[[nodiscard]] std::string quota_bar(const std::uint64_t used, const std::uint64_t hard,
                                    const std::size_t width) {
    /** @brief 限制到 100% 的比例 / Ratio clamped to 100%. */
    const long double ratio =
        std::min(1.0L, static_cast<long double>(used) / static_cast<long double>(hard));
    /** @brief 填充格数 / Filled cell count. */
    std::size_t filled = static_cast<std::size_t>(ratio * static_cast<long double>(width));
    if (used > 0U && filled == 0U) {
        filled = 1U;
    }
    /** @brief bar 文本 / Bar text. */
    std::string output{"["};
    output.append(filled, used > hard ? '!' : '#');
    output.append(width - filled, '.');
    output.push_back(']');
    return output;
}

/**
 * @brief 向固定宽度 box 追加边界 / Append a border to a fixed-width box.
 * @param output 输出 buffer / Output buffer.
 * @param columns box 总可见宽度 / Total visible box width.
 */
void append_box_border(std::string& output, const std::size_t columns) {
    output.push_back('+');
    output.append(columns - 2U, '-');
    output.append("+\n");
}

/**
 * @brief 向固定宽度 box 追加一行 / Append one row to a fixed-width box.
 * @param output 输出 buffer / Output buffer.
 * @param text 行正文 / Row body.
 * @param columns box 总可见宽度 / Total visible box width.
 * @param tone 语义色调 / Semantic tone.
 * @param color 是否启用 ANSI / Whether ANSI is enabled.
 */
void append_box_row(std::string& output, const std::string_view text, const std::size_t columns,
                    const Tone tone, const bool color) {
    /** @brief 正文可见容量 / Visible body capacity. */
    const std::size_t capacity = columns - 4U;
    /** @brief 截断后的正文 / Truncated body. */
    std::string body = truncate_ascii(text, capacity);
    body.append(capacity - body.size(), ' ');
    output.append("| ");
    output.append(decorate(body, tone, color));
    output.append(" |\n");
}

/**
 * @brief 渲染宽屏 dashboard / Render a wide dashboard.
 * @param status allowlisted 状态 / Allowlisted status.
 * @param options 渲染选项 / Render options.
 * @param columns 安全列宽 / Safe column width.
 * @return 宽屏 ASCII box / Wide ASCII box.
 */
[[nodiscard]] std::string render_wide_dashboard(const domain::OperatorWorkspaceStatus& status,
                                                const RenderOptions& options,
                                                const std::size_t columns) {
    /** @brief 综合健康度 / Aggregate health. */
    const DashboardHealth health = dashboard_health(status);
    /** @brief 输出页面 / Output page. */
    std::string output;
    append_box_border(output, columns);
    append_box_row(output, "WSPCTL  OPERATOR DASHBOARD", columns, Tone::title, options.color);
    append_box_row(output, options.watching ? "READ-ONLY WATCH" : "READ-ONLY SNAPSHOT", columns,
                   Tone::muted, options.color);
    append_box_border(output, columns);
    append_box_row(output, "Runtime    " + status.runtime().value(), columns, Tone::data,
                   options.color);
    append_box_row(output, "Health     " + std::string(health_name(health)), columns,
                   health_tone(health), options.color);
    append_box_row(output, "Activity   " + std::string(activity_name(status.activity())), columns,
                   health_tone(health), options.color);
    append_box_row(
        output, "Workspace  " + std::string(persistence_name(status.persistence())), columns,
        status.persistence() == domain::WorkspacePersistence::ready ? Tone::healthy : Tone::warning,
        options.color);
    append_box_border(output, columns);
    if (status.quota().has_value()) {
        /** @brief quota 快照 / Quota snapshot. */
        const domain::WorkspaceQuotaUsage& quota = *status.quota();
        /** @brief 宽屏 bar 内部宽度 / Wide bar interior width. */
        const std::size_t bar_width = std::min<std::size_t>(36U, columns - 18U);
        append_box_row(output,
                       "Storage    " + format_bytes(quota.used_bytes()) + " / " +
                           format_bytes(quota.hard_bytes()) + "  " +
                           format_percent(quota.used_bytes(), quota.hard_bytes()),
                       columns, quota.used_bytes() > quota.hard_bytes() ? Tone::danger : Tone::data,
                       options.color);
        append_box_row(
            output, "           " + quota_bar(quota.used_bytes(), quota.hard_bytes(), bar_width),
            columns, quota.used_bytes() > quota.hard_bytes() ? Tone::danger : Tone::healthy,
            options.color);
        append_box_row(output,
                       "Inodes     " + std::to_string(quota.used_inodes()) + " / " +
                           std::to_string(quota.hard_inodes()) + "  " +
                           format_percent(quota.used_inodes(), quota.hard_inodes()),
                       columns,
                       quota.used_inodes() > quota.hard_inodes() ? Tone::danger : Tone::data,
                       options.color);
        append_box_row(
            output, "           " + quota_bar(quota.used_inodes(), quota.hard_inodes(), bar_width),
            columns, quota.used_inodes() > quota.hard_inodes() ? Tone::danger : Tone::healthy,
            options.color);
    } else {
        append_box_row(output, "Quota      unavailable (workspace has not been created)", columns,
                       Tone::warning, options.color);
    }
    append_box_border(output, columns);
    append_box_row(output, "Next  workspace ls --runtime " + status.runtime().value(), columns,
                   Tone::muted, options.color);
    append_box_row(output,
                   options.watching ? "Refresh    Ctrl-C to stop"
                                    : "Refresh    add --watch [--refresh SECONDS]",
                   columns, Tone::muted, options.color);
    append_box_border(output, columns);
    return output;
}

/**
 * @brief 渲染窄屏 dashboard / Render a narrow dashboard.
 * @param status allowlisted 状态 / Allowlisted status.
 * @param options 渲染选项 / Render options.
 * @param columns 安全列宽 / Safe column width.
 * @return 窄屏 ASCII 页面 / Narrow ASCII page.
 */
[[nodiscard]] std::string render_compact_dashboard(const domain::OperatorWorkspaceStatus& status,
                                                   const RenderOptions& options,
                                                   const std::size_t columns) {
    /** @brief 综合健康度 / Aggregate health. */
    const DashboardHealth health = dashboard_health(status);
    /** @brief 输出页面 / Output page. */
    std::string output;
    output.append(
        decorate(truncate_ascii("WSPCTL DASHBOARD", columns), Tone::title, options.color));
    output.push_back('\n');
    output.append(columns, '=');
    output.push_back('\n');
    append_wrapped(output, "", options.watching ? "READ-ONLY WATCH" : "READ-ONLY SNAPSHOT", columns,
                   Tone::muted, options.color);
    output.append("\nRUNTIME\n");
    append_chunked_value(output, status.runtime().value(), columns, Tone::data, options.color);
    output.append("\nSTATE\n");
    append_wrapped(output, "  ", "health=" + std::string(health_name(health)), columns,
                   health_tone(health), options.color);
    append_wrapped(output, "  ", "activity=" + std::string(activity_name(status.activity())),
                   columns, health_tone(health), options.color);
    append_wrapped(
        output, "  ", "workspace=" + std::string(persistence_name(status.persistence())), columns,
        status.persistence() == domain::WorkspacePersistence::ready ? Tone::healthy : Tone::warning,
        options.color);
    output.append("\nQUOTA\n");
    if (status.quota().has_value()) {
        /** @brief quota 快照 / Quota snapshot. */
        const domain::WorkspaceQuotaUsage& quota = *status.quota();
        /** @brief 窄屏 bar 内部宽度 / Compact bar interior width. */
        const std::size_t bar_width = std::min<std::size_t>(30U, columns - 4U);
        append_wrapped(output, "  ",
                       "bytes " + format_bytes(quota.used_bytes()) + " / " +
                           format_bytes(quota.hard_bytes()) + " (" +
                           format_percent(quota.used_bytes(), quota.hard_bytes()) + ")",
                       columns, quota.used_bytes() > quota.hard_bytes() ? Tone::danger : Tone::data,
                       options.color);
        append_wrapped(
            output, "  ", quota_bar(quota.used_bytes(), quota.hard_bytes(), bar_width), columns,
            quota.used_bytes() > quota.hard_bytes() ? Tone::danger : Tone::healthy, options.color);
        append_wrapped(output, "  ",
                       "inodes " + std::to_string(quota.used_inodes()) + " / " +
                           std::to_string(quota.hard_inodes()) + " (" +
                           format_percent(quota.used_inodes(), quota.hard_inodes()) + ")",
                       columns,
                       quota.used_inodes() > quota.hard_inodes() ? Tone::danger : Tone::data,
                       options.color);
        append_wrapped(output, "  ", quota_bar(quota.used_inodes(), quota.hard_inodes(), bar_width),
                       columns,
                       quota.used_inodes() > quota.hard_inodes() ? Tone::danger : Tone::healthy,
                       options.color);
    } else {
        append_wrapped(output, "  ", "unavailable: workspace has not been created", columns,
                       Tone::warning, options.color);
    }
    output.append("\nNEXT\n");
    append_wrapped(output, "  ", "workspace ls --runtime", columns);
    append_chunked_value(output, status.runtime().value(), columns, Tone::muted, options.color);
    append_wrapped(
        output, "  ",
        options.watching ? "Ctrl-C to stop" : "add --watch [--refresh SECONDS] to follow", columns);
    return output;
}

/**
 * @brief 渲染 workspace listing 的窄屏条目 / Render compact workspace-listing entries.
 * @param output 输出 buffer / Output buffer.
 * @param listing 有界 listing / Bounded listing.
 * @param options 渲染选项 / Render options.
 * @param columns 安全列宽 / Safe column width.
 */
void append_compact_entries(std::string& output, const domain::WorkspaceListing& listing,
                            const RenderOptions& options, const std::size_t columns) {
    for (const domain::WorkspaceEntry& entry : listing.entries) {
        /** @brief 类型和名称前缀 / Kind-and-name prefix. */
        const std::string prefix = std::string(entry_kind_name(entry.kind())) + " ";
        /** @brief 名称首行容量 / First-line name capacity. */
        const std::size_t name_width =
            columns > prefix.size() ? columns - prefix.size() : std::size_t{1U};
        /** @brief 名称首段 / First name chunk. */
        const std::string first = truncate_ascii(entry.encoded_name(), name_width);
        output.append(decorate(prefix, Tone::muted, options.color));
        output.append(decorate(first, Tone::data, options.color));
        output.push_back('\n');
        if (entry.kind() == domain::WorkspaceEntryKind::regular_file) {
            append_wrapped(output, "  ", "size=" + format_bytes(entry.size_bytes()), columns);
        }
    }
}

/**
 * @brief 解析 status 的局部 option / Parse status-local options.
 * @param arguments 完整参数 view / Complete argument view.
 * @param index status option 起点 / Start of status options.
 * @param socket_path 已验证 endpoint / Validated endpoint.
 * @param color 用户色彩策略 / User color policy.
 * @return 完整 status 调用或用法错误 / Complete status invocation or a usage error.
 */
[[nodiscard]] ParseResult parse_status_command(const std::span<const std::string_view> arguments,
                                               std::size_t index, std::string socket_path,
                                               const ColorMode color) {
    /** @brief 原始 runtime option / Raw runtime option. */
    std::optional<std::string_view> runtime_text;
    while (index < arguments.size()) {
        if (arguments[index] != "--runtime" || runtime_text.has_value() ||
            index + 1U >= arguments.size()) {
            return parse_failure("status requires exactly one --runtime RUNTIME_UUID");
        }
        runtime_text = arguments[index + 1U];
        index += 2U;
    }
    if (!runtime_text.has_value()) {
        return parse_failure("status requires --runtime RUNTIME_UUID");
    }
    /** @brief 已验证 runtime / Validated runtime. */
    auto runtime = domain::RuntimeId::parse(std::string(*runtime_text));
    if (!runtime) {
        return parse_failure("--runtime must be a canonical lowercase UUID");
    }
    return Invocation{
        .socket_path = std::move(socket_path),
        .color = color,
        .command = StatusCommand{.runtime = std::move(*runtime)},
    };
}

/**
 * @brief 解析 workspace ls 的局部 option / Parse workspace-ls local options.
 * @param arguments 完整参数 view / Complete argument view.
 * @param index workspace 子命令起点 / Start of workspace subcommand.
 * @param socket_path 已验证 endpoint / Validated endpoint.
 * @param color 用户色彩策略 / User color policy.
 * @return 完整 listing 调用或用法错误 / Complete listing invocation or a usage error.
 */
[[nodiscard]] ParseResult parse_workspace_command(const std::span<const std::string_view> arguments,
                                                  std::size_t index, std::string socket_path,
                                                  const ColorMode color) {
    if (index >= arguments.size() || arguments[index] != "ls") {
        return parse_failure("workspace currently supports only: workspace ls");
    }
    ++index;
    /** @brief 原始 runtime option / Raw runtime option. */
    std::optional<std::string_view> runtime_text;
    /** @brief 原始 workspace path，默认根 / Raw workspace path, defaulting to its root. */
    std::string_view path_text{"/workspace"};
    /** @brief 是否已经出现 --path / Whether --path has already appeared. */
    bool seen_path{false};
    while (index < arguments.size()) {
        if (arguments[index] == "--runtime" && !runtime_text.has_value() &&
            index + 1U < arguments.size()) {
            runtime_text = arguments[index + 1U];
            index += 2U;
            continue;
        }
        if (arguments[index] == "--path" && !seen_path && index + 1U < arguments.size()) {
            path_text = arguments[index + 1U];
            seen_path = true;
            index += 2U;
            continue;
        }
        return parse_failure("workspace ls accepts one --runtime and at most one --path");
    }
    if (!runtime_text.has_value()) {
        return parse_failure("workspace ls requires --runtime RUNTIME_UUID");
    }
    /** @brief 已验证 runtime / Validated runtime. */
    auto runtime = domain::RuntimeId::parse(std::string(*runtime_text));
    /** @brief 已验证 workspace path / Validated workspace path. */
    auto path = domain::OperatorWorkspacePath::parse(std::string(path_text));
    if (!runtime) {
        return parse_failure("--runtime must be a canonical lowercase UUID");
    }
    if (!path) {
        return parse_failure("--path must be canonical and remain inside /workspace");
    }
    return Invocation{
        .socket_path = std::move(socket_path),
        .color = color,
        .command =
            WorkspaceListCommand{
                .runtime = std::move(*runtime),
                .path = std::move(*path),
            },
    };
}

/**
 * @brief 解析 dashboard 的局部 option / Parse dashboard-local options.
 * @param arguments 完整参数 view / Complete argument view.
 * @param index dashboard option 起点 / Start of dashboard options.
 * @param socket_path 已验证 endpoint / Validated endpoint.
 * @param color 用户色彩策略 / User color policy.
 * @return 完整 dashboard 调用或用法错误 / Complete dashboard invocation or a usage error.
 */
[[nodiscard]] ParseResult parse_dashboard_command(const std::span<const std::string_view> arguments,
                                                  std::size_t index, std::string socket_path,
                                                  const ColorMode color) {
    /** @brief 原始 runtime option / Raw runtime option. */
    std::optional<std::string_view> runtime_text;
    /** @brief 是否启用 watch / Whether watch is enabled. */
    bool watch{false};
    /** @brief 是否已经出现 --watch / Whether --watch has already appeared. */
    bool seen_watch{false};
    /** @brief 刷新周期 / Refresh period. */
    std::chrono::seconds refresh{kDefaultRefreshInterval};
    /** @brief 是否已经出现 --refresh / Whether --refresh has already appeared. */
    bool seen_refresh{false};
    while (index < arguments.size()) {
        if (arguments[index] == "--runtime" && !runtime_text.has_value() &&
            index + 1U < arguments.size()) {
            runtime_text = arguments[index + 1U];
            index += 2U;
            continue;
        }
        if (arguments[index] == "--watch" && !seen_watch) {
            watch = true;
            seen_watch = true;
            ++index;
            continue;
        }
        if (arguments[index] == "--refresh" && !seen_refresh && index + 1U < arguments.size()) {
            /** @brief 已解析刷新周期 / Parsed refresh period. */
            const auto parsed = parse_refresh_interval(arguments[index + 1U]);
            if (!parsed.has_value()) {
                return parse_failure("--refresh must be an integer from 1 to 3600 seconds");
            }
            refresh = *parsed;
            seen_refresh = true;
            index += 2U;
            continue;
        }
        return parse_failure("dashboard accepts one --runtime, --watch, and --refresh SECONDS");
    }
    if (!runtime_text.has_value()) {
        return parse_failure("dashboard requires --runtime RUNTIME_UUID");
    }
    if (seen_refresh && !watch) {
        return parse_failure("--refresh is meaningful only together with --watch");
    }
    /** @brief 已验证 runtime / Validated runtime. */
    auto runtime = domain::RuntimeId::parse(std::string(*runtime_text));
    if (!runtime) {
        return parse_failure("--runtime must be a canonical lowercase UUID");
    }
    return Invocation{
        .socket_path = std::move(socket_path),
        .color = color,
        .command =
            DashboardCommand{
                .runtime = std::move(*runtime),
                .watch = watch,
                .refresh = refresh,
            },
    };
}

} // namespace

bool TerminalProfile::use_color(const ColorMode mode) const noexcept {
    switch (mode) {
    case ColorMode::automatic:
        return is_tty && !no_color && !dumb_terminal;
    case ColorMode::always:
        return true;
    case ColorMode::never:
        return false;
    }
    return false;
}

TerminalProfile detect_terminal_profile(const int descriptor) noexcept {
    /** @brief ioctl 返回的终端尺寸 / Terminal dimensions returned by ioctl. */
    winsize dimensions{};
    /** @brief 非零且可信的列数 / Non-zero trustworthy column count. */
    const std::size_t columns =
        ioctl(descriptor, TIOCGWINSZ, &dimensions) == 0 && dimensions.ws_col > 0U
            ? static_cast<std::size_t>(dimensions.ws_col)
            : 80U;
    /** @brief `NO_COLOR` 原始环境值 / Raw `NO_COLOR` environment value. */
    const char* const no_color = std::getenv("NO_COLOR");
    /** @brief `TERM` 原始环境值 / Raw `TERM` environment value. */
    const char* const term = std::getenv("TERM");
    return TerminalProfile{
        .is_tty = isatty(descriptor) == 1,
        .no_color = no_color != nullptr && no_color[0] != '\0',
        .dumb_terminal = term != nullptr && std::string_view(term) == "dumb",
        .columns = columns,
    };
}

ParseResult parse_arguments(const std::span<const std::string_view> arguments,
                            const std::string_view default_socket) {
    /** @brief 当前参数位置 / Current argument position. */
    std::size_t index{0U};
    /** @brief operator endpoint / Operator endpoint. */
    std::string socket_path{default_socket};
    /** @brief 用户色彩策略 / User color policy. */
    ColorMode color{ColorMode::automatic};
    /** @brief 是否已经出现 --socket / Whether --socket has already appeared. */
    bool seen_socket{false};
    /** @brief 是否已经出现 --color / Whether --color has already appeared. */
    bool seen_color{false};

    while (index < arguments.size()) {
        /** @brief 当前全局参数 / Current global argument. */
        const std::string_view argument = arguments[index];
        if (is_help(argument)) {
            return Invocation{
                .socket_path = std::move(socket_path),
                .color = color,
                .command = HelpCommand{},
            };
        }
        if (argument == "--socket") {
            if (seen_socket || index + 1U >= arguments.size()) {
                return parse_failure("--socket requires one unique absolute endpoint");
            }
            socket_path = arguments[index + 1U];
            seen_socket = true;
            index += 2U;
            continue;
        }
        if (argument.starts_with("--socket=")) {
            if (seen_socket) {
                return parse_failure("--socket may be specified only once");
            }
            socket_path = argument.substr(std::string_view("--socket=").size());
            seen_socket = true;
            ++index;
            continue;
        }
        if (argument == "--color") {
            if (seen_color || index + 1U >= arguments.size()) {
                return parse_failure("--color requires one of: auto, always, never");
            }
            /** @brief 已解析色彩 option / Parsed color option. */
            const auto parsed = parse_color_mode(arguments[index + 1U]);
            if (!parsed.has_value()) {
                return parse_failure("--color requires one of: auto, always, never");
            }
            color = *parsed;
            seen_color = true;
            index += 2U;
            continue;
        }
        if (argument == "--plain") {
            if (seen_color) {
                return parse_failure("--plain conflicts with --color");
            }
            color = ColorMode::never;
            seen_color = true;
            ++index;
            continue;
        }
        if (argument.starts_with("--color=")) {
            if (seen_color) {
                return parse_failure("--color may be specified only once");
            }
            /** @brief 已解析色彩 option / Parsed color option. */
            const auto parsed =
                parse_color_mode(argument.substr(std::string_view("--color=").size()));
            if (!parsed.has_value()) {
                return parse_failure("--color requires one of: auto, always, never");
            }
            color = *parsed;
            seen_color = true;
            ++index;
            continue;
        }
        break;
    }

    if (index >= arguments.size()) {
        return parse_failure("a command is required");
    }
    /** @brief 顶层命令名 / Top-level command name. */
    const std::string_view command = arguments[index++];
    if (is_help(command)) {
        return Invocation{
            .socket_path = std::move(socket_path),
            .color = color,
            .command = HelpCommand{},
        };
    }
    for (std::size_t help_index = index; help_index < arguments.size(); ++help_index) {
        if (is_help(arguments[help_index])) {
            return Invocation{
                .socket_path = std::move(socket_path),
                .color = color,
                .command = HelpCommand{},
            };
        }
    }
    if (socket_path.empty()) {
        return parse_failure("no operator endpoint is configured; pass --socket ABSOLUTE_SOCKET");
    }
    if (!OperatorGatewayClient::validate_socket_path(socket_path)) {
        return parse_failure("--socket must be an absolute AF_UNIX endpoint");
    }

    if (command == "status") {
        return parse_status_command(arguments, index, std::move(socket_path), color);
    }
    if (command == "workspace") {
        return parse_workspace_command(arguments, index, std::move(socket_path), color);
    }
    if (command == "dashboard") {
        return parse_dashboard_command(arguments, index, std::move(socket_path), color);
    }
    return parse_failure("unknown command: " + terminal_safe_text(command, false, false));
}

std::string render_help(const RenderOptions& options) {
    /** @brief 安全列宽 / Safe column width. */
    const std::size_t columns = safe_columns(options.columns);
    /** @brief 输出页面 / Output page. */
    std::string output;
    if (options.interactive && options.color && columns >= 64U) {
        output.append(decorate(" __        ______  ____   ____ _____ _     \n"
                               " \\ \\      / / ___||  _ \\ / ___|_   _| |    \n"
                               "  \\ \\ /\\ / /\\___ \\| |_) | |     | | | |    \n"
                               "   \\ V  V /  ___) |  __/| |___  | | | |___ \n"
                               "    \\_/\\_/  |____/|_|    \\____| |_| |_____|\n",
                               Tone::title, options.color));
    }
    append_wrapped(output, "", "WSPCTL / read-only operator console", columns, Tone::title,
                   options.color);
    output.append("\nUSAGE\n");
    append_wrapped(output, "  ",
                   "wspctl [--socket ABSOLUTE_SOCKET] [--color auto|always|never] [--plain] "
                   "status --runtime RUNTIME_UUID",
                   columns);
    append_wrapped(output, "  ",
                   "wspctl [GLOBAL OPTIONS] dashboard --runtime RUNTIME_UUID "
                   "[--watch [--refresh SECONDS]]",
                   columns);
    append_wrapped(output, "  ",
                   "wspctl [GLOBAL OPTIONS] workspace ls --runtime RUNTIME_UUID "
                   "[--path /workspace/PATH]",
                   columns);
    output.append("\nCOMMANDS\n");
    append_wrapped(output, "  ", "status       Stable one-shot status; pipe-friendly off TTY.",
                   columns);
    append_wrapped(output, "  ", "dashboard    Human-first runtime, lifecycle, and quota snapshot.",
                   columns);
    append_wrapped(output, "  ",
                   "workspace ls Inspect one level of the persistent OverlayFS upper layer.",
                   columns);
    output.append("\nOUTPUT\n");
    append_wrapped(output, "  ",
                   "Color is automatic on a capable TTY. NO_COLOR, TERM=dumb, non-TTY output, "
                   "or --color never/--plain produce plain text. --color always is an explicit "
                   "override.",
                   columns);
    append_wrapped(output, "  ",
                   "Dashboard defaults to one snapshot. --watch requires a TTY and Ctrl-C stops "
                   "it; --refresh accepts 1..3600 seconds.",
                   columns);
    output.append("\nEXIT STATUS\n");
    append_wrapped(output, "  ",
                   "0 success; 64 usage; 66 not found; 69 unavailable; 70 software/protocol; "
                   "77 permission; 130 interrupted.",
                   columns);
    output.append("\nSAFETY\n");
    append_wrapped(output, "  ",
                   "This client never starts a runtime, executes commands, writes workspace data, "
                   "or reads file contents. Root-owned deployments may require sudo.",
                   columns);
    return output;
}

std::string render_status_record(const domain::OperatorWorkspaceStatus& status) {
    /** @brief 输出记录 / Output record. */
    std::ostringstream output;
    output << "runtime=" << status.runtime().value() << '\n';
    output << "persistence=" << persistence_name(status.persistence()) << '\n';
    output << "activity=" << activity_name(status.activity()) << '\n';
    if (!status.quota().has_value()) {
        output << "quota_used_bytes=-\n"
                  "quota_hard_bytes=-\n"
                  "quota_used_inodes=-\n"
                  "quota_hard_inodes=-\n";
        return output.str();
    }
    output << "quota_used_bytes=" << status.quota()->used_bytes() << '\n';
    output << "quota_hard_bytes=" << status.quota()->hard_bytes() << '\n';
    output << "quota_used_inodes=" << status.quota()->used_inodes() << '\n';
    output << "quota_hard_inodes=" << status.quota()->hard_inodes() << '\n';
    return output.str();
}

std::string render_dashboard(const domain::OperatorWorkspaceStatus& status,
                             const RenderOptions& options) {
    /** @brief 安全列宽 / Safe column width. */
    const std::size_t columns = safe_columns(options.columns);
    if (columns >= kWideDashboardColumns) {
        return render_wide_dashboard(status, options, columns);
    }
    return render_compact_dashboard(status, options, columns);
}

std::string render_listing_record(const domain::WorkspaceListing& listing) {
    /** @brief 输出记录 / Output record. */
    std::ostringstream output;
    output << "path=" << terminal_safe_text(listing.path.value(), true, false) << '\n';
    output << "truncated=" << (listing.truncated ? "true" : "false") << '\n';
    output << "kind\tsize_bytes\tencoded_name\n";
    for (const domain::WorkspaceEntry& entry : listing.entries) {
        output << entry_kind_letter(entry.kind()) << '\t' << entry.size_bytes() << '\t'
               << entry.encoded_name() << '\n';
    }
    return output.str();
}

std::string render_listing_page(const domain::WorkspaceListing& listing,
                                const RenderOptions& options) {
    /** @brief 安全列宽 / Safe column width. */
    const std::size_t columns = safe_columns(options.columns);
    /** @brief 输出页面 / Output page. */
    std::string output;
    append_wrapped(output, "", "WORKSPACE / PERSISTENT UPPER LAYER", columns, Tone::title,
                   options.color);
    append_wrapped(output, "", "Path: " + terminal_safe_text(listing.path.value(), true, false),
                   columns);
    append_wrapped(output, "",
                   listing.truncated ? "Result: truncated at safety limit"
                                     : "Result: complete one-level listing",
                   columns);
    output.push_back('\n');
    if (listing.entries.empty()) {
        append_wrapped(output, "", "No entries in this persistent upper-layer directory.", columns);
        return output;
    }
    if (columns < 58U) {
        append_compact_entries(output, listing, options, columns);
    } else {
        /** @brief type 列宽 / Type-column width. */
        constexpr std::size_t kTypeWidth{6U};
        /** @brief size 列宽 / Size-column width. */
        constexpr std::size_t kSizeWidth{12U};
        /** @brief name 列宽 / Name-column width. */
        const std::size_t name_width = columns - kTypeWidth - kSizeWidth - 2U;
        /** @brief 表头 / Table header. */
        std::string header = "TYPE";
        header.append(kTypeWidth - header.size(), ' ');
        header.append("SIZE");
        header.append(kSizeWidth - std::string_view("SIZE").size(), ' ');
        header.append("NAME");
        output.append(decorate(header, Tone::muted, options.color));
        output.push_back('\n');
        output.append(columns, '-');
        output.push_back('\n');
        for (const domain::WorkspaceEntry& entry : listing.entries) {
            /** @brief 类型 cell / Kind cell. */
            std::string kind{entry_kind_name(entry.kind())};
            kind.append(kTypeWidth - kind.size(), ' ');
            /** @brief 大小 cell / Size cell. */
            std::string size = entry.kind() == domain::WorkspaceEntryKind::regular_file
                                   ? format_bytes(entry.size_bytes())
                                   : "-";
            size.append(kSizeWidth - size.size(), ' ');
            output.append(decorate(kind, Tone::muted, options.color));
            output.append(size);
            output.append(truncate_ascii(entry.encoded_name(), name_width));
            output.push_back('\n');
        }
    }
    if (listing.truncated) {
        output.push_back('\n');
        append_wrapped(output, "", "More entries exist; narrow the --path and query again.",
                       columns);
    }
    return output;
}

ExitCode exit_code_for(const Error& error) noexcept {
    switch (error.code) {
    case ErrorCode::invalid_argument:
        return ExitCode::usage;
    case ErrorCode::not_found:
        return ExitCode::not_found;
    case ErrorCode::authentication_failed:
    case ErrorCode::permission_denied:
        return ExitCode::permission;
    case ErrorCode::busy:
    case ErrorCode::timeout:
    case ErrorCode::io_failure:
        return ExitCode::unavailable;
    case ErrorCode::malformed_frame:
    case ErrorCode::frame_too_large:
    case ErrorCode::unsupported_version:
    case ErrorCode::protocol_violation:
    case ErrorCode::sandbox_preflight_failed:
    case ErrorCode::already_exists:
    case ErrorCode::journal_conflict:
    case ErrorCode::invocation_in_doubt:
    case ErrorCode::child_failure:
    case ErrorCode::internal:
        return ExitCode::software;
    }
    return ExitCode::software;
}

std::string render_failure(const std::string_view action, const Error& error) {
    /** @brief terminal-safe action / Terminal-safe action. */
    const std::string safe_action = terminal_safe_text(action, false, true);
    /** @brief terminal-safe native diagnostic / Terminal-safe native diagnostic. */
    const std::string safe_message = terminal_safe_text(error.message, true, true);
    /** @brief 输出诊断 / Output diagnostic. */
    std::string output{"wspctl: "};
    output.append(safe_action);
    output.append(" failed: ");
    output.append(safe_message);
    output.push_back('\n');
    switch (exit_code_for(error)) {
    case ExitCode::not_found:
        output.append(
            "hint: verify the runtime UUID and whether its persistent workspace exists\n");
        break;
    case ExitCode::permission:
        output.append(
            "hint: use an authorized operator UID; root-owned deployments commonly require sudo\n");
        break;
    case ExitCode::unavailable:
        output.append(
            "hint: verify wspctld is running and the configured operator socket is reachable\n");
        break;
    case ExitCode::software:
        output.append("hint: check wspctld and client versions, then inspect service logs\n");
        break;
    case ExitCode::usage:
        output.append("hint: run wspctl --help for the accepted command shape\n");
        break;
    case ExitCode::success:
    case ExitCode::interrupted:
        break;
    }
    return output;
}

} // namespace wspctl::presentation::operator_cli
