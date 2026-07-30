#pragma once

#include "wspctl/application/operator_workspace.hpp"
#include "wspctl/infrastructure/common.hpp"
#include "wspctl/infrastructure/journal.hpp"
#include "wspctl/infrastructure/protocol.hpp"
#include "wspctl/infrastructure/runtime_gate.hpp"
#include "wspctl/infrastructure/sandbox.hpp"

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <sys/types.h>
#include <unordered_map>

namespace wspctl {

/**
 * @brief wspctld broker 配置 / wspctld broker configuration.
 */
struct BrokerConfig final {
    /** @brief 仅本机可访问的 UNIX SOCK_SEQPACKET 路径 / Local-only UNIX SOCK_SEQPACKET path. */
    std::filesystem::path socket_path;
    /** @brief 允许的 Bot UNIX UID / Permitted Bot UNIX UID. */
    uid_t client_uid{};
    /** @brief 独立 operator UNIX SOCK_SEQPACKET 路径 / Independent operator UNIX SOCK_SEQPACKET
     * path. */
    std::filesystem::path operator_socket_path;
    /** @brief 唯一允许的 operator UNIX UID；生产默认 root / Sole permitted operator UNIX UID; root
     * by default in production. */
    uid_t operator_uid{};
    /** @brief sandbox 配置 / Sandbox configuration. */
    SandboxConfig sandbox;
    /** @brief 空闲 activation 缓存时长 / Idle activation cache duration. */
    std::chrono::minutes idle_ttl{15};
    /** @brief 显式承认 checkout 祖先不安全的本机开发模式 / Explicit local-development opt-in for
     * unsafe checkout ancestors. */
    bool allow_insecure_dev_root{false};
};

/**
 * @brief 特权 host broker / Privileged host broker.
 *
 * Python 只连接 socket；所有 namespace/mount/cgroup 操作均留在该进程。
 * Python only connects a socket; all namespace/mount/cgroup operations remain in this process.
 */
class Broker final : private application::OperatorWorkspaceReadPort {
public:
    /** @brief listener 与 worker 就绪后的单次回调 / One-shot callback after listeners and workers
     * are ready. */
    using ReadyCallback = Result<void> (*)();

    /**
     * @brief 构造并执行 fail-closed 验证 / Construct and run fail-closed validation.
     * @param config broker 配置 / Broker configuration.
     * @return 已就绪 broker 或错误 / Ready broker or error.
     */
    [[nodiscard]] static Result<Broker> create(BrokerConfig config);

    /** @brief 禁止复制，避免多个 owner 解绑同一路径 / Copying is forbidden to avoid two owners
     * unlinking one path. */
    Broker(const Broker&) = delete;
    /** @brief 禁止复制赋值 / Copy assignment is forbidden. */
    Broker& operator=(const Broker&) = delete;
    /** @brief 支持移动 / Moving is supported. */
    Broker(Broker&&) noexcept;
    /** @brief 支持移动赋值 / Move assignment is supported. */
    Broker& operator=(Broker&&) noexcept;
    /** @brief 关闭监听 socket 并回收 supervisor / Close listener and reap supervisors. */
    ~Broker();

    /**
     * @brief 监听并处理控制请求 / Listen and process control requests.
     * @param ready_callback worker pool 建立后、进入 accept loop 前执行的可选回调 /
     *        Optional callback run after worker-pool creation and before the accept loop.
     * @return 直到不可恢复错误 / Runs until an unrecoverable error.
     */
    [[nodiscard]] Result<void> serve_forever(ReadyCallback ready_callback = nullptr);

private:
    /**
     * @brief 以已验证配置构造 broker / Construct a broker from validated configuration.
     * @param config broker 配置 / Broker configuration.
     */
    explicit Broker(BrokerConfig config);
    /** @brief 活跃 runtime supervisor 句柄 / Handle to an active runtime supervisor. */
    struct RuntimeSession;
    /** @brief 持有 runtime session mutex 与 reaper 借用的执行租约 / Execution lease holding a
     * session mutex and reaper reference. */
    struct SessionLease;
    /** @brief 多 worker 共享的 runtime session 状态 / Runtime-session state shared by multiple
     * workers. */
    struct SharedState;
    /** @brief fork-server 返回的启动句柄 / Launch handle returned by the fork server. */
    struct LauncherReply;
    /** @brief broker 配置 / Broker configuration. */
    BrokerConfig config_;
    /** @brief runtime storage 的 XFS-only project-quota 服务 / XFS-only project-quota service for
     * runtime storage. */
    XfsProjectQuota quota_;
    /** @brief journal / Journal. */
    Journal journal_;
    /** @brief 防止同 runtime 并发 workspace 写入 / Prevent concurrent writes to one runtime
     * workspace. */
    RuntimeExecutionGate execution_gate_;
    /** @brief 监听 FD / Listening FD. */
    int listen_fd_{-1};
    /** @brief 本 broker 绑定的 socket 设备号 / Device number of the socket bound by this broker. */
    dev_t socket_device_{};
    /** @brief 本 broker 绑定的 socket inode / Inode of the socket bound by this broker. */
    ino_t socket_inode_{};
    /** @brief 是否确实拥有 socket pathname / Whether this broker truly owns the socket pathname. */
    bool owns_socket_path_{false};
    /** @brief 独立 operator 监听 FD / Independent operator listening FD. */
    int operator_listen_fd_{-1};
    /** @brief 本 broker 绑定的 operator socket 设备号 / Device number of the operator socket bound
     * by this broker. */
    dev_t operator_socket_device_{};
    /** @brief 本 broker 绑定的 operator socket inode / Inode of the operator socket bound by this
     * broker. */
    ino_t operator_socket_inode_{};
    /** @brief 是否确实拥有 operator socket pathname / Whether this broker truly owns the operator
     * socket pathname. */
    bool owns_operator_socket_path_{false};
    /** @brief 按 runtime key 维护的可同步惰性 activation / Synchronized lazy activations keyed by
     * runtime. */
    std::unique_ptr<SharedState> state_;

    /**
     * @brief 绑定监听 socket / Bind the listening socket.
     * @return 成功或 I/O 错误 / Success or I/O error.
     */
    [[nodiscard]] Result<void> bind_listener();

    /**
     * @brief 绑定独立 operator 监听 socket / Bind the independent operator listening socket.
     * @return 成功或 I/O 错误 / Success or an I/O error.
     */
    [[nodiscard]] Result<void> bind_operator_listener();

    /**
     * @brief 服务一个经认证 client / Serve one authenticated client.
     * @param client_fd 已 accept 的 FD / Accepted FD.
     * @return 成功或连接错误 / Success or connection error.
     */
    [[nodiscard]] Result<void> serve_client(int client_fd);

    /**
     * @brief 服务一个经 ACL 认证的 operator client / Serve one ACL-authenticated operator client.
     * @param client_fd 已 accept 的 operator FD / Accepted operator FD.
     * @return 成功或连接错误 / Success or a connection error.
     * @note 此方法只接受独立 operator protocol；它绝不调用 Bot socket handler。
     *       This method accepts only the independent operator protocol; it never invokes the Bot
     * socket handler.
     */
    [[nodiscard]] Result<void> serve_operator_client(int client_fd);

    /**
     * @brief 读取 runtime 的 operator allowlisted 状态 / Read a runtime's operator allowlisted
     * status.
     * @param runtime 已验证长期 runtime 标识 / Validated long-lived runtime identity.
     * @return 状态或应用层只读查询错误 / Status or an application-layer read-only query error.
     */
    [[nodiscard]] application::OperatorWorkspaceQueryResult<domain::OperatorWorkspaceStatus>
    status(const domain::RuntimeId& runtime) const override;

    /**
     * @brief 列举 runtime 的一层 operator workspace 目录 / List one operator workspace directory
     * level for a runtime.
     * @param runtime 已验证长期 runtime 标识 / Validated long-lived runtime identity.
     * @param path 已验证 `/workspace` 逻辑路径 / Validated `/workspace` logical path.
     * @return 目录列举或应用层只读查询错误 / Directory listing or an application-layer read-only
     * query error.
     */
    [[nodiscard]] application::OperatorWorkspaceQueryResult<domain::WorkspaceListing>
    list(const domain::RuntimeId& runtime,
         const domain::OperatorWorkspacePath& path) const override;

    /**
     * @brief 转发给对应 supervisor / Forward to the corresponding supervisor.
     * @param request 已验证请求 / Validated request.
     * @return 命令结果 / Command result.
     */
    [[nodiscard]] Result<ExecutionResult> dispatch(const ExecuteRequest& request);

    /**
     * @brief 从共享读模型读取 runtime 状态 / Read runtime status from the shared read model.
     * @param request 已验证的无副作用状态请求 / Validated side-effect-free status request.
     * @return allowlisted 状态结果或错误 / Allowlisted status result or error.
     * @note 此方法不得调用 acquire_session、quota provisioning、journal 或 supervisor control
     * socket。 This method must not call acquire_session, quota provisioning, journal, or the
     *       supervisor control socket.
     */
    [[nodiscard]] Result<RuntimeStatusResult>
    read_runtime_status(const RuntimeStatusRequest& request) const;

    /**
     * @brief 在必要时惰性激活并独占取得一个 runtime session / Lazily activate and exclusively
     * acquire one runtime session.
     * @param runtime_key 已校验的持久 runtime 标识 / Validated persistent runtime identifier.
     * @param activation_id 已校验的 RuntimeProcess activation 标识 / Validated RuntimeProcess
     * activation identifier.
     * @return 持有 session mutex 的租约或错误 / A mutex-owning lease or an error.
     * @note 租约在 map lookup 与 mutex 获取之间持有 reaper reference；其生命周期覆盖整次流式
     *       文件传输，避免断连时 PID 1 的临时文件与 session 生命周期脱节。/ The lease holds a
     *       reaper reference between map lookup and mutex acquisition and spans the whole streamed
     *       file transfer, preventing PID1 temporary-file cleanup from drifting from session
     * lifetime.
     */
    [[nodiscard]] Result<std::unique_ptr<SessionLease>>
    acquire_session(const std::string& runtime_key, const std::string& activation_id);

    /**
     * @brief 在已认证 client 上完成一次流式文件写入 / Complete one streamed file ingress on an
     * authenticated client.
     * @param client_fd 已认证 client 的 SOCK_SEQPACKET FD / Authenticated client SOCK_SEQPACKET FD.
     * @param request 已验证的文件开始请求 / Validated file-begin request.
     * @return 成功、客户端断连或精确错误 / Success, client disconnect, or a precise error.
     */
    [[nodiscard]] Result<void> dispatch_payload_stream(int client_fd,
                                                       const PayloadBeginRequest& request);

    /**
     * @brief 只读恢复已完成文件 ingress 的 durable receipt / Read-only replay of a completed
     * file-ingress durable receipt.
     * @param request 不含 activation 的已验证 replay 查询 / Validated activation-free replay query.
     * @return ``replayed=true`` 的已验证收据，或 not-found/conflict/in-doubt / Verified receipt
     * with ``replayed=true``, or not-found/conflict/in-doubt.
     * @note 此路径不得创建 journal、不得传输 chunks，且不得调用 lazy session activation。
     *       This path must not create a journal, transfer chunks, or call lazy session activation.
     */
    [[nodiscard]] Result<PayloadResult> replay_payload(const PayloadReplayRequest& request);

    /**
     * @brief 在 worker 启动前创建单线程 fork-server / Create the single-threaded fork server before
     * workers start.
     * @return 成功或 fail-closed 错误 / Success or a fail-closed error.
     */
    [[nodiscard]] Result<void> start_launcher_server();

    /**
     * @brief 经 fork-server 启动 namespace PID 1 / Start namespace PID 1 through the fork server.
     * @param layer 已准备 OverlayFS 层 / Prepared OverlayFS layer.
     * @param cgroup 传给 PID 1 的 cgroup 控制 FD / Cgroup control FDs passed to PID 1.
     * @param control_fd supervisor control socket 的 PID 1 一端 / PID 1 end of the supervisor
     * control socket.
     * @return launcher PID、pidfd、PID 1 与 release FD / Launcher PID, pidfd, PID 1, and release
     * FD.
     */
    [[nodiscard]] Result<LauncherReply>
    launch_runtime(const TaskLayer& layer, const TaskCgroupControl& cgroup, int control_fd);

    /**
     * @brief 确认已将 PID 1 放入 cgroup 并释放 / Commit that PID 1 was placed in cgroup and
     * released.
     * @param launch_id fork-server launch ID / Fork-server launch ID.
     * @return helper terminal acknowledgement or error / Helper terminal acknowledgement or error.
     */
    [[nodiscard]] Result<void> commit_launch(std::uint64_t launch_id);

    /**
     * @brief 取消未确认的启动并等待 helper terminal/reap ACK / Cancel an uncommitted launch and
     * await helper terminal/reap ACK.
     * @param launch_id fork-server launch ID / Fork-server launch ID.
     * @return helper terminal acknowledgement or error / Helper terminal acknowledgement or error.
     */
    [[nodiscard]] Result<void> cancel_launch(std::uint64_t launch_id);

    /**
     * @brief 确认 broker 已实际写入 release pipe / Confirm broker actually wrote the release pipe.
     * @param launch_id fork-server launch ID / Fork-server launch ID.
     * @return helper terminal acknowledgement or error / Helper terminal acknowledgement or error.
     */
    [[nodiscard]] Result<void> release_launch(std::uint64_t launch_id);

    /**
     * @brief 终止一个 session 并在 launcher 已退出后条件移除 / Retire a session and conditionally
     * erase it after launcher exit.
     * @param runtime_key runtime 标识 / Runtime key.
     * @param session 已由调用方持有 mutex 的 session / Session whose mutex is held by the caller.
     * @return cgroup 与 launcher 均确认终止，或保留 poisoned tracking 的错误 / Confirmed cgroup and
     * launcher termination, or an error retaining poisoned tracking.
     */
    [[nodiscard]] Result<void> retire_session(const std::string& runtime_key,
                                              const std::shared_ptr<RuntimeSession>& session);

    /** @brief 停止并回收 fork-server / Stop and reap the fork server. */
    void stop_launcher_server() noexcept;

    /** @brief 在已持有 launcher RPC mutex 时隔离并杀死失步 helper / Isolate and kill a
     * desynchronized helper while launcher RPC mutex is held. */
    void poison_launcher_server_locked() noexcept;

    /**
     * @brief 回收超时缓存与死亡 child / Reap expired cache entries and dead children.
     */
    void reap_expired_sessions() noexcept;
};

} // namespace wspctl
