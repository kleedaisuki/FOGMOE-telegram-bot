#pragma once

#include "wspctl/domain/runtime.hpp"

namespace wspctl::application {

/** @brief RuntimeProcess 建立失败后的已证明处置 / Proven disposition after RuntimeProcess
 * establishment failure. */
enum class RuntimeEstablishFailureDisposition : std::uint8_t {
    /** @brief 已证明没有待清理 RuntimeProcess / Proven to leave no RuntimeProcess to clean up. */
    rejected_cleanly,
    /** @brief 已知存在可由通用 terminate 清理的部分建立 / Known partial establishment that the
     * generic terminate effect can clean up. */
    cleanup_required,
    /** @brief 外部结果未知，必须隔离并交给 recovery ledger / External outcome is unknown and must
     * be quarantined for the recovery ledger. */
    outcome_unknown,
};

/** @brief 携带建立失败原因与已证明处置的封闭结果 / Closed establishment failure carrying its
 * cause and proven disposition. */
class RuntimeEstablishFailure final {
public:
    /**
     * @brief 构造已证明 clean rejection / Construct a proven clean rejection.
     * @param cause 归一化失败原因 / Normalized failure cause.
     * @return clean rejection / Clean rejection.
     */
    [[nodiscard]] static RuntimeEstablishFailure rejected_cleanly(domain::Error cause);
    /**
     * @brief 构造需要通用清理的失败 / Construct a failure requiring generic cleanup.
     * @param cause 归一化失败原因 / Normalized failure cause.
     * @return cleanup-required failure / Cleanup-required failure.
     */
    [[nodiscard]] static RuntimeEstablishFailure cleanup_required(domain::Error cause);
    /**
     * @brief 构造结果未知的失败 / Construct an unknown-outcome failure.
     * @param cause 归一化失败原因 / Normalized failure cause.
     * @return unknown-outcome failure / Unknown-outcome failure.
     */
    [[nodiscard]] static RuntimeEstablishFailure outcome_unknown(domain::Error cause);

    /** @brief 取得已证明处置 / Get the proven disposition. */
    [[nodiscard]] RuntimeEstablishFailureDisposition disposition() const noexcept;
    /** @brief 取得归一化失败原因 / Get the normalized failure cause. */
    [[nodiscard]] const domain::Error& cause() const noexcept;

private:
    /**
     * @brief 从已证明处置与原因构造 / Construct from a proven disposition and cause.
     * @param disposition 已证明处置 / Proven disposition.
     * @param cause 归一化失败原因 / Normalized failure cause.
     */
    RuntimeEstablishFailure(RuntimeEstablishFailureDisposition disposition,
                            domain::Error cause) noexcept;

    /** @brief 已证明处置 / Proven disposition. */
    RuntimeEstablishFailureDisposition disposition_;
    /** @brief 归一化失败原因 / Normalized failure cause. */
    domain::Error cause_;
};

/** @brief RuntimeProcess 建立结果 / RuntimeProcess establishment result. */
using RuntimeEstablishResult = std::expected<void, RuntimeEstablishFailure>;

/**
 * @brief RuntimeProcess 生命周期外设端口 / RuntimeProcess lifecycle outbound port.
 *
 * 端口描述应用层所需的效果，而不暴露 namespace、cgroup、socket 或 filesystem 实现。
 * The port describes effects required by the application layer without exposing namespace, cgroup,
 * socket, or filesystem implementations.
 */
class RuntimeActivationPort {
public:
    /** @brief 虚析构，允许通过接口安全销毁 / Virtual destructor for safe interface destruction. */
    virtual ~RuntimeActivationPort() = default;

    /**
     * @brief 建立指定 activation 的 RuntimeProcess / Establish the RuntimeProcess for an
     * activation.
     * @param runtime 长期 runtime 标识 / Long-lived runtime identifier.
     * @param activation 新 activation 标识 / New activation identifier.
     * @return 成功或带已证明处置的建立失败 / Success or establishment failure with a proven
     * disposition.
     */
    [[nodiscard]] virtual RuntimeEstablishResult
    establish(const domain::RuntimeId& runtime, const domain::ActivationId& activation) = 0;

    /**
     * @brief 终止并清理指定 activation / Terminate and clean up an activation.
     * @param runtime 长期 runtime 标识 / Long-lived runtime identifier.
     * @param activation 待终止 activation 标识 / Activation to terminate.
     * @return 成功或已归一化的领域错误 / Success or a normalized domain error.
     */
    [[nodiscard]] virtual domain::Result<void>
    terminate(const domain::RuntimeId& runtime, const domain::ActivationId& activation) = 0;
};

/**
 * @brief RuntimeProcess 生命周期用例 / RuntimeProcess lifecycle use case.
 *
 * 该服务是领域状态机与外部副作用之间的事务编排点：外设终止失败时聚合保留清理 ownership，
 * 因此不会把未证明的 RuntimeProcess 作为 ready 返回，也不会静默脱离其 owner。
 * This service is the transaction orchestration point between the domain state machine and external
 * effects. When termination fails, the aggregate retains cleanup ownership; an unproven
 * RuntimeProcess is never returned as ready or silently detached from its owner.
 */
class RuntimeActivationService final {
public:
    /**
     * @brief 激活一个 dormant runtime / Activate a dormant runtime.
     * @param runtime 待激活聚合 / Aggregate to activate.
     * @param activation 请求 activation / Requested activation.
     * @param port 执行受控外设效果的端口 / Port that performs controlled external effects.
     * @return ready 状态或失败原因 / Ready state or failure reason.
     */
    [[nodiscard]] domain::Result<void> activate(domain::Runtime& runtime,
                                                const domain::ActivationId& activation,
                                                RuntimeActivationPort& port) const;

    /**
     * @brief 停止或重试清理当前 activation / Stop or retry cleanup of the current activation.
     * @param runtime 待停止聚合 / Aggregate to stop.
     * @param activation 当前 lifecycle owner / Current lifecycle owner.
     * @param port 执行受控外设清理的端口 / Port that performs controlled external cleanup.
     * @return 外部清理与领域转换均完成，或精确失败原因 / Completed external cleanup and domain
     * transition, or a precise failure reason.
     * @note 由 Runtime 根据当前状态选择正常退役或失败清理；调用方不能通过读取 enum 手工拼接
     * 生命周期。/ Runtime chooses normal retirement or failure cleanup from its current state;
     * callers cannot splice the lifecycle by inspecting an enum.
     */
    [[nodiscard]] domain::Result<void> stop(domain::Runtime& runtime,
                                            const domain::ActivationId& activation,
                                            RuntimeActivationPort& port) const;
};

} // namespace wspctl::application
