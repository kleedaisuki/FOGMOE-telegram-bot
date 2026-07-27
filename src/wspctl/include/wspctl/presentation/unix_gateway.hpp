#pragma once

#include "wspctl/domain/runtime.hpp"
#include "wspctl/infrastructure/common.hpp"

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace wspctl::presentation {

/**
 * @brief Bot 到 Unix gateway 的命令 DTO / Command DTO from the Bot to the Unix gateway.
 *
 * 这是 presentation 边界的未特权请求形状；gateway 在写入 wire 前将其转换为受验证的 control request。
 * This is the unprivileged request shape at the presentation boundary; the gateway converts it to a validated control request before writing the wire.
 */
struct ClientExecuteRequest final {
    /** @brief 长期 runtime UUID / Long-lived runtime UUID. */
    std::string runtime_key;
    /** @brief RuntimeProcess activation / RuntimeProcess activation. */
    std::string activation_id;
    /** @brief 幂等命令调用 ID / Idempotent command invocation ID. */
    std::string request_id;
    /** @brief 调用方语义 SHA-256 / Caller semantic SHA-256. */
    std::string request_hash;
    /** @brief 直接 exec argv / Direct exec argv. */
    std::vector<std::string> argv;
    /** @brief 标准输入 bytes / Standard-input bytes. */
    std::string stdin_data;
    /** @brief runtime 内 cwd / Runtime-internal cwd. */
    std::string cwd{"/workspace"};
    /** @brief 墙钟超时 / Wall-clock timeout. */
    std::chrono::milliseconds timeout{30'000};
    /** @brief 合并输出上限 / Combined output cap. */
    std::size_t output_limit{64U * 1024U};
};

/** @brief Unix gateway 返回给 Bot 的结果 DTO / Result DTO returned by the Unix gateway to the Bot. */
struct ClientExecutionResult final {
    /** @brief 对应请求 ID / Corresponding request ID. */
    std::string request_id;
    /** @brief POSIX/Bash 风格退出码；超时时为空 / POSIX/Bash-style exit code; empty on timeout. */
    std::optional<std::int32_t> exit_code;
    /** @brief 是否超时 / Whether execution timed out. */
    bool timed_out{false};
    /** @brief 是否截断输出 / Whether output was truncated. */
    bool truncated{false};
    /** @brief 是否来自 durable replay / Whether this came from durable replay. */
    bool replayed{false};
    /** @brief 已规范化 stdout / Normalized stdout. */
    std::string stdout_data;
    /** @brief 已规范化 stderr / Normalized stderr. */
    std::string stderr_data;
};

/** @brief Bot 到 Unix gateway 的无副作用状态查询 DTO / Side-effect-free status-query DTO from the Bot to the Unix gateway. */
struct ClientRuntimeStatusRequest final {
    /** @brief 长期 runtime UUID / Long-lived runtime UUID. */
    std::string runtime_key;
    /** @brief 调用 handle 绑定的 activation / Activation bound to the calling handle. */
    std::string activation_id;
};

/**
 * @brief Unix gateway 返回给 Bot 的 allowlisted 运行态 DTO / Allowlisted operating-status DTO returned by the Unix gateway.
 *
 * 此 DTO 是 protocol 结果的 presentation 投影；不添加 host 路径、PID、command 或 payload
 * 字段。/ This DTO is a presentation projection of the protocol result; it adds no host path,
 * PID, command, or payload fields.
 */
struct ClientRuntimeStatus final {
    /** @brief 被观察的持久 runtime UUID / Persistent runtime UUID observed. */
    std::string runtime_key;
    /** @brief 聚合生命周期状态 / Aggregate lifecycle state. */
    domain::RuntimeState state{domain::RuntimeState::dormant};
    /** @brief 当前 owner activation；不活跃时为空 / Current owner activation; empty when inactive. */
    std::optional<std::string> active_activation_id;
    /** @brief 此 handle activation 是否是当前 owner / Whether this handle activation is the current owner. */
    bool handle_activation_matches{false};
    /** @brief 是否有健康、可复用的 supervisor / Whether a healthy reusable supervisor exists. */
    bool supervisor_alive{false};
    /** @brief ready 状态空闲年龄；其他状态为空 / Idle age while ready; empty for other states. */
    std::optional<std::chrono::milliseconds> idle_for;
    /** @brief broker idle 回收阈值 / Broker idle-retirement threshold. */
    std::chrono::milliseconds idle_ttl{1};
    /** @brief 当前借用 session 的 broker dispatch 数 / Current broker dispatches borrowing the session. */
    std::uint64_t borrowed_dispatches{};
    /** @brief 是否有已知清理/隔离待办 / Whether known cleanup/quarantine is pending. */
    bool cleanup_pending{false};
};

/**
 * @brief presentation 层的单消费文件 chunk 源 / Single-consumption file-chunk source at the presentation layer.
 *
 * @note source 每次最多产生一个 64 KiB raw bytes 分块；它不暴露 host 文件描述符、路径或内容
 * 分类。/ A source produces at most one 64 KiB raw-byte chunk at a time; it exposes no host file
 * descriptor, path, or content classification.
 */
class PayloadChunkSource {
public:
    /** @brief 支持多态析构 / Support polymorphic destruction. */
    virtual ~PayloadChunkSource() = default;

    /**
     * @brief 取得下一个分块或 EOF / Obtain the next chunk or EOF.
     * @return 一个分块、EOF（``nullopt``）或错误 / One chunk, EOF (``nullopt``), or an error.
     */
    [[nodiscard]] virtual Result<std::optional<std::vector<std::byte>>> next_chunk() = 0;
};

/** @brief Bot 到 Unix gateway 的文件写入 DTO / File-ingress DTO from the Bot to the Unix gateway. */
struct ClientAddFileRequest final {
    /** @brief 长期 runtime UUID / Long-lived runtime UUID. */
    std::string runtime_key;
    /** @brief RuntimeProcess activation / RuntimeProcess activation. */
    std::string activation_id;
    /** @brief 幂等文件写入调用 ID / Idempotent file-ingress invocation ID. */
    std::string request_id;
    /** @brief 调用方语义 SHA-256 / Caller semantic SHA-256. */
    std::string request_hash;
    /** @brief 可信上层产生的受限 opaque 目录 ID / Constrained opaque directory ID generated by a trusted upper layer. */
    std::string opaque_id;
    /** @brief 声明完整文件字节数 / Declared complete file byte count. */
    std::size_t byte_size{};
    /** @brief 声明完整文件 SHA-256 / Declared complete file SHA-256. */
    std::string sha256;
};

/**
 * @brief Bot 到 Unix gateway 的只读文件恢复 DTO / Read-only file-replay DTO from the Bot to the Unix gateway.
 *
 * 不含 activation，因而该 DTO 不可触发 RuntimeProcess 启动、替换或 retire。/ It carries no
 * activation and therefore cannot trigger RuntimeProcess startup, replacement, or retirement.
 */
struct ClientReplayFileRequest final {
    /** @brief 长期 runtime UUID / Long-lived runtime UUID. */
    std::string runtime_key;
    /** @brief 原始幂等文件写入调用 ID / Original idempotent file-ingress invocation ID. */
    std::string request_id;
    /** @brief 原始调用方语义 SHA-256 / Original caller semantic SHA-256. */
    std::string request_hash;
    /** @brief 可信上层产生的受限 opaque 目录 ID / Constrained opaque directory ID generated by a trusted upper layer. */
    std::string opaque_id;
    /** @brief 已持久写入文件的完整字节数 / Complete byte size of the persisted file. */
    std::size_t byte_size{};
    /** @brief 已持久写入文件的完整 SHA-256 / Complete SHA-256 of the persisted file. */
    std::string sha256;
};

/** @brief Unix gateway 返回给 Bot 的文件写入收据 DTO / File-ingress receipt DTO returned by the Unix gateway. */
struct ClientAddFileResult final {
    /** @brief 对应请求 ID / Corresponding request ID. */
    std::string request_id;
    /** @brief 是否来自 durable journal 回放 / Whether this came from durable journal replay. */
    bool replayed{false};
    /** @brief runtime 内唯一允许的最终路径 / Sole allowed final path inside the runtime. */
    std::string path;
    /** @brief 已验证完整文件字节数 / Verified complete file byte count. */
    std::size_t byte_size{};
    /** @brief 已验证完整文件 SHA-256 / Verified complete file SHA-256. */
    std::string sha256;
};

/**
 * @brief 非特权 Unix SOCK_SEQPACKET gateway client / Unprivileged Unix SOCK_SEQPACKET gateway client.
 *
 * 它没有 namespace、mount、cgroup 或 host 权限；每次 execute 建立一条短连接并验证对端是 root-owned broker。
 * It has no namespace, mount, cgroup, or host privileges; each execute creates a short connection and verifies the peer is the root-owned broker.
 */
class UnixGatewayClient final {
public:
    /**
     * @brief 构造 client / Construct a client.
     * @param socket_path broker UNIX socket 的绝对路径 / Absolute path of the broker UNIX socket.
     */
    explicit UnixGatewayClient(std::string socket_path);

    /**
     * @brief 取得不可变 broker endpoint / Get the immutable broker endpoint.
     * @return broker UNIX socket 的绝对路径 / Absolute path of the broker UNIX socket.
     */
    [[nodiscard]] const std::string& socket_path() const noexcept;

    /**
     * @brief 校验绝对 AF_UNIX endpoint / Validate an absolute AF_UNIX endpoint.
     * @param socket_path endpoint 路径 / Endpoint path.
     * @return 成功或参数错误 / Success or argument error.
     */
    [[nodiscard]] static Result<void> validate_socket_path(std::string_view socket_path);

    /**
     * @brief 经一条短连接执行命令 / Execute a command over one short connection.
     * @param request presentation 请求 DTO / Presentation request DTO.
     * @return presentation 结果 DTO 或 transport 错误 / Presentation result DTO or transport error.
     */
    [[nodiscard]] Result<ClientExecutionResult> execute(const ClientExecuteRequest& request) const;

    /**
     * @brief 经一条短连接读取 runtime 状态 / Read runtime status over one short connection.
     * @param request presentation 状态查询 DTO / Presentation status-query DTO.
     * @return allowlisted 运行态或 transport 错误 / Allowlisted operating status or a transport error.
     * @note 此调用不会激活、替换或 retire RuntimeProcess，也不读取 journal/payload。
     *       This call neither activates, replaces, nor retires a RuntimeProcess and reads no
     *       journal/payload.
     */
    [[nodiscard]] Result<ClientRuntimeStatus> status(const ClientRuntimeStatusRequest& request) const;

    /**
     * @brief 经一条短连接流式写入文件 / Stream a file over one short connection.
     * @param request presentation 文件写入 DTO / Presentation file-ingress DTO.
     * @param source 单消费、分块的 raw-byte source / Single-consumption chunked raw-byte source.
     * @return 规范文件写入收据或 transport 错误 / Canonical file receipt or a transport error.
     */
    [[nodiscard]] Result<ClientAddFileResult> add_file(
        const ClientAddFileRequest& request,
        PayloadChunkSource& source) const;

    /**
     * @brief 只读恢复一个已完成的文件 ingress 收据 / Read-only replay of a completed file-ingress receipt.
     * @param request 已持久的文件 import metadata / Persisted file-import metadata.
     * @return ``replayed=true`` 的 canonical 收据，或 ``not_found`` / in-doubt / conflict / A canonical receipt with ``replayed=true``, or ``not_found`` / in-doubt / conflict.
     * @note 该调用不发送 chunks、不会创建 pending journal，且不会激活 RuntimeProcess。
     *       This call sends no chunks, creates no pending journal, and does not activate a
     *       RuntimeProcess.
     */
    [[nodiscard]] Result<ClientAddFileResult> replay_file(const ClientReplayFileRequest& request) const;

private:
    /** @brief broker UNIX socket 的绝对路径 / Absolute path of the broker UNIX socket. */
    std::string socket_path_;
};

}  // namespace wspctl::presentation
