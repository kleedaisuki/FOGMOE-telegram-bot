#include "wspctl/domain/runtime.hpp"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>

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
 * @brief 构造测试 runtime ID / Construct a test runtime ID.
 * @return 已验证 runtime ID / Validated runtime ID.
 */
[[nodiscard]] wspctl::domain::RuntimeId test_runtime_id() {
    const auto parsed = wspctl::domain::RuntimeId::parse("123e4567-e89b-12d3-a456-426614174000");
    expect(parsed.has_value(), "parse canonical runtime UUID");
    return *parsed;
}

/**
 * @brief 构造测试 activation / Construct a test activation.
 * @param value activation 文本 / Activation text.
 * @return 已验证 activation / Validated activation.
 */
[[nodiscard]] wspctl::domain::ActivationId test_activation(const std::string& value) {
    const auto parsed = wspctl::domain::ActivationId::parse(value);
    expect(parsed.has_value(), "parse safe activation identifier");
    return *parsed;
}

/** @brief 测试标识、摘要与预算值对象 / Test identity, digest, and budget value objects. */
void test_value_objects() {
    expect(!wspctl::domain::RuntimeId::parse("123E4567-e89b-12d3-a456-426614174000").has_value(),
           "reject uppercase runtime UUID");
    expect(!wspctl::domain::ActivationId::parse("activation/escapes").has_value(),
           "reject path-shaped activation ID");
    expect(!wspctl::domain::CommandId::parse("").has_value(), "reject empty command ID");
    expect(wspctl::domain::Sha256Digest::parse(std::string(64U, 'a')).has_value(),
           "accept lowercase SHA-256 digest");
    expect(!wspctl::domain::Sha256Digest::parse(std::string(63U, 'a')).has_value(),
           "reject short SHA-256 digest");
    expect(!wspctl::domain::Sha256Digest::parse(std::string(64U, 'A')).has_value(),
           "reject uppercase SHA-256 digest");
    expect(wspctl::domain::ExecutionBudget::create(std::chrono::milliseconds(1), 1U).has_value(),
           "accept positive execution budget");
    expect(!wspctl::domain::ExecutionBudget::create(std::chrono::milliseconds::zero(), 1U).has_value(),
           "reject zero wall-clock budget");
    expect(!wspctl::domain::ExecutionBudget::create(std::chrono::milliseconds(1), 0U).has_value(),
           "reject zero output budget");
}

/** @brief 测试 activation 所有权与完整生命周期 / Test activation ownership and the complete lifecycle. */
void test_activation_ownership() {
    wspctl::domain::Runtime runtime(test_runtime_id());
    const wspctl::domain::ActivationId owner = test_activation("activation-owner");
    const wspctl::domain::ActivationId stranger = test_activation("activation-stranger");
    expect(runtime.begin_activation(owner).has_value(), "begin owned activation");
    expect(runtime.state() == wspctl::domain::RuntimeState::activating && runtime.active_activation() == owner,
           "activation establishes unique owner");
    const auto stranger_ready = runtime.mark_ready(stranger);
    expect(!stranger_ready.has_value() && stranger_ready.error().code == wspctl::domain::ErrorCode::activation_mismatch,
           "reject ready transition from non-owner activation");
    expect(runtime.mark_ready(owner).has_value(), "owner marks runtime ready");
    const auto stranger_execute = runtime.begin_execution(stranger);
    expect(!stranger_execute.has_value() && stranger_execute.error().code == wspctl::domain::ErrorCode::activation_mismatch,
           "reject execute transition from non-owner activation");
    expect(runtime.begin_execution(owner).has_value(), "owner starts execution");
    const auto retirement_while_executing = runtime.begin_retirement(owner);
    expect(!retirement_while_executing.has_value() &&
               retirement_while_executing.error().code == wspctl::domain::ErrorCode::illegal_transition,
           "reject retirement while a task executes");
    expect(runtime.finish_execution(owner).has_value(), "owner finishes execution");
    expect(runtime.begin_retirement(owner).has_value(), "owner begins retirement");
    const auto stranger_finish = runtime.finish_retirement(stranger);
    expect(!stranger_finish.has_value() && stranger_finish.error().code == wspctl::domain::ErrorCode::activation_mismatch,
           "reject retirement completion from non-owner activation");
    expect(runtime.finish_retirement(owner).has_value(), "owner completes retirement");
    expect(runtime.state() == wspctl::domain::RuntimeState::dormant && !runtime.active_activation().has_value(),
           "completed retirement clears active activation");
}

/** @brief 测试 failed 状态清除 activation / Test that failed state clears the activation. */
void test_failure_clears_owner() {
    wspctl::domain::Runtime runtime(test_runtime_id());
    const wspctl::domain::ActivationId owner = test_activation("activation-owner");
    expect(runtime.begin_activation(owner).has_value(), "begin activation before failure");
    runtime.fail();
    expect(runtime.state() == wspctl::domain::RuntimeState::failed && !runtime.active_activation().has_value(),
           "failure clears owner and is explicit");
}

}  // namespace

/**
 * @brief domain CTest 入口 / Domain CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_value_objects();
    test_activation_ownership();
    test_failure_clears_owner();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
