#include "wspctl/application/operator_workspace.hpp"
#include "wspctl/application/runtime_activation.hpp"
#include "wspctl/application/runtime_status.hpp"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <optional>
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
    const auto runtime_id =
        wspctl::domain::RuntimeId::parse("123e4567-e89b-12d3-a456-426614174000");
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
    [[nodiscard]] wspctl::application::RuntimeEstablishResult
    establish(const wspctl::domain::RuntimeId& runtime,
              const wspctl::domain::ActivationId& activation) override {
        calls.emplace_back("establish:" + runtime.value() + ":" + activation.value());
        return {};
    }

    /**
     * @brief 记录 terminate / Record terminate.
     * @param runtime runtime ID / Runtime ID.
     * @param activation activation ID / Activation ID.
     * @return 成功 / Success.
     */
    [[nodiscard]] wspctl::domain::Result<void>
    terminate(const wspctl::domain::RuntimeId& runtime,
              const wspctl::domain::ActivationId& activation) override {
        calls.emplace_back("terminate:" + runtime.value() + ":" + activation.value());
        return {};
    }

    /** @brief 记录的外设调用 / Recorded external calls. */
    std::vector<std::string> calls;
};

/** @brief 返回指定建立失败处置的 fake port / Fake port returning a selected establishment-failure
 * disposition. */
class EstablishOutcomePort final : public wspctl::application::RuntimeActivationPort {
public:
    /**
     * @brief 构造固定处置的 fake / Construct a fake with a fixed disposition.
     * @param disposition 待返回的失败处置 / Failure disposition to return.
     * @param terminate_fails 通用清理是否失败 / Whether generic cleanup fails.
     */
    explicit EstablishOutcomePort(
        const wspctl::application::RuntimeEstablishFailureDisposition disposition,
        const bool terminate_fails = false) noexcept
        : disposition_(disposition), terminate_fails_(terminate_fails) {}

    /**
     * @brief 返回选定的 establish 失败 / Return the selected establish failure.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param activation 未使用 activation / Unused activation.
     * @return 带已证明处置的失败 / Failure with a proven disposition.
     */
    [[nodiscard]] wspctl::application::RuntimeEstablishResult
    establish(const wspctl::domain::RuntimeId& runtime,
              const wspctl::domain::ActivationId& activation) override {
        static_cast<void>(runtime);
        static_cast<void>(activation);
        ++establish_calls;
        /** @brief 三种处置共享的测试原因 / Test cause shared by all three dispositions. */
        wspctl::domain::Error cause = wspctl::domain::make_error(
            wspctl::domain::ErrorCode::illegal_transition, "fake establish failed");
        switch (disposition_) {
        case wspctl::application::RuntimeEstablishFailureDisposition::rejected_cleanly:
            return std::unexpected(
                wspctl::application::RuntimeEstablishFailure::rejected_cleanly(std::move(cause)));
        case wspctl::application::RuntimeEstablishFailureDisposition::cleanup_required:
            return std::unexpected(
                wspctl::application::RuntimeEstablishFailure::cleanup_required(std::move(cause)));
        case wspctl::application::RuntimeEstablishFailureDisposition::outcome_unknown:
            return std::unexpected(
                wspctl::application::RuntimeEstablishFailure::outcome_unknown(std::move(cause)));
        }
        return std::unexpected(
            wspctl::application::RuntimeEstablishFailure::outcome_unknown(std::move(cause)));
    }

    /**
     * @brief 记录通用 terminate / Record generic termination.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param activation 未使用 activation / Unused activation.
     * @return 成功 / Success.
     */
    [[nodiscard]] wspctl::domain::Result<void>
    terminate(const wspctl::domain::RuntimeId& runtime,
              const wspctl::domain::ActivationId& activation) override {
        static_cast<void>(runtime);
        static_cast<void>(activation);
        ++terminate_calls;
        if (terminate_fails_) {
            return std::unexpected(wspctl::domain::make_error(
                wspctl::domain::ErrorCode::illegal_transition, "fake terminate failed"));
        }
        return {};
    }

    /** @brief establish 调用次数 / Number of establish calls. */
    unsigned int establish_calls{0U};
    /** @brief terminate 调用次数 / Number of terminate calls. */
    unsigned int terminate_calls{0U};

private:
    /** @brief 固定建立失败处置 / Fixed establishment-failure disposition. */
    wspctl::application::RuntimeEstablishFailureDisposition disposition_;
    /** @brief 通用清理是否故意失败 / Whether generic cleanup intentionally fails. */
    bool terminate_fails_{false};
};

/** @brief 第一次 terminate 失败、第二次成功的 fake port / Fake port whose first terminate fails
 * and second succeeds. */
class RetryingTerminationPort final : public wspctl::application::RuntimeActivationPort {
public:
    /**
     * @brief 返回未使用的 establish 错误 / Return an unused establish error.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param activation 未使用 activation / Unused activation.
     * @return 领域错误 / Domain error.
     */
    [[nodiscard]] wspctl::application::RuntimeEstablishResult
    establish(const wspctl::domain::RuntimeId& runtime,
              const wspctl::domain::ActivationId& activation) override {
        static_cast<void>(runtime);
        static_cast<void>(activation);
        return std::unexpected(wspctl::application::RuntimeEstablishFailure::rejected_cleanly(
            wspctl::domain::make_error(wspctl::domain::ErrorCode::illegal_transition,
                                       "fake establish should not run")));
    }

    /**
     * @brief 第一次调用失败，后续调用成功 / Fail the first call and succeed thereafter.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param activation 未使用 activation / Unused activation.
     * @return 首次为领域错误，后续成功 / Domain error on the first call, then success.
     */
    [[nodiscard]] wspctl::domain::Result<void>
    terminate(const wspctl::domain::RuntimeId& runtime,
              const wspctl::domain::ActivationId& activation) override {
        static_cast<void>(runtime);
        static_cast<void>(activation);
        ++calls;
        if (calls == 1U) {
            return std::unexpected(wspctl::domain::make_error(
                wspctl::domain::ErrorCode::illegal_transition, "fake terminate failed"));
        }
        return {};
    }

    /** @brief terminate 调用次数 / Number of terminate calls. */
    unsigned int calls{0U};
};

/** @brief 记录无副作用状态读取的 fake port / Fake port recording a side-effect-free status read. */
class RecordingStatusPort final : public wspctl::application::RuntimeStatusPort {
public:
    /**
     * @brief 返回一个 ready runtime 的 allowlisted 状态 / Return allowlisted status for a ready
     * runtime.
     * @param query 已验证只读查询 / Validated read-only query.
     * @return ready 状态或领域错误 / Ready status or a domain error.
     */
    [[nodiscard]] wspctl::domain::Result<wspctl::application::RuntimeStatus>
    observe(const wspctl::application::RuntimeStatusQuery& query) const override {
        ++calls;
        const auto snapshot = wspctl::domain::RuntimeSnapshot::create(
            query.runtime(), wspctl::domain::RuntimeState::ready, query.handle_activation());
        if (!snapshot) {
            return std::unexpected(snapshot.error());
        }
        return wspctl::application::RuntimeStatus::create(query, *snapshot, true,
                                                          std::chrono::milliseconds(123),
                                                          std::chrono::minutes(15), 2U, false);
    }

    /** @brief 被调用次数 / Invocation count. */
    mutable unsigned int calls{0U};
};

/**
 * @brief 返回规范 listing 或故意错配路径的 operator fake port / Operator fake port returning a
 * canonical listing or an intentionally mismatched path.
 */
class RecordingOperatorWorkspacePort final : public wspctl::application::OperatorWorkspaceReadPort {
public:
    /**
     * @brief 构造可选择路径错配的 fake / Construct a fake with optional path mismatch.
     * @param mismatch_path 是否返回另一条路径 / Whether to return another path.
     */
    explicit RecordingOperatorWorkspacePort(const bool mismatch_path) noexcept
        : mismatch_path_(mismatch_path) {}

    /**
     * @brief 返回未使用的 status 错误 / Return an unused status error.
     * @param runtime 未使用 runtime / Unused runtime.
     * @return unavailable 错误 / Unavailable error.
     */
    [[nodiscard]] wspctl::application::OperatorWorkspaceQueryResult<
        wspctl::domain::OperatorWorkspaceStatus>
    status(const wspctl::domain::RuntimeId& runtime) const override {
        static_cast<void>(runtime);
        return std::unexpected(wspctl::application::make_operator_workspace_query_error(
            wspctl::application::OperatorWorkspaceQueryErrorCode::unavailable,
            "status is not used by this application test"));
    }

    /**
     * @brief 返回规范 listing 并记录调用 / Return a canonical listing and record the call.
     * @param runtime 未使用 runtime / Unused runtime.
     * @param path 请求路径 / Requested path.
     * @return 规范 listing 或 fixture 错误 / Canonical listing or a fixture error.
     */
    [[nodiscard]] wspctl::application::OperatorWorkspaceQueryResult<
        wspctl::domain::WorkspaceListing>
    list(const wspctl::domain::RuntimeId& runtime,
         const wspctl::domain::OperatorWorkspacePath& path) const override {
        static_cast<void>(runtime);
        ++calls;
        /** @brief 用于制造路径错配的替代路径 / Alternate path used to produce a mismatch. */
        const auto other_path = wspctl::domain::OperatorWorkspacePath::parse("/workspace/other");
        if (!other_path) {
            return std::unexpected(wspctl::application::make_operator_workspace_query_error(
                wspctl::application::OperatorWorkspaceQueryErrorCode::inconsistent,
                "operator test path fixture is invalid"));
        }
        /** @brief fake 返回的封闭领域 listing / Closed domain listing returned by the fake. */
        const auto listing = wspctl::domain::WorkspaceListing::create(
            mismatch_path_ ? *other_path : path, {}, false);
        if (!listing) {
            return std::unexpected(wspctl::application::make_operator_workspace_query_error(
                wspctl::application::OperatorWorkspaceQueryErrorCode::inconsistent,
                listing.error().message));
        }
        return *listing;
    }

    /** @brief list 调用次数 / Number of list calls. */
    mutable unsigned int calls{0U};

private:
    /** @brief 是否故意返回错配路径 / Whether to intentionally return a mismatched path. */
    bool mismatch_path_{false};
};

/** @brief 测试正常的激活/退役编排 / Test successful activation/retirement orchestration. */
void test_lifecycle_orchestration() {
    wspctl::domain::Runtime runtime = test_runtime();
    const wspctl::domain::ActivationId activation = test_activation();
    wspctl::application::RuntimeActivationService service;
    RecordingPort port;
    expect(service.activate(runtime, activation, port).has_value(),
           "application service activates aggregate through port");
    expect(runtime.state() == wspctl::domain::RuntimeState::ready &&
               runtime.active_activation() == activation,
           "activation leaves aggregate ready with owner");
    expect(port.calls.size() == 1U && port.calls.front().starts_with("establish:"),
           "activate calls only establish exactly once");
    expect(service.stop(runtime, activation, port).has_value(),
           "application service stops aggregate through port");
    expect(runtime.state() == wspctl::domain::RuntimeState::dormant &&
               !runtime.active_activation().has_value(),
           "normal stop returns aggregate to dormant");
    expect(port.calls.size() == 2U && port.calls.back().starts_with("terminate:"),
           "stop invokes termination after a legal domain transition");
}

/** @brief 测试三种建立失败处置驱动不同领域转换 / Test that the three establishment-failure
 * dispositions drive distinct domain transitions. */
void test_establish_failure_dispositions() {
    /** @brief 当前 activation owner / Current activation owner. */
    const wspctl::domain::ActivationId activation = test_activation();
    /** @brief 被测 lifecycle 用例 / Lifecycle use case under test. */
    const wspctl::application::RuntimeActivationService service;

    {
        /** @brief clean rejection 场景 runtime / Runtime for the clean-rejection scenario. */
        wspctl::domain::Runtime runtime = test_runtime();
        /** @brief clean rejection port / Clean-rejection port. */
        EstablishOutcomePort port(
            wspctl::application::RuntimeEstablishFailureDisposition::rejected_cleanly);
        /** @brief clean rejection 激活结果 / Clean-rejection activation result. */
        const auto activated = service.activate(runtime, activation, port);
        expect(!activated.has_value() && port.establish_calls == 1U && port.terminate_calls == 0U &&
                   runtime.state() == wspctl::domain::RuntimeState::failed &&
                   !runtime.active_activation().has_value() && !runtime.cleanup_pending() &&
                   !runtime.quarantined(),
               "clean rejection fails without fabricating cleanup or quarantine");
    }

    {
        /** @brief known-partial 场景 runtime / Runtime for the known-partial scenario. */
        wspctl::domain::Runtime runtime = test_runtime();
        /** @brief cleanup-required port / Cleanup-required port. */
        EstablishOutcomePort port(
            wspctl::application::RuntimeEstablishFailureDisposition::cleanup_required);
        /** @brief cleanup-required 激活结果 / Cleanup-required activation result. */
        const auto activated = service.activate(runtime, activation, port);
        expect(!activated.has_value() && port.establish_calls == 1U && port.terminate_calls == 1U &&
                   runtime.state() == wspctl::domain::RuntimeState::failed &&
                   !runtime.active_activation().has_value() && !runtime.cleanup_pending() &&
                   !runtime.quarantined(),
               "known partial establishment runs generic termination before failing");
    }

    {
        /** @brief unknown-outcome 场景 runtime / Runtime for the unknown-outcome scenario. */
        wspctl::domain::Runtime runtime = test_runtime();
        /** @brief unknown-outcome port / Unknown-outcome port. */
        EstablishOutcomePort port(
            wspctl::application::RuntimeEstablishFailureDisposition::outcome_unknown);
        /** @brief unknown-outcome 激活结果 / Unknown-outcome activation result. */
        const auto activated = service.activate(runtime, activation, port);
        expect(!activated.has_value() && port.establish_calls == 1U && port.terminate_calls == 0U &&
                   runtime.state() == wspctl::domain::RuntimeState::failed &&
                   !runtime.active_activation().has_value() && !runtime.cleanup_pending() &&
                   runtime.quarantined(),
               "unknown establishment is quarantined without guessing generic termination");
    }

    {
        /** @brief 已知部分建立且清理失败的 runtime / Runtime whose known-partial
         * establishment cleanup fails. */
        wspctl::domain::Runtime runtime = test_runtime();
        /** @brief 清理故意失败的 port / Port whose cleanup intentionally fails. */
        EstablishOutcomePort port(
            wspctl::application::RuntimeEstablishFailureDisposition::cleanup_required, true);
        /** @brief 清理失败的激活结果 / Activation result whose cleanup fails. */
        const auto activated = service.activate(runtime, activation, port);
        expect(!activated.has_value() && activated.error().message == "fake terminate failed" &&
                   port.establish_calls == 1U && port.terminate_calls == 1U &&
                   runtime.state() == wspctl::domain::RuntimeState::failed &&
                   !runtime.active_activation().has_value() && runtime.cleanup_pending() &&
                   !runtime.quarantined(),
               "failed known-partial cleanup retains private cleanup ownership");
    }
}

/** @brief 测试停止失败由领域保留 owner，且只有同一 owner 可重试 / Test that the domain retains
 * ownership after stop failure and only the same owner may retry. */
void test_stop_failure_compensation_and_retry() {
    /** @brief 被测 runtime / Runtime under test. */
    wspctl::domain::Runtime runtime = test_runtime();
    /** @brief 正确 activation owner / Correct activation owner. */
    const wspctl::domain::ActivationId owner = test_activation();
    /** @brief 非 owner activation / Non-owner activation. */
    const auto stranger = wspctl::domain::ActivationId::parse("application-stranger");
    expect(stranger.has_value(), "parse non-owner application activation");
    if (!stranger) {
        return;
    }
    /** @brief 被测 lifecycle 用例 / Lifecycle use case under test. */
    const wspctl::application::RuntimeActivationService service;
    /** @brief 用于先建立 ready runtime 的成功 port / Successful port used to establish a ready
     * runtime. */
    RecordingPort activation_port;
    expect(service.activate(runtime, owner, activation_port).has_value(),
           "activate runtime before stop retry test");

    /** @brief 首次清理失败、随后成功的 port / Port failing cleanup once before succeeding. */
    RetryingTerminationPort termination_port;
    /** @brief 非 owner 的正常停止结果 / Normal-stop result for a non-owner. */
    const auto stranger_stop = service.stop(runtime, *stranger, termination_port);
    expect(!stranger_stop.has_value() &&
               stranger_stop.error().code == wspctl::domain::ErrorCode::activation_mismatch &&
               termination_port.calls == 0U &&
               runtime.state() == wspctl::domain::RuntimeState::ready &&
               runtime.active_activation() == owner,
           "application rejects non-owner before invoking external cleanup");

    /** @brief owner 的首次停止结果 / Owner's first stop result. */
    const auto first_stop = service.stop(runtime, owner, termination_port);
    expect(!first_stop.has_value() && termination_port.calls == 1U &&
               runtime.state() == wspctl::domain::RuntimeState::failed &&
               !runtime.active_activation().has_value() && runtime.cleanup_pending(),
           "failed external cleanup leaves domain-owned retry state");

    /** @brief 非 owner 的失败清理重试 / Failed-cleanup retry by a non-owner. */
    const auto stranger_retry = service.stop(runtime, *stranger, termination_port);
    expect(!stranger_retry.has_value() &&
               stranger_retry.error().code == wspctl::domain::ErrorCode::activation_mismatch &&
               termination_port.calls == 1U && runtime.cleanup_pending(),
           "non-owner cannot invoke failed cleanup retry");

    expect(service.stop(runtime, owner, termination_port).has_value() &&
               termination_port.calls == 2U &&
               runtime.state() == wspctl::domain::RuntimeState::failed &&
               !runtime.cleanup_pending(),
           "same owner completes failed cleanup without reviving runtime");
}

/** @brief 测试执行中停止完成后仍保持 failed / Test that a completed stop during execution remains
 * failed. */
void test_executing_stop_remains_failed() {
    /** @brief 被测 runtime / Runtime under test. */
    wspctl::domain::Runtime runtime = test_runtime();
    /** @brief 当前 activation owner / Current activation owner. */
    const wspctl::domain::ActivationId owner = test_activation();
    /** @brief 被测 lifecycle 用例 / Lifecycle use case under test. */
    const wspctl::application::RuntimeActivationService service;
    /** @brief 成功执行所有效果的 port / Port succeeding all effects. */
    RecordingPort port;
    expect(service.activate(runtime, owner, port).has_value() &&
               runtime.begin_execution(owner).has_value(),
           "enter executing state before application abnormal stop");
    expect(service.stop(runtime, owner, port).has_value(),
           "application completes abnormal execution cleanup");
    expect(runtime.state() == wspctl::domain::RuntimeState::failed &&
               !runtime.active_activation().has_value() && !runtime.cleanup_pending() &&
               port.calls.size() == 2U && port.calls.back().starts_with("terminate:"),
           "abnormal stop cannot revive or normalize a failed execution");
}

/** @brief 测试应用状态查询只使用 read port 且验证 health 不变量 / Test status queries through the
 * read port and validate health invariants. */
void test_status_read_port_contract() {
    const auto runtime = wspctl::domain::RuntimeId::parse("123e4567-e89b-12d3-a456-426614174000");
    const wspctl::domain::ActivationId activation = test_activation();
    expect(runtime.has_value(), "parse runtime for status query");
    if (!runtime) {
        return;
    }
    const wspctl::application::RuntimeStatusQuery query(*runtime, activation);
    RecordingStatusPort port;
    const auto observed = port.observe(query);
    expect(observed.has_value(), "inspect runtime through read-only status port");
    expect(port.calls == 1U, "status owner invokes the read port exactly once");
    if (observed) {
        expect(observed->snapshot().state() == wspctl::domain::RuntimeState::ready &&
                   observed->handle_activation_matches() && observed->supervisor_alive() &&
                   observed->idle_for() == std::chrono::milliseconds(123) &&
                   observed->idle_ttl() == std::chrono::minutes(15) &&
                   observed->borrowed_dispatches() == 2U && !observed->cleanup_pending(),
               "status preserves allowlisted operating indicators");
    }

    const auto dormant = wspctl::domain::RuntimeSnapshot::create(
        *runtime, wspctl::domain::RuntimeState::dormant, std::nullopt);
    expect(dormant.has_value(), "construct dormant status snapshot");
    if (dormant) {
        const auto invalid = wspctl::application::RuntimeStatus::create(
            query, *dormant, true, std::nullopt, std::chrono::minutes(15), 0U, false);
        expect(!invalid.has_value(), "reject dormant runtime claiming a live reusable supervisor");
    }
}

/**
 * @brief 测试 operator 查询用例拒绝 adapter 的路径归因错误 / Test that the operator query use case
 * rejects path-attribution errors from an adapter.
 */
void test_operator_workspace_query_boundary() {
    /** @brief 查询 runtime / Runtime being queried. */
    const wspctl::domain::Runtime runtime = test_runtime();
    /** @brief 查询路径 / Path being queried. */
    const auto path = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    expect(path.has_value(), "parse application operator query path");
    if (!path) {
        return;
    }
    /** @brief 被测 operator 查询用例 / Operator query use case under test. */
    const wspctl::application::OperatorWorkspaceQueryService service;
    /** @brief 返回匹配路径的 port / Port returning the requested path. */
    RecordingOperatorWorkspacePort matching_port(false);
    /** @brief 匹配路径查询结果 / Matching-path query result. */
    const auto matching = service.list(runtime.id(), *path, matching_port);
    expect(matching.has_value() && matching->path() == *path && matching_port.calls == 1U,
           "operator query accepts a canonical listing attributed to the requested path");

    /** @brief 返回错配路径的 port / Port returning a mismatched path. */
    RecordingOperatorWorkspacePort mismatching_port(true);
    /** @brief 错配路径查询结果 / Mismatched-path query result. */
    const auto mismatching = service.list(runtime.id(), *path, mismatching_port);
    expect(!mismatching.has_value() &&
               mismatching.error().code ==
                   wspctl::application::OperatorWorkspaceQueryErrorCode::inconsistent &&
               mismatching_port.calls == 1U,
           "operator query rejects a listing attributed to another path");
}

} // namespace

/**
 * @brief application CTest 入口 / Application CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_lifecycle_orchestration();
    test_establish_failure_dispositions();
    test_stop_failure_compensation_and_retry();
    test_executing_stop_remains_failed();
    test_status_read_port_contract();
    test_operator_workspace_query_boundary();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
