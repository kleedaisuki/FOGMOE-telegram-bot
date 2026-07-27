#pragma once

#include "wspctl/infrastructure/common.hpp"

#include <mutex>
#include <string>
#include <unordered_set>

namespace wspctl {

/** @brief 每 runtime 的互斥执行闸门 / Per-runtime mutually exclusive execution gate. */
class RuntimeExecutionGate;

/**
 * @brief 持有 runtime 执行权的 RAII 租约 / RAII lease holding exclusive runtime execution.
 */
class RuntimeLease final {
public:
    /** @brief 禁止复制，防止双重释放 / Copying is forbidden to prevent double release. */
    RuntimeLease(const RuntimeLease&) = delete;
    /** @brief 禁止复制赋值 / Copy assignment is forbidden. */
    RuntimeLease& operator=(const RuntimeLease&) = delete;
    /** @brief 支持移动 / Moving is supported. */
    RuntimeLease(RuntimeLease&& other) noexcept;
    /** @brief 支持移动赋值 / Move assignment is supported. */
    RuntimeLease& operator=(RuntimeLease&& other) noexcept;
    /** @brief 析构时释放租约 / Release the lease at destruction. */
    ~RuntimeLease();

private:
    /** @brief 只有闸门可以构造有效租约 / Only the gate may construct a valid lease. */
    friend class RuntimeExecutionGate;
    /** @brief 所属闸门 / Owning gate. */
    RuntimeExecutionGate* gate_{nullptr};
    /** @brief 已占用的 runtime 标识 / Acquired runtime key. */
    std::string runtime_key_;

    /**
     * @brief 构造有效租约 / Construct a valid lease.
     * @param gate 所属闸门 / Owning gate.
     * @param runtime_key runtime 标识 / Runtime key.
     */
    RuntimeLease(RuntimeExecutionGate* gate, std::string runtime_key);
};

/**
 * @brief 防止共享 workspace 并发写入的闸门 / Gate preventing concurrent writes to a shared workspace.
 */
class RuntimeExecutionGate final {
public:
    /**
     * @brief 尝试独占一个 runtime / Try to exclusively acquire a runtime.
     * @param runtime_key runtime 标识 / Runtime key.
     * @return 租约，或 busy 错误 / Lease, or a busy error.
     */
    [[nodiscard]] Result<RuntimeLease> try_acquire(const std::string& runtime_key);

private:
    /** @brief 租约析构需要释放闸门 / Lease destruction needs to release the gate. */
    friend class RuntimeLease;
    /** @brief 保护 active_ 的互斥锁 / Mutex protecting active_. */
    std::mutex mutex_;
    /** @brief 正在执行的 runtime 集合 / Set of runtimes currently executing. */
    std::unordered_set<std::string> active_;

    /**
     * @brief 释放内部租约 / Release an internal lease.
     * @param runtime_key runtime 标识 / Runtime key.
     */
    void release(const std::string& runtime_key) noexcept;
};

}  // namespace wspctl
