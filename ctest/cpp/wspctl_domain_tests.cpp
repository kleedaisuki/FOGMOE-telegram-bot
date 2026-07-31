#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/domain/runtime.hpp"

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
    expect(
        !wspctl::domain::ExecutionBudget::create(std::chrono::milliseconds::zero(), 1U).has_value(),
        "reject zero wall-clock budget");
    expect(!wspctl::domain::ExecutionBudget::create(std::chrono::milliseconds(1), 0U).has_value(),
           "reject zero output budget");
}

/** @brief 测试 activation 所有权与完整生命周期 / Test activation ownership and the complete
 * lifecycle. */
void test_activation_ownership() {
    wspctl::domain::Runtime runtime(test_runtime_id());
    const wspctl::domain::ActivationId owner = test_activation("activation-owner");
    const wspctl::domain::ActivationId stranger = test_activation("activation-stranger");
    expect(!runtime.reusable(), "dormant runtime is not a reusable live session");
    expect(runtime.begin_activation(owner).has_value(), "begin owned activation");
    expect(runtime.state() == wspctl::domain::RuntimeState::activating &&
               runtime.active_activation() == owner && !runtime.reusable(),
           "activation establishes unique owner");
    const auto stranger_ready = runtime.mark_ready(stranger);
    expect(!stranger_ready.has_value() &&
               stranger_ready.error().code == wspctl::domain::ErrorCode::activation_mismatch,
           "reject ready transition from non-owner activation");
    expect(runtime.mark_ready(owner).has_value() && runtime.reusable(),
           "owner marks runtime ready and reusable");
    const auto stranger_execute = runtime.begin_execution(stranger);
    expect(!stranger_execute.has_value() &&
               stranger_execute.error().code == wspctl::domain::ErrorCode::activation_mismatch,
           "reject execute transition from non-owner activation");
    expect(runtime.begin_execution(owner).has_value() && !runtime.reusable(),
           "owner starts non-reusable execution");
    expect(runtime.finish_execution(owner).has_value() && runtime.reusable(),
           "owner finishes execution back to reusable ready");
    /** @brief 非 owner 的停止结果 / Stop result for a non-owner. */
    const auto stranger_stop = runtime.begin_stop(stranger);
    expect(!stranger_stop.has_value() &&
               stranger_stop.error().code == wspctl::domain::ErrorCode::activation_mismatch,
           "reject stop transition from non-owner activation");
    expect(runtime.begin_stop(owner).has_value() && !runtime.reusable(),
           "owner begins non-reusable normal stop");
    expect(runtime.state() == wspctl::domain::RuntimeState::retiring &&
               runtime.active_activation() == owner && !runtime.cleanup_pending(),
           "normal stop retains active owner while retiring");
    /** @brief 非 owner 伪造的停止失败 / Stop failure forged by a non-owner. */
    const auto stranger_failure = runtime.record_stop_failure(stranger);
    expect(!stranger_failure.has_value() &&
               stranger_failure.error().code == wspctl::domain::ErrorCode::activation_mismatch &&
               runtime.state() == wspctl::domain::RuntimeState::retiring &&
               runtime.active_activation() == owner && !runtime.cleanup_pending(),
           "non-owner cannot convert normal retirement into failed cleanup");
    /** @brief 非 owner 的停止完成结果 / Stop-completion result for a non-owner. */
    const auto stranger_finish = runtime.finish_stop(stranger);
    expect(!stranger_finish.has_value() &&
               stranger_finish.error().code == wspctl::domain::ErrorCode::activation_mismatch,
           "reject stop completion from non-owner activation");
    expect(runtime.finish_stop(owner).has_value(), "owner completes normal stop");
    expect(runtime.state() == wspctl::domain::RuntimeState::dormant &&
               !runtime.active_activation().has_value() && !runtime.cleanup_pending() &&
               !runtime.reusable(),
           "completed normal stop clears all activation ownership");
}

/** @brief 测试 activation 建立失败只能由 owner 拒绝 / Test that only the owner can reject a failed
 * activation establishment. */
void test_activation_rejection_requires_owner() {
    wspctl::domain::Runtime runtime(test_runtime_id());
    const wspctl::domain::ActivationId owner = test_activation("activation-owner");
    const wspctl::domain::ActivationId stranger = test_activation("activation-stranger");
    expect(runtime.begin_activation(owner).has_value(), "begin activation before failure");
    /** @brief 非 owner 的 activation rejection / Activation rejection by a non-owner. */
    const auto stranger_rejection = runtime.reject_activation(stranger);
    expect(!stranger_rejection.has_value() &&
               stranger_rejection.error().code == wspctl::domain::ErrorCode::activation_mismatch &&
               runtime.state() == wspctl::domain::RuntimeState::activating &&
               runtime.active_activation() == owner,
           "non-owner cannot erase an activating owner");
    expect(runtime.reject_activation(owner).has_value(), "owner rejects failed establishment");
    expect(runtime.state() == wspctl::domain::RuntimeState::failed &&
               !runtime.active_activation().has_value() && !runtime.cleanup_pending() &&
               !runtime.quarantined() && !runtime.reusable(),
           "failed establishment becomes terminal without fabricated cleanup ownership");
}

/** @brief 测试 unknown establishment 只能由 owner 隔离且不能走通用 stop / Test that only the
 * owner can quarantine unknown establishment and generic stop cannot guess its cleanup. */
void test_unknown_establishment_quarantine() {
    wspctl::domain::Runtime runtime(test_runtime_id());
    const wspctl::domain::ActivationId owner = test_activation("activation-owner");
    const wspctl::domain::ActivationId stranger = test_activation("activation-stranger");
    expect(runtime.begin_activation(owner).has_value(), "begin activation before quarantine");
    /** @brief 非 owner 的 quarantine 结果 / Quarantine result for a non-owner. */
    const auto stranger_quarantine = runtime.quarantine_activation(stranger);
    expect(!stranger_quarantine.has_value() &&
               stranger_quarantine.error().code == wspctl::domain::ErrorCode::activation_mismatch &&
               runtime.state() == wspctl::domain::RuntimeState::activating &&
               runtime.active_activation() == owner && !runtime.quarantined(),
           "non-owner cannot quarantine or erase activating owner");
    expect(runtime.quarantine_activation(owner).has_value(),
           "owner quarantines unknown establishment");
    expect(runtime.state() == wspctl::domain::RuntimeState::failed && runtime.quarantined() &&
               !runtime.active_activation().has_value() && !runtime.cleanup_pending() &&
               !runtime.reusable(),
           "quarantine is a non-reusable failed state without guessed cleanup ownership");
    /** @brief 被禁止的通用停止结果 / Forbidden generic-stop result. */
    const auto guessed_stop = runtime.begin_stop(owner);
    expect(!guessed_stop.has_value() &&
               guessed_stop.error().code == wspctl::domain::ErrorCode::illegal_transition &&
               runtime.quarantined(),
           "generic stop cannot guess recovery for quarantined establishment");
}

/** @brief 测试异常停止保留私有清理 ownership 并允许同 owner 重试 / Test that abnormal stop
 * retains private cleanup ownership and permits retry only by the same owner. */
void test_failed_stop_preserves_cleanup_owner() {
    wspctl::domain::Runtime runtime(test_runtime_id());
    const wspctl::domain::ActivationId owner = test_activation("activation-owner");
    const wspctl::domain::ActivationId stranger = test_activation("activation-stranger");
    expect(runtime.begin_activation(owner).has_value() && runtime.mark_ready(owner).has_value() &&
               runtime.begin_execution(owner).has_value(),
           "enter executing state before abnormal stop");

    /** @brief 非 owner 的异常停止结果 / Abnormal-stop result for a non-owner. */
    const auto stranger_stop = runtime.begin_stop(stranger);
    expect(!stranger_stop.has_value() &&
               stranger_stop.error().code == wspctl::domain::ErrorCode::activation_mismatch &&
               runtime.state() == wspctl::domain::RuntimeState::executing &&
               runtime.active_activation() == owner,
           "non-owner cannot fail an executing runtime");
    expect(runtime.begin_stop(owner).has_value(), "owner begins abnormal stop");
    expect(runtime.state() == wspctl::domain::RuntimeState::failed &&
               !runtime.active_activation().has_value() && runtime.cleanup_pending(),
           "abnormal stop hides active owner but retains private cleanup ownership");

    /** @brief 非 owner 的清理重试结果 / Cleanup-retry result for a non-owner. */
    const auto stranger_retry = runtime.begin_stop(stranger);
    expect(!stranger_retry.has_value() &&
               stranger_retry.error().code == wspctl::domain::ErrorCode::activation_mismatch &&
               runtime.cleanup_pending(),
           "non-owner cannot take over failed cleanup");
    expect(runtime.record_stop_failure(owner).has_value() && runtime.cleanup_pending(),
           "failed cleanup keeps ownership available for retry");
    expect(runtime.begin_stop(owner).has_value(), "same owner retries failed cleanup");
    expect(runtime.finish_stop(owner).has_value(), "same owner completes failed cleanup");
    expect(runtime.state() == wspctl::domain::RuntimeState::failed &&
               !runtime.active_activation().has_value() && !runtime.cleanup_pending(),
           "completed abnormal cleanup remains failed without a dangling owner");

    /** @brief 已完成清理后的重复停止 / Repeated stop after completed cleanup. */
    const auto repeated_stop = runtime.begin_stop(owner);
    expect(!repeated_stop.has_value() &&
               repeated_stop.error().code == wspctl::domain::ErrorCode::illegal_transition,
           "completed failed cleanup cannot be reopened implicitly");
}

/** @brief 测试快照只能投影完整领域状态 / Test that snapshots can project only complete domain
 * states. */
void test_runtime_snapshots_preserve_lifecycle_invariants() {
    const wspctl::domain::RuntimeId runtime_id = test_runtime_id();
    const wspctl::domain::ActivationId activation = test_activation("snapshot-owner");
    const auto dormant = wspctl::domain::RuntimeSnapshot::create(
        runtime_id, wspctl::domain::RuntimeState::dormant, std::nullopt);
    expect(dormant.has_value() && dormant->runtime() == runtime_id &&
               dormant->state() == wspctl::domain::RuntimeState::dormant &&
               !dormant->active_activation().has_value(),
           "construct payload-free dormant snapshot");
    expect(!wspctl::domain::RuntimeSnapshot::create(runtime_id, wspctl::domain::RuntimeState::ready,
                                                    std::nullopt)
                .has_value(),
           "reject ready snapshot without activation owner");
    expect(!wspctl::domain::RuntimeSnapshot::create(
                runtime_id, wspctl::domain::RuntimeState::failed, activation)
                .has_value(),
           "reject failed snapshot retaining an activation owner");

    wspctl::domain::Runtime aggregate(runtime_id);
    expect(aggregate.begin_activation(activation).has_value(),
           "begin aggregate activation for snapshot projection");
    const wspctl::domain::RuntimeSnapshot activating = aggregate.snapshot();
    expect(activating.state() == wspctl::domain::RuntimeState::activating &&
               activating.active_activation() == activation,
           "aggregate snapshot preserves active owner");
    expect(wspctl::domain::runtime_state_name(wspctl::domain::RuntimeState::executing) ==
               "executing",
           "render stable runtime-state vocabulary");
}

/**
 * @brief 测试 workspace listing 只允许规范、唯一且有界的目录项 / Test that workspace listings
 * admit only canonical, unique, bounded entries.
 */
void test_workspace_listing_invariants() {
    /** @brief 已验证 workspace 根路径 / Validated workspace root path. */
    const auto path = wspctl::domain::OperatorWorkspacePath::parse("/workspace");
    /** @brief 排序后靠后的测试项 / Test entry ordered later after canonicalization. */
    const auto later = wspctl::domain::WorkspaceEntry::create(
        "z.txt", wspctl::domain::WorkspaceEntryKind::regular_file, 2U);
    /** @brief 排序后靠前的测试项 / Test entry ordered earlier after canonicalization. */
    const auto earlier = wspctl::domain::WorkspaceEntry::create(
        "a.txt", wspctl::domain::WorkspaceEntryKind::regular_file, 1U);
    expect(path.has_value() && later.has_value() && earlier.has_value(),
           "construct workspace listing fixtures");
    if (!path || !later || !earlier) {
        return;
    }

    /** @brief 输入顺序非规范的 listing / Listing supplied in non-canonical order. */
    const auto canonical = wspctl::domain::WorkspaceListing::create(
        *path, std::vector<wspctl::domain::WorkspaceEntry>{*later, *earlier}, false);
    expect(canonical.has_value() && canonical->path() == *path && !canonical->truncated() &&
               canonical->entries().size() == 2U &&
               canonical->entries().front().encoded_name() == "a.txt" &&
               canonical->entries().back().encoded_name() == "z.txt",
           "listing factory canonicalizes encoded-name order");

    /** @brief 未填满上限却声称截断的 listing / Listing claiming truncation below the fixed cap. */
    const auto false_truncation = wspctl::domain::WorkspaceListing::create(
        *path, std::vector<wspctl::domain::WorkspaceEntry>{*later, *earlier}, true);
    expect(!false_truncation.has_value() &&
               false_truncation.error().code == wspctl::domain::ErrorCode::invalid_budget,
           "listing factory rejects false truncation below the fixed cap");

    /** @brief 含重复名称的 listing / Listing containing a duplicate name. */
    const auto duplicate = wspctl::domain::WorkspaceListing::create(
        *path, std::vector<wspctl::domain::WorkspaceEntry>{*earlier, *earlier}, false);
    expect(!duplicate.has_value() &&
               duplicate.error().code == wspctl::domain::ErrorCode::invalid_identity,
           "listing factory rejects duplicate encoded names");

    /** @brief 超过固定条目上限的 listing / Listing above the fixed entry cap. */
    std::vector<wspctl::domain::WorkspaceEntry> bounded_entries;
    bounded_entries.reserve(wspctl::domain::kOperatorWorkspaceListingLimit + 1U);
    /** @brief 唯一测试目录项的序号 / Sequence number of each unique test directory entry. */
    for (std::size_t index = 0U; index <= wspctl::domain::kOperatorWorkspaceListingLimit; ++index) {
        /** @brief 当前唯一测试目录项 / Current unique test directory entry. */
        const auto entry = wspctl::domain::WorkspaceEntry::create(
            "entry-" + std::to_string(index), wspctl::domain::WorkspaceEntryKind::regular_file, 0U);
        expect(entry.has_value(), "construct bounded-listing test entry");
        if (!entry) {
            return;
        }
        bounded_entries.push_back(*entry);
    }
    /** @brief 恰好填满固定上限的条目 / Entries exactly saturating the fixed cap. */
    std::vector<wspctl::domain::WorkspaceEntry> saturated_entries = bounded_entries;
    saturated_entries.pop_back();
    /** @brief 合法截断 factory 结果 / Valid truncated factory result. */
    const auto saturated =
        wspctl::domain::WorkspaceListing::create(*path, std::move(saturated_entries), true);
    expect(saturated.has_value() && saturated->truncated() &&
               saturated->entries().size() == wspctl::domain::kOperatorWorkspaceListingLimit,
           "listing factory accepts truncation only at the saturated fixed cap");
    /** @brief 超上限 factory 结果 / Above-cap factory result. */
    const auto excessive =
        wspctl::domain::WorkspaceListing::create(*path, std::move(bounded_entries), false);
    expect(!excessive.has_value() &&
               excessive.error().code == wspctl::domain::ErrorCode::invalid_budget,
           "listing factory rejects collections above the fixed cap");
}

} // namespace

/**
 * @brief domain CTest 入口 / Domain CTest entry point.
 * @return 成功为 0 / Zero on success.
 */
int main() {
    test_value_objects();
    test_activation_ownership();
    test_activation_rejection_requires_owner();
    test_unknown_establishment_quarantine();
    test_failed_stop_preserves_cleanup_owner();
    test_runtime_snapshots_preserve_lifecycle_invariants();
    test_workspace_listing_invariants();
    return g_failures == 0U ? EXIT_SUCCESS : EXIT_FAILURE;
}
