#pragma once

#include "wspctl/domain/runtime.hpp"

namespace wspctl::application {

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
     * @return 成功或已归一化的领域错误 / Success or a normalized domain error.
     */
    [[nodiscard]] virtual domain::Result<void>
    establish(const domain::RuntimeId& runtime, const domain::ActivationId& activation) = 0;

    /**
     * @brief 终止并清理指定 activation / Terminate and clean up an activation.
     * @param runtime 长期 runtime 标识 / Long-lived runtime identifier.
     * @param activation 待退役 activation 标识 / Activation to retire.
     * @return 成功或已归一化的领域错误 / Success or a normalized domain error.
     */
    [[nodiscard]] virtual domain::Result<void> retire(const domain::RuntimeId& runtime,
                                                      const domain::ActivationId& activation) = 0;
};

/**
 * @brief RuntimeProcess 激活用例 / RuntimeProcess activation use case.
 *
 * 该服务是领域状态机与外部副作用之间的事务编排点：外设失败时聚合进入 failed，
 * 因此不会把一个未证明的 RuntimeProcess 作为 ready 返回给调用方。
 * This service is the transaction orchestration point between the domain state machine and external
 * effects. When the port fails the aggregate becomes failed, so an unproven RuntimeProcess is never
 * returned as ready.
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
     * @brief 退役一个空闲 activation / Retire an idle activation.
     * @param runtime 待退役聚合 / Aggregate to retire.
     * @param activation 当前 activation / Current activation.
     * @param port 执行受控外设效果的端口 / Port that performs controlled external effects.
     * @return dormant 状态或失败原因 / Dormant state or failure reason.
     */
    [[nodiscard]] domain::Result<void> retire(domain::Runtime& runtime,
                                              const domain::ActivationId& activation,
                                              RuntimeActivationPort& port) const;

    /**
     * @brief 在执行或协议失败后强制回收 activation / Force cleanup of an activation after execution
     * or protocol failure.
     * @param runtime 待标记失败的聚合 / Aggregate to mark failed.
     * @param activation 待清理 activation / Activation to clean up.
     * @param port 执行受控外设清理的端口 / Port that performs controlled external cleanup.
     * @return 外设清理成功或失败原因 / External cleanup success or failure reason.
     * @note 该用例故意不要求 ready 状态；失败态不能继续接受任务，但仍必须回收真实 Process。
     *       This use case intentionally does not require ready state: a failed aggregate cannot
     * accept work but must still reap a real Process.
     */
    [[nodiscard]] domain::Result<void> abort(domain::Runtime& runtime,
                                             const domain::ActivationId& activation,
                                             RuntimeActivationPort& port) const;
};

} // namespace wspctl::application
