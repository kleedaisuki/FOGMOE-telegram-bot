#pragma once

#include "wspctl/infrastructure/common.hpp"
#include "wspctl/infrastructure/protocol.hpp"

#include <filesystem>
#include <optional>
#include <string>

namespace wspctl {

/** @brief journal 调用记录状态 / Journal invocation record state. */
enum class JournalState : unsigned char {
    pending = 1,
    completed = 2,
};

/** @brief journal 记录的副作用类别 / Side-effect category represented by a journal record. */
enum class JournalOperation : unsigned char {
    /** @brief 直接 argv 命令执行 / Direct-argv command execution. */
    execution = 1,
    /** @brief 受限 runtime 文件写入 / Constrained runtime file ingress. */
    payload = 2,
};

/**
 * @brief 持久化的幂等记录 / Persisted idempotency record.
 */
struct JournalRecord final {
    /** @brief 调用状态 / Invocation state. */
    JournalState state;
    /** @brief 被保护副作用的类别 / Kind of protected side effect. */
    JournalOperation operation;
    /** @brief 上层语义哈希 / Caller semantic hash. */
    std::string request_hash;
    /** @brief broker 计算的规范载荷哈希 / Broker-computed canonical payload hash. */
    std::string payload_hash;
    /** @brief 已完成命令的执行结果 / Execution result for a completed command. */
    std::optional<ExecutionResult> execution_result;
    /** @brief 已完成文件写入的收据 / Receipt for a completed file ingress. */
    std::optional<PayloadResult> payload_result;
};

/**
 * @brief 以 (runtime_key, request_id) 索引的崩溃安全 journal / Crash-safe journal indexed by runtime and request.
 *
 * 同 ID 不同哈希绝不重新执行；pending 在重启后返回 in-doubt，避免副作用重复。
 * Same ID with another hash is never re-executed; pending becomes in-doubt after restart to avoid duplicate effects.
 */
class Journal final {
public:
    /**
     * @brief 打开 state root 下各 runtime control tree 的 journal / Open journals below runtime control trees in a state root.
     * @param state_root 受 broker 管理的绝对状态根 / Broker-managed absolute state root.
     * @note 构造不创建 ``state_root/journal``；每个调用只会打开 quota service 已创建并验证的
     *       ``runtimes/<hash>/control/journal``。 Construction never creates ``state_root/journal``;
     *       each invocation only opens ``runtimes/<hash>/control/journal`` created and verified by
     *       the quota service.
     */
    explicit Journal(std::filesystem::path state_root);

    /**
     * @brief 查询调用记录 / Look up an invocation record.
     * @param runtime_key runtime 标识 / Runtime key.
     * @param request_id 稳定调用标识 / Stable invocation ID.
     * @return 无记录、记录或错误 / No record, record, or error.
     */
    [[nodiscard]] Result<std::optional<JournalRecord>> lookup(
        const std::string& runtime_key,
        const std::string& request_id) const;

    /**
     * @brief 在启动副作用前写入 pending 记录 / Persist a pending record before side effects begin.
     * @param request 已校验请求 / Validated request.
     * @return 成功或冲突 / Success or conflict.
     */
    [[nodiscard]] Result<void> begin(const ExecuteRequest& request) const;

    /**
     * @brief 原子替换为完成记录 / Atomically replace with a completed record.
     * @param request 原始请求 / Original request.
     * @param result 已完成结果 / Completed result.
     * @return 成功或 I/O 错误 / Success or I/O error.
     */
    [[nodiscard]] Result<void> complete(
        const ExecuteRequest& request,
        const ExecutionResult& result) const;

    /**
     * @brief 在文件原子 publish 前写入 pending 记录 / Persist a pending record before atomic file publish.
     * @param request 已 seal、待 publish 的文件开始请求 / Sealed file-begin request awaiting publish.
     * @return 成功或冲突 / Success or conflict.
     * @note 调用者必须只在 PID 1 已校验 bytes/SHA-256 并 fdatasync 临时文件后调用；因此损坏的
     * chunk stream 不会留下 pending。/ Callers must invoke this only after PID 1 verified bytes/SHA-256
     * and fdatasynced the temporary file, so malformed chunk streams leave no pending record.
     */
    [[nodiscard]] Result<void> begin_payload(const PayloadBeginRequest& request) const;

    /**
     * @brief 原子替换为已完成文件收据 / Atomically replace with a completed file receipt.
     * @param request 原始已 seal 文件开始请求 / Original sealed file-begin request.
     * @param result 已原子 publish 的文件收据 / Receipt for the atomically published file.
     * @return 成功或 I/O 错误 / Success or an I/O error.
     */
    [[nodiscard]] Result<void> complete_payload(
        const PayloadBeginRequest& request,
        const PayloadResult& result) const;

private:
    /** @brief 状态根目录 / State root directory. */
    std::filesystem::path state_root_;

    /**
     * @brief 得到 control project 内不含用户路径片段的记录路径 / Get a record path inside the control project without user-controlled path segments.
     * @param runtime_key runtime 标识 / Runtime key.
     * @param request_id 调用标识 / Invocation ID.
     * @return SHA-256 命名的记录路径 / SHA-256-named record path.
     */
    [[nodiscard]] std::filesystem::path record_path(
        const std::string& runtime_key,
        const std::string& request_id) const;
};

}  // namespace wspctl
