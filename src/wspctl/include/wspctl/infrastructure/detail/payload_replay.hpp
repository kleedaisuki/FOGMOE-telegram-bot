#pragma once

#include "wspctl/infrastructure/common.hpp"
#include "wspctl/infrastructure/journal.hpp"
#include "wspctl/infrastructure/xfs_project_quota.hpp"

namespace wspctl::detail {

/**
 * @brief 只读解析已完成文件 ingress 的 durable 回执 / Resolve a durable receipt for completed file ingress read-only.
 * @param journal 每 runtime control tree 下的 crash-safe journal / Crash-safe journal below each runtime control tree.
 * @param request 不含 activation 的 replay 查询 / Activation-free replay query.
 * @return 严格匹配且 ``replayed=true`` 的回执，或 not-found/conflict/in-doubt / Strict matching receipt with ``replayed=true``, or not-found/conflict/in-doubt.
 * @note ``not_found`` 是唯一允许上层重新下载附件的结果；本函数绝不创建 pending journal。
 *       ``not_found`` is the sole result that permits the caller to download an attachment again;
 *       this function never creates a pending journal.
 */
[[nodiscard]] Result<PayloadResult> resolve_payload_replay_receipt(
    const Journal& journal,
    const PayloadReplayRequest& request);

/**
 * @brief 只读验证 completed 回执指向的持久 payload object / Read-only verify the persistent payload object named by a completed receipt.
 * @param binding 已验证的 runtime XFS binding / Verified runtime XFS binding.
 * @param request 不含 activation 的 replay 查询 / Activation-free replay query.
 * @param receipt 已严格匹配且标记 replayed 的 durable 回执 / Strictly matched durable receipt marked replayed.
 * @return payload object 仍可证明一致时成功；不可证明时为 ``invocation_in_doubt`` /
 *         Success when the payload object remains provably consistent; ``invocation_in_doubt`` when it cannot be proven.
 * @note 该函数只使用固定的 ``upper/uploads/<opaque>/payload`` no-follow 链，不接收 host path，
 *       不 mount、不启动 PID 1、也不写入 workspace。 This uses only the fixed
 *       ``upper/uploads/<opaque>/payload`` no-follow chain, accepts no host path, mounts nothing,
 *       starts no PID 1, and writes no workspace state.
 */
[[nodiscard]] Result<void> verify_replayable_payload_object(
    const RuntimeQuotaBinding& binding,
    const PayloadReplayRequest& request,
    const PayloadResult& receipt);

}  // namespace wspctl::detail
