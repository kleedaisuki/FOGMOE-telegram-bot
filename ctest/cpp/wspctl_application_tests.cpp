#include "wspctl/application/runtime_activation.hpp"

#include <cstdlib>
#include <iostream>
#include <string>
#include <utility>
#include <vector>

namespace {

/** @brief 测试失败计数 / Test failure count. */
unsigned int g_failures{0U};

/**
 * @brief 断言一个条件 / Assert one condition.
 * @param condition 待断言条件 / Condition to assert.
 * @param message 失败说明 / Failure description.
 */
void expect(const bool condition, const std::string& message) {
    if (!condition) {
        ++g_failures;
        std::cerr << "FAIL: " << message << '\n';
    }
}

/**
 * @brief 构造测试 runtime / Construct a test runtime.
 * @return 处于 dormant 状态的 runtime / Runtime in dormant state.
 */
[[nodiscard]] wspctl::domain::Runtime test_runtime() {
    const auto runtime_id = wspctl::domain::RuntimeId::parse("123e4567-e89b-12d3-a456-426614174000");
    expect(runtime_id.has_value(), "parse application test runtime ID");
    return wspctl::domain::Runtime(*runtime_id);
}

/**
 * @brief 构造测试 activation / Construct a test activation.
 * @return 已验证 activation / Validated activation.
 */
[[nodiscard]] wspctl::domain::ActivationId test_activation() {
    const auto activation = wspctl::domain::ActivationId::parse("application-activation");
    expect(activation.has_value(), "parse application test activation");
    return *activation;
}

/** @brief 记录外设调用的 fake port / Fake port that records external calls. */
class RecordingPort final : public wspctl::application::RuntimeActivationPort {
public:
    /**
     * @brief 记录 establish / Record establish.
     * @param runtime runtime ID / Runtime ID.
     * @param activation activation ID / Activation ID.
     * @return 成功 / Success.
     */
    [[nodiscard]] wspctl::domain::Result<void> establish(
        const wspctl::domain::RuntimeId& runtime,
        const wspctl::domain::ActivationId& activation) override {
        calls.emplace_back("establish:" + runtime.value() + ":" + activation.value());
        return {};
    }

    /**
     * @brief 记录 retire / Record retire.
     * @param runtime runtime ID / Runtime ID.
     * @param activation activation ID / Activation ID.
     * @return 成功 / Success.
     */
    [[nodiscard]] wspctl::domain::Result<void> retire(
        const wspctl::domain::RuntimeId& runtime,
        const wspctl::domain::ActivationId& activation) override {
        calls.emplace_back("retire:" + runtime.value() + ":" + activation.value());
        return {};
    }

    /** @brief 记录的外设调用 / Recorded external calls. */
    std::vector<std::string> calls;
};

/** @brief 建立失败的 fake port / Fake port whose establish operation fails. */
class FailingEstablishPort final : public wspctl::application::RuntimeActivationPort {
public:
    /**
     * @brief 返回可传播的 establish 失败 / Return a propagatable establish failure.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param activation 未使用 activation / Unused activation.
     * @return 领域错误 / Domain error.
     */
    [[nodiscard]] wspctl::domain::Result<void> establish(
        const wspctl::domain::RuntimeId& runtime,
        const wspctl::domain::ActivationId& activation) override {
        static_cast<void>(runtime);
        static_cast<void>(activation);
        return std::unexpected(wspctl::domain::make_error(
            wspctl::domain::ErrorCode::illegal_transition,
            "fake establish failed"));
    }

    /**
     * @brief 不应抵达的 retire / Retire that must not be reached.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param activation 未使用 activation / Unused activation.
     * @return 领域错误 / Domain error.
     */
    [[nodiscard]] wspctl::domain::Result<void> retire(
        const wspctl::domain::RuntimeId& runtime,
        const wspctl::domain::ActivationId& activation) override {
        static_cast<void>(runtime);
        static_cast<void>(activation);
        return std::unexpected(wspctl::domain::make_error(
            wspctl::domain::ErrorCode::illegal_transition,
            "fake retire should not run"));
    }
};

/** @brief 测试正常的激活/退役编排 / Test successful activation/retirement orchestration. */
void test_lifecycle_orchestration() {
    wspctl::domain::Runtime runtime = test_runtime();
    const wspctl::domain::ActivationId activation = test_activation();
    wspctl::application::RuntimeActivationService service;
    RecordingPort port;
    expect(service.activate(runtime, activation, port).has_value(), "application service activates aggregate through port");
    expect(runtime.state() == wspctl::domain::RuntimeState::ready && runtime.active_activation() == activation,
           "activation leaves aggregate ready with owner");
    expect(port.calls.size() == 1U && port.calls.front().starts_with("establish:"),
           "activate calls only establish exactly once");
    expect(service.retire(runtime, activation, port).has_value(), "application service retires aggregate through port");
    expect(runtime.state() == wspctl::domain::RuntimeState::dormant && !runtime.active_activation().has_value(),
           "retirement returns aggregate to dormant");
    expect(port.calls.size() == 2U && port.calls.back().starts_with("retire:"),
           "retire calls port after legal domain transition");
}

/** @brief 测试外设失败不会伪造 ready runtime / Test that a port failure never fabricates a ready runtime. */
void test_failure_compensation() {
    wspctl::domain::Runtime runtime = test_runtime();
    const wspctl::domain::ActivationId activation = test_activation();
    wspctl::application::RuntimeActivationService service;
    FailingEstablishPort port;
    const auto activated = service.activate(runtime, activation, port);
    expect(!activated.has_value(), "surface establish failure");
    expect(runtime.state() == wspctl::domain::RuntimeState::failed && !runtime.active_activation().has_value(),
           "failed establish clears owner instead of leaving half-ready state");
}

}  // namespace

/**
 * @brief application CTest 入口 / Application CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_lifecycle_orchestration();
    test_failure_compensation();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
