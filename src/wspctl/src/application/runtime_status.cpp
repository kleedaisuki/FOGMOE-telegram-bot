#include "wspctl/application/runtime_status.hpp"

#include <utility>

namespace wspctl::application {

RuntimeStatusQuery::RuntimeStatusQuery(domain::RuntimeId runtime,
                                       domain::ActivationId handle_activation) noexcept
    : runtime_(std::move(runtime)), handle_activation_(std::move(handle_activation)) {}

const domain::RuntimeId& RuntimeStatusQuery::runtime() const noexcept { return runtime_; }

const domain::ActivationId& RuntimeStatusQuery::handle_activation() const noexcept {
    return handle_activation_;
}

RuntimeStatus::RuntimeStatus(domain::RuntimeSnapshot snapshot, const bool handle_activation_matches,
                             const bool supervisor_alive,
                             std::optional<std::chrono::milliseconds> idle_for,
                             const std::chrono::milliseconds idle_ttl,
                             const std::uint64_t borrowed_dispatches,
                             const bool cleanup_pending) noexcept
    : snapshot_(std::move(snapshot)), handle_activation_matches_(handle_activation_matches),
      supervisor_alive_(supervisor_alive), idle_for_(std::move(idle_for)), idle_ttl_(idle_ttl),
      borrowed_dispatches_(borrowed_dispatches), cleanup_pending_(cleanup_pending) {}

domain::Result<RuntimeStatus> RuntimeStatus::create(
    const RuntimeStatusQuery& query, domain::RuntimeSnapshot snapshot, const bool supervisor_alive,
    std::optional<std::chrono::milliseconds> idle_for, const std::chrono::milliseconds idle_ttl,
    const std::uint64_t borrowed_dispatches, const bool cleanup_pending) {
    if (snapshot.runtime() != query.runtime()) {
        return std::unexpected(
            domain::make_error(domain::ErrorCode::activation_mismatch,
                               "runtime status query does not match snapshot runtime"));
    }
    if (idle_ttl.count() <= 0) {
        return std::unexpected(domain::make_error(domain::ErrorCode::invalid_budget,
                                                  "runtime status idle TTL must be positive"));
    }
    if (idle_for.has_value() && idle_for->count() < 0) {
        return std::unexpected(domain::make_error(domain::ErrorCode::invalid_budget,
                                                  "runtime status idle age cannot be negative"));
    }
    if (idle_for.has_value() && snapshot.state() != domain::RuntimeState::ready) {
        return std::unexpected(domain::make_error(domain::ErrorCode::illegal_transition,
                                                  "only a ready runtime may expose an idle age"));
    }
    if (supervisor_alive && (snapshot.state() == domain::RuntimeState::dormant ||
                             snapshot.state() == domain::RuntimeState::failed)) {
        return std::unexpected(domain::make_error(
            domain::ErrorCode::illegal_transition,
            "a dormant or failed runtime cannot expose a healthy live supervisor"));
    }
    const bool handle_activation_matches =
        snapshot.active_activation().has_value() &&
        *snapshot.active_activation() == query.handle_activation();
    return RuntimeStatus(std::move(snapshot), handle_activation_matches, supervisor_alive,
                         std::move(idle_for), idle_ttl, borrowed_dispatches, cleanup_pending);
}

const domain::RuntimeSnapshot& RuntimeStatus::snapshot() const noexcept { return snapshot_; }

bool RuntimeStatus::handle_activation_matches() const noexcept {
    return handle_activation_matches_;
}

bool RuntimeStatus::supervisor_alive() const noexcept { return supervisor_alive_; }

const std::optional<std::chrono::milliseconds>& RuntimeStatus::idle_for() const noexcept {
    return idle_for_;
}

std::chrono::milliseconds RuntimeStatus::idle_ttl() const noexcept { return idle_ttl_; }

std::uint64_t RuntimeStatus::borrowed_dispatches() const noexcept { return borrowed_dispatches_; }

bool RuntimeStatus::cleanup_pending() const noexcept { return cleanup_pending_; }

domain::Result<RuntimeStatus> RuntimeStatusService::inspect(const RuntimeStatusQuery& query,
                                                            const RuntimeStatusPort& port) const {
    return port.observe(query);
}

} // namespace wspctl::application
