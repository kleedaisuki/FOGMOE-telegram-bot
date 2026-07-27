#pragma once

#include "wspctl/infrastructure/common.hpp"
#include "wspctl/infrastructure/protocol.hpp"

#include <sys/types.h>

#include <memory>
#include <string>

namespace wspctl {

/**
 * @brief wsp-systemd 的最小配置 / Minimal wsp-systemd configuration.
 */
struct SupervisorConfig final {
    /** @brief 与 broker 通信的 SOCK_SEQPACKET FD / SOCK_SEQPACKET FD communicating with broker. */
    int control_fd{-1};
    /** @brief broker 预打开的 task cgroup.procs FD / Broker-preopened task cgroup.procs FD. */
    int task_cgroup_procs_fd{-1};
    /** @brief broker 预打开的 task cgroup.kill FD / Broker-preopened task cgroup.kill FD. */
    int task_cgroup_kill_fd{-1};
    /** @brief broker 预打开的 task cgroup.events FD / Broker-preopened task cgroup.events FD. */
    int task_cgroup_events_fd{-1};
    /** @brief PID 1 持有的 /workspace 目录 FD，用于 completion 前 syncfs / PID 1-held /workspace directory FD for syncfs before completion. */
    int workspace_fd{-1};
    /** @brief 仅 CTest 可替换的 workspace host path / CTest-only replacement for the workspace host path. */
    std::string test_workspace_root{"/workspace"};
    /** @brief task 降权 UID / Task privilege-drop UID. */
    uid_t sandbox_uid{};
    /** @brief task 降权 GID / Task privilege-drop GID. */
    gid_t sandbox_gid{};
};

/**
 * @brief runtime PID 1 supervisor / Runtime PID 1 supervisor.
 *
 * 它不是 shell：只接受已验证帧，启动直接 argv，回收僵尸，并以进程组处理超时。
 * It is not a shell: it accepts only validated frames, starts direct argv, reaps zombies, and handles timeout by process group.
 */
class Supervisor final {
public:
    /**
     * @brief 构造 supervisor / Construct a supervisor.
     * @param config 最小配置 / Minimal configuration.
     */
    explicit Supervisor(SupervisorConfig config);

    /** @brief 析构时丢弃未发布的文件临时层 / Discard an unpublished file staging layer on destruction. */
    ~Supervisor();

    /**
     * @brief 作为 PID 1 服务控制 socket / Serve the control socket as PID 1.
     * @return 正常 shutdown 或错误 / Clean shutdown or error.
     */
    [[nodiscard]] Result<void> serve();

    /**
     * @brief 执行一次直接 argv 任务 / Execute one direct-argv task.
     * @param request 已校验请求 / Validated request.
     * @return 结果或 supervisor 错误 / Result or supervisor error.
     * @note 此函数同时供 CTest 验证 timeout/output 行为；生产路径通过 serve 调用它。
     *       This function also lets CTest verify timeout/output behavior; production invokes it via serve.
     */
    [[nodiscard]] Result<ExecutionResult> execute_once(const ExecuteRequest& request);

    /**
     * @brief 开始一个受限文件写入 / Begin one constrained file ingress.
     * @param request 已校验的文件开始请求 / Validated file-begin request.
     * @return 已创建临时文件的 ACK / Acknowledgement for the created temporary file.
     */
    [[nodiscard]] Result<PayloadAck> begin_payload(const PayloadBeginRequest& request);

    /**
     * @brief 追加一个未经解释的文件分块 / Append one uninterpreted file chunk.
     * @param chunk 已校验的文件分块 / Validated file chunk.
     * @return 已写入分块的 ACK / Acknowledgement for the written chunk.
     */
    [[nodiscard]] Result<PayloadAck> append_payload(const PayloadChunk& chunk);

    /**
     * @brief 校验并同步临时文件 / Verify and synchronize the temporary file.
     * @param request 已校验的 seal 控制请求 / Validated seal control request.
     * @return 已 seal 临时文件的 ACK / Acknowledgement for the sealed temporary file.
     */
    [[nodiscard]] Result<PayloadAck> seal_payload(const PayloadControlRequest& request);

    /**
     * @brief 原子发布已经 seal 的文件 / Atomically publish a sealed file.
     * @param request 已校验的 publish 控制请求 / Validated publish control request.
     * @return 已发布文件的规范收据 / Canonical receipt for the published file.
     */
    [[nodiscard]] Result<PayloadResult> publish_payload(const PayloadControlRequest& request);

    /**
     * @brief 安全丢弃尚未发布的临时文件 / Safely discard an unpublished temporary file.
     * @param request 已校验的 abort 控制请求 / Validated abort control request.
     * @return 已丢弃临时文件的 ACK / Acknowledgement for the discarded temporary file.
     */
    [[nodiscard]] Result<PayloadAck> abort_payload(const PayloadControlRequest& request);

    /**
     * @brief 回收所有已退出孤儿 / Reap all exited orphan children.
     * @return 已回收个数 / Number of reaped children.
     */
    [[nodiscard]] unsigned int reap_children() noexcept;

private:
    /** @brief PID 1 持有的单个文件写入状态 / One file-ingress state retained by PID 1. */
    struct ActivePayload;

    /**
     * @brief 无条件释放未发布临时状态 / Unconditionally release unpublished staging state.
     * @return 成功或清理错误 / Success or cleanup error.
     * @note 该方法只由 PID 1 内部失败路径与析构调用。/ This method is used only by PID 1
     * internal failure paths and destruction.
     */
    [[nodiscard]] Result<void> discard_active_payload();

    /** @brief 不可变配置 / Immutable configuration. */
    SupervisorConfig config_;
    /** @brief 当前尚未 publish 的文件状态 / Current file state that has not been published. */
    std::unique_ptr<ActivePayload> active_payload_;
};

}  // namespace wspctl
