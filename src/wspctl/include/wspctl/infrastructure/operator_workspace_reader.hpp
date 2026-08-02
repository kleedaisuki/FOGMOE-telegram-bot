#pragma once

#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/common.hpp"
#include "wspctl/infrastructure/xfs_project_quota.hpp"

#include <cstddef>
#include <string>
#include <string_view>
#include <vector>

namespace wspctl {

/** @brief 从 persistent workspace 读取的完整普通文件 / Complete regular file read from a persistent workspace. */
struct FetchedWorkspaceFile final {
    /** @brief 未解释的完整文件内容 / Complete uninterpreted file content. */
    std::vector<std::byte> contents;
    /** @brief 完整内容 SHA-256 / SHA-256 of the complete content. */
    std::string sha256;
};

/**
 * @brief 基于 verified quota binding 的 operator workspace 目录读取器 / Operator
 * workspace-directory reader based on a verified quota binding.
 *
 * 读取器只从 `workspace/upper` 的目录 FD 开始，绝不把 operator 的逻辑路径拼接到 host path。
 * 它不读取文件内容、link target、journal 或 payload receipt。/ The reader starts only at a
 * directory FD for `workspace/upper` and never concatenates an operator logical path into a host
 * path. It reads no file contents, link targets, journals, or payload receipts.
 */
class OperatorWorkspaceReader final {
public:
    /**
     * @brief 列举一层逻辑 workspace 目录 / List one logical workspace directory level.
     * @param binding 已由 quota 服务 read-back 验证的 runtime binding / Runtime binding read-back
     * verified by the quota service.
     * @param path 已验证 `/workspace` 逻辑路径 / Validated `/workspace` logical path.
     * @return 排序、有界、安全编码的目录列举或错误 / Sorted bounded safely encoded directory
     * listing or an error.
     * @note 内层分量以
     * `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS|RESOLVE_NO_XDEV)` fail
     * closed；内核不支持时不会退化到字符串路径解析。/ Inner components use
     *       `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS|RESOLVE_NO_XDEV)`
     *       and fail closed; unsupported kernels never fall back to string-path resolution.
     */
    [[nodiscard]] Result<domain::WorkspaceListing>
    list(const RuntimeQuotaBinding& binding, const domain::OperatorWorkspacePath& path) const;

    /**
     * @brief 从 persistent upper 安全读取一个普通文件 / Safely read one regular file from the persistent upper.
     * @param binding 已验证 runtime quota binding / Verified runtime quota binding.
     * @param relative_path 相对 ``/workspace`` 的已验证路径 / Validated path relative to ``/workspace``.
     * @param max_bytes 调用方字节上限 / Caller byte limit.
     * @return 完整 bytes 与 SHA-256，或精确错误 / Complete bytes and SHA-256, or a precise error.
     * @note 每个路径分量均由 ``openat2`` 与 ``RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS`` 解析；
     *       不存在先检查再打开的 TOCTOU 窗口。/ Every path component is resolved by ``openat2``
     *       with ``RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS``; there is no check-then-open TOCTOU window.
     */
    [[nodiscard]] Result<FetchedWorkspaceFile>
    fetch_file(const RuntimeQuotaBinding& binding, std::string_view relative_path,
               std::size_t max_bytes) const;
};

} // namespace wspctl
