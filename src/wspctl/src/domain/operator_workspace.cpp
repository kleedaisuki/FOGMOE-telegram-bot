#include "wspctl/domain/operator_workspace.hpp"

#include <algorithm>
#include <utility>

namespace wspctl::domain {
namespace {

/** @brief workspace 路径的唯一允许根 / Sole allowed root for an operator workspace path. */
constexpr std::string_view kWorkspaceRoot{"/workspace"};
/** @brief runtime 内逻辑路径的最大 byte 数 / Maximum byte count of one runtime logical path. */
constexpr std::size_t kMaximumWorkspacePathBytes{4096U};
/** @brief POSIX 单一 filename 的最大 byte 数 / Maximum byte count of one POSIX filename. */
constexpr std::size_t kMaximumFilenameBytes{255U};

/**
 * @brief 判断安全显示名的保留 ASCII byte / Check an ASCII byte preserved in a safe display name.
 * @param byte 待检查 byte / Byte to inspect.
 * @return 是否可直接显示 / Whether it may be displayed directly.
 */
[[nodiscard]] bool is_display_safe_byte(const unsigned char byte) noexcept {
    return (byte >= static_cast<unsigned char>('a') && byte <= static_cast<unsigned char>('z')) ||
           (byte >= static_cast<unsigned char>('A') && byte <= static_cast<unsigned char>('Z')) ||
           (byte >= static_cast<unsigned char>('0') && byte <= static_cast<unsigned char>('9')) ||
           byte == static_cast<unsigned char>('.') || byte == static_cast<unsigned char>('_') ||
           byte == static_cast<unsigned char>('-');
}

/**
 * @brief 判断大写十六进制 digit / Check an uppercase hexadecimal digit.
 * @param byte 待检查 byte / Byte to inspect.
 * @return 是否为大写十六进制 digit / Whether it is an uppercase hexadecimal digit.
 */
[[nodiscard]] bool is_upper_hex_digit(const unsigned char byte) noexcept {
    return (byte >= static_cast<unsigned char>('0') && byte <= static_cast<unsigned char>('9')) ||
           (byte >= static_cast<unsigned char>('A') && byte <= static_cast<unsigned char>('F'));
}

/**
 * @brief 将一个大写十六进制字符转成 nibble / Convert one uppercase hexadecimal character into a nibble.
 * @param byte 待转换字符 / Character to convert.
 * @return 0..15 的 nibble / Nibble in the range 0..15.
 */
[[nodiscard]] unsigned char upper_hex_value(const unsigned char byte) noexcept {
    return byte >= static_cast<unsigned char>('0') && byte <= static_cast<unsigned char>('9')
        ? static_cast<unsigned char>(byte - static_cast<unsigned char>('0'))
        : static_cast<unsigned char>(byte - static_cast<unsigned char>('A') + 10U);
}

/**
 * @brief 判断安全显示名是否严格为 percent encoding / Check whether a safe display name is strict percent encoding.
 * @param encoded_name 待验证显示名 / Display name to validate.
 * @return 是否是固定、可逆编码 / Whether it is the fixed reversible encoding.
 */
[[nodiscard]] bool is_strict_encoded_name(const std::string_view encoded_name) noexcept {
    if (encoded_name.empty() || encoded_name.size() > kMaximumFilenameBytes * 3U) {
        return false;
    }
    for (std::size_t offset = 0U; offset < encoded_name.size();) {
        const unsigned char byte = static_cast<unsigned char>(encoded_name[offset]);
        if (is_display_safe_byte(byte)) {
            ++offset;
            continue;
        }
        if (byte != static_cast<unsigned char>('%') || offset + 2U >= encoded_name.size() ||
            !is_upper_hex_digit(static_cast<unsigned char>(encoded_name[offset + 1U])) ||
            !is_upper_hex_digit(static_cast<unsigned char>(encoded_name[offset + 2U]))) {
            return false;
        }
        /** @brief 被 percent encoding 表示的原始 byte / Raw byte represented by percent encoding. */
        const unsigned char decoded = static_cast<unsigned char>(
            (upper_hex_value(static_cast<unsigned char>(encoded_name[offset + 1U])) << 4U) |
            upper_hex_value(static_cast<unsigned char>(encoded_name[offset + 2U])));
        if (is_display_safe_byte(decoded)) {
            return false;
        }
        offset += 3U;
    }
    return true;
}

/**
 * @brief 将一个 nibble 编码为大写十六进制 / Encode one nibble as uppercase hexadecimal.
 * @param value nibble 值 / Nibble value.
 * @return 大写十六进制字符 / Uppercase hexadecimal character.
 */
[[nodiscard]] char upper_hex_digit(const unsigned char value) noexcept {
    constexpr std::string_view kDigits{"0123456789ABCDEF"};
    return kDigits[value & 0x0fU];
}

/**
 * @brief 判断 workspace persistence 是否为领域词表成员 / Check whether a workspace-persistence value belongs to the domain vocabulary.
 * @param persistence 待检查 persistence / Persistence to inspect.
 * @return 是否为已知 persistence / Whether it is known.
 */
[[nodiscard]] bool is_known_workspace_persistence(const WorkspacePersistence persistence) noexcept {
    switch (persistence) {
        case WorkspacePersistence::absent:
        case WorkspacePersistence::ready:
            return true;
    }
    return false;
}

/**
 * @brief 判断 workspace activity 是否为领域词表成员 / Check whether a workspace-activity value belongs to the domain vocabulary.
 * @param activity 待检查 activity / Activity to inspect.
 * @return 是否为已知 activity / Whether it is known.
 */
[[nodiscard]] bool is_known_workspace_activity(const WorkspaceActivity activity) noexcept {
    switch (activity) {
        case WorkspaceActivity::inactive:
        case WorkspaceActivity::activating:
        case WorkspaceActivity::ready:
        case WorkspaceActivity::executing:
        case WorkspaceActivity::retiring:
        case WorkspaceActivity::failed:
            return true;
    }
    return false;
}

}  // namespace

WorkspaceQuotaUsage::WorkspaceQuotaUsage(
    const std::uint64_t used_bytes,
    const std::uint64_t hard_bytes,
    const std::uint64_t used_inodes,
    const std::uint64_t hard_inodes) noexcept
    : used_bytes_(used_bytes),
      hard_bytes_(hard_bytes),
      used_inodes_(used_inodes),
      hard_inodes_(hard_inodes) {}

Result<WorkspaceQuotaUsage> WorkspaceQuotaUsage::create(
    const std::uint64_t used_bytes,
    const std::uint64_t hard_bytes,
    const std::uint64_t used_inodes,
    const std::uint64_t hard_inodes) {
    if (hard_bytes == 0U || hard_inodes == 0U) {
        return std::unexpected(make_error(
            ErrorCode::invalid_budget,
            "operator workspace quota hard limits must be non-zero"));
    }
    return WorkspaceQuotaUsage(used_bytes, hard_bytes, used_inodes, hard_inodes);
}

std::uint64_t WorkspaceQuotaUsage::used_bytes() const noexcept {
    return used_bytes_;
}

std::uint64_t WorkspaceQuotaUsage::hard_bytes() const noexcept {
    return hard_bytes_;
}

std::uint64_t WorkspaceQuotaUsage::used_inodes() const noexcept {
    return used_inodes_;
}

std::uint64_t WorkspaceQuotaUsage::hard_inodes() const noexcept {
    return hard_inodes_;
}

OperatorWorkspaceStatus::OperatorWorkspaceStatus(
    RuntimeId runtime,
    const WorkspacePersistence persistence,
    const WorkspaceActivity activity,
    std::optional<WorkspaceQuotaUsage> quota) noexcept
    : runtime_(std::move(runtime)),
      persistence_(persistence),
      activity_(activity),
      quota_(std::move(quota)) {}

Result<OperatorWorkspaceStatus> OperatorWorkspaceStatus::create(
    RuntimeId runtime,
    const WorkspacePersistence persistence,
    const WorkspaceActivity activity,
    std::optional<WorkspaceQuotaUsage> quota) {
    if (!is_known_workspace_persistence(persistence) || !is_known_workspace_activity(activity)) {
        return std::unexpected(make_error(
            ErrorCode::illegal_transition,
            "operator workspace status contains an unknown lifecycle value"));
    }
    if ((persistence == WorkspacePersistence::ready) != quota.has_value()) {
        return std::unexpected(make_error(
            ErrorCode::illegal_transition,
            "operator workspace persistence and quota presence disagree"));
    }
    return OperatorWorkspaceStatus(std::move(runtime), persistence, activity, std::move(quota));
}

const RuntimeId& OperatorWorkspaceStatus::runtime() const noexcept {
    return runtime_;
}

WorkspacePersistence OperatorWorkspaceStatus::persistence() const noexcept {
    return persistence_;
}

WorkspaceActivity OperatorWorkspaceStatus::activity() const noexcept {
    return activity_;
}

const std::optional<WorkspaceQuotaUsage>& OperatorWorkspaceStatus::quota() const noexcept {
    return quota_;
}

OperatorWorkspacePath::OperatorWorkspacePath(std::string value) : value_(std::move(value)) {}

Result<OperatorWorkspacePath> OperatorWorkspacePath::parse(std::string value) {
    if (value.size() > kMaximumWorkspacePathBytes || value.find('\0') != std::string::npos ||
        (value != kWorkspaceRoot && !value.starts_with(std::string(kWorkspaceRoot) + "/")) ||
        value.ends_with('/') || value.find("//") != std::string::npos) {
        return std::unexpected(make_error(
            ErrorCode::invalid_identity,
            "operator workspace path must be a normalized path below /workspace"));
    }
    if (value == kWorkspaceRoot) {
        return OperatorWorkspacePath(std::move(value));
    }
    std::size_t start = kWorkspaceRoot.size() + 1U;
    while (start < value.size()) {
        /** @brief 当前分量结尾 / End offset of the current component. */
        const std::size_t end = value.find('/', start);
        /** @brief 当前分量视图 / Current component view. */
        const std::string_view component(
            value.data() + static_cast<std::ptrdiff_t>(start),
            (end == std::string::npos ? value.size() : end) - start);
        if (component.empty() || component == "." || component == ".." || component.size() > kMaximumFilenameBytes) {
            return std::unexpected(make_error(
                ErrorCode::invalid_identity,
                "operator workspace path contains an unsafe component"));
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1U;
    }
    return OperatorWorkspacePath(std::move(value));
}

const std::string& OperatorWorkspacePath::value() const noexcept {
    return value_;
}

std::vector<std::string_view> OperatorWorkspacePath::relative_components() const {
    std::vector<std::string_view> components;
    if (value_ == kWorkspaceRoot) {
        return components;
    }
    std::size_t start = kWorkspaceRoot.size() + 1U;
    while (start < value_.size()) {
        /** @brief 当前分量结尾 / End offset of the current component. */
        const std::size_t end = value_.find('/', start);
        /** @brief 当前分量长度 / Byte length of the current component. */
        const std::size_t length = (end == std::string::npos ? value_.size() : end) - start;
        components.emplace_back(value_.data() + static_cast<std::ptrdiff_t>(start), length);
        if (end == std::string::npos) {
            break;
        }
        start = end + 1U;
    }
    return components;
}

WorkspaceEntry::WorkspaceEntry(
    std::string encoded_name,
    const WorkspaceEntryKind kind,
    const std::uint64_t size_bytes) noexcept
    : encoded_name_(std::move(encoded_name)), kind_(kind), size_bytes_(size_bytes) {}

Result<WorkspaceEntry> WorkspaceEntry::create(
    std::string encoded_name,
    const WorkspaceEntryKind kind,
    const std::uint64_t size_bytes) {
    if (!is_strict_encoded_name(encoded_name) || encoded_name == "." || encoded_name == "..") {
        return std::unexpected(make_error(ErrorCode::invalid_identity, "operator workspace entry name is not safe percent encoding"));
    }
    if (kind != WorkspaceEntryKind::regular_file && kind != WorkspaceEntryKind::directory &&
        kind != WorkspaceEntryKind::symbolic_link) {
        return std::unexpected(make_error(ErrorCode::invalid_identity, "operator workspace entry kind is invalid"));
    }
    if (kind != WorkspaceEntryKind::regular_file && size_bytes != 0U) {
        return std::unexpected(make_error(ErrorCode::invalid_identity, "only regular workspace files may expose a byte size"));
    }
    return WorkspaceEntry(std::move(encoded_name), kind, size_bytes);
}

const std::string& WorkspaceEntry::encoded_name() const noexcept {
    return encoded_name_;
}

WorkspaceEntryKind WorkspaceEntry::kind() const noexcept {
    return kind_;
}

std::uint64_t WorkspaceEntry::size_bytes() const noexcept {
    return size_bytes_;
}

Result<std::string> encode_workspace_entry_name(const std::string_view raw_name) {
    if (raw_name.empty() || raw_name.size() > kMaximumFilenameBytes || raw_name == "." || raw_name == ".." ||
        raw_name.find('/') != std::string_view::npos || raw_name.find('\0') != std::string_view::npos) {
        return std::unexpected(make_error(ErrorCode::invalid_identity, "raw workspace entry name is unsafe"));
    }
    std::string encoded;
    encoded.reserve(raw_name.size() * 3U);
    for (const unsigned char byte : raw_name) {
        if (is_display_safe_byte(byte)) {
            encoded.push_back(static_cast<char>(byte));
            continue;
        }
        encoded.push_back('%');
        encoded.push_back(upper_hex_digit(static_cast<unsigned char>(byte >> 4U)));
        encoded.push_back(upper_hex_digit(byte));
    }
    return encoded;
}

}  // namespace wspctl::domain
