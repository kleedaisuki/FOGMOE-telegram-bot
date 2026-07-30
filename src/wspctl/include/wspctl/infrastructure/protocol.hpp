#pragma once

#include "wspctl/domain/runtime.hpp"
#include "wspctl/infrastructure/common.hpp"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <vector>

namespace wspctl {

/** @brief 协议魔数 / Control protocol magic number. */
inline constexpr std::uint32_t kProtocolMagic = 0x31505357U; // "WSP1" in little endian.
/** @brief 当前协议版本 / Current control protocol version. */
inline constexpr std::uint16_t kProtocolVersion = 3;
/** @brief 固定帧头长度 / Fixed wire-frame header length. */
inline constexpr std::size_t kFrameHeaderBytes = 12;
/** @brief 单个 SOCK_SEQPACKET 消息的硬上限 / Hard maximum one SOCK_SEQPACKET message. */
inline constexpr std::size_t kMaxFrameBytes = 128U * 1024U;
/** @brief 单个 stdout/stderr 合并输出上限 / Maximum combined stdout/stderr output. */
inline constexpr std::size_t kMaxOutputBytes = 96U * 1024U;
/** @brief 单个 stdin 上限 / Maximum stdin payload. */
inline constexpr std::size_t kMaxStdinBytes = 64U * 1024U;
/** @brief 一个命令允许的 argv 元素上限 / Maximum argv entries per command. */
inline constexpr std::size_t kMaxArgvEntries = 128;
/** @brief 单次文件写入的总字节硬上限 / Hard total-byte cap for one file ingress. */
inline constexpr std::size_t kMaxAddFileBytes = 8U * 1024U * 1024U;
/** @brief 单个文件分块的字节硬上限 / Hard byte cap for one file chunk. */
inline constexpr std::size_t kMaxAddFileChunkBytes = 64U * 1024U;
/** @brief 文件 opaque ID 的最大字节数 / Maximum byte count of a file opaque ID. */
inline constexpr std::size_t kMaxFileOpaqueIdBytes = 128U;

/**
 * @brief 控制帧类别 / Control frame kinds.
 *
 * SOCK_SEQPACKET 保留消息边界；帧头中的长度仍用于拒绝截断、拼接与资源耗尽。
 * SOCK_SEQPACKET preserves message boundaries; the length still rejects truncation, concatenation,
 * and exhaustion.
 */
enum class MessageKind : std::uint16_t {
    execute = 1,
    result = 2,
    error = 3,
    shutdown = 4,
    /** @brief 文件传输开始元数据 / File-transfer begin metadata. */
    payload_begin = 5,
    /** @brief 文件传输的一段原始 bytes / One raw-byte chunk of a file transfer. */
    payload_chunk = 6,
    /** @brief 已完成 byte/hash 流校验 / Completed byte/hash stream validation. */
    payload_seal = 7,
    /** @brief 将已 seal 临时文件原子发布 / Atomically publish a sealed temporary file. */
    payload_publish = 8,
    /** @brief 丢弃尚未发布的临时文件 / Discard an unpublished temporary file. */
    payload_abort = 9,
    /** @brief 文件传输阶段确认 / File-transfer phase acknowledgement. */
    payload_ack = 10,
    /** @brief 文件写入收据 / File-ingress receipt. */
    payload_result = 11,
    /** @brief 已完成文件收据的只读恢复查询 / Read-only recovery lookup for a completed file
       receipt. */
    payload_replay = 12,
    /** @brief 无副作用 runtime 状态查询 / Side-effect-free runtime-status query. */
    runtime_status = 13,
    /** @brief allowlisted runtime 状态快照 / Allowlisted runtime-status snapshot. */
    runtime_status_result = 14,
};

/**
 * @brief RuntimeProcess 的无副作用状态请求 / Side-effect-free RuntimeProcess status request.
 *
 * 此请求只携带 handle 已有的身份，不携带调用、文件或任何 payload 元数据；broker 必须以
 * SharedState 的读模型回答，绝不能走 lazy activation 路径。/ This request carries only identities
 * already held by the handle, never an invocation, file, or payload metadata; the broker must
 * answer from the SharedState read model and must never take the lazy-activation path.
 */
struct RuntimeStatusRequest final {
    /** @brief 持久 runtime UUID / Persistent runtime UUID. */
    std::string runtime_key;
    /** @brief 调用 handle 绑定的 activation / Activation bound to the calling handle. */
    std::string activation_id;
};

/**
 * @brief 跨 control protocol 的 allowlisted runtime 状态 / Allowlisted runtime status across the
 * control protocol.
 *
 * 这是固定、平面的遥测形状。它刻意不含 host 路径、PID、mount/cgroup、command、request ID/hash、
 * stdin/stdout/stderr 或 payload 任何字段。/ This is a fixed, flat telemetry shape. It deliberately
 * contains no host path, PID, mount/cgroup, command, request ID/hash, stdin/stdout/stderr, or any
 * payload field.
 */
struct RuntimeStatusResult final {
    /** @brief 被观察的持久 runtime UUID / Persistent runtime UUID observed. */
    std::string runtime_key;
    /** @brief 聚合生命周期状态 / Aggregate lifecycle state. */
    domain::RuntimeState state{domain::RuntimeState::dormant};
    /** @brief 当前 owner activation；dormant/failed 时为空 / Current owner activation; empty while
     * dormant/failed. */
    std::optional<std::string> active_activation_id;
    /** @brief 此 handle activation 是否是当前 owner / Whether this handle activation is the current
     * owner. */
    bool handle_activation_matches{false};
    /** @brief supervisor 是否可观察为存活 / Whether the supervisor is observably alive. */
    bool supervisor_alive{false};
    /** @brief ready 状态的空闲年龄；其他状态为空 / Idle age while ready; empty for other states. */
    std::optional<std::chrono::milliseconds> idle_for;
    /** @brief broker idle 回收阈值 / Broker idle-retirement threshold. */
    std::chrono::milliseconds idle_ttl{1};
    /** @brief 当前借用 session 的 broker dispatch 数 / Current broker dispatches borrowing the
     * session. */
    std::uint64_t borrowed_dispatches{};
    /** @brief 是否有已知的清理/隔离待办 / Whether known cleanup/quarantine is pending. */
    bool cleanup_pending{false};
};

/**
 * @brief 运行命令请求 / Command execution request.
 * @note argv 永远作为 vector 传给 exec，不经 shell 拼接。
 *       argv is always passed as a vector to exec and never shell-concatenated.
 */
struct ExecuteRequest final {
    /** @brief 持久 runtime 标识 / Persistent runtime key. */
    std::string runtime_key;
    /** @brief 一次激活标识 / One activation identifier. */
    std::string activation_id;
    /** @brief 稳定调用/幂等标识 / Stable invocation/idempotency identifier. */
    std::string request_id;
    /** @brief 上层业务计算的 SHA-256 语义哈希 / Caller-supplied SHA-256 semantic hash. */
    std::string request_hash;
    /** @brief 直接 exec 的参数向量 / Arguments supplied directly to exec. */
    std::vector<std::string> argv;
    /** @brief 标准输入字节 / Standard-input bytes. */
    std::string stdin_data;
    /** @brief runtime 内的工作目录 / Working directory inside the runtime. */
    std::string cwd;
    /** @brief 墙钟超时 / Wall-clock timeout. */
    std::chrono::milliseconds timeout{30'000};
    /** @brief stdout 与 stderr 合并的字节上限 / Combined stdout/stderr byte cap. */
    std::size_t output_limit{64U * 1024U};
};

/**
 * @brief 命令执行结果 / Command execution result.
 */
struct ExecutionResult final {
    /** @brief 对应调用 ID / Corresponding invocation ID. */
    std::string request_id;
    /** @brief POSIX/Bash 风格退出码；仅 timeout 为空 / POSIX/Bash-style exit code; empty only for
     * timeout. */
    std::optional<std::int32_t> exit_code;
    /** @brief 是否超过超时 / Whether the command exceeded its timeout. */
    bool timed_out{false};
    /** @brief 是否丢弃了超额输出 / Whether output beyond the cap was discarded. */
    bool truncated{false};
    /** @brief 是否来自 journal 回放 / Whether the result was replayed from journal. */
    bool replayed{false};
    /** @brief 已规范化、无 NUL 的 UTF-8 标准输出 / Normalized NUL-free UTF-8 standard output. */
    std::string stdout_data;
    /** @brief 已规范化、无 NUL 的 UTF-8 标准错误 / Normalized NUL-free UTF-8 standard error. */
    std::string stderr_data;
};

/** @brief payload ACK 的确定状态 / Deterministic state of one payload acknowledgement. */
enum class PayloadAckStage : std::uint8_t {
    /** @brief 已创建临时文件并接受元数据 / Temporary file was created and metadata accepted. */
    begun = 1,
    /** @brief 已写入一个分块 / One chunk was written. */
    chunk_written = 2,
    /** @brief byte count、SHA-256 与 fsync 已完成 / Byte count, SHA-256, and fsync completed. */
    sealed = 3,
    /** @brief 临时文件已被安全丢弃 / Temporary file was safely discarded. */
    aborted = 4,
};

/**
 * @brief 文件写入开始请求 / File-ingress begin request.
 *
 * @note ``opaque_id`` 仅是可信上层生成的目录 capability，不是文件名；唯一可发布路径是
 * ``/workspace/uploads/<opaque_id>/payload``。/ ``opaque_id`` is only a directory capability
 * generated by a trusted upper layer, not a filename; the sole publishable path is
 * ``/workspace/uploads/<opaque_id>/payload``.
 */
struct PayloadBeginRequest final {
    /** @brief 持久 runtime 标识 / Persistent runtime key. */
    std::string runtime_key;
    /** @brief 当前 RuntimeProcess activation / Current RuntimeProcess activation. */
    std::string activation_id;
    /** @brief 稳定、可去重的写入调用标识 / Stable deduplicable ingress invocation ID. */
    std::string request_id;
    /** @brief 调用方计算的语义 SHA-256 / Caller-computed semantic SHA-256. */
    std::string request_hash;
    /** @brief 受限 uploads 子目录 opaque component / Constrained uploads-subdirectory opaque
     * component. */
    std::string opaque_id;
    /** @brief 声明的完整字节数 / Declared complete byte count. */
    std::size_t byte_size{};
    /** @brief 完整内容的规范小写 SHA-256 / Canonical lowercase SHA-256 of complete content. */
    std::string sha256;
};

/**
 * @brief 已完成文件收据的只读恢复查询 / Read-only recovery lookup for a completed file receipt.
 *
 * 该请求刻意不带 activation：它绝不能激活、替换或终止一个 RuntimeProcess。它仅证明指定的
 * durable ingress receipt 是否仍对应可恢复的 persistent workspace object。/ This request
 * deliberately carries no activation: it must never activate, replace, or retire a
 * RuntimeProcess. It only proves whether the specified durable ingress receipt still corresponds
 * to a recoverable persistent-workspace object.
 */
struct PayloadReplayRequest final {
    /** @brief 持久 runtime 标识 / Persistent runtime key. */
    std::string runtime_key;
    /** @brief 稳定、可去重的原始写入调用标识 / Stable deduplicable original ingress invocation ID.
     */
    std::string request_id;
    /** @brief 原始写入调用方计算的语义 SHA-256 / Caller-computed semantic SHA-256 of the original
     * ingress. */
    std::string request_hash;
    /** @brief 受限 uploads 子目录 opaque component / Constrained uploads-subdirectory opaque
     * component. */
    std::string opaque_id;
    /** @brief 原始完整文件字节数 / Original complete file byte count. */
    std::size_t byte_size{};
    /** @brief 原始完整内容的规范小写 SHA-256 / Canonical lowercase SHA-256 of the original complete
     * content. */
    std::string sha256;
};

/** @brief 一个未经解释的文件 bytes 分块 / One uninterpreted file-byte chunk. */
struct PayloadChunk final {
    /** @brief 所属稳定调用标识 / Owning stable invocation ID. */
    std::string request_id;
    /** @brief 原始内容 bytes / Raw content bytes. */
    std::vector<std::byte> bytes;
};

/** @brief seal、publish 或 abort 的文件控制请求 / File control request for seal, publish, or abort.
 */
struct PayloadControlRequest final {
    /** @brief 所属稳定调用标识 / Owning stable invocation ID. */
    std::string request_id;
};

/** @brief 一个 payload 阶段 ACK / One payload phase acknowledgement. */
struct PayloadAck final {
    /** @brief 所属稳定调用标识 / Owning stable invocation ID. */
    std::string request_id;
    /** @brief 已确认阶段 / Confirmed phase. */
    PayloadAckStage stage{PayloadAckStage::begun};
    /** @brief 当前已接收或已 seal 的字节数 / Current received or sealed byte count. */
    std::size_t received_bytes{};
};

/** @brief 文件写入的规范收据 / Canonical receipt for a file ingress. */
struct PayloadResult final {
    /** @brief 已发布或回放的稳定调用标识 / Stable invocation ID published or replayed. */
    std::string request_id;
    /** @brief 是否来自 durable journal 回放 / Whether this came from a durable journal replay. */
    bool replayed{false};
    /** @brief runtime 内唯一允许的最终路径 / Sole allowed final path inside the runtime. */
    std::string path;
    /** @brief 已验证的完整字节数 / Verified complete byte count. */
    std::size_t byte_size{};
    /** @brief 已验证内容的规范小写 SHA-256 / Canonical lowercase SHA-256 of verified content. */
    std::string sha256;
};

/** @brief 解码后的完整帧 / Fully decoded wire frame. */
struct Frame final {
    /** @brief 帧类别 / Frame kind. */
    MessageKind kind;
    /** @brief 已验证的载荷 / Validated payload. */
    std::vector<std::byte> payload;
};

/**
 * @brief 校验请求字段与配额 / Validate request fields and quotas.
 * @param request 待校验请求 / Request to validate.
 * @return 成功或精确错误 / Success or precise error.
 */
[[nodiscard]] Result<void> validate_execute_request(const ExecuteRequest& request);

/**
 * @brief 校验无副作用 runtime 状态请求 / Validate a side-effect-free runtime-status request.
 * @param request 待校验请求 / Request to validate.
 * @return 成功或精确错误 / Success or a precise error.
 */
[[nodiscard]] Result<void> validate_runtime_status_request(const RuntimeStatusRequest& request);

/**
 * @brief 校验 allowlisted runtime 状态结果 / Validate an allowlisted runtime-status result.
 * @param result 待校验状态结果 / Status result to validate.
 * @return 成功或精确错误 / Success or a precise error.
 */
[[nodiscard]] Result<void> validate_runtime_status_result(const RuntimeStatusResult& result);

/**
 * @brief 校验文件开始请求的 capability 与资源上限 / Validate file-begin capability and resource
 * caps.
 * @param request 待校验文件开始请求 / File-begin request to validate.
 * @return 成功或精确错误 / Success or precise error.
 */
[[nodiscard]] Result<void> validate_payload_begin_request(const PayloadBeginRequest& request);

/**
 * @brief 校验只读文件恢复查询的 capability 与资源上限 / Validate a read-only file-replay query's
 * capability and resource caps.
 * @param request 待校验恢复查询 / Replay query to validate.
 * @return 成功或精确错误 / Success or precise error.
 */
[[nodiscard]] Result<void> validate_payload_replay_request(const PayloadReplayRequest& request);

/**
 * @brief 校验一个文件原始分块 / Validate one raw file chunk.
 * @param chunk 待校验分块 / Chunk to validate.
 * @return 成功或精确错误 / Success or precise error.
 */
[[nodiscard]] Result<void> validate_payload_chunk(const PayloadChunk& chunk);

/**
 * @brief 校验文件控制请求 / Validate a file control request.
 * @param request 待校验控制请求 / Control request to validate.
 * @return 成功或精确错误 / Success or precise error.
 */
[[nodiscard]] Result<void> validate_payload_control_request(const PayloadControlRequest& request);

/**
 * @brief 校验文件写入收据 / Validate a file-ingress receipt.
 * @param result 待校验收据 / Receipt to validate.
 * @return 成功或精确错误 / Success or precise error.
 */
[[nodiscard]] Result<void> validate_payload_result(const PayloadResult& result);

/**
 * @brief 计算不含 request_hash 的规范载荷 SHA-256 / Hash canonical payload excluding request_hash.
 * @param request 请求 / Request.
 * @return 64 位小写十六进制 SHA-256 / Lowercase 64-character SHA-256.
 */
[[nodiscard]] std::string canonical_request_hash(const ExecuteRequest& request);

/**
 * @brief 计算不含 request_hash/activation 的规范文件元数据 SHA-256 / Hash canonical file metadata
 * excluding request_hash and activation.
 * @param request 文件开始请求 / File-begin request.
 * @return 64 位小写十六进制 SHA-256 / Lowercase 64-character SHA-256.
 */
[[nodiscard]] std::string canonical_payload_hash(const PayloadBeginRequest& request);

/**
 * @brief 计算与原始文件 ingress 相同的规范元数据 SHA-256 / Compute the same canonical metadata
 * SHA-256 as the original file ingress.
 * @param request 已验证只读恢复查询 / Validated read-only replay query.
 * @return 64 位小写十六进制 SHA-256 / Lowercase 64-character SHA-256.
 * @note 此值与相同 runtime/request/opaque/size/content-digest 的 ``PayloadBeginRequest`` 完全
 *       相同，且不含 activation/request_hash。/ This is exactly equal to the
 *       ``PayloadBeginRequest`` hash for the same runtime/request/opaque/size/content digest;
 *       it excludes activation and request_hash.
 */
[[nodiscard]] std::string canonical_payload_hash(const PayloadReplayRequest& request);

/**
 * @brief 编码执行请求 / Encode an execution request.
 * @param request 已校验请求 / Validated request.
 * @return 二进制载荷 / Binary payload.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_execute_request(const ExecuteRequest& request);

/**
 * @brief 解码执行请求 / Decode an execution request.
 * @param payload 二进制载荷 / Binary payload.
 * @return 已校验的请求 / Validated request.
 */
[[nodiscard]] Result<ExecuteRequest> decode_execute_request(std::span<const std::byte> payload);

/** @brief 编码无副作用 runtime 状态请求 / Encode a side-effect-free runtime-status request. */
[[nodiscard]] Result<std::vector<std::byte>>
encode_runtime_status_request(const RuntimeStatusRequest& request);
/** @brief 解码无副作用 runtime 状态请求 / Decode a side-effect-free runtime-status request. */
[[nodiscard]] Result<RuntimeStatusRequest>
decode_runtime_status_request(std::span<const std::byte> payload);
/** @brief 编码 allowlisted runtime 状态结果 / Encode an allowlisted runtime-status result. */
[[nodiscard]] Result<std::vector<std::byte>>
encode_runtime_status_result(const RuntimeStatusResult& result);
/** @brief 解码 allowlisted runtime 状态结果 / Decode an allowlisted runtime-status result. */
[[nodiscard]] Result<RuntimeStatusResult>
decode_runtime_status_result(std::span<const std::byte> payload);

/** @brief 编码文件开始请求 / Encode a file-begin request. */
[[nodiscard]] Result<std::vector<std::byte>>
encode_payload_begin_request(const PayloadBeginRequest& request);
/** @brief 解码文件开始请求 / Decode a file-begin request. */
[[nodiscard]] Result<PayloadBeginRequest>
decode_payload_begin_request(std::span<const std::byte> payload);
/** @brief 编码只读文件恢复查询 / Encode a read-only file-replay query. */
[[nodiscard]] Result<std::vector<std::byte>>
encode_payload_replay_request(const PayloadReplayRequest& request);
/** @brief 解码只读文件恢复查询 / Decode a read-only file-replay query. */
[[nodiscard]] Result<PayloadReplayRequest>
decode_payload_replay_request(std::span<const std::byte> payload);
/** @brief 编码一个文件分块 / Encode one file chunk. */
[[nodiscard]] Result<std::vector<std::byte>> encode_payload_chunk(const PayloadChunk& chunk);
/** @brief 解码一个文件分块 / Decode one file chunk. */
[[nodiscard]] Result<PayloadChunk> decode_payload_chunk(std::span<const std::byte> payload);
/** @brief 编码文件控制请求 / Encode a file control request. */
[[nodiscard]] Result<std::vector<std::byte>>
encode_payload_control_request(const PayloadControlRequest& request);
/** @brief 解码文件控制请求 / Decode a file control request. */
[[nodiscard]] Result<PayloadControlRequest>
decode_payload_control_request(std::span<const std::byte> payload);
/** @brief 编码文件阶段 ACK / Encode a file phase acknowledgement. */
[[nodiscard]] Result<std::vector<std::byte>> encode_payload_ack(const PayloadAck& acknowledgement);
/** @brief 解码文件阶段 ACK / Decode a file phase acknowledgement. */
[[nodiscard]] Result<PayloadAck> decode_payload_ack(std::span<const std::byte> payload);
/** @brief 编码文件写入收据 / Encode a file-ingress receipt. */
[[nodiscard]] Result<std::vector<std::byte>> encode_payload_result(const PayloadResult& result);
/** @brief 解码文件写入收据 / Decode a file-ingress receipt. */
[[nodiscard]] Result<PayloadResult> decode_payload_result(std::span<const std::byte> payload);

/**
 * @brief 编码执行结果 / Encode an execution result.
 * @param result 待编码结果 / Result to encode.
 * @return 二进制载荷 / Binary payload.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_execution_result(const ExecutionResult& result);

/**
 * @brief 解码执行结果 / Decode an execution result.
 * @param payload 二进制载荷 / Binary payload.
 * @return 已校验结果 / Validated result.
 */
[[nodiscard]] Result<ExecutionResult> decode_execution_result(std::span<const std::byte> payload);

/**
 * @brief 编码错误文本载荷 / Encode an error payload.
 * @param error 错误 / Error.
 * @return 二进制载荷 / Binary payload.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_error(const Error& error);

/**
 * @brief 解码错误文本载荷 / Decode an error payload.
 * @param payload 二进制载荷 / Binary payload.
 * @return 结构化错误 / Structured error.
 */
[[nodiscard]] Result<Error> decode_error(std::span<const std::byte> payload);

/**
 * @brief 以版本化头包装载荷 / Wrap payload in a versioned header.
 * @param kind 帧类别 / Frame kind.
 * @param payload 载荷 / Payload.
 * @return 一整个 SOCK_SEQPACKET 消息 / One whole SOCK_SEQPACKET message.
 */
[[nodiscard]] Result<std::vector<std::byte>> encode_frame(MessageKind kind,
                                                          std::span<const std::byte> payload);

/**
 * @brief 解析并严格校验一整个帧 / Parse and strictly validate one whole frame.
 * @param wire 从 SOCK_SEQPACKET 收到的消息 / Message read from SOCK_SEQPACKET.
 * @return 帧 / Frame.
 */
[[nodiscard]] Result<Frame> decode_frame(std::span<const std::byte> wire);

/**
 * @brief 发送一整个有界帧 / Send one bounded frame.
 * @param fd UNIX SOCK_SEQPACKET 文件描述符 / UNIX SOCK_SEQPACKET descriptor.
 * @param frame 待发送帧 / Frame to send.
 * @return 成功或 I/O 错误 / Success or I/O error.
 */
[[nodiscard]] Result<void> send_frame(int fd, std::span<const std::byte> frame);

/**
 * @brief 接收一整个有界帧 / Receive one bounded frame.
 * @param fd UNIX SOCK_SEQPACKET 文件描述符 / UNIX SOCK_SEQPACKET descriptor.
 * @return 完整帧或 I/O/截断错误 / Complete frame or I/O/truncation error.
 */
[[nodiscard]] Result<std::vector<std::byte>> receive_frame(int fd);

} // namespace wspctl
