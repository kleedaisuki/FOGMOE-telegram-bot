#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <expected>
#include <optional>
#include <string>
#include <string_view>
#include <utility>

namespace wspctl::domain {

/** @brief 领域层可恢复错误码 / Recoverable domain error codes. */
enum class ErrorCode : std::uint8_t {
    /** @brief 标识不符合领域语义 / An identity violates domain semantics. */
    invalid_identity,
    /** @brief 执行预算不符合领域语义 / An execution budget violates domain semantics. */
    invalid_budget,
    /** @brief 生命周期状态迁移非法 / A lifecycle state transition is illegal. */
    illegal_transition,
    /** @brief 请求者不是当前 activation / The caller is not the active activation. */
    activation_mismatch,
};

/** @brief 领域错误值 / Domain error value. */
struct Error final {
    /** @brief 稳定、可分支的错误分类 / Stable branchable error category. */
    ErrorCode code;
    /** @brief 仅供诊断的错误说明 / Diagnostic-only error description. */
    std::string message;
};

/** @brief 携带领域错误的结果 / Result carrying a domain error. */
template <typename Value>
using Result = std::expected<Value, Error>;

/**
 * @brief 构造领域错误 / Construct a domain error.
 * @param code 领域错误分类 / Domain error category.
 * @param message 诊断文本 / Diagnostic text.
 * @return 可传播的领域错误 / Propagatable domain error.
 */
[[nodiscard]] Error make_error(ErrorCode code, std::string message);

/** @brief 长期 workspace runtime 标识 / Long-lived workspace runtime identifier. */
class RuntimeId final {
public:
    /**
     * @brief 解析 canonical lowercase UUID / Parse a canonical lowercase UUID.
     * @param value UUID 文本 / UUID text.
     * @return 已验证的 runtime 标识或错误 / Validated runtime identifier or error.
     */
    [[nodiscard]] static Result<RuntimeId> parse(std::string value);

    /** @brief 取得 canonical 文本 / Get the canonical text. */
    [[nodiscard]] const std::string& value() const noexcept;

    /** @brief 比较 runtime 标识 / Compare runtime identifiers. */
    [[nodiscard]] bool operator==(const RuntimeId&) const noexcept = default;

private:
    /**
     * @brief 以已验证文本构造 / Construct from already validated text.
     * @param value 已验证 UUID / Validated UUID.
     */
    explicit RuntimeId(std::string value);

    /** @brief 已验证 UUID 文本 / Validated UUID text. */
    std::string value_;
};

/** @brief 一次 RuntimeProcess 激活标识 / One RuntimeProcess activation identifier. */
class ActivationId final {
public:
    /**
     * @brief 解析安全 activation 标识 / Parse a safe activation identifier.
     * @param value activation 文本 / Activation text.
     * @return 已验证 activation 标识或错误 / Validated activation identifier or error.
     */
    [[nodiscard]] static Result<ActivationId> parse(std::string value);

    /** @brief 取得文本值 / Get the text value. */
    [[nodiscard]] const std::string& value() const noexcept;

    /** @brief 比较 activation 标识 / Compare activation identifiers. */
    [[nodiscard]] bool operator==(const ActivationId&) const noexcept = default;

private:
    /**
     * @brief 以已验证文本构造 / Construct from already validated text.
     * @param value 已验证 activation / Validated activation.
     */
    explicit ActivationId(std::string value);

    /** @brief 已验证 activation 文本 / Validated activation text. */
    std::string value_;
};

/** @brief 持久命令调用标识 / Durable command invocation identifier. */
class CommandId final {
public:
    /**
     * @brief 解析安全命令标识 / Parse a safe command identifier.
     * @param value 命令调用文本 / Command invocation text.
     * @return 已验证命令标识或错误 / Validated command identifier or error.
     */
    [[nodiscard]] static Result<CommandId> parse(std::string value);

    /** @brief 取得文本值 / Get the text value. */
    [[nodiscard]] const std::string& value() const noexcept;

    /** @brief 比较命令标识 / Compare command identifiers. */
    [[nodiscard]] bool operator==(const CommandId&) const noexcept = default;

private:
    /**
     * @brief 以已验证文本构造 / Construct from already validated text.
     * @param value 已验证调用 ID / Validated invocation ID.
     */
    explicit CommandId(std::string value);

    /** @brief 已验证调用 ID 文本 / Validated invocation-ID text. */
    std::string value_;
};

/** @brief SHA-256 小写十六进制摘要值对象 / Lowercase hexadecimal SHA-256 digest value object. */
class Sha256Digest final {
public:
    /**
     * @brief 解析 SHA-256 小写十六进制文本 / Parse lowercase hexadecimal SHA-256 text.
     * @param value 64 字符摘要文本 / 64-character digest text.
     * @return 已验证摘要或错误 / Validated digest or error.
     */
    [[nodiscard]] static Result<Sha256Digest> parse(std::string value);

    /** @brief 取得 canonical 摘要文本 / Get the canonical digest text. */
    [[nodiscard]] const std::string& value() const noexcept;

    /** @brief 比较摘要 / Compare digests. */
    [[nodiscard]] bool operator==(const Sha256Digest&) const noexcept = default;

private:
    /**
     * @brief 以已验证摘要构造 / Construct from a validated digest.
     * @param value 已验证摘要文本 / Validated digest text.
     */
    explicit Sha256Digest(std::string value);

    /** @brief 64 字符小写十六进制摘要 / 64-character lowercase hexadecimal digest. */
    std::string value_;
};

/** @brief 可持久重放的命令意图 / Durable replayable command intent. */
struct CommandIntent final {
    /** @brief 所属长期 runtime / Owning long-lived runtime. */
    RuntimeId runtime;
    /** @brief 稳定命令调用 / Stable command invocation. */
    CommandId command;
    /** @brief 调用方提供的语义哈希 / Caller-supplied semantic hash. */
    Sha256Digest request_hash;
    /** @brief control plane 计算的载荷哈希 / Payload hash computed by the control plane. */
    Sha256Digest payload_hash;
};

/** @brief 任务级执行预算值对象 / Per-task execution-budget value object. */
class ExecutionBudget final {
public:
    /**
     * @brief 创建非零、有限的任务预算 / Create a non-zero finite task budget.
     * @param wall_clock 墙钟时间上限 / Wall-clock time cap.
     * @param output_bytes 合并输出字节上限 / Combined output byte cap.
     * @return 已验证预算或错误 / Validated budget or error.
     */
    [[nodiscard]] static Result<ExecutionBudget> create(
        std::chrono::milliseconds wall_clock,
        std::size_t output_bytes);

    /** @brief 取得墙钟上限 / Get the wall-clock cap. */
    [[nodiscard]] std::chrono::milliseconds wall_clock() const noexcept;
    /** @brief 取得输出上限 / Get the output cap. */
    [[nodiscard]] std::size_t output_bytes() const noexcept;

private:
    /**
     * @brief 以已验证值构造 / Construct from validated values.
     * @param wall_clock 已验证墙钟上限 / Validated wall-clock cap.
     * @param output_bytes 已验证输出上限 / Validated output cap.
     */
    ExecutionBudget(std::chrono::milliseconds wall_clock, std::size_t output_bytes) noexcept;

    /** @brief 墙钟时间上限 / Wall-clock time cap. */
    std::chrono::milliseconds wall_clock_;
    /** @brief 合并输出字节上限 / Combined output byte cap. */
    std::size_t output_bytes_;
};

/** @brief runtime 聚合根的生命周期状态 / Runtime aggregate lifecycle state. */
enum class RuntimeState : std::uint8_t {
    /** @brief 未激活 / Not activated. */
    dormant,
    /** @brief 正建立 RuntimeProcess / Establishing a RuntimeProcess. */
    activating,
    /** @brief 可接受任务 / Ready to accept a task. */
    ready,
    /** @brief 正执行一个任务 / Executing one task. */
    executing,
    /** @brief 正退役 activation / Retiring an activation. */
    retiring,
    /** @brief 本进程观察到不可恢复失败 / This process observed an unrecoverable failure. */
    failed,
};

/** @brief journal 对调用的确定性决定 / Deterministic journal decision for an invocation. */
enum class CommandJournalDecision : std::uint8_t {
    /** @brief 首次执行 / Execute for the first time. */
    execute_new,
    /** @brief 回放已完成结果 / Replay a completed result. */
    replay_completed,
    /** @brief 请求哈希冲突 / Request hashes conflict. */
    reject_hash_conflict,
    /** @brief 先前结果未知 / The prior outcome is unknown. */
    reject_outcome_unknown,
};

/**
 * @brief 具有 activation 所有权不变量的 runtime 聚合 / Runtime aggregate with activation-ownership invariants.
 *
 * 该聚合只表达业务生命周期；mount、cgroup、socket 和 Linux 权限均属于基础设施层。
 * This aggregate expresses only business lifecycle; mounts, cgroups, sockets, and Linux privileges belong to infrastructure.
 */
class Runtime final {
public:
    /**
     * @brief 以 dormant 状态构造 runtime / Construct a dormant runtime.
     * @param id 长期 runtime 标识 / Long-lived runtime identifier.
     */
    explicit Runtime(RuntimeId id);

    /** @brief 取得当前生命周期状态 / Get the current lifecycle state. */
    [[nodiscard]] RuntimeState state() const noexcept;
    /** @brief 取得长期 runtime 标识 / Get the long-lived runtime identifier. */
    [[nodiscard]] const RuntimeId& id() const noexcept;
    /** @brief 取得当前 activation；dormant/failed 时为空 / Get the active activation; empty while dormant/failed. */
    [[nodiscard]] const std::optional<ActivationId>& active_activation() const noexcept;

    /**
     * @brief 请求新的 activation / Request a new activation.
     * @param activation 请求拥有该 runtime 的 activation / Activation requesting ownership of this runtime.
     * @return 成功或生命周期错误 / Success or lifecycle error.
     */
    [[nodiscard]] Result<void> begin_activation(const ActivationId& activation);
    /**
     * @brief 标记 activation 已可用 / Mark an activation ready.
     * @param activation 当前 activation / Current activation.
     * @return 成功或所有权/状态错误 / Success or ownership/state error.
     */
    [[nodiscard]] Result<void> mark_ready(const ActivationId& activation);
    /**
     * @brief 标记任务开始 / Mark task execution started.
     * @param activation 当前 activation / Current activation.
     * @return 成功或所有权/状态错误 / Success or ownership/state error.
     */
    [[nodiscard]] Result<void> begin_execution(const ActivationId& activation);
    /**
     * @brief 标记任务结束 / Mark task execution finished.
     * @param activation 当前 activation / Current activation.
     * @return 成功或所有权/状态错误 / Success or ownership/state error.
     */
    [[nodiscard]] Result<void> finish_execution(const ActivationId& activation);
    /**
     * @brief 开始退役空闲 activation / Begin retiring an idle activation.
     * @param activation 当前 activation / Current activation.
     * @return 成功或所有权/状态错误 / Success or ownership/state error.
     */
    [[nodiscard]] Result<void> begin_retirement(const ActivationId& activation);
    /**
     * @brief 确认退役完成并回到 dormant / Confirm retirement and return to dormant.
     * @param activation 当前 activation / Current activation.
     * @return 成功或所有权/状态错误 / Success or ownership/state error.
     */
    [[nodiscard]] Result<void> finish_retirement(const ActivationId& activation);
    /** @brief 标记本进程失败并清除 activation / Mark this process failed and clear its activation. */
    void fail() noexcept;

private:
    /**
     * @brief 验证状态与 activation 所有权 / Validate state and activation ownership.
     * @param expected 预期当前状态 / Expected current state.
     * @param activation 发起者 activation / Caller activation.
     * @param operation 操作诊断名 / Diagnostic operation name.
     * @return 成功或精确不变量错误 / Success or precise invariant error.
     */
    [[nodiscard]] Result<void> require_active(
        RuntimeState expected,
        const ActivationId& activation,
        std::string_view operation) const;

    /** @brief 长期 runtime 标识 / Long-lived runtime identifier. */
    RuntimeId id_;
    /** @brief 当前状态 / Current state. */
    RuntimeState state_{RuntimeState::dormant};
    /** @brief 对当前 RuntimeProcess 的唯一所有权 / Exclusive ownership of the current RuntimeProcess. */
    std::optional<ActivationId> active_activation_;
};

}  // namespace wspctl::domain
