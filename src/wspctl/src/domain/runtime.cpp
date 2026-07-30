#include "wspctl/domain/runtime.hpp"

#include <algorithm>
#include <utility>

namespace wspctl::domain {
namespace {

/**
 * @brief 判断小写十六进制字符 / Check a lowercase hexadecimal character.
 * @param character 待检查字符 / Character to inspect.
 * @return 是否为小写十六进制 / Whether it is lowercase hexadecimal.
 */
[[nodiscard]] bool is_lower_hex(const char character) noexcept {
    return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
}

/**
 * @brief 判断 wire-safe 领域标识 / Check a wire-safe domain identifier.
 * @param value 待检查文本 / Text to inspect.
 * @return 是否满足有限 ASCII 标识语法 / Whether it satisfies the bounded ASCII identifier grammar.
 */
[[nodiscard]] bool is_safe_identifier(const std::string_view value) noexcept {
    if (value.empty() || value.size() > 128U) {
        return false;
    }
    return std::ranges::all_of(value, [](const unsigned char character) noexcept {
        return (character >= static_cast<unsigned char>('a') &&
                character <= static_cast<unsigned char>('z')) ||
               (character >= static_cast<unsigned char>('A') &&
                character <= static_cast<unsigned char>('Z')) ||
               (character >= static_cast<unsigned char>('0') &&
                character <= static_cast<unsigned char>('9')) ||
               character == static_cast<unsigned char>('_') ||
               character == static_cast<unsigned char>('-') ||
               character == static_cast<unsigned char>('.') ||
               character == static_cast<unsigned char>(':');
    });
}

/**
 * @brief 构造非法状态迁移错误 / Construct an illegal-state-transition error.
 * @param operation 操作名称 / Operation name.
 * @return 状态迁移错误 / State-transition error.
 */
[[nodiscard]] Error illegal_transition(const std::string_view operation) {
    return make_error(ErrorCode::illegal_transition,
                      "illegal runtime lifecycle transition: " + std::string(operation));
}

/**
 * @brief 判断状态是否必须拥有 activation / Check whether a state must own an activation.
 * @param state 待检查生命周期状态 / Lifecycle state to inspect.
 * @return 状态是否必须带 activation / Whether the state must carry an activation.
 */
[[nodiscard]] bool state_requires_activation(const RuntimeState state) noexcept {
    return state == RuntimeState::activating || state == RuntimeState::ready ||
           state == RuntimeState::executing || state == RuntimeState::retiring;
}

} // namespace

Error make_error(const ErrorCode code, std::string message) {
    return Error{.code = code, .message = std::move(message)};
}

RuntimeId::RuntimeId(std::string value) : value_(std::move(value)) {}

Result<RuntimeId> RuntimeId::parse(std::string value) {
    /** @brief canonical UUID 的固定文本长度 / Fixed textual length of a canonical UUID. */
    constexpr std::size_t kUuidLength{36U};
    const bool valid =
        value.size() == kUuidLength &&
        std::ranges::all_of(value, [position = std::size_t{0U}](const char character) mutable {
            const bool hyphen =
                position == 8U || position == 13U || position == 18U || position == 23U;
            ++position;
            return hyphen ? character == '-' : is_lower_hex(character);
        });
    if (!valid) {
        return std::unexpected(make_error(ErrorCode::invalid_identity,
                                          "runtime_key must be a canonical lowercase UUID"));
    }
    return RuntimeId(std::move(value));
}

const std::string& RuntimeId::value() const noexcept { return value_; }

ActivationId::ActivationId(std::string value) : value_(std::move(value)) {}

Result<ActivationId> ActivationId::parse(std::string value) {
    if (!is_safe_identifier(value)) {
        return std::unexpected(make_error(ErrorCode::invalid_identity,
                                          "activation_id must be a bounded safe identifier"));
    }
    return ActivationId(std::move(value));
}

const std::string& ActivationId::value() const noexcept { return value_; }

CommandId::CommandId(std::string value) : value_(std::move(value)) {}

Result<CommandId> CommandId::parse(std::string value) {
    if (!is_safe_identifier(value)) {
        return std::unexpected(make_error(ErrorCode::invalid_identity,
                                          "request_id must be a bounded safe identifier"));
    }
    return CommandId(std::move(value));
}

const std::string& CommandId::value() const noexcept { return value_; }

Sha256Digest::Sha256Digest(std::string value) : value_(std::move(value)) {}

Result<Sha256Digest> Sha256Digest::parse(std::string value) {
    /** @brief SHA-256 小写十六进制的固定字符数 / Fixed character count of lowercase hexadecimal
     * SHA-256. */
    constexpr std::size_t kSha256HexLength{64U};
    if (value.size() != kSha256HexLength || !std::ranges::all_of(value, is_lower_hex)) {
        return std::unexpected(
            make_error(ErrorCode::invalid_identity,
                       "SHA-256 digest must be 64 lowercase hexadecimal characters"));
    }
    return Sha256Digest(std::move(value));
}

const std::string& Sha256Digest::value() const noexcept { return value_; }

ExecutionBudget::ExecutionBudget(const std::chrono::milliseconds wall_clock,
                                 const std::size_t output_bytes) noexcept
    : wall_clock_(wall_clock), output_bytes_(output_bytes) {}

Result<ExecutionBudget> ExecutionBudget::create(const std::chrono::milliseconds wall_clock,
                                                const std::size_t output_bytes) {
    if (wall_clock.count() <= 0 || output_bytes == 0U) {
        return std::unexpected(
            make_error(ErrorCode::invalid_budget,
                       "execution wall-clock and output budgets must both be non-zero"));
    }
    return ExecutionBudget(wall_clock, output_bytes);
}

std::chrono::milliseconds ExecutionBudget::wall_clock() const noexcept { return wall_clock_; }

std::size_t ExecutionBudget::output_bytes() const noexcept { return output_bytes_; }

std::string_view runtime_state_name(const RuntimeState state) noexcept {
    switch (state) {
    case RuntimeState::dormant:
        return "dormant";
    case RuntimeState::activating:
        return "activating";
    case RuntimeState::ready:
        return "ready";
    case RuntimeState::executing:
        return "executing";
    case RuntimeState::retiring:
        return "retiring";
    case RuntimeState::failed:
        return "failed";
    }
    return "failed";
}

RuntimeSnapshot::RuntimeSnapshot(RuntimeId runtime, const RuntimeState state,
                                 std::optional<ActivationId> active_activation) noexcept
    : runtime_(std::move(runtime)), state_(state),
      active_activation_(std::move(active_activation)) {}

Result<RuntimeSnapshot> RuntimeSnapshot::create(RuntimeId runtime, const RuntimeState state,
                                                std::optional<ActivationId> active_activation) {
    if (state_requires_activation(state) != active_activation.has_value()) {
        return std::unexpected(
            make_error(ErrorCode::illegal_transition,
                       "runtime snapshot state and activation ownership disagree"));
    }
    return RuntimeSnapshot(std::move(runtime), state, std::move(active_activation));
}

const RuntimeId& RuntimeSnapshot::runtime() const noexcept { return runtime_; }

RuntimeState RuntimeSnapshot::state() const noexcept { return state_; }

const std::optional<ActivationId>& RuntimeSnapshot::active_activation() const noexcept {
    return active_activation_;
}

Runtime::Runtime(RuntimeId id) : id_(std::move(id)) {}

RuntimeState Runtime::state() const noexcept { return state_; }

const RuntimeId& Runtime::id() const noexcept { return id_; }

const std::optional<ActivationId>& Runtime::active_activation() const noexcept {
    return active_activation_;
}

RuntimeSnapshot Runtime::snapshot() const {
    // Runtime owns the state machine, so its state/activation pair already satisfies
    // RuntimeSnapshot::create's invariant. Keeping this construction here also prevents a
    // caller from accidentally projecting half a lifecycle transition as a public observation.
    return RuntimeSnapshot(id_, state_, active_activation_);
}

Result<void> Runtime::begin_activation(const ActivationId& activation) {
    if (state_ != RuntimeState::dormant || active_activation_.has_value()) {
        return std::unexpected(illegal_transition("begin activation"));
    }
    active_activation_ = activation;
    state_ = RuntimeState::activating;
    return {};
}

Result<void> Runtime::mark_ready(const ActivationId& activation) {
    if (const auto valid = require_active(RuntimeState::activating, activation, "mark ready");
        !valid) {
        return std::unexpected(valid.error());
    }
    state_ = RuntimeState::ready;
    return {};
}

Result<void> Runtime::begin_execution(const ActivationId& activation) {
    if (const auto valid = require_active(RuntimeState::ready, activation, "begin execution");
        !valid) {
        return std::unexpected(valid.error());
    }
    state_ = RuntimeState::executing;
    return {};
}

Result<void> Runtime::finish_execution(const ActivationId& activation) {
    if (const auto valid = require_active(RuntimeState::executing, activation, "finish execution");
        !valid) {
        return std::unexpected(valid.error());
    }
    state_ = RuntimeState::ready;
    return {};
}

Result<void> Runtime::begin_retirement(const ActivationId& activation) {
    if (const auto valid = require_active(RuntimeState::ready, activation, "begin retirement");
        !valid) {
        return std::unexpected(valid.error());
    }
    state_ = RuntimeState::retiring;
    return {};
}

Result<void> Runtime::finish_retirement(const ActivationId& activation) {
    if (const auto valid = require_active(RuntimeState::retiring, activation, "finish retirement");
        !valid) {
        return std::unexpected(valid.error());
    }
    active_activation_.reset();
    state_ = RuntimeState::dormant;
    return {};
}

void Runtime::fail() noexcept {
    active_activation_.reset();
    state_ = RuntimeState::failed;
}

Result<void> Runtime::require_active(const RuntimeState expected, const ActivationId& activation,
                                     const std::string_view operation) const {
    if (state_ != expected) {
        return std::unexpected(illegal_transition(operation));
    }
    if (!active_activation_.has_value() || *active_activation_ != activation) {
        return std::unexpected(
            make_error(ErrorCode::activation_mismatch,
                       "activation does not own runtime for " + std::string(operation)));
    }
    return {};
}

} // namespace wspctl::domain
