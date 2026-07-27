#pragma once

#include "wspctl/domain/runtime.hpp"

#include <chrono>
#include <cstdint>
#include <optional>

namespace wspctl::application {

/**
 * @brief 一次无副作用 runtime 状态查询 / One side-effect-free runtime-status query.
 *
 * 查询绑定一个 RuntimeProcess handle 的 activation，但它绝不授予 activation 所有权、更换
 * activation 或创建 runtime。/ The query is bound to a RuntimeProcess handle's activation, but it
 * never grants ownership, replaces an activation, or creates a runtime.
 */
class RuntimeStatusQuery final {
public:
    /**
     * @brief 以已验证领域身份构造查询 / Construct a query from validated domain identities.
     * @param runtime 查询目标 runtime / Target runtime to inspect.
     * @param handle_activation 调用 handle 绑定的 activation / Activation bound to the calling handle.
     */
    RuntimeStatusQuery(domain::RuntimeId runtime, domain::ActivationId handle_activation) noexcept;

    /** @brief 取得查询目标 runtime / Get the target runtime. */
    [[nodiscard]] const domain::RuntimeId& runtime() const noexcept;
    /** @brief 取得调用 handle 的 activation / Get the calling handle's activation. */
    [[nodiscard]] const domain::ActivationId& handle_activation() const noexcept;

private:
    /** @brief 查询目标 runtime / Target runtime. */
    domain::RuntimeId runtime_;
    /** @brief 调用 handle 的 activation / Calling handle activation. */
    domain::ActivationId handle_activation_;
};

/**
 * @brief 面向 Python 的受限 runtime 运行态 / Constrained runtime operating status for Python.
 *
 * 它组合领域生命周期快照与少量有界运行指标。指标只描述 session 健康度、空闲年龄和 broker
 * 借用数；它不携带命令、journal ID/hash、payload、路径、PID、cgroup 或 mount。/ It combines
 * a domain lifecycle snapshot with a small set of bounded operating indicators. The indicators
 * describe only session health, idle age, and broker borrow count; they carry no command, journal
 * ID/hash, payload, path, PID, cgroup, or mount.
 */
class RuntimeStatus final {
public:
    /**
     * @brief 验证并创建一个运行态 DTO / Validate and create an operating-status DTO.
     * @param query 此观察请求 / Observation request.
     * @param snapshot 聚合生命周期快照 / Aggregate lifecycle snapshot.
     * @param supervisor_alive 已激活 supervisor 是否仍可轮询为存活 /
     *     Whether an activated supervisor remains pollably alive.
     * @param idle_for 空闲年龄；只在 ready 时存在 / Idle age; present only while ready.
     * @param idle_ttl broker 的空闲回收阈值 / Broker idle-retirement threshold.
     * @param borrowed_dispatches 当前借用 session 的 broker dispatch 数 /
     *     Number of broker dispatches currently borrowing the session.
     * @param cleanup_pending 是否有已知清理/隔离待办 / Whether known cleanup/quarantine remains pending.
     * @return 已验证状态或领域不变量错误 / Validated status or a domain-invariant error.
     */
    [[nodiscard]] static domain::Result<RuntimeStatus> create(
        const RuntimeStatusQuery& query,
        domain::RuntimeSnapshot snapshot,
        bool supervisor_alive,
        std::optional<std::chrono::milliseconds> idle_for,
        std::chrono::milliseconds idle_ttl,
        std::uint64_t borrowed_dispatches,
        bool cleanup_pending);

    /** @brief 取得领域生命周期快照 / Get the domain lifecycle snapshot. */
    [[nodiscard]] const domain::RuntimeSnapshot& snapshot() const noexcept;
    /** @brief handle activation 是否正是 snapshot owner / Whether the handle activation is the snapshot owner. */
    [[nodiscard]] bool handle_activation_matches() const noexcept;
    /** @brief supervisor 是否可观察为存活 / Whether the supervisor is observably alive. */
    [[nodiscard]] bool supervisor_alive() const noexcept;
    /** @brief ready 状态的空闲年龄 / Idle age while in ready state. */
    [[nodiscard]] const std::optional<std::chrono::milliseconds>& idle_for() const noexcept;
    /** @brief broker 的 idle 退役阈值 / Broker idle retirement threshold. */
    [[nodiscard]] std::chrono::milliseconds idle_ttl() const noexcept;
    /** @brief 当前借用 session 的 broker dispatch 数 / Current broker dispatches borrowing the session. */
    [[nodiscard]] std::uint64_t borrowed_dispatches() const noexcept;
    /** @brief 是否存在已知清理/隔离待办 / Whether known cleanup/quarantine remains pending. */
    [[nodiscard]] bool cleanup_pending() const noexcept;

private:
    /**
     * @brief 从已验证字段构造状态 / Construct status from validated fields.
     * @param snapshot 已验证领域快照 / Validated domain snapshot.
     * @param handle_activation_matches 已验证 handle 所有权匹配位 / Validated handle-ownership match bit.
     * @param supervisor_alive 已验证 supervisor 存活位 / Validated supervisor liveness bit.
     * @param idle_for 已验证空闲年龄 / Validated idle age.
     * @param idle_ttl 已验证回收阈值 / Validated retirement threshold.
     * @param borrowed_dispatches 已验证 broker 借用数 / Validated broker borrow count.
     * @param cleanup_pending 已验证清理待办位 / Validated cleanup-pending bit.
     */
    RuntimeStatus(
        domain::RuntimeSnapshot snapshot,
        bool handle_activation_matches,
        bool supervisor_alive,
        std::optional<std::chrono::milliseconds> idle_for,
        std::chrono::milliseconds idle_ttl,
        std::uint64_t borrowed_dispatches,
        bool cleanup_pending) noexcept;

    /** @brief 领域生命周期快照 / Domain lifecycle snapshot. */
    domain::RuntimeSnapshot snapshot_;
    /** @brief handle activation 是否是当前 owner / Whether handle activation is current owner. */
    bool handle_activation_matches_{false};
    /** @brief supervisor 是否存活 / Whether supervisor is alive. */
    bool supervisor_alive_{false};
    /** @brief ready 状态空闲年龄 / Idle age in ready state. */
    std::optional<std::chrono::milliseconds> idle_for_;
    /** @brief broker idle 退役阈值 / Broker idle retirement threshold. */
    std::chrono::milliseconds idle_ttl_;
    /** @brief broker dispatch 借用数 / Broker dispatch borrow count. */
    std::uint64_t borrowed_dispatches_{};
    /** @brief 清理/隔离待办位 / Cleanup/quarantine pending bit. */
    bool cleanup_pending_{false};
};

/**
 * @brief 运行态读模型端口 / Runtime operating-status read-model port.
 *
 * 该端口只能读取。实现不得通过状态查询调用 activation、quota provisioning、journal 写入、
 * OverlayFS 操作或 supervisor control socket。/ This port is read-only. Its implementation must
 * not call activation, quota provisioning, journal writes, OverlayFS work, or the supervisor
 * control socket as part of a status query.
 */
class RuntimeStatusPort {
public:
    /** @brief 支持多态析构 / Support polymorphic destruction. */
    virtual ~RuntimeStatusPort() = default;

    /**
     * @brief 读取一个无副作用运行态 / Read one side-effect-free operating status.
     * @param query 已验证领域身份组成的查询 / Query composed of validated domain identities.
     * @return 运行态或已归一化领域错误 / Operating status or a normalized domain error.
     */
    [[nodiscard]] virtual domain::Result<RuntimeStatus> observe(const RuntimeStatusQuery& query) const = 0;
};

/**
 * @brief RuntimeProcess 状态查询用例 / RuntimeProcess status-query use case.
 *
 * 保留一个显式 CQRS read boundary：调用方只能得到 allowlisted snapshot，不能把 status 查询意外
 * 变成 activation 或任意 host 检查入口。/ This preserves an explicit CQRS read boundary: callers
 * receive only an allowlisted snapshot and cannot accidentally turn a status query into activation
 * or an arbitrary host-inspection entry point.
 */
class RuntimeStatusService final {
public:
    /**
     * @brief 读取 runtime 状态 / Read runtime status.
     * @param query 已验证身份的只读查询 / Read-only query with validated identities.
     * @param port read-model 实现 / Read-model implementation.
     * @return allowlisted 运行态或错误 / Allowlisted operating status or error.
     */
    [[nodiscard]] domain::Result<RuntimeStatus> inspect(
        const RuntimeStatusQuery& query,
        const RuntimeStatusPort& port) const;
};

}  // namespace wspctl::application
