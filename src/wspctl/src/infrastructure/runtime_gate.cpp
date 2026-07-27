#include "wspctl/infrastructure/runtime_gate.hpp"

#include <utility>

namespace wspctl {

RuntimeLease::RuntimeLease(RuntimeExecutionGate* gate, std::string runtime_key)
    : gate_(gate), runtime_key_(std::move(runtime_key)) {}

RuntimeLease::RuntimeLease(RuntimeLease&& other) noexcept
    : gate_(std::exchange(other.gate_, nullptr)), runtime_key_(std::move(other.runtime_key_)) {}

RuntimeLease& RuntimeLease::operator=(RuntimeLease&& other) noexcept {
    if (this != &other) {
        if (gate_ != nullptr) {
            gate_->release(runtime_key_);
        }
        gate_ = std::exchange(other.gate_, nullptr);
        runtime_key_ = std::move(other.runtime_key_);
    }
    return *this;
}

RuntimeLease::~RuntimeLease() {
    if (gate_ != nullptr) {
        gate_->release(runtime_key_);
    }
}

Result<RuntimeLease> RuntimeExecutionGate::try_acquire(const std::string& runtime_key) {
    std::lock_guard lock(mutex_);
    const auto [unused, inserted] = active_.insert(runtime_key);
    static_cast<void>(unused);
    if (!inserted) {
        return std::unexpected(make_error(ErrorCode::busy, "runtime already has an active command"));
    }
    return RuntimeLease(this, runtime_key);
}

void RuntimeExecutionGate::release(const std::string& runtime_key) noexcept {
    std::lock_guard lock(mutex_);
    active_.erase(runtime_key);
}

}  // namespace wspctl
