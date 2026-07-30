#pragma once

#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/infrastructure/common.hpp"
#include "wspctl/infrastructure/xfs_project_quota.hpp"

namespace wspctl {

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
};

} // namespace wspctl
