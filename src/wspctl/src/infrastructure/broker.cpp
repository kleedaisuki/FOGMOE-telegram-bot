#include "wspctl/infrastructure/broker.hpp"

#include "wspctl/application/operator_workspace.hpp"
#include "wspctl/application/runtime_activation.hpp"
#include "wspctl/application/runtime_status.hpp"
#include "wspctl/domain/operator_workspace.hpp"
#include "wspctl/domain/runtime.hpp"
#include "wspctl/infrastructure/detail/launcher_transport.hpp"
#include "wspctl/infrastructure/operator_endpoint.hpp"
#include "wspctl/infrastructure/operator_protocol.hpp"
#include "wspctl/infrastructure/operator_workspace_reader.hpp"
#include "wspctl/infrastructure/detail/payload_replay.hpp"
#include "wspctl/infrastructure/detail/pidfd_control.hpp"
#include "wspctl/infrastructure/protocol.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <climits>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <functional>
#include <linux/close_range.h>
#include <linux/magic.h>
#include <memory>
#include <poll.h>
#include <mutex>
#include <optional>
#include <sys/resource.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/time.h>
#include <sys/un.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <system_error>
#include <thread>
#include <unordered_set>
#include <unistd.h>
#include <vector>

namespace wspctl::detail::launcher_transport {

void close_launcher_packet_fds(LauncherPacket& packet) noexcept {
    for (int& fd : packet.fds) {
        if (fd >= 0) {
            static_cast<void>(close(fd));
            fd = -1;
        }
    }
    packet.fd_count = 0U;
}

Result<void> send_launcher_packet(
    const int fd,
    const std::span<const std::byte> bytes,
    const std::span<const int> fds) {
    if (bytes.empty() || bytes.size() > kMaxPacketBytes || fds.size() > kMaxFileDescriptors) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid fork-server packet bounds"));
    }
    iovec vector{.iov_base = const_cast<std::byte*>(bytes.data()), .iov_len = bytes.size()};
    std::array<std::byte, CMSG_SPACE(sizeof(int) * kMaxFileDescriptors)> control{};
    msghdr message{};
    message.msg_iov = &vector;
    message.msg_iovlen = 1U;
    if (!fds.empty()) {
        message.msg_control = control.data();
        message.msg_controllen = CMSG_SPACE(static_cast<unsigned int>(fds.size() * sizeof(int)));
        cmsghdr* header = CMSG_FIRSTHDR(&message);
        header->cmsg_level = SOL_SOCKET;
        header->cmsg_type = SCM_RIGHTS;
        header->cmsg_len = CMSG_LEN(static_cast<unsigned int>(fds.size() * sizeof(int)));
        std::memcpy(CMSG_DATA(header), fds.data(), fds.size() * sizeof(int));
    }
    const ssize_t sent = sendmsg(fd, &message, MSG_NOSIGNAL);
    if (sent != static_cast<ssize_t>(bytes.size())) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "send fork-server packet"));
    }
    return {};
}

Result<LauncherPacket> receive_launcher_packet(const int fd) {
    LauncherPacket packet;
    packet.bytes.resize(kMaxPacketBytes);
    iovec vector{.iov_base = packet.bytes.data(), .iov_len = packet.bytes.size()};
    std::array<std::byte, CMSG_SPACE(sizeof(int) * kMaxFileDescriptors)> control{};
    msghdr message{};
    message.msg_iov = &vector;
    message.msg_iovlen = 1U;
    message.msg_control = control.data();
    message.msg_controllen = control.size();
    const ssize_t received = recvmsg(fd, &message, MSG_CMSG_CLOEXEC);
    if (received <= 0) {
        return std::unexpected(received == 0 ? make_error(ErrorCode::io_failure, "fork-server peer closed") : errno_error(ErrorCode::io_failure, "receive fork-server packet"));
    }
    for (cmsghdr* header = CMSG_FIRSTHDR(&message); header != nullptr; header = CMSG_NXTHDR(&message, header)) {
        if (header->cmsg_level != SOL_SOCKET || header->cmsg_type != SCM_RIGHTS || header->cmsg_len < CMSG_LEN(0)) {
            close_launcher_packet_fds(packet);
            return std::unexpected(make_error(ErrorCode::protocol_violation, "unexpected fork-server ancillary data"));
        }
        const std::size_t payload_bytes = header->cmsg_len - CMSG_LEN(0);
        if (payload_bytes % sizeof(int) != 0U) {
            close_launcher_packet_fds(packet);
            return std::unexpected(make_error(ErrorCode::protocol_violation, "misaligned fork-server SCM_RIGHTS payload"));
        }
        const std::size_t count = payload_bytes / sizeof(int);
        if (count == 0U || packet.fd_count + count > kMaxFileDescriptors) {
            close_launcher_packet_fds(packet);
            return std::unexpected(make_error(ErrorCode::protocol_violation, "invalid fork-server FD count"));
        }
        const auto* received_fds = reinterpret_cast<const int*>(CMSG_DATA(header));
        for (std::size_t index = 0; index < count; ++index) {
            packet.fds[packet.fd_count++] = received_fds[index];
        }
    }
    if ((message.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) != 0) {
        close_launcher_packet_fds(packet);
        return std::unexpected(make_error(ErrorCode::frame_too_large, "truncated fork-server packet or ancillary data"));
    }
    packet.bytes.resize(static_cast<std::size_t>(received));
    return packet;
}

}  // namespace wspctl::detail::launcher_transport

namespace wspctl {
namespace {

using detail::launcher_transport::LauncherPacket;
using detail::launcher_transport::close_launcher_packet_fds;
using detail::launcher_transport::kMaxPacketBytes;
using detail::launcher_transport::receive_launcher_packet;
using detail::launcher_transport::send_launcher_packet;

/**
 * @brief 将纯领域错误投影到 native transport 错误 / Project a pure domain error into a native transport error.
 * @param error 领域错误 / Domain error.
 * @return 可通过 broker 传播的错误 / Error that can cross the broker boundary.
 */
[[nodiscard]] Error transport_error(const domain::Error& error) {
    switch (error.code) {
        case domain::ErrorCode::invalid_identity:
        case domain::ErrorCode::invalid_budget:
            return make_error(ErrorCode::invalid_argument, error.message);
        case domain::ErrorCode::illegal_transition:
        case domain::ErrorCode::activation_mismatch:
            return make_error(ErrorCode::protocol_violation, error.message);
    }
    return make_error(ErrorCode::internal, "unknown domain error category");
}

/**
 * @brief 已通过领域值对象验证的 execute admission / Execute admission validated by domain value objects.
 *
 * 该对象是 journal、quota registry 与任何 Linux side effect 前的语义闸门；wire parser 的
 * bounded-string 检查不能替代 canonical UUID、command ID、digest 与 budget 的领域约束。/
 * This object is the semantic gate before journal, quota registry, and any Linux side effect;
 * bounded-string checks in the wire parser do not replace domain constraints for canonical UUIDs,
 * command IDs, digests, and budgets.
 */
struct TypedExecuteAdmission final {
    /** @brief canonical long-lived runtime identity / Canonical long-lived runtime identity. */
    domain::RuntimeId runtime;
    /** @brief activation ownership identity / Activation ownership identity. */
    domain::ActivationId activation;
    /** @brief durable command identity / Durable command identity. */
    domain::CommandId command;
    /** @brief caller-supplied semantic digest / Caller-supplied semantic digest. */
    domain::Sha256Digest request_hash;
    /** @brief wall-clock/output budget / Wall-clock/output budget. */
    domain::ExecutionBudget budget;
};

/**
 * @brief 在任何 durable storage 之前执行 typed command admission / Perform typed command admission before any durable storage.
 * @param request 已通过 wire parser 的执行请求 / Execute request already accepted by the wire parser.
 * @return 强类型 admission 或 transport error / Typed admission or a transport error.
 */
[[nodiscard]] Result<TypedExecuteAdmission> admit_execute_request(const ExecuteRequest& request) {
    const auto runtime = domain::RuntimeId::parse(request.runtime_key);
    const auto activation = domain::ActivationId::parse(request.activation_id);
    const auto command = domain::CommandId::parse(request.request_id);
    const auto request_hash = domain::Sha256Digest::parse(request.request_hash);
    const auto budget = domain::ExecutionBudget::create(request.timeout, request.output_limit);
    if (!runtime || !activation || !command || !request_hash || !budget) {
        const domain::Error* const error = !runtime
            ? &runtime.error()
            : !activation
            ? &activation.error()
            : !command
            ? &command.error()
            : !request_hash
            ? &request_hash.error()
            : &budget.error();
        return std::unexpected(transport_error(*error));
    }
    return TypedExecuteAdmission{
        .runtime = std::move(*runtime),
        .activation = std::move(*activation),
        .command = std::move(*command),
        .request_hash = std::move(*request_hash),
        .budget = std::move(*budget),
    };
}

/**
 * @brief 已通过领域值对象验证的文件写入 admission / File-ingress admission validated by domain value objects.
 *
 * 文件内容本身不进入 broker 内存；这里验证其声明的 digest、调用身份与 runtime ownership，实际 bytes
 * 由 PID 1 以流式 SHA-256 校验。/ File contents never enter broker memory here; this validates their
 * declared digest, invocation identity, and runtime ownership, while PID1 validates actual bytes by
 * streaming SHA-256.
 */
struct TypedPayloadAdmission final {
    /** @brief canonical long-lived runtime identity / Canonical long-lived runtime identity. */
    domain::RuntimeId runtime;
    /** @brief RuntimeProcess activation ownership identity / RuntimeProcess activation ownership identity. */
    domain::ActivationId activation;
    /** @brief durable ingress invocation identity / Durable ingress invocation identity. */
    domain::CommandId command;
    /** @brief caller-supplied semantic digest / Caller-supplied semantic digest. */
    domain::Sha256Digest request_hash;
    /** @brief declared complete-content digest / Declared complete-content digest. */
    domain::Sha256Digest content_hash;
};

/**
 * @brief 在任何 durable storage 之前执行 typed file admission / Perform typed file admission before durable storage.
 * @param request 已通过 wire parser 的文件开始请求 / File-begin request already accepted by the wire parser.
 * @return 强类型 admission 或 transport error / Typed admission or a transport error.
 */
[[nodiscard]] Result<TypedPayloadAdmission> admit_payload_begin_request(const PayloadBeginRequest& request) {
    const auto runtime = domain::RuntimeId::parse(request.runtime_key);
    const auto activation = domain::ActivationId::parse(request.activation_id);
    const auto command = domain::CommandId::parse(request.request_id);
    const auto request_hash = domain::Sha256Digest::parse(request.request_hash);
    const auto content_hash = domain::Sha256Digest::parse(request.sha256);
    if (!runtime || !activation || !command || !request_hash || !content_hash) {
        const domain::Error* const error = !runtime
            ? &runtime.error()
            : !activation
            ? &activation.error()
            : !command
            ? &command.error()
            : !request_hash
            ? &request_hash.error()
            : &content_hash.error();
        return std::unexpected(transport_error(*error));
    }
    return TypedPayloadAdmission{
        .runtime = std::move(*runtime),
        .activation = std::move(*activation),
        .command = std::move(*command),
        .request_hash = std::move(*request_hash),
        .content_hash = std::move(*content_hash),
    };
}

/**
 * @brief 已通过领域值对象验证的只读文件恢复 admission / Read-only file-replay admission validated by domain value objects.
 *
 * 和写入 admission 分开表达，以类型系统禁止 replay 路径依赖 activation。/ This is represented
 * separately from write admission so the type system prevents the replay path from depending on
 * an activation.
 */
struct TypedPayloadReplayAdmission final {
    /** @brief canonical long-lived runtime identity / Canonical long-lived runtime identity. */
    domain::RuntimeId runtime;
    /** @brief durable original ingress invocation identity / Durable original ingress invocation identity. */
    domain::CommandId command;
    /** @brief caller-supplied original semantic digest / Caller-supplied original semantic digest. */
    domain::Sha256Digest request_hash;
    /** @brief declared original complete-content digest / Declared original complete-content digest. */
    domain::Sha256Digest content_hash;
};

/**
 * @brief 在任何 durable storage 或 runtime session 前执行 typed replay admission / Perform typed replay admission before durable storage or a runtime session.
 * @param request 已通过 wire parser 的 activation-free replay 请求 / Activation-free replay request accepted by the wire parser.
 * @return 强类型 admission 或 transport error / Typed admission or a transport error.
 */
[[nodiscard]] Result<TypedPayloadReplayAdmission> admit_payload_replay_request(const PayloadReplayRequest& request) {
    const auto runtime = domain::RuntimeId::parse(request.runtime_key);
    const auto command = domain::CommandId::parse(request.request_id);
    const auto request_hash = domain::Sha256Digest::parse(request.request_hash);
    const auto content_hash = domain::Sha256Digest::parse(request.sha256);
    if (!runtime || !command || !request_hash || !content_hash) {
        const domain::Error* const error = !runtime
            ? &runtime.error()
            : !command
            ? &command.error()
            : !request_hash
            ? &request_hash.error()
            : &content_hash.error();
        return std::unexpected(transport_error(*error));
    }
    return TypedPayloadReplayAdmission{
        .runtime = std::move(*runtime),
        .command = std::move(*command),
        .request_hash = std::move(*request_hash),
        .content_hash = std::move(*content_hash),
    };
}

/**
 * @brief 将受限 host 效果适配为 application activation port / Adapt constrained host effects into the application activation port.
 *
 * 此类保留 native Error，避免 domain 层为表达 cgroup 清理不确定性而依赖 transport 错误类型。
 * This class retains the native Error so the domain layer never depends on transport errors merely to express uncertain cgroup cleanup.
 */
class BrokerRuntimeActivationPort final : public application::RuntimeActivationPort {
public:
    /** @brief 一个受控 host 效果 / One controlled host effect. */
    using Operation = std::function<Result<void>()>;

    /**
     * @brief 构造绑定单一 runtime/activation 的适配器 / Construct an adapter bound to one runtime/activation.
     * @param runtime 预期长期 runtime / Expected long-lived runtime.
     * @param activation 预期 activation / Expected activation.
     * @param establish 建立 PID1/cgroup/overlay 的效果 / Effect that establishes PID1/cgroup/overlay.
     * @param retire 终止 PID1/cgroup/overlay 的效果 / Effect that retires PID1/cgroup/overlay.
     */
    BrokerRuntimeActivationPort(
        const domain::RuntimeId& runtime,
        const domain::ActivationId& activation,
        Operation establish,
        Operation retire)
        : runtime_(runtime), activation_(activation), establish_(std::move(establish)), retire_(std::move(retire)) {}

    /**
     * @brief 执行建立效果 / Perform the establish effect.
     * @param runtime application 提供的 runtime / Runtime supplied by application.
     * @param activation application 提供的 activation / Activation supplied by application.
     * @return 成功或映射后的领域错误 / Success or mapped domain error.
     */
    [[nodiscard]] domain::Result<void> establish(
        const domain::RuntimeId& runtime,
        const domain::ActivationId& activation) override {
        return invoke(runtime, activation, establish_, "establish");
    }

    /**
     * @brief 执行退役效果 / Perform the retire effect.
     * @param runtime application 提供的 runtime / Runtime supplied by application.
     * @param activation application 提供的 activation / Activation supplied by application.
     * @return 成功或映射后的领域错误 / Success or mapped domain error.
     */
    [[nodiscard]] domain::Result<void> retire(
        const domain::RuntimeId& runtime,
        const domain::ActivationId& activation) override {
        return invoke(runtime, activation, retire_, "retire");
    }

    /** @brief 返回保留的 native failure / Return the retained native failure. */
    [[nodiscard]] const std::optional<Error>& native_error() const noexcept { return native_error_; }

private:
    /**
     * @brief 验证所有权后执行一个 host 效果 / Validate ownership and run one host effect.
     * @param runtime application 提供的 runtime / Runtime supplied by application.
     * @param activation application 提供的 activation / Activation supplied by application.
     * @param operation 待执行效果 / Effect to perform.
     * @param name 诊断操作名 / Diagnostic operation name.
     * @return 成功或映射后的领域错误 / Success or mapped domain error.
     */
    [[nodiscard]] domain::Result<void> invoke(
        const domain::RuntimeId& runtime,
        const domain::ActivationId& activation,
        Operation& operation,
        const std::string_view name) {
        if (runtime != runtime_ || activation != activation_) {
            return std::unexpected(domain::make_error(
                domain::ErrorCode::activation_mismatch,
                "activation port was invoked for a different runtime or activation"));
        }
        if (!operation) {
            return std::unexpected(domain::make_error(
                domain::ErrorCode::illegal_transition,
                "runtime activation port has no " + std::string(name) + " operation"));
        }
        const auto completed = operation();
        if (completed) {
            return {};
        }
        native_error_ = completed.error();
        const domain::ErrorCode category = completed.error().code == ErrorCode::invalid_argument
            ? domain::ErrorCode::invalid_identity
            : domain::ErrorCode::illegal_transition;
        return std::unexpected(domain::make_error(
            category,
            "privileged runtime " + std::string(name) + " failed: " + completed.error().message));
    }

    /** @brief 绑定 runtime / Bound runtime. */
    const domain::RuntimeId& runtime_;
    /** @brief 绑定 activation / Bound activation. */
    const domain::ActivationId& activation_;
    /** @brief 实际 establish 操作 / Actual establish operation. */
    Operation establish_;
    /** @brief 实际 retire 操作 / Actual retire operation. */
    Operation retire_;
    /** @brief 最近一次 native 失败 / Most recent native failure. */
    std::optional<Error> native_error_;
};

/**
 * @brief 将 broker SharedState 读模型适配为 application 状态端口 / Adapt the broker SharedState read model to the application status port.
 *
 * 这个适配器只持有一个已构造的读取函数；它没有 activation、quota、journal 或 supervisor I/O
 * 能力。/ This adapter holds only a preconstructed read function; it has no activation, quota,
 * journal, or supervisor-I/O capability.
 */
class BrokerRuntimeStatusPort final : public application::RuntimeStatusPort {
public:
    /** @brief 一个无副作用 runtime 状态读取函数 / One side-effect-free runtime-status reader. */
    using Reader = std::function<domain::Result<application::RuntimeStatus>(const application::RuntimeStatusQuery&)>;

    /**
     * @brief 以读取函数构造端口 / Construct the port from a read function.
     * @param reader 已绑定 SharedState 的读取函数 / Reader bound to SharedState.
     */
    explicit BrokerRuntimeStatusPort(Reader reader) : reader_(std::move(reader)) {}

    /**
     * @brief 执行只读状态查询 / Perform the read-only status query.
     * @param query 已验证 runtime/activation 查询 / Validated runtime/activation query.
     * @return allowlisted 运行态或领域错误 / Allowlisted operating status or a domain error.
     */
    [[nodiscard]] domain::Result<application::RuntimeStatus> observe(
        const application::RuntimeStatusQuery& query) const override {
        if (!reader_) {
            return std::unexpected(domain::make_error(
                domain::ErrorCode::illegal_transition,
                "runtime status port has no read model"));
        }
        return reader_(query);
    }

private:
    /** @brief 已绑定 SharedState 的只读函数 / Read-only function bound to SharedState. */
    Reader reader_;
};

/** @brief runtime supervisor 启动时固定保留的 FD / Fixed FDs retained while launching runtime supervisor. */
enum class LaunchFd : int {
    control = 3,
    pid_report = 4,
    release = 5,
    supervisor_cgroup_procs = 6,
    task_cgroup_procs = 7,
    task_cgroup_kill = 8,
    task_cgroup_events = 9,
};

/** @brief Bot endpoint 同时服务 client 的硬上限 / Hard cap for clients served from the Bot endpoint. */
constexpr std::size_t kMaxClientWorkers{32U};
/** @brief 为独立 operator endpoint 保留的 worker 数 / Workers reserved for the independent operator endpoint. */
constexpr std::size_t kReservedOperatorWorkers{2U};
/** @brief 等待 worker 的已 accept client 硬上限 / Hard cap for accepted clients waiting for a worker. */
constexpr std::size_t kMaxQueuedClients{64U};
/** @brief 为独立 operator endpoint 保留的已 accept client 硬上限 / Reserved accepted-client cap for the independent operator endpoint. */
constexpr std::size_t kMaxQueuedOperatorClients{16U};

/** @brief 关闭临时或借用 FD / Close a temporary or borrowed FD. */
void close_fd(const int& fd) noexcept {
    if (fd >= 0) {
        static_cast<void>(close(fd));
    }
}

/** @brief 关闭并清空拥有的 FD / Close and invalidate an owned FD. */
void close_fd(int& fd) noexcept {
    if (fd >= 0) {
        static_cast<void>(close(fd));
        fd = -1;
    }
}

/** @brief 为控制 socket 配置有界单包与 I/O deadline / Configure bounded packets and an I/O deadline on a control socket. */
[[nodiscard]] Result<void> configure_control_socket(const int fd, const std::chrono::milliseconds deadline) {
    const int requested_buffer = static_cast<int>(kMaxFrameBytes * 2U);
    if (setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &requested_buffer, sizeof(requested_buffer)) != 0 ||
        setsockopt(fd, SOL_SOCKET, SO_RCVBUF, &requested_buffer, sizeof(requested_buffer)) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "configure SOCK_SEQPACKET buffer"));
    }
    int actual_buffer = 0;
    socklen_t actual_size = sizeof(actual_buffer);
    if (getsockopt(fd, SOL_SOCKET, SO_SNDBUF, &actual_buffer, &actual_size) != 0 ||
        actual_buffer < static_cast<int>(kMaxFrameBytes)) {
        return std::unexpected(make_error(ErrorCode::io_failure, "SOCK_SEQPACKET buffer cannot carry one bounded frame"));
    }
    const auto milliseconds = std::max<std::int64_t>(1, deadline.count());
    const timeval timeout{
        .tv_sec = static_cast<time_t>(milliseconds / 1000),
        .tv_usec = static_cast<suseconds_t>((milliseconds % 1000) * 1000),
    };
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "configure SOCK_SEQPACKET deadline"));
    }
    return {};
}

/** @brief 尽力关闭一段 FD 范围 / Close a range of FDs. */
void close_range_from(const unsigned int first) noexcept {
#ifdef SYS_close_range
    if (syscall(SYS_close_range, first, UINT_MAX, 0U) == 0) {
        return;
    }
#endif
    struct rlimit limit {};
    if (getrlimit(RLIMIT_NOFILE, &limit) != 0) {
        limit.rlim_cur = 65'536U;
    }
    const rlim_t cap = std::min<rlim_t>(limit.rlim_cur, 1'048'576U);
    for (int fd = static_cast<int>(first); static_cast<rlim_t>(fd) < cap; ++fd) {
        close_fd(fd);
    }
}

/** @brief 清除一个 FD 的 CLOEXEC / Clear FD_CLOEXEC on one FD. */
[[nodiscard]] bool clear_cloexec(const int fd) noexcept {
    const int flags = fcntl(fd, F_GETFD);
    return flags >= 0 && fcntl(fd, F_SETFD, flags & ~FD_CLOEXEC) == 0;
}

/**
 * @brief 将 launcher 进程缩减为七个白名单 FD / Reduce launcher process to seven whitelisted FDs.
 * @param control supervisor control socket / Supervisor control socket.
 * @param pid_report PID report pipe write end / PID report pipe write end.
 * @param release broker release pipe read end / Broker release pipe read end.
 * @param cgroup cgroup task controls / cgroup task controls.
 * @return 成功与否 / Whether whitelisting succeeded.
 */
[[nodiscard]] bool install_launcher_fd_whitelist(
    const int control,
    const int pid_report,
    const int release,
    const TaskCgroupControl& cgroup) noexcept {
    const std::array<int, 7> sources{
        control, pid_report, release, cgroup.supervisor_procs_fd, cgroup.procs_fd, cgroup.kill_fd, cgroup.events_fd};
    std::array<int, 7> staging{-1, -1, -1, -1, -1, -1, -1};
    for (std::size_t index = 0; index < sources.size(); ++index) {
        staging[index] = fcntl(sources[index], F_DUPFD_CLOEXEC, 32);
        if (staging[index] < 0) {
            for (const int fd : staging) {
                close_fd(fd);
            }
            return false;
        }
    }
    constexpr std::array<LaunchFd, 7> kDestinations{
        LaunchFd::control,
        LaunchFd::pid_report,
        LaunchFd::release,
        LaunchFd::supervisor_cgroup_procs,
        LaunchFd::task_cgroup_procs,
        LaunchFd::task_cgroup_kill,
        LaunchFd::task_cgroup_events,
    };
    for (std::size_t index = 0; index < staging.size(); ++index) {
        if (dup3(staging[index], static_cast<int>(kDestinations[index]), 0) < 0 ||
            !clear_cloexec(static_cast<int>(kDestinations[index]))) {
            for (const int fd : staging) {
                close_fd(fd);
            }
            return false;
        }
    }
    for (const int fd : staging) {
        close_fd(fd);
    }
    close_range_from(10U);
    return true;
}

/**
 * @brief 向 cgroup.procs FD 写入当前调用者 ``0`` / Write caller ``0`` to a cgroup.procs FD.
 * @param descriptor 已验证的可写 cgroup.procs FD / Verified writable cgroup.procs FD.
 * @return 写入完整 ``0`` 时为真 / True when a complete ``0`` was written.
 * @note launcher 在 fork namespace PID 1 前调用它；PID 1 因而继承 supervisor cgroup，broker
 *       不再向 cgroup.procs 写可复用的 host PID。 The launcher calls this before forking namespace
 *       PID 1, which inherits the supervisor cgroup; the broker no longer writes a reusable host PID.
 */
[[nodiscard]] bool join_cgroup_self(const int descriptor) noexcept {
    /** @brief cgroup self-join payload / cgroup self-join payload. */
    constexpr std::string_view kSelf{"0"};
    std::size_t offset{0U};
    while (offset < kSelf.size()) {
        const ssize_t count = write(
            descriptor,
            kSelf.data() + static_cast<std::ptrdiff_t>(offset),
            kSelf.size() - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(count);
    }
    return true;
}

/** @brief 向 pipe 写入完整 PID / Write a complete PID to a pipe. */
[[nodiscard]] bool write_pid(const int fd, const pid_t pid) noexcept {
    const auto* bytes = reinterpret_cast<const std::byte*>(&pid);
    std::size_t offset = 0;
    while (offset < sizeof(pid)) {
        const ssize_t count = write(fd, bytes + static_cast<std::ptrdiff_t>(offset), sizeof(pid) - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return false;
        }
        offset += static_cast<std::size_t>(count);
    }
    return true;
}

/** @brief 从 pipe 读取完整 PID / Read a complete PID from a pipe. */
[[nodiscard]] Result<pid_t> read_pid(const int fd) {
    pid_t pid = -1;
    auto* bytes = reinterpret_cast<std::byte*>(&pid);
    std::size_t offset = 0;
    while (offset < sizeof(pid)) {
        const ssize_t count = read(fd, bytes + static_cast<std::ptrdiff_t>(offset), sizeof(pid) - offset);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return std::unexpected(make_error(ErrorCode::child_failure, "runtime launcher failed before reporting PID 1"));
        }
        offset += static_cast<std::size_t>(count);
    }
    if (pid <= 0) {
        return std::unexpected(make_error(ErrorCode::child_failure, "runtime launcher reported invalid PID 1"));
    }
    return pid;
}

/** @brief 发送一个 Error 帧 / Send one Error frame. */
[[nodiscard]] Result<void> send_error_frame(const int fd, const Error& error) {
    const auto payload = encode_error(error);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = encode_frame(MessageKind::error, *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return send_frame(fd, *wire);
}

/** @brief 发送一个 Result 帧 / Send one Result frame. */
[[nodiscard]] Result<void> send_result_frame(const int fd, const ExecutionResult& result) {
    const auto payload = encode_execution_result(result);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = encode_frame(MessageKind::result, *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return send_frame(fd, *wire);
}

/** @brief 发送一个 allowlisted runtime 状态快照 / Send one allowlisted runtime-status snapshot. */
[[nodiscard]] Result<void> send_runtime_status_frame(const int fd, const RuntimeStatusResult& result) {
    const auto payload = encode_runtime_status_result(result);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = encode_frame(MessageKind::runtime_status_result, *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return send_frame(fd, *wire);
}

/**
 * @brief 将 application operator 查询错误映射为不带文本的 operator wire 错误 / Map an application operator-query error to a text-free operator wire error.
 * @param error 应用层查询错误 / Application query error.
 * @return operator wire 错误码 / Operator wire error code.
 */
[[nodiscard]] operator_protocol::OperatorErrorCode operator_error_code(
    const application::OperatorWorkspaceQueryError& error) noexcept {
    switch (error.code) {
        case application::OperatorWorkspaceQueryErrorCode::not_found:
            return operator_protocol::OperatorErrorCode::not_found;
        case application::OperatorWorkspaceQueryErrorCode::inconsistent:
        case application::OperatorWorkspaceQueryErrorCode::unavailable:
            return operator_protocol::OperatorErrorCode::unavailable;
    }
    return operator_protocol::OperatorErrorCode::unavailable;
}

/**
 * @brief 发送不含诊断或 payload 的 operator 错误帧 / Send an operator error frame without diagnostics or payload.
 * @param fd operator client FD / Operator client FD.
 * @param code operator wire 错误码 / Operator wire error code.
 * @return 成功或 transport 错误 / Success or a transport error.
 */
[[nodiscard]] Result<void> send_operator_error_frame(
    const int fd,
    const operator_protocol::OperatorErrorCode code) {
    const auto payload = operator_protocol::encode_error_response(operator_protocol::ErrorResponse{.code = code});
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = operator_protocol::encode_operator_frame(
        operator_protocol::OperatorMessageKind::error_response,
        *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return operator_protocol::send_operator_frame(fd, *wire);
}

/**
 * @brief 发送 operator runtime 状态帧 / Send an operator runtime-status frame.
 * @param fd operator client FD / Operator client FD.
 * @param status allowlisted runtime 状态 / Allowlisted runtime status.
 * @return 成功或 transport 错误 / Success or a transport error.
 */
[[nodiscard]] Result<void> send_operator_status_frame(
    const int fd,
    const domain::OperatorWorkspaceStatus& status) {
    const auto payload = operator_protocol::encode_status_response(operator_protocol::StatusResponse{.status = status});
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = operator_protocol::encode_operator_frame(
        operator_protocol::OperatorMessageKind::status_response,
        *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return operator_protocol::send_operator_frame(fd, *wire);
}

/**
 * @brief 发送 operator workspace 目录帧 / Send an operator workspace-directory frame.
 * @param fd operator client FD / Operator client FD.
 * @param listing 有界目录列举 / Bounded directory listing.
 * @return 成功或 transport 错误 / Success or a transport error.
 */
[[nodiscard]] Result<void> send_operator_list_frame(
    const int fd,
    const domain::WorkspaceListing& listing) {
    const auto payload = operator_protocol::encode_list_response(operator_protocol::ListResponse{.listing = listing});
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = operator_protocol::encode_operator_frame(
        operator_protocol::OperatorMessageKind::list_response,
        *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return operator_protocol::send_operator_frame(fd, *wire);
}

/**
 * @brief 将 runtime 生命周期状态投影为 operator 活动状态 / Project a runtime lifecycle state into an operator activity state.
 * @param state 已观察 runtime 生命周期状态 / Observed runtime lifecycle state.
 * @return 不含 activation ID 的 operator 活动状态 / Operator activity state without an activation ID.
 */
[[nodiscard]] domain::WorkspaceActivity workspace_activity_from_runtime_state(
    const domain::RuntimeState state) noexcept {
    switch (state) {
        case domain::RuntimeState::dormant:
            return domain::WorkspaceActivity::inactive;
        case domain::RuntimeState::activating:
            return domain::WorkspaceActivity::activating;
        case domain::RuntimeState::ready:
            return domain::WorkspaceActivity::ready;
        case domain::RuntimeState::executing:
            return domain::WorkspaceActivity::executing;
        case domain::RuntimeState::retiring:
            return domain::WorkspaceActivity::retiring;
        case domain::RuntimeState::failed:
            return domain::WorkspaceActivity::failed;
    }
    return domain::WorkspaceActivity::failed;
}

/**
 * @brief 将 native 只读失败归一化为 application operator 查询错误 / Normalize a native read-only failure into an application operator-query error.
 * @param error native 失败 / Native failure.
 * @return 不含 host path 的 application 查询错误 / Application query error without a host path.
 */
[[nodiscard]] application::OperatorWorkspaceQueryError normalize_operator_read_error(const Error& error) {
    if (error.code == ErrorCode::not_found) {
        return application::make_operator_workspace_query_error(
            application::OperatorWorkspaceQueryErrorCode::not_found,
            "operator workspace object was not found");
    }
    if (error.code == ErrorCode::invocation_in_doubt || error.code == ErrorCode::journal_conflict) {
        return application::make_operator_workspace_query_error(
            application::OperatorWorkspaceQueryErrorCode::inconsistent,
            "operator workspace read model is inconsistent");
    }
    return application::make_operator_workspace_query_error(
        application::OperatorWorkspaceQueryErrorCode::unavailable,
        "operator workspace read model is unavailable");
}

/** @brief 发送一个文件传输阶段 ACK / Send one file-transfer phase acknowledgement. */
[[nodiscard]] Result<void> send_payload_ack_frame(const int fd, const PayloadAck& acknowledgement) {
    const auto payload = encode_payload_ack(acknowledgement);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = encode_frame(MessageKind::payload_ack, *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return send_frame(fd, *wire);
}

/** @brief 发送一个文件写入收据 / Send one file-ingress receipt. */
[[nodiscard]] Result<void> send_payload_result_frame(const int fd, const PayloadResult& result) {
    const auto payload = encode_payload_result(result);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto wire = encode_frame(MessageKind::payload_result, *payload);
    if (!wire) {
        return std::unexpected(wire.error());
    }
    return send_frame(fd, *wire);
}

/**
 * @brief 在一次 gate 内解析命令 journal 决定 / Resolve an execution journal decision while holding one runtime gate.
 * @param journal runtime journal / Runtime journal.
 * @param request 已验证 command 请求 / Validated command request.
 * @return 空表示首次执行；非空表示完成回放；或 conflict/in-doubt/error /
 *         Empty means first execution; a value means completed replay; otherwise conflict/in-doubt/error.
 * @note gate 前调用可快速回放；任何准备 begin 副作用的调用者还必须在 gate 内再次调用。
 *       A pre-gate call may fast-replay; every caller about to begin a side effect must call it again inside the gate.
 */
[[nodiscard]] Result<std::optional<ExecutionResult>> resolve_execution_journal(
    const Journal& journal,
    const ExecuteRequest& request) {
    const auto existing = journal.lookup(request.runtime_key, request.request_id);
    if (!existing) {
        return std::unexpected(existing.error());
    }
    if (!existing->has_value()) {
        return std::optional<ExecutionResult>{};
    }
    const JournalRecord& record = **existing;
    if (record.operation != JournalOperation::execution || record.request_hash != request.request_hash ||
        record.payload_hash != canonical_request_hash(request)) {
        return std::unexpected(make_error(
            ErrorCode::journal_conflict,
            "request ID was previously used with another journal operation or command metadata"));
    }
    if (record.state == JournalState::pending) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "invocation is pending after an interrupted execution"));
    }
    if (!record.execution_result.has_value()) {
        return std::unexpected(make_error(ErrorCode::journal_conflict, "completed execution journal has no result"));
    }
    ExecutionResult replay = *record.execution_result;
    replay.replayed = true;
    return std::optional<ExecutionResult>{std::move(replay)};
}

/**
 * @brief 在一次 gate 内解析文件 ingress journal 决定 / Resolve a file-ingress journal decision while holding one runtime gate.
 * @param journal runtime journal / Runtime journal.
 * @param request 已验证文件开始请求 / Validated file-begin request.
 * @return 空表示可开始流；非空表示完成收据回放；或 conflict/in-doubt/error /
 *         Empty means a stream may begin; a value means a completed-receipt replay; otherwise conflict/in-doubt/error.
 * @note 此决定可在 gate 前用于快速回放；开始流之前必须在 gate 内重读，因此另一 worker 在第一次
 *       lookup 后完成/中断时，本 worker 不会重新 seal 相同 invocation。/ This decision may
 *       fast-replay before the gate; it must be reread inside the gate before streaming, so if another
 *       worker completes or interrupts after the first lookup, this worker never reseals the same invocation.
 */
[[nodiscard]] Result<std::optional<PayloadResult>> resolve_payload_journal(
    const Journal& journal,
    const PayloadBeginRequest& request) {
    const auto existing = journal.lookup(request.runtime_key, request.request_id);
    if (!existing) {
        return std::unexpected(existing.error());
    }
    if (!existing->has_value()) {
        return std::optional<PayloadResult>{};
    }
    const JournalRecord& record = **existing;
    if (record.operation != JournalOperation::payload || record.request_hash != request.request_hash ||
        record.payload_hash != canonical_payload_hash(request)) {
        return std::unexpected(make_error(
            ErrorCode::journal_conflict,
            "request ID was previously used with another journal operation or file metadata"));
    }
    if (record.state == JournalState::pending) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "file invocation is pending after an interrupted publish"));
    }
    if (!record.payload_result.has_value()) {
        return std::unexpected(make_error(ErrorCode::journal_conflict, "completed file journal has no receipt"));
    }
    PayloadResult replay = *record.payload_result;
    const std::string expected_path = "/workspace/uploads/" + request.opaque_id + "/payload";
    if (replay.path != expected_path || replay.byte_size != request.byte_size || replay.sha256 != request.sha256) {
        return std::unexpected(make_error(
            ErrorCode::journal_conflict,
            "completed file receipt does not match file begin metadata"));
    }
    replay.replayed = true;
    return std::optional<PayloadResult>{std::move(replay)};
}

/** @brief 验证并规范化 broker socket 目录为 root-owned 且不可被他人写 / Validate and canonicalize a root-owned non-writable broker socket directory. */
[[nodiscard]] Result<std::filesystem::path> validate_socket_parent(
    const std::filesystem::path& socket_path,
    const bool allow_insecure_dev_root) {
    if (!socket_path.is_absolute() || socket_path.filename().empty()) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "broker socket path must be absolute"));
    }
    std::error_code error;
    const std::filesystem::path parent = std::filesystem::canonical(socket_path.parent_path(), error);
    if (error) {
        return std::unexpected(make_error(ErrorCode::not_found, "broker socket parent does not exist"));
    }
    if (const auto secure = validate_secure_directory_ancestry(parent, allow_insecure_dev_root); !secure) {
        return std::unexpected(secure.error());
    }
    return parent;
}

/**
 * @brief 判断一个已 canonical 的目录是否包含另一个目录 / Check whether one canonical directory contains another.
 * @param ancestor 候选祖先目录 / Candidate ancestor directory.
 * @param descendant 候选后代目录 / Candidate descendant directory.
 * @return 相等或 ancestor 是 descendant 的祖先时为真 / True when equal or when ancestor contains descendant.
 */
[[nodiscard]] bool canonical_directory_contains(
    const std::filesystem::path& ancestor,
    const std::filesystem::path& descendant) noexcept {
    auto ancestor_component = ancestor.begin();
    auto descendant_component = descendant.begin();
    while (ancestor_component != ancestor.end()) {
        if (descendant_component == descendant.end() || *ancestor_component != *descendant_component) {
            return false;
        }
        ++ancestor_component;
        ++descendant_component;
    }
    return true;
}

/**
 * @brief 在已验证私有父目录中回收一个无 listener 的 stale UNIX socket / Reclaim one stale UNIX socket below a verified private parent.
 * @param socket_path 待绑定的受控 endpoint / Controlled endpoint about to be bound.
 * @param description 仅供 host 诊断的 endpoint 名称 / Endpoint name used only for host diagnostics.
 * @return 成功、路径不存在或已安全 unlink；活 listener/不确定状态返回 fail-closed 错误 /
 *     Success, a missing path, or a safely unlinked stale socket; a live listener or uncertain
 *     state returns a fail-closed error.
 * @note 调用方必须已验证 parent 的 root ownership/non-writability。只会删除已通过 `lstat`
 *       证明为 socket 且 nonblocking `connect` 明确返回 `ECONNREFUSED` 的同一 inode；绝不删除
 *       regular file、directory、symlink 或可连通 listener。/ The caller must have validated root
 *       ownership/non-writability of the parent. This removes only the same inode proven by
 *       `lstat` to be a socket whose nonblocking `connect` explicitly returns `ECONNREFUSED`; it
 *       never removes a regular file, directory, symlink, or connectable listener.
 */
[[nodiscard]] Result<void> reclaim_stale_listener_path(
    const std::filesystem::path& socket_path,
    const std::string_view description) {
    struct stat original {};
    if (lstat(socket_path.c_str(), &original) != 0) {
        if (errno == ENOENT) {
            return {};
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "lstat " + std::string(description) + " socket"));
    }
    if (!S_ISSOCK(original.st_mode)) {
        return std::unexpected(make_error(
            ErrorCode::already_exists,
            std::string(description) + " socket path names a non-socket object"));
    }
    const int probe_fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (probe_fd < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create stale " + std::string(description) + " probe"));
    }
    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, socket_path.c_str(), sizeof(address.sun_path) - 1U);
    int connection_error{0};
    if (connect(probe_fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        connection_error = errno;
        if (connection_error == EINPROGRESS || connection_error == EALREADY) {
            pollfd readiness{.fd = probe_fd, .events = POLLOUT, .revents = 0};
            const int ready = poll(&readiness, 1U, 250);
            if (ready < 0) {
                const Error error = errno_error(ErrorCode::io_failure, "poll stale " + std::string(description) + " probe");
                close_fd(probe_fd);
                return std::unexpected(error);
            }
            if (ready == 0 || (readiness.revents & POLLNVAL) != 0) {
                close_fd(probe_fd);
                return std::unexpected(make_error(
                    ErrorCode::already_exists,
                    std::string(description) + " socket liveness is indeterminate; refusing unlink"));
            }
            socklen_t error_size = sizeof(connection_error);
            if (getsockopt(probe_fd, SOL_SOCKET, SO_ERROR, &connection_error, &error_size) != 0) {
                const Error error = errno_error(ErrorCode::io_failure, "read stale " + std::string(description) + " probe status");
                close_fd(probe_fd);
                return std::unexpected(error);
            }
            if (error_size != sizeof(connection_error)) {
                close_fd(probe_fd);
                return std::unexpected(make_error(
                    ErrorCode::io_failure,
                    "stale " + std::string(description) + " probe returned an invalid status size"));
            }
        }
    }
    close_fd(probe_fd);
    if (connection_error == 0) {
        return std::unexpected(make_error(
            ErrorCode::already_exists,
            std::string(description) + " socket has a live listener; refusing replacement"));
    }
    if (connection_error != ECONNREFUSED) {
        return std::unexpected(make_error(
            ErrorCode::already_exists,
            std::string(description) + " socket probe did not prove a stale listener; refusing unlink"));
    }
    struct stat current {};
    if (lstat(socket_path.c_str(), &current) != 0) {
        if (errno == ENOENT) {
            return {};
        }
        return std::unexpected(errno_error(ErrorCode::io_failure, "recheck stale " + std::string(description) + " socket"));
    }
    if (!S_ISSOCK(current.st_mode) || current.st_dev != original.st_dev || current.st_ino != original.st_ino) {
        return std::unexpected(make_error(
            ErrorCode::already_exists,
            std::string(description) + " socket path changed during stale recovery; refusing unlink"));
    }
    if (unlink(socket_path.c_str()) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "unlink stale " + std::string(description) + " socket"));
    }
    return {};
}

/**
 * @brief 在 namespace 中启动 PID 1 / Launch PID 1 in a new namespace.
 * @param config broker 配置 / Broker configuration.
 * @param layer 已准备 OverlayFS 层 / Prepared OverlayFS layer.
 * @param cgroup task cgroup 控制 FD / Task cgroup control FDs.
 * @param control_fd PID 1 一端的控制 socket / PID 1 end of control socket.
 * @param pid_report_fd launcher 写端 / Launcher write end.
 * @param release_fd PID 1 读端 / PID 1 read end.
 * @return 不返回 / Does not return.
 */
[[noreturn]] void launch_namespace_pid1(
    const BrokerConfig& config,
    const TaskLayer& layer,
    const TaskCgroupControl& cgroup,
    const int control_fd,
    const int pid_report_fd,
    const int release_fd) {
    if (!install_launcher_fd_whitelist(control_fd, pid_report_fd, release_fd, cgroup)) {
        _exit(125);
    }
    if (!join_cgroup_self(static_cast<int>(LaunchFd::supervisor_cgroup_procs))) {
        _exit(125);
    }
    if (unshare(CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWUTS | CLONE_NEWIPC | CLONE_NEWNET | CLONE_NEWCGROUP) != 0) {
        _exit(125);
    }
    const pid_t pid1 = fork();
    if (pid1 < 0) {
        _exit(125);
    }
    if (pid1 > 0) {
        close_fd(static_cast<int>(LaunchFd::control));
        close_fd(static_cast<int>(LaunchFd::release));
        close_fd(static_cast<int>(LaunchFd::supervisor_cgroup_procs));
        close_fd(static_cast<int>(LaunchFd::task_cgroup_procs));
        close_fd(static_cast<int>(LaunchFd::task_cgroup_kill));
        close_fd(static_cast<int>(LaunchFd::task_cgroup_events));
        const bool reported = write_pid(static_cast<int>(LaunchFd::pid_report), pid1);
        close_fd(static_cast<int>(LaunchFd::pid_report));
        int status = 0;
        while (waitpid(pid1, &status, 0) < 0 && errno == EINTR) {
        }
        _exit(reported ? 0 : 125);
    }
    close_fd(static_cast<int>(LaunchFd::pid_report));
    close_fd(static_cast<int>(LaunchFd::supervisor_cgroup_procs));
    char release = 0;
    ssize_t count = 0;
    do {
        count = read(static_cast<int>(LaunchFd::release), &release, 1U);
    } while (count < 0 && errno == EINTR);
    close_fd(static_cast<int>(LaunchFd::release));
    if (count != 1 || release != 'R') {
        _exit(125);
    }
    if (const auto mounted = setup_runtime_mounts(config.sandbox, layer); !mounted) {
        _exit(125);
    }
    const std::string control = std::to_string(static_cast<int>(LaunchFd::control));
    const std::string procs = std::to_string(static_cast<int>(LaunchFd::task_cgroup_procs));
    const std::string kill = std::to_string(static_cast<int>(LaunchFd::task_cgroup_kill));
    const std::string events = std::to_string(static_cast<int>(LaunchFd::task_cgroup_events));
    const std::string uid = std::to_string(config.sandbox.sandbox_uid);
    const std::string gid = std::to_string(config.sandbox.sandbox_gid);
    constexpr const char* kSupervisorPath =
        "/usr/local/libexec/wspctl/wsp-systemd";
    execl(
        kSupervisorPath,
        kSupervisorPath,
        "--control-fd",
        control.c_str(),
        "--task-cgroup-procs-fd",
        procs.c_str(),
        "--task-cgroup-kill-fd",
        kill.c_str(),
        "--task-cgroup-events-fd",
        events.c_str(),
        "--sandbox-uid",
        uid.c_str(),
        "--sandbox-gid",
        gid.c_str(),
        static_cast<char*>(nullptr));
    _exit(125);
}

/** @brief fork-server 私有消息类型 / Private fork-server message kinds. */
enum class LauncherMessageKind : std::uint16_t {
    /** @brief 请求启动一个 runtime PID 1 / Request one runtime PID 1 launch. */
    launch = 1,
    /** @brief 返回启动句柄 / Return a launch handle. */
    launched = 2,
    /** @brief 返回启动拒绝 / Return a launch rejection. */
    error = 3,
    /** @brief 正常停止 fork-server / Cleanly stop the fork server. */
    shutdown = 4,
    /** @brief broker 已完成 cgroup placement 与 release / Broker completed cgroup placement and release. */
    commit = 5,
    /** @brief broker 放弃尚未 commit 的 launch / Broker abandons a launch that has not committed. */
    cancel = 6,
    /** @brief helper 已达 commit/cancel 终态 / Helper reached a commit/cancel terminal state. */
    terminal = 7,
    /** @brief broker 已实际写入 release pipe / Broker actually wrote the release pipe. */
    released = 8,
};

/** @brief fork-server wire magic / Fork-server wire magic. */
constexpr std::array<std::byte, 4> kLauncherMagic{std::byte{'W'}, std::byte{'S'}, std::byte{'P'}, std::byte{'L'}};
/** @brief fork-server wire version / Fork-server wire version. */
constexpr std::uint16_t kLauncherVersion{1U};
/** @brief fork-server 已解析包头 / Parsed fork-server packet header. */
struct LauncherHeader final {
    /** @brief 消息类型 / Message kind. */
    LauncherMessageKind kind;
    /** @brief broker 生成的 request ID / Broker-generated request ID. */
    std::uint64_t request_id;
    /** @brief payload 起始位置 / Payload start offset. */
    std::size_t payload_offset;
};

/** @brief helper 持有的未回收 launcher 记录 / Launcher record retained by the helper until reaping. */
struct HelperLaunchRecord final {
    /** @brief helper 的直接 child PID / Helper's direct child PID. */
    pid_t launcher_pid{-1};
    /** @brief namespace PID 1 的 identity-stable pidfd / Identity-stable pidfd of namespace PID 1. */
    int pid1_pidfd{-1};
    /** @brief helper 保留的 release pipe 写端 / Release-pipe write end retained by helper. */
    int retained_release_fd{-1};
    /** @brief 是否已由 broker commit / Whether broker has committed this launch. */
    bool committed{false};
    /** @brief 是否已由 broker 写入 release pipe / Whether broker wrote the release pipe. */
    bool released{false};
    /** @brief 尚未 release 时的绝对过期点 / Absolute expiry while not released. */
    std::chrono::steady_clock::time_point expiry;
};

/** @brief 写入 little-endian u16 / Append a little-endian u16. */
void append_launcher_u16(std::vector<std::byte>& bytes, const std::uint16_t value) {
    bytes.push_back(static_cast<std::byte>(value & 0xffU));
    bytes.push_back(static_cast<std::byte>((value >> 8U) & 0xffU));
}

/** @brief 写入 little-endian u32 / Append a little-endian u32. */
void append_launcher_u32(std::vector<std::byte>& bytes, const std::uint32_t value) {
    for (unsigned int index = 0; index < 4U; ++index) {
        bytes.push_back(static_cast<std::byte>((value >> (index * 8U)) & 0xffU));
    }
}

/** @brief 写入 little-endian u64 / Append a little-endian u64. */
void append_launcher_u64(std::vector<std::byte>& bytes, const std::uint64_t value) {
    for (unsigned int index = 0; index < 8U; ++index) {
        bytes.push_back(static_cast<std::byte>((value >> (index * 8U)) & 0xffU));
    }
}

/** @brief 写入有界字符串 / Append a bounded string. */
[[nodiscard]] Result<void> append_launcher_string(std::vector<std::byte>& bytes, const std::string_view value) {
    if (value.size() > 4096U || value.find('\0') != std::string_view::npos) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "invalid fork-server string"));
    }
    append_launcher_u32(bytes, static_cast<std::uint32_t>(value.size()));
    for (const char character : value) {
        bytes.push_back(static_cast<std::byte>(static_cast<unsigned char>(character)));
    }
    return {};
}

/** @brief 从 bytes 读取 little-endian u16 / Read a little-endian u16 from bytes. */
[[nodiscard]] Result<std::uint16_t> read_launcher_u16(const std::span<const std::byte> bytes, std::size_t& offset) {
    if (offset > bytes.size() || bytes.size() - offset < 2U) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated fork-server u16"));
    }
    const std::uint16_t value = static_cast<std::uint16_t>(std::to_integer<unsigned char>(bytes[offset])) |
        (static_cast<std::uint16_t>(std::to_integer<unsigned char>(bytes[offset + 1U])) << 8U);
    offset += 2U;
    return value;
}

/** @brief 从 bytes 读取 little-endian u32 / Read a little-endian u32 from bytes. */
[[nodiscard]] Result<std::uint32_t> read_launcher_u32(const std::span<const std::byte> bytes, std::size_t& offset) {
    if (offset > bytes.size() || bytes.size() - offset < 4U) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated fork-server u32"));
    }
    std::uint32_t value = 0U;
    for (unsigned int index = 0; index < 4U; ++index) {
        value |= static_cast<std::uint32_t>(std::to_integer<unsigned char>(bytes[offset + index])) << (index * 8U);
    }
    offset += 4U;
    return value;
}

/** @brief 从 bytes 读取 little-endian u64 / Read a little-endian u64 from bytes. */
[[nodiscard]] Result<std::uint64_t> read_launcher_u64(const std::span<const std::byte> bytes, std::size_t& offset) {
    if (offset > bytes.size() || bytes.size() - offset < 8U) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "truncated fork-server u64"));
    }
    std::uint64_t value = 0U;
    for (unsigned int index = 0; index < 8U; ++index) {
        value |= static_cast<std::uint64_t>(std::to_integer<unsigned char>(bytes[offset + index])) << (index * 8U);
    }
    offset += 8U;
    return value;
}

/** @brief 从 bytes 读取有界字符串 / Read a bounded string from bytes. */
[[nodiscard]] Result<std::string> read_launcher_string(const std::span<const std::byte> bytes, std::size_t& offset) {
    const auto length = read_launcher_u32(bytes, offset);
    if (!length || *length > 4096U || *length > bytes.size() - offset) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid fork-server string length"));
    }
    std::string value;
    value.reserve(*length);
    for (std::uint32_t index = 0; index < *length; ++index) {
        value.push_back(static_cast<char>(std::to_integer<unsigned char>(bytes[offset + index])));
    }
    offset += *length;
    return value;
}

/** @brief 编码 fork-server 包头 / Encode a fork-server packet header. */
void append_launcher_header(
    std::vector<std::byte>& bytes,
    const LauncherMessageKind kind,
    const std::uint64_t request_id) {
    bytes.insert(bytes.end(), kLauncherMagic.begin(), kLauncherMagic.end());
    append_launcher_u16(bytes, kLauncherVersion);
    append_launcher_u16(bytes, static_cast<std::uint16_t>(kind));
    append_launcher_u64(bytes, request_id);
}

/** @brief 严格解析 fork-server 包头 / Strictly parse a fork-server packet header. */
[[nodiscard]] Result<LauncherHeader> parse_launcher_header(const std::span<const std::byte> bytes) {
    if (bytes.size() < 16U || bytes.size() > kMaxPacketBytes ||
        !std::equal(kLauncherMagic.begin(), kLauncherMagic.end(), bytes.begin())) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid fork-server packet magic or length"));
    }
    std::size_t offset = 4U;
    const auto version = read_launcher_u16(bytes, offset);
    const auto raw_kind = read_launcher_u16(bytes, offset);
    const auto request_id = read_launcher_u64(bytes, offset);
    if (!version || !raw_kind || !request_id || *version != kLauncherVersion ||
        (*raw_kind < static_cast<std::uint16_t>(LauncherMessageKind::launch) ||
         *raw_kind > static_cast<std::uint16_t>(LauncherMessageKind::released))) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid fork-server packet header"));
    }
    return LauncherHeader{
        .kind = static_cast<LauncherMessageKind>(*raw_kind),
        .request_id = *request_id,
        .payload_offset = offset,
    };
}

/** @brief 编码启动请求 / Encode a launch request. */
[[nodiscard]] Result<std::vector<std::byte>> encode_launcher_request(
    const std::uint64_t request_id,
    const TaskLayer& layer) {
    std::vector<std::byte> bytes;
    bytes.reserve(512U);
    append_launcher_header(bytes, LauncherMessageKind::launch, request_id);
    for (const std::filesystem::path* path : std::array<const std::filesystem::path*, 6>{
             &layer.runtime_dir, &layer.upper_dir, &layer.work_dir, &layer.root_dir,
             &layer.workspace_lower_dir, &layer.merged_dir}) {
        if (!path->is_absolute()) {
            return std::unexpected(make_error(ErrorCode::invalid_argument, "fork-server layer path must be absolute"));
        }
        if (const auto appended = append_launcher_string(bytes, path->string()); !appended) {
            return std::unexpected(appended.error());
        }
    }
    if (bytes.size() > kMaxPacketBytes) {
        return std::unexpected(make_error(ErrorCode::frame_too_large, "fork-server launch request exceeds quota"));
    }
    return bytes;
}

/** @brief 解码启动请求 / Decode a launch request. */
[[nodiscard]] Result<std::pair<std::uint64_t, TaskLayer>> decode_launcher_request(const std::span<const std::byte> bytes) {
    const auto header = parse_launcher_header(bytes);
    if (!header || header->kind != LauncherMessageKind::launch) {
        return std::unexpected(header ? make_error(ErrorCode::protocol_violation, "unexpected fork-server request kind") : header.error());
    }
    std::size_t offset = header->payload_offset;
    std::array<std::string, 6> paths;
    for (std::string& path : paths) {
        const auto decoded = read_launcher_string(bytes, offset);
        if (!decoded || !std::filesystem::path(*decoded).is_absolute()) {
            return std::unexpected(decoded ? make_error(ErrorCode::malformed_frame, "fork-server layer path is not absolute") : decoded.error());
        }
        path = *decoded;
    }
    if (offset != bytes.size()) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "trailing fork-server launch bytes"));
    }
    return std::pair{
        header->request_id,
        TaskLayer{
            .quota_binding = {},
            .activation_id = {},
            .runtime_dir = std::move(paths[0]),
            .upper_dir = std::move(paths[1]),
            .work_dir = std::move(paths[2]),
            .root_dir = std::move(paths[3]),
            .workspace_lower_dir = std::move(paths[4]),
            .merged_dir = std::move(paths[5]),
        }};
}

/** @brief 编码成功启动回复 / Encode a successful launch reply. */
[[nodiscard]] std::vector<std::byte> encode_launcher_reply(
    const std::uint64_t request_id,
    const pid_t launcher_pid,
    const pid_t pid1_pid) {
    std::vector<std::byte> bytes;
    bytes.reserve(24U);
    append_launcher_header(bytes, LauncherMessageKind::launched, request_id);
    append_launcher_u32(bytes, static_cast<std::uint32_t>(launcher_pid));
    append_launcher_u32(bytes, static_cast<std::uint32_t>(pid1_pid));
    return bytes;
}

/** @brief 解码成功启动回复 / Decode a successful launch reply. */
[[nodiscard]] Result<std::pair<pid_t, pid_t>> decode_launcher_reply(
    const std::span<const std::byte> bytes,
    const std::uint64_t expected_request_id) {
    const auto header = parse_launcher_header(bytes);
    if (!header || header->kind != LauncherMessageKind::launched || header->request_id != expected_request_id) {
        return std::unexpected(header ? make_error(ErrorCode::protocol_violation, "fork-server reply identity mismatch") : header.error());
    }
    std::size_t offset = header->payload_offset;
    const auto launcher_pid = read_launcher_u32(bytes, offset);
    const auto pid1_pid = read_launcher_u32(bytes, offset);
    if (!launcher_pid || !pid1_pid || offset != bytes.size() || *launcher_pid == 0U || *pid1_pid == 0U ||
        *launcher_pid > static_cast<std::uint32_t>(INT_MAX) || *pid1_pid > static_cast<std::uint32_t>(INT_MAX)) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid fork-server launch PIDs"));
    }
    return std::pair{static_cast<pid_t>(*launcher_pid), static_cast<pid_t>(*pid1_pid)};
}

/** @brief helper launch 终态 / Helper launch terminal state. */
enum class LauncherTerminalState : std::uint16_t {
    /** @brief broker 已 commit / Broker committed the launch. */
    committed = 1,
    /** @brief helper 已取消且回收 launcher / Helper cancelled and reaped the launcher. */
    cancelled = 2,
    /** @brief broker 已 release，helper 可关闭保留 FD / Broker released; helper may close retained FD. */
    released = 3,
};

/** @brief 编码 helper 终态确认 / Encode a helper terminal acknowledgement. */
[[nodiscard]] std::vector<std::byte> encode_launcher_terminal(
    const std::uint64_t request_id,
    const LauncherTerminalState state) {
    std::vector<std::byte> bytes;
    bytes.reserve(20U);
    append_launcher_header(bytes, LauncherMessageKind::terminal, request_id);
    append_launcher_u16(bytes, static_cast<std::uint16_t>(state));
    return bytes;
}

/** @brief 解码 helper 终态确认 / Decode a helper terminal acknowledgement. */
[[nodiscard]] Result<LauncherTerminalState> decode_launcher_terminal(
    const std::span<const std::byte> bytes,
    const std::uint64_t expected_request_id) {
    const auto header = parse_launcher_header(bytes);
    if (!header || header->kind != LauncherMessageKind::terminal || header->request_id != expected_request_id) {
        return std::unexpected(header ? make_error(ErrorCode::protocol_violation, "fork-server terminal identity mismatch") : header.error());
    }
    std::size_t offset = header->payload_offset;
    const auto state = read_launcher_u16(bytes, offset);
    if (!state || offset != bytes.size() ||
        (*state != static_cast<std::uint16_t>(LauncherTerminalState::committed) &&
         *state != static_cast<std::uint16_t>(LauncherTerminalState::cancelled) &&
         *state != static_cast<std::uint16_t>(LauncherTerminalState::released))) {
        return std::unexpected(make_error(ErrorCode::malformed_frame, "invalid fork-server terminal state"));
    }
    return static_cast<LauncherTerminalState>(*state);
}

/** @brief 编码 fork-server 错误 / Encode a fork-server error. */
[[nodiscard]] std::vector<std::byte> encode_launcher_error(const std::uint64_t request_id, const std::string_view message) {
    std::vector<std::byte> bytes;
    bytes.reserve(64U);
    append_launcher_header(bytes, LauncherMessageKind::error, request_id);
    static_cast<void>(append_launcher_string(bytes, message.substr(0U, 4096U)));
    return bytes;
}

/** @brief 验证 broker 传给 helper 的控制 socket 角色 / Validate the control-socket role broker passes to helper. */
[[nodiscard]] bool is_unix_seqpacket_socket(const int fd) noexcept {
    struct stat metadata {};
    int type = 0;
    socklen_t type_size = sizeof(type);
    sockaddr_storage address {};
    socklen_t address_size = sizeof(address);
    return fstat(fd, &metadata) == 0 && S_ISSOCK(metadata.st_mode) &&
           getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &type_size) == 0 && type == SOCK_SEQPACKET &&
           getsockname(fd, reinterpret_cast<sockaddr*>(&address), &address_size) == 0 &&
           address.ss_family == AF_UNIX;
}

/** @brief 读取一个 FD 在 procfs 中的稳定 link 文本 / Read one FD's stable procfs link text. */
[[nodiscard]] std::optional<std::string> fd_link_target(const int fd) {
    std::array<char, PATH_MAX> buffer{};
    const std::string path = "/proc/self/fd/" + std::to_string(fd);
    const ssize_t size = readlink(path.c_str(), buffer.data(), buffer.size());
    if (size <= 0 || static_cast<std::size_t>(size) >= buffer.size()) {
        return std::nullopt;
    }
    return std::string(buffer.data(), static_cast<std::size_t>(size));
}

/** @brief 验证一个 cgroup control FD 的 fs 与文件角色 / Validate one cgroup-control FD's filesystem and file role. */
[[nodiscard]] bool is_cgroup_control_fd(const int fd, const std::string_view expected_name) {
    struct statfs filesystem {};
    const int flags = fcntl(fd, F_GETFL);
    const auto target = fd_link_target(fd);
    return fstatfs(fd, &filesystem) == 0 && filesystem.f_type == CGROUP2_SUPER_MAGIC && flags >= 0 &&
           (flags & O_ACCMODE) == O_WRONLY && target.has_value() && target->ends_with(expected_name);
}

/** @brief 验证 task cgroup.events 读取 FD 的 fs 与文件角色 / Validate task cgroup.events FD role. */
[[nodiscard]] bool is_task_cgroup_events_fd(const int fd) {
    struct statfs filesystem {};
    const int flags = fcntl(fd, F_GETFL);
    const auto target = fd_link_target(fd);
    return fstatfs(fd, &filesystem) == 0 && filesystem.f_type == CGROUP2_SUPER_MAGIC && flags >= 0 &&
           (flags & O_ACCMODE) == O_RDONLY && target.has_value() && target->ends_with("/cgroup.events");
}

/** @brief 验证 helper 回传的 release pipe 写端 / Validate the helper-returned release-pipe write end. */
[[nodiscard]] bool is_pipe_write_fd(const int fd) noexcept {
    struct stat metadata {};
    const int flags = fcntl(fd, F_GETFL);
    return fstat(fd, &metadata) == 0 && S_ISFIFO(metadata.st_mode) && flags >= 0 &&
           (flags & O_ACCMODE) == O_WRONLY;
}

/** @brief 验证 helper 回传的 pidfd 类型 / Validate the helper-returned pidfd type. */
[[nodiscard]] bool is_pidfd(const int fd) {
    const std::string path = "/proc/self/fdinfo/" + std::to_string(fd);
    const int info_fd = open(path.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (info_fd < 0) {
        return false;
    }
    std::array<char, 512> buffer{};
    const ssize_t size = read(info_fd, buffer.data(), buffer.size());
    close_fd(info_fd);
    return size > 0 && std::string_view(buffer.data(), static_cast<std::size_t>(size)).find("Pid:\t") != std::string_view::npos;
}

/** @brief 验证 broker->helper launch FD 集 / Validate the exact broker-to-helper launch FD set. */
[[nodiscard]] bool has_valid_launch_request_fds(const LauncherPacket& packet) {
    return packet.fd_count == detail::launcher_transport::kMaxFileDescriptors && is_unix_seqpacket_socket(packet.fds[0]) &&
           is_cgroup_control_fd(packet.fds[1], "/supervisor/cgroup.procs") &&
           is_cgroup_control_fd(packet.fds[2], "/task/cgroup.procs") &&
           is_cgroup_control_fd(packet.fds[3], "/task/cgroup.kill") &&
           is_task_cgroup_events_fd(packet.fds[4]);
}

/** @brief 验证 helper->broker launch reply FD 集 / Validate the exact helper-to-broker launch reply FD set. */
[[nodiscard]] bool has_valid_launch_reply_fds(const LauncherPacket& packet) {
    return packet.fd_count == 2U && is_pipe_write_fd(packet.fds[0]) && is_pidfd(packet.fds[1]);
}

/** @brief 在 deadline 内读取 launcher 报告的 PID / Read a launcher-reported PID within a deadline. */
[[nodiscard]] Result<pid_t> read_pid_with_deadline(const int fd, const std::chrono::milliseconds deadline) {
    pollfd descriptor{.fd = fd, .events = POLLIN | POLLHUP, .revents = 0};
    const int ready = poll(&descriptor, 1U, static_cast<int>(std::min<std::int64_t>(deadline.count(), INT_MAX)));
    if (ready <= 0) {
        return std::unexpected(ready == 0 ? make_error(ErrorCode::timeout, "fork-server timed out waiting for namespace PID 1") : errno_error(ErrorCode::io_failure, "poll namespace PID 1 report"));
    }
    return read_pid(fd);
}

/** @brief 判断 helper-owned launcher 的 pidfd 是否报告退出 / Check whether a helper-owned launcher's pidfd reports exit. */
[[nodiscard]] bool launcher_exited(const int pidfd) noexcept {
    if (pidfd < 0) {
        return true;
    }
    pollfd descriptor{.fd = pidfd, .events = POLLIN | POLLHUP, .revents = 0};
    const int ready = poll(&descriptor, 1U, 0);
    return ready > 0 && (descriptor.revents & (POLLIN | POLLHUP)) != 0 &&
           (descriptor.revents & (POLLERR | POLLNVAL)) == 0;
}

/** @brief 等待 helper-owned launcher 的 pidfd 退出 / Wait for a helper-owned launcher pidfd to exit. */
[[nodiscard]] Result<void> wait_launcher_exit(const int pidfd, const std::chrono::milliseconds deadline) {
    if (pidfd < 0) {
        return std::unexpected(make_error(ErrorCode::child_failure, "missing launcher pidfd"));
    }
    pollfd descriptor{.fd = pidfd, .events = POLLIN | POLLHUP, .revents = 0};
    const int ready = poll(&descriptor, 1U, static_cast<int>(std::min<std::int64_t>(deadline.count(), INT_MAX)));
    if (ready > 0 && (descriptor.revents & (POLLIN | POLLHUP)) != 0 &&
        (descriptor.revents & (POLLERR | POLLNVAL)) == 0) {
        return {};
    }
    if (ready > 0) {
        return std::unexpected(make_error(ErrorCode::child_failure, "launcher pidfd reported an invalid poll state"));
    }
    return std::unexpected(ready == 0 ? make_error(ErrorCode::timeout, "launcher did not exit after cgroup cleanup") : errno_error(ErrorCode::child_failure, "poll launcher pidfd"));
}

/** @brief helper 保存的 launch 表 / Launch table retained by the helper. */
using HelperLaunches = std::unordered_map<std::uint64_t, HelperLaunchRecord>;

/** @brief fork-server 终止请求标记 / Fork-server termination request marker. */
volatile sig_atomic_t g_launcher_stop_requested = 0;

/**
 * @brief 最小 helper 终止信号处理器 / Minimal helper termination signal handler.
 * @param signal 收到的信号 / Received signal.
 */
void on_launcher_stop_signal(const int signal) {
    if (signal == SIGTERM || signal == SIGINT) {
        g_launcher_stop_requested = 1;
    }
}

/** @brief 尝试回收一个 helper-owned launcher / Try to reap one helper-owned launcher. */
[[nodiscard]] bool try_reap_helper_launch(HelperLaunchRecord& record) noexcept {
    int status = 0;
    const pid_t waited = waitpid(record.launcher_pid, &status, WNOHANG);
    return waited == record.launcher_pid || (waited < 0 && errno == ECHILD);
}

/** @brief 取消未 commit launch，并取得 helper 的 terminal/reap 状态 / Cancel an uncommitted launch and reach helper terminal/reap state. */
[[nodiscard]] bool cancel_helper_launch(HelperLaunchRecord& record) noexcept {
    close_fd(record.retained_release_fd);
    // Once a broker has released PID 1, closing the pipe alone is no longer sufficient. Linux
    // treats namespace PID 1 specially: a SIGTERM with no installed handler may be ignored, and
    // wsp-systemd deliberately does not install an async teardown handler. This helper path is
    // only broker-loss/launch-cancellation recovery, not the normal graceful retirement path;
    // SIGKILL through the identity-stable pidfd therefore provides the required namespace-wide
    // termination guarantee before the helper reaps its direct launcher child. A raw host PID is
    // deliberately never used here: PID reuse would let a privileged helper signal an unrelated
    // process.
    const bool pid1_terminal = detail::signal_and_close_pidfd(record.pid1_pidfd, SIGKILL);
    static_cast<void>(kill(record.launcher_pid, SIGTERM));
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (std::chrono::steady_clock::now() < deadline) {
        if (try_reap_helper_launch(record)) {
            return pid1_terminal;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    static_cast<void>(kill(record.launcher_pid, SIGKILL));
    int status = 0;
    while (waitpid(record.launcher_pid, &status, 0) < 0 && errno == EINTR) {
    }
    return pid1_terminal;
}

/** @brief 取消并回收 helper 跟踪的全部 launch / Cancel and reap every launch tracked by the helper. */
void cancel_all_helper_launches(HelperLaunches& launches) noexcept {
    for (auto& [request_id, record] : launches) {
        static_cast<void>(request_id);
        static_cast<void>(cancel_helper_launch(record));
    }
    launches.clear();
}

/** @brief 回收已退出 launcher，并让未 commit launch 自动过期 / Reap exited launchers and expire uncommitted launches. */
void reap_helper_launches(HelperLaunches& launches) noexcept {
    const auto now = std::chrono::steady_clock::now();
    for (auto iterator = launches.begin(); iterator != launches.end();) {
        HelperLaunchRecord& record = iterator->second;
        if (!record.released && now >= record.expiry) {
            static_cast<void>(cancel_helper_launch(record));
            iterator = launches.erase(iterator);
            continue;
        }
        if (try_reap_helper_launch(record)) {
            close_fd(record.retained_release_fd);
            close_fd(record.pid1_pidfd);
            iterator = launches.erase(iterator);
            continue;
        }
        ++iterator;
    }
}

/** @brief 在单线程 helper 中处理一个 launch request / Handle one launch request in the single-threaded helper. */
void handle_launcher_request(
    const int server_fd,
    const BrokerConfig& config,
    HelperLaunches& launches,
    LauncherPacket& packet,
    const std::uint64_t request_id,
    const TaskLayer& layer) noexcept {
    const auto send_error = [&](const std::string_view message) {
        const std::vector<std::byte> error = encode_launcher_error(request_id, message);
        static_cast<void>(send_launcher_packet(server_fd, error, {}));
    };
    if (!has_valid_launch_request_fds(packet) || launches.contains(request_id)) {
        send_error("fork-server launch requires validated control/supervisor/task cgroup FDs");
        return;
    }
    TaskCgroupControl cgroup{
        .supervisor_procs_fd = packet.fds[1],
        .procs_fd = packet.fds[2],
        .kill_fd = packet.fds[3],
        .events_fd = packet.fds[4],
    };
    packet.fds[1] = -1;
    packet.fds[2] = -1;
    packet.fds[3] = -1;
    packet.fds[4] = -1;
    const int control_fd = packet.fds[0];
    packet.fds[0] = -1;
    std::array<int, 2> pid_report{-1, -1};
    std::array<int, 2> release{-1, -1};
    if (pipe2(pid_report.data(), O_CLOEXEC) != 0 || pipe2(release.data(), O_CLOEXEC) != 0) {
        send_error("fork-server cannot create launch pipes");
        close_fd(control_fd);
        close_fd(cgroup.supervisor_procs_fd);
        close_fd(cgroup.procs_fd);
        close_fd(cgroup.kill_fd);
        close_fd(cgroup.events_fd);
        close_fd(pid_report[0]);
        close_fd(pid_report[1]);
        close_fd(release[0]);
        close_fd(release[1]);
        return;
    }
    const pid_t launcher = fork();
    if (launcher < 0) {
        send_error("fork-server cannot fork launcher");
        close_fd(control_fd);
        close_fd(cgroup.supervisor_procs_fd);
        close_fd(cgroup.procs_fd);
        close_fd(cgroup.kill_fd);
        close_fd(cgroup.events_fd);
        close_fd(pid_report[0]);
        close_fd(pid_report[1]);
        close_fd(release[0]);
        close_fd(release[1]);
        return;
    }
    if (launcher == 0) {
        close_fd(server_fd);
        close_fd(pid_report[0]);
        close_fd(release[1]);
        launch_namespace_pid1(config, layer, cgroup, control_fd, pid_report[1], release[0]);
    }
    close_fd(control_fd);
    close_fd(cgroup.supervisor_procs_fd);
    close_fd(cgroup.procs_fd);
    close_fd(cgroup.kill_fd);
    close_fd(cgroup.events_fd);
    close_fd(pid_report[1]);
    close_fd(release[0]);
    const auto pid1 = read_pid_with_deadline(pid_report[0], std::chrono::seconds(5));
    close_fd(pid_report[0]);
    if (!pid1) {
        static_cast<void>(kill(launcher, SIGKILL));
        static_cast<void>(waitpid(launcher, nullptr, 0));
        close_fd(release[1]);
        send_error("fork-server did not receive namespace PID 1");
        return;
    }
/** @brief launcher 的 identity-stable pidfd / Identity-stable launcher pidfd. */
#ifdef SYS_pidfd_open
    int launcher_pidfd = static_cast<int>(syscall(SYS_pidfd_open, launcher, 0U));
    /** @brief namespace PID 1 的 identity-stable pidfd / Identity-stable namespace PID 1 pidfd. */
    int pid1_pidfd = static_cast<int>(syscall(SYS_pidfd_open, *pid1, 0U));
#else
    int launcher_pidfd = -1;
    int pid1_pidfd = -1;
#endif
    if (launcher_pidfd < 0 || pid1_pidfd < 0) {
        static_cast<void>(kill(launcher, SIGKILL));
        static_cast<void>(waitpid(launcher, nullptr, 0));
        close_fd(release[1]);
        close_fd(launcher_pidfd);
        close_fd(pid1_pidfd);
        send_error("fork-server cannot open identity-stable launcher/PID1 pidfds");
        return;
    }
    const std::vector<std::byte> reply = encode_launcher_reply(request_id, launcher, *pid1);
    const std::array<int, 2> response_fds{release[1], launcher_pidfd};
    const auto sent = send_launcher_packet(server_fd, reply, response_fds);
    close_fd(launcher_pidfd);
    // Keep a helper-owned duplicate of release until broker commit. A lost SCM_RIGHTS response
    // therefore cannot leave a namespace PID 1 permanently blocked outside its cgroup.
    launches.emplace(
        request_id,
        HelperLaunchRecord{
            .launcher_pid = launcher,
            .pid1_pidfd = std::exchange(pid1_pidfd, -1),
            .retained_release_fd = release[1],
            .committed = false,
            .released = false,
            .expiry = std::chrono::steady_clock::now() + std::chrono::seconds(5),
        });
    if (!sent) {
        // The record's bounded expiry performs the same cancellation path after the peer-loss
        // window, while preserving helper ownership/reaping semantics.
    }
}

/** @brief 运行单线程 fork-server / Run the single-threaded fork server. */
[[noreturn]] void run_launcher_server(const int server_fd, const BrokerConfig& config, const pid_t expected_parent) {
    struct sigaction stop_action {};
    stop_action.sa_handler = on_launcher_stop_signal;
    sigemptyset(&stop_action.sa_mask);
    stop_action.sa_flags = 0;
    if (sigaction(SIGTERM, &stop_action, nullptr) != 0 || sigaction(SIGINT, &stop_action, nullptr) != 0) {
        _exit(125);
    }
    if (prctl(PR_SET_PDEATHSIG, SIGTERM) != 0 || getppid() != expected_parent) {
        _exit(125);
    }
    ucred credentials{};
    socklen_t credential_size = sizeof(credentials);
    // SOCK_SEQPACKET socketpair credentials are creation-time metadata on Linux; they prove the
    // privileged UID only. Endpoint exclusivity comes from the private FD topology and the
    // immediate close of the opposite end in broker/helper/launcher children.
    if (getsockopt(server_fd, SOL_SOCKET, SO_PEERCRED, &credentials, &credential_size) != 0 ||
        credential_size != sizeof(credentials) || credentials.uid != 0U) {
        _exit(125);
    }
    if (const auto configured = configure_control_socket(server_fd, std::chrono::seconds(5)); !configured) {
        _exit(125);
    }
    HelperLaunches launches;
    for (;;) {
        if (g_launcher_stop_requested != 0) {
            cancel_all_helper_launches(launches);
            _exit(0);
        }
        reap_helper_launches(launches);
        pollfd descriptor{.fd = server_fd, .events = POLLIN | POLLHUP, .revents = 0};
        const int ready = poll(&descriptor, 1U, 250);
        if (ready < 0 && errno == EINTR) {
            continue;
        }
        if (ready <= 0 || (descriptor.revents & (POLLHUP | POLLERR | POLLNVAL)) != 0) {
            if (ready == 0) {
                continue;
            }
            cancel_all_helper_launches(launches);
            _exit(0);
        }
        auto packet = receive_launcher_packet(server_fd);
        if (!packet) {
            cancel_all_helper_launches(launches);
            _exit(0);
        }
        const auto header = parse_launcher_header(packet->bytes);
        if (!header) {
            close_launcher_packet_fds(*packet);
            cancel_all_helper_launches(launches);
            _exit(125);
        }
        if (header->kind == LauncherMessageKind::shutdown) {
            close_launcher_packet_fds(*packet);
            cancel_all_helper_launches(launches);
            _exit(0);
        }
        if (header->kind == LauncherMessageKind::launch) {
            const auto request = decode_launcher_request(packet->bytes);
            if (!request || request->first != header->request_id) {
                const std::vector<std::byte> error = encode_launcher_error(header->request_id, "malformed fork-server launch request");
                static_cast<void>(send_launcher_packet(server_fd, error, {}));
                close_launcher_packet_fds(*packet);
                continue;
            }
            handle_launcher_request(server_fd, config, launches, *packet, request->first, request->second);
            close_launcher_packet_fds(*packet);
            continue;
        }
        if ((header->kind == LauncherMessageKind::commit || header->kind == LauncherMessageKind::cancel ||
             header->kind == LauncherMessageKind::released) &&
            packet->fd_count == 0U && header->payload_offset == packet->bytes.size()) {
            const auto current = launches.find(header->request_id);
            if (current == launches.end()) {
                const std::vector<std::byte> error = encode_launcher_error(header->request_id, "unknown fork-server launch ID");
                static_cast<void>(send_launcher_packet(server_fd, error, {}));
                close_launcher_packet_fds(*packet);
                continue;
            }
            if (header->kind == LauncherMessageKind::commit) {
                current->second.committed = true;
                const std::vector<std::byte> terminal = encode_launcher_terminal(header->request_id, LauncherTerminalState::committed);
                static_cast<void>(send_launcher_packet(server_fd, terminal, {}));
            } else if (header->kind == LauncherMessageKind::released) {
                if (!current->second.committed) {
                    const std::vector<std::byte> error = encode_launcher_error(header->request_id, "release before commit");
                    static_cast<void>(send_launcher_packet(server_fd, error, {}));
                    close_launcher_packet_fds(*packet);
                    continue;
                }
                close_fd(current->second.retained_release_fd);
                current->second.released = true;
                const std::vector<std::byte> terminal = encode_launcher_terminal(header->request_id, LauncherTerminalState::released);
                static_cast<void>(send_launcher_packet(server_fd, terminal, {}));
            } else {
                const bool cancelled = cancel_helper_launch(current->second);
                launches.erase(current);
                if (!cancelled) {
                    const std::vector<std::byte> error = encode_launcher_error(
                        header->request_id,
                        "fork-server could not prove namespace PID 1 termination through pidfd");
                    static_cast<void>(send_launcher_packet(server_fd, error, {}));
                } else {
                    const std::vector<std::byte> terminal = encode_launcher_terminal(header->request_id, LauncherTerminalState::cancelled);
                    static_cast<void>(send_launcher_packet(server_fd, terminal, {}));
                }
            }
            close_launcher_packet_fds(*packet);
            continue;
        }
        const std::vector<std::byte> error = encode_launcher_error(header->request_id, "unexpected fork-server request kind or FD set");
        static_cast<void>(send_launcher_packet(server_fd, error, {}));
        close_launcher_packet_fds(*packet);
    }
}

}  // namespace

/** @brief 运行中的 supervisor session / Running supervisor session. */
struct Broker::RuntimeSession final {
    /**
     * @brief 用已验证的 domain runtime 构造 session / Construct a session from a validated domain runtime.
     * @param runtime_id 长期 runtime ID / Long-lived runtime ID.
     */
    explicit RuntimeSession(domain::RuntimeId runtime_id, domain::ActivationId activation_value)
        : runtime(std::move(runtime_id)), activation(std::move(activation_value)) {}
    /** @brief host namespace 的 launcher PID / Launcher PID in host namespace. */
    pid_t launcher_pid{-1};
    /** @brief host namespace 中可见的 PID 1 PID / PID 1 as visible in host namespace. */
    pid_t pid1_pid{-1};
    /** @brief fork-server 返回的 launcher pidfd / Launcher pidfd returned by the fork server. */
    int launcher_pidfd{-1};
    /** @brief broker 到 PID 1 的 control socket / Broker-to-PID1 control socket. */
    int control_fd{-1};
    /** @brief 串行化该 runtime control socket 与最后使用时间 / Serialize this runtime control socket and last-use time. */
    std::mutex mutex;
    /** @brief 已从 map 借用且尚未取得 session mutex 的 dispatch 数 / Dispatches borrowing this session from the map. */
    std::atomic<unsigned int> dispatch_references{0U};
    /** @brief 强制 lifecycle 不变量的 domain aggregate / Domain aggregate enforcing lifecycle invariants. */
    domain::Runtime runtime;
    /** @brief 清理失败后不可复用的 session / Session that cannot be reused after cleanup failure. */
    bool poisoned{false};
    /** @brief session 所绑定的强类型 activation / Strongly typed activation bound to this session. */
    domain::ActivationId activation;
    /** @brief 仅该 activation 可删除的 transient staging 层 / Transient staging layer deletable only for this activation. */
    TaskLayer layer;
    /** @brief 覆盖存活 PID 1 与 cgroup 的 runtime activation 排他租约 / Runtime activation lease covering the live PID 1 and cgroup. */
    std::optional<RuntimeActivationLease> activation_lease;
    /** @brief 最近使用时刻 / Last use time. */
    std::chrono::steady_clock::time_point last_used;

    /** @brief 析构时关闭 control socket 与 pidfd / Close control socket and pidfd at destruction. */
    ~RuntimeSession() {
        close_fd(control_fd);
        close_fd(launcher_pidfd);
    }
};

/** @brief 将 session mutex 与 reaper 借用绑定为一个 RAII 租约 / RAII lease binding a session mutex to a reaper reference. */
struct Broker::SessionLease final {
    /**
     * @brief 构造独占 session 租约 / Construct an exclusive session lease.
     * @param session_value 已增加 dispatch reference 的 session / Session whose dispatch reference was incremented.
     * @param lock_value 已持有的 session mutex / Already-held session mutex.
     */
    SessionLease(std::shared_ptr<RuntimeSession> session_value, std::unique_lock<std::mutex> lock_value)
        : session(std::move(session_value)), lock(std::move(lock_value)) {}

    /** @brief 租约结束时释放 reaper 借用；mutex 随成员析构解锁 / Release reaper reference; member destruction unlocks mutex. */
    ~SessionLease() {
        if (session) {
            static_cast<void>(session->dispatch_references.fetch_sub(1U, std::memory_order_release));
        }
    }

    /** @brief 禁止复制，确保借用只释放一次 / Copying is forbidden so the reference is released once. */
    SessionLease(const SessionLease&) = delete;
    /** @brief 禁止复制赋值 / Copy assignment is forbidden. */
    SessionLease& operator=(const SessionLease&) = delete;

    /** @brief 被独占持有的 session / Exclusively held session. */
    std::shared_ptr<RuntimeSession> session;
    /** @brief 被独占持有的 session mutex / Exclusively held session mutex. */
    std::unique_lock<std::mutex> lock;
};

/** @brief worker 之间共享的 session map / Session map shared between broker workers. */
struct Broker::SharedState final {
    /** @brief 保护 session map / Protect the session map. */
    std::mutex sessions_mutex;
    /** @brief runtime 到可共享 session 的映射 / Mapping from runtime to shareable session. */
    std::unordered_map<std::string, std::shared_ptr<RuntimeSession>> sessions;
    /** @brief 正在进行慢 activation 的 runtime 及其强类型 owner / Runtimes undergoing slow activation and their typed owners. */
    std::unordered_map<std::string, domain::ActivationId> activating;
    /** @brief fork-server unknown outcome 后禁止复用的 runtime / Runtimes blocked after an unknown fork-server outcome. */
    std::unordered_set<std::string> launch_unknown;
    /** @brief 串行化对单个 fork-server socket 的 RPC / Serialize RPCs over the single fork-server socket. */
    std::mutex launcher_mutex;
    /** @brief broker 一端 fork-server SOCK_SEQPACKET / Broker end of the fork-server SOCK_SEQPACKET. */
    int launcher_fd{-1};
    /** @brief fork-server host PID（仅诊断；helper 自己回收其 child） / Fork-server host PID (diagnostic only; helper reaps its children). */
    pid_t launcher_server_pid{-1};
    /** @brief 单调 fork-server request ID / Monotonic fork-server request ID. */
    std::uint64_t next_launcher_request_id{1U};
};

/** @brief fork-server 的启动回复 / Fork-server launch reply. */
struct Broker::LauncherReply final {
    /** @brief 构造空 FD owner / Construct an empty FD owner. */
    LauncherReply() = default;
    /** @brief broker/helper 共享的 launch ID / Launch ID shared by broker and helper. */
    std::uint64_t launch_id{};
    /** @brief helper 直接 child launcher 的 host PID / Host PID of the helper's direct launcher child. */
    pid_t launcher_pid{-1};
    /** @brief host namespace 中的 namespace PID 1 / Namespace PID 1 visible in the host namespace. */
    pid_t pid1_pid{-1};
    /** @brief 可轮询 launcher 退出的 pidfd / pidfd that can be polled for launcher exit. */
    int launcher_pidfd{-1};
    /** @brief broker 释放 PID 1 的 pipe 写端 / Broker write end of the PID 1 release pipe. */
    int release_fd{-1};

    /** @brief 析构时关闭尚未移交的 FD / Close FDs not yet handed off. */
    ~LauncherReply() {
        close_fd(launcher_pidfd);
        close_fd(release_fd);
    }
    /** @brief 禁止复制 FD owner / Copying FD ownership is forbidden. */
    LauncherReply(const LauncherReply&) = delete;
    /** @brief 禁止复制赋值 / Copy assignment is forbidden. */
    LauncherReply& operator=(const LauncherReply&) = delete;
    /** @brief 支持移交 FD owner / Moving FD ownership is supported. */
    LauncherReply(LauncherReply&& other) noexcept
        : launch_id(other.launch_id), launcher_pid(other.launcher_pid), pid1_pid(other.pid1_pid),
          launcher_pidfd(std::exchange(other.launcher_pidfd, -1)), release_fd(std::exchange(other.release_fd, -1)) {}
    /** @brief 支持移动赋值 / Move assignment is supported. */
    LauncherReply& operator=(LauncherReply&& other) noexcept {
        if (this != &other) {
            close_fd(launcher_pidfd);
            close_fd(release_fd);
            launch_id = other.launch_id;
            launcher_pid = other.launcher_pid;
            pid1_pid = other.pid1_pid;
            launcher_pidfd = std::exchange(other.launcher_pidfd, -1);
            release_fd = std::exchange(other.release_fd, -1);
        }
        return *this;
    }
};

Broker::Broker(BrokerConfig config)
    : config_(std::move(config)),
      quota_(config_.sandbox.state_root, config_.sandbox.xfs_project_quota),
      journal_(config_.sandbox.state_root),
      state_(std::make_unique<SharedState>()) {}

Result<void> Broker::start_launcher_server() {
    if (!state_) {
        return std::unexpected(make_error(ErrorCode::internal, "fork-server state is unavailable"));
    }
    std::array<int, 2> channel{-1, -1};
    if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, channel.data()) != 0) {
        return std::unexpected(errno_error(ErrorCode::child_failure, "create fork-server channel"));
    }
    if (const auto parent_socket = configure_control_socket(channel[0], std::chrono::seconds(5)); !parent_socket ||
        !configure_control_socket(channel[1], std::chrono::seconds(5))) {
        const Error error = parent_socket ? make_error(ErrorCode::io_failure, "configure fork-server socket") : parent_socket.error();
        close_fd(channel[0]);
        close_fd(channel[1]);
        return std::unexpected(error);
    }
    const pid_t parent = getpid();
    // This is intentionally the only broker-process fork before serve_forever creates workers.
    const pid_t helper = fork();
    if (helper < 0) {
        const Error error = errno_error(ErrorCode::child_failure, "fork single-threaded fork-server");
        close_fd(channel[0]);
        close_fd(channel[1]);
        return std::unexpected(error);
    }
    if (helper == 0) {
        close_fd(channel[0]);
        run_launcher_server(channel[1], config_, parent);
    }
    close_fd(channel[1]);
    state_->launcher_fd = channel[0];
    state_->launcher_server_pid = helper;
    return {};
}

Result<Broker::LauncherReply> Broker::launch_runtime(
    const TaskLayer& layer,
    const TaskCgroupControl& cgroup,
    const int control_fd) {
    if (!state_ || state_->launcher_fd < 0 || control_fd < 0 || cgroup.supervisor_procs_fd < 0 ||
        cgroup.procs_fd < 0 || cgroup.kill_fd < 0 || cgroup.events_fd < 0) {
        return std::unexpected(make_error(ErrorCode::internal, "fork-server launch controls are unavailable"));
    }
    std::lock_guard launch_lock(state_->launcher_mutex);
    const std::uint64_t request_id = state_->next_launcher_request_id++;
    const auto request = encode_launcher_request(request_id, layer);
    if (!request) {
        return std::unexpected(request.error());
    }
    const std::array<int, detail::launcher_transport::kMaxFileDescriptors> request_fds{
        control_fd,
        cgroup.supervisor_procs_fd,
        cgroup.procs_fd,
        cgroup.kill_fd,
        cgroup.events_fd,
    };
    if (const auto sent = send_launcher_packet(state_->launcher_fd, *request, request_fds); !sent) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server launch send outcome is unknown"));
    }
    auto response = receive_launcher_packet(state_->launcher_fd);
    if (!response) {
        // A request may have reached helper before a reply timeout. Because the RPC is serialized,
        // the next packet can only be this launch's late reply; drain it, cancel it, and require
        // helper's terminal/reap ACK before allowing any later helper RPC.
        auto late_response = receive_launcher_packet(state_->launcher_fd);
        if (late_response) {
            const auto late_header = parse_launcher_header(late_response->bytes);
            if (late_header && late_header->request_id == request_id && late_header->kind == LauncherMessageKind::launched &&
                has_valid_launch_reply_fds(*late_response)) {
                close_launcher_packet_fds(*late_response);
                std::vector<std::byte> cancel;
                append_launcher_header(cancel, LauncherMessageKind::cancel, request_id);
                const auto cancelled = send_launcher_packet(state_->launcher_fd, cancel, {});
                auto terminal = cancelled ? receive_launcher_packet(state_->launcher_fd) : Result<LauncherPacket>{std::unexpected(cancelled.error())};
                if (terminal) {
                    const auto terminal_state = decode_launcher_terminal(terminal->bytes, request_id);
                    close_launcher_packet_fds(*terminal);
                    if (terminal_state && *terminal_state == LauncherTerminalState::cancelled) {
                        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server launch was cancelled after reply timeout"));
                    }
                }
            } else {
                close_launcher_packet_fds(*late_response);
            }
        }
        // No terminal/reap acknowledgement means the private protocol is desynchronized. Close
        // and kill the helper rather than ever treating its socket as reusable; its retained
        // release FD closes, so an uncommitted PID 1 cannot remain blocked outside cgroup.
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server launch outcome is unknown"));
    }
    const auto header = parse_launcher_header(response->bytes);
    if (!header || header->request_id != request_id) {
        close_launcher_packet_fds(*response);
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server response identity is unknown"));
    }
    if (header->kind == LauncherMessageKind::error) {
        std::size_t offset = header->payload_offset;
        const auto message = read_launcher_string(response->bytes, offset);
        const bool valid_error = response->fd_count == 0U && message && offset == response->bytes.size();
        close_launcher_packet_fds(*response);
        if (!valid_error) {
            poison_launcher_server_locked();
            return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server returned malformed launch rejection"));
        }
        return std::unexpected(make_error(ErrorCode::child_failure, "fork-server rejected launch: " + *message));
    }
    if (header->kind != LauncherMessageKind::launched || !has_valid_launch_reply_fds(*response)) {
        close_launcher_packet_fds(*response);
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server response FD contract is unknown"));
    }
    const auto identities = decode_launcher_reply(response->bytes, request_id);
    if (!identities) {
        close_launcher_packet_fds(*response);
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server launch reply payload is unknown"));
    }
    LauncherReply reply;
    reply.launch_id = request_id;
    reply.launcher_pid = identities->first;
    reply.pid1_pid = identities->second;
    reply.release_fd = response->fds[0];
    reply.launcher_pidfd = response->fds[1];
    response->fds[0] = -1;
    response->fds[1] = -1;
    close_launcher_packet_fds(*response);
    return reply;
}

Result<void> Broker::commit_launch(const std::uint64_t launch_id) {
    if (!state_ || state_->launcher_fd < 0) {
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server commit controls are unavailable"));
    }
    if (launch_id == 0U) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "fork-server commit launch ID is invalid"));
    }
    std::lock_guard launch_lock(state_->launcher_mutex);
    std::vector<std::byte> request;
    append_launcher_header(request, LauncherMessageKind::commit, launch_id);
    if (const auto sent = send_launcher_packet(state_->launcher_fd, request, {}); !sent) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server commit send outcome is unknown"));
    }
    auto response = receive_launcher_packet(state_->launcher_fd);
    if (!response) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server commit acknowledgement is unknown"));
    }
    const auto terminal = decode_launcher_terminal(response->bytes, launch_id);
    const bool fd_contract = response->fd_count == 0U;
    close_launcher_packet_fds(*response);
    if (!terminal || !fd_contract || *terminal != LauncherTerminalState::committed) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server commit terminal state is unknown"));
    }
    return {};
}

Result<void> Broker::cancel_launch(const std::uint64_t launch_id) {
    if (!state_ || state_->launcher_fd < 0) {
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server cancel controls are unavailable"));
    }
    if (launch_id == 0U) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "fork-server cancel launch ID is invalid"));
    }
    std::lock_guard launch_lock(state_->launcher_mutex);
    std::vector<std::byte> request;
    append_launcher_header(request, LauncherMessageKind::cancel, launch_id);
    if (const auto sent = send_launcher_packet(state_->launcher_fd, request, {}); !sent) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server cancel send outcome is unknown"));
    }
    auto response = receive_launcher_packet(state_->launcher_fd);
    if (!response) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server cancel acknowledgement is unknown"));
    }
    const auto terminal = decode_launcher_terminal(response->bytes, launch_id);
    const bool fd_contract = response->fd_count == 0U;
    close_launcher_packet_fds(*response);
    if (!terminal || !fd_contract || *terminal != LauncherTerminalState::cancelled) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server cancel terminal state is unknown"));
    }
    return {};
}

Result<void> Broker::release_launch(const std::uint64_t launch_id) {
    if (!state_ || state_->launcher_fd < 0) {
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server release controls are unavailable"));
    }
    if (launch_id == 0U) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "fork-server release launch ID is invalid"));
    }
    std::lock_guard launch_lock(state_->launcher_mutex);
    std::vector<std::byte> request;
    append_launcher_header(request, LauncherMessageKind::released, launch_id);
    if (const auto sent = send_launcher_packet(state_->launcher_fd, request, {}); !sent) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server release send outcome is unknown"));
    }
    auto response = receive_launcher_packet(state_->launcher_fd);
    if (!response) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server release acknowledgement is unknown"));
    }
    const auto terminal = decode_launcher_terminal(response->bytes, launch_id);
    const bool fd_contract = response->fd_count == 0U;
    close_launcher_packet_fds(*response);
    if (!terminal || !fd_contract || *terminal != LauncherTerminalState::released) {
        poison_launcher_server_locked();
        return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "fork-server release terminal state is unknown"));
    }
    return {};
}

Result<void> Broker::retire_session(
    const std::string& runtime_key,
    const std::shared_ptr<RuntimeSession>& session) {
    if (!session) {
        return std::unexpected(make_error(ErrorCode::internal, "cannot retire an empty runtime session"));
    }
    const auto cleanup = [this, &runtime_key, &session]() -> Result<void> {
        if (const auto killed = kill_runtime_cgroup(config_.sandbox, runtime_key); !killed) {
            session->poisoned = true;
            return std::unexpected(killed.error());
        }
        if (const auto exited = wait_launcher_exit(session->launcher_pidfd, std::chrono::seconds(5)); !exited) {
            session->poisoned = true;
            return std::unexpected(exited.error());
        }
        if (!session->activation_lease.has_value()) {
            session->poisoned = true;
            return std::unexpected(make_error(ErrorCode::internal, "running runtime session lost its activation lease"));
        }
        if (const auto cleaned = cleanup_task_layer(config_.sandbox, quota_, *session->activation_lease, session->layer); !cleaned) {
            session->poisoned = true;
            return std::unexpected(cleaned.error());
        }
        return {};
    };
    BrokerRuntimeActivationPort port(
        session->runtime.id(),
        session->activation,
        {},
        cleanup);
    application::RuntimeActivationService lifecycle;
    const domain::Result<void> retired = session->runtime.state() == domain::RuntimeState::ready
        ? lifecycle.retire(session->runtime, session->activation, port)
        : lifecycle.abort(session->runtime, session->activation, port);
    if (!retired) {
        session->poisoned = true;
        if (port.native_error().has_value()) {
            return std::unexpected(*port.native_error());
        }
        return std::unexpected(transport_error(retired.error()));
    }
    // Slow operations are complete before taking sessions_mutex. Conditional erasure protects a
    // newly activated session for the same runtime from an old reaper/worker observation.
    std::lock_guard map_lock(state_->sessions_mutex);
    const auto current = state_->sessions.find(runtime_key);
    if (current != state_->sessions.end() && current->second == session) {
        state_->sessions.erase(current);
    }
    return {};
}

void Broker::poison_launcher_server_locked() noexcept {
    if (!state_) {
        return;
    }
    const int server_fd = std::exchange(state_->launcher_fd, -1);
    const pid_t helper = std::exchange(state_->launcher_server_pid, -1);
    close_fd(server_fd);
    if (helper > 0) {
        static_cast<void>(kill(helper, SIGKILL));
        while (waitpid(helper, nullptr, 0) < 0 && errno == EINTR) {
        }
    }
}

void Broker::stop_launcher_server() noexcept {
    if (!state_) {
        return;
    }
    std::lock_guard launch_lock(state_->launcher_mutex);
    const int server_fd = std::exchange(state_->launcher_fd, -1);
    const pid_t helper = std::exchange(state_->launcher_server_pid, -1);
    if (server_fd >= 0) {
        const std::vector<std::byte> shutdown = [&]() {
            std::vector<std::byte> bytes;
            append_launcher_header(bytes, LauncherMessageKind::shutdown, state_->next_launcher_request_id++);
            return bytes;
        }();
        static_cast<void>(send_launcher_packet(server_fd, shutdown, {}));
        close_fd(server_fd);
    }
    if (helper <= 0) {
        return;
    }
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    int status = 0;
    while (std::chrono::steady_clock::now() < deadline) {
        const pid_t waited = waitpid(helper, &status, WNOHANG);
        if (waited == helper || (waited < 0 && errno == ECHILD)) {
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    static_cast<void>(kill(helper, SIGKILL));
    while (waitpid(helper, &status, 0) < 0 && errno == EINTR) {
    }
}

Broker::Broker(Broker&& other) noexcept
    : config_(std::move(other.config_)),
      quota_(std::move(other.quota_)),
      journal_(std::move(other.journal_)),
      listen_fd_(std::exchange(other.listen_fd_, -1)),
      socket_device_(other.socket_device_),
      socket_inode_(other.socket_inode_),
      owns_socket_path_(std::exchange(other.owns_socket_path_, false)),
      operator_listen_fd_(std::exchange(other.operator_listen_fd_, -1)),
      operator_socket_device_(other.operator_socket_device_),
      operator_socket_inode_(other.operator_socket_inode_),
      owns_operator_socket_path_(std::exchange(other.owns_operator_socket_path_, false)),
      state_(std::move(other.state_)) {}

Broker& Broker::operator=(Broker&& other) noexcept {
    if (this != &other) {
        this->~Broker();
        new (this) Broker(std::move(other));
    }
    return *this;
}

Broker::~Broker() {
    if (state_) {
        std::unordered_map<std::string, std::shared_ptr<RuntimeSession>> sessions;
        {
            std::lock_guard lock(state_->sessions_mutex);
            sessions.swap(state_->sessions);
        }
        for (auto& [runtime_key, session] : sessions) {
            if (!session) {
                continue;
            }
            std::lock_guard session_lock(session->mutex);
            if (session->control_fd >= 0) {
                const auto shutdown = encode_frame(MessageKind::shutdown, {});
                if (shutdown) {
                    static_cast<void>(send_frame(session->control_fd, *shutdown));
                }
            }
            static_cast<void>(kill_runtime_cgroup(config_.sandbox, runtime_key));
        }
    }
    stop_launcher_server();
    close_fd(listen_fd_);
    close_fd(operator_listen_fd_);
    if (owns_socket_path_ && !config_.socket_path.empty()) {
        struct stat metadata {};
        if (lstat(config_.socket_path.c_str(), &metadata) == 0 && S_ISSOCK(metadata.st_mode) &&
            metadata.st_dev == socket_device_ && metadata.st_ino == socket_inode_) {
            static_cast<void>(unlink(config_.socket_path.c_str()));
        }
    }
    if (owns_operator_socket_path_ && !config_.operator_socket_path.empty()) {
        struct stat metadata {};
        if (lstat(config_.operator_socket_path.c_str(), &metadata) == 0 && S_ISSOCK(metadata.st_mode) &&
            metadata.st_dev == operator_socket_device_ && metadata.st_ino == operator_socket_inode_) {
            static_cast<void>(unlink(config_.operator_socket_path.c_str()));
        }
    }
}

Result<Broker> Broker::create(BrokerConfig config) {
    if (config.client_uid == 0U || config.operator_socket_path.empty() || config.idle_ttl.count() <= 0) {
        return std::unexpected(make_error(
            ErrorCode::invalid_argument,
            "broker Bot UID, independent operator endpoint, and idle TTL must be set"));
    }
    // Quota 持有 host-side upper root，而 SandboxConfig 持有 payload identity；从同一个
    // trusted value 派生两者，避免持久 upper 被重新绑定到 caller-selected UID/GID。/
    // Quota owns the host-side upper root while SandboxConfig owns payload identity. Derive
    // both views from one trusted value so a persisted upper can never be rebound to a
    // caller-selected UID/GID.
    config.sandbox.xfs_project_quota.workspace_uid = config.sandbox.sandbox_uid;
    config.sandbox.xfs_project_quota.workspace_gid = config.sandbox.sandbox_gid;
    if (const auto separated = validate_operator_endpoint_separation(
            config.socket_path,
            config.client_uid,
            config.operator_socket_path,
            config.operator_uid);
        !separated) {
        return std::unexpected(separated.error());
    }
    if (const auto preflight = preflight_sandbox(config.sandbox); !preflight) {
        return std::unexpected(preflight.error());
    }
    const auto base_root = image_root(config.sandbox);
    if (!base_root) {
        return std::unexpected(base_root.error());
    }
    for (const std::filesystem::path* root : std::array<const std::filesystem::path*, 3>{
             &config.sandbox.state_root,
             &config.sandbox.images_root,
             &*base_root}) {
        if (const auto secure = validate_secure_directory_ancestry(*root, config.allow_insecure_dev_root); !secure) {
            return std::unexpected(secure.error());
        }
    }
    const auto socket_parent = validate_socket_parent(config.socket_path, config.allow_insecure_dev_root);
    if (!socket_parent) {
        return std::unexpected(socket_parent.error());
    }
    const auto operator_socket_parent = validate_socket_parent(
        config.operator_socket_path,
        config.allow_insecure_dev_root);
    if (!operator_socket_parent) {
        return std::unexpected(operator_socket_parent.error());
    }
    if (canonical_directory_contains(*socket_parent, *operator_socket_parent) ||
        canonical_directory_contains(*operator_socket_parent, *socket_parent)) {
        return std::unexpected(make_error(
            ErrorCode::invalid_argument,
            "Bot and operator socket parent directories resolve to overlapping host views"));
    }
    if (config.allow_insecure_dev_root) {
        std::fputs(
            "wspctld: WARNING --allow-insecure-dev-root trusts non-root checkout ancestors; production must not use it\n",
            stderr);
    }
    if (const auto manager = prepare_broker_cgroup(config.sandbox); !manager) {
        return std::unexpected(manager.error());
    }
    struct sigaction pipe_action {};
    pipe_action.sa_handler = SIG_IGN;
    sigemptyset(&pipe_action.sa_mask);
    pipe_action.sa_flags = 0;
    if (sigaction(SIGPIPE, &pipe_action, nullptr) != 0) {
        return std::unexpected(errno_error(ErrorCode::internal, "ignore SIGPIPE in broker"));
    }
    Broker broker(std::move(config));
    if (const auto launcher = broker.start_launcher_server(); !launcher) {
        return std::unexpected(launcher.error());
    }
    if (const auto bound = broker.bind_listener(); !bound) {
        return std::unexpected(bound.error());
    }
    if (const auto operator_bound = broker.bind_operator_listener(); !operator_bound) {
        return std::unexpected(operator_bound.error());
    }
    return broker;
}

Result<void> Broker::bind_listener() {
    if (config_.socket_path.string().size() >= sizeof(sockaddr_un::sun_path)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "broker socket path exceeds AF_UNIX limit"));
    }
    if (const auto stale = reclaim_stale_listener_path(config_.socket_path, "Bot"); !stale) {
        return std::unexpected(stale.error());
    }
    listen_fd_ = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (listen_fd_ < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create broker SOCK_SEQPACKET"));
    }
    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, config_.socket_path.c_str(), sizeof(address.sun_path) - 1U);
    if (bind(listen_fd_, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "bind broker socket"));
    }
    struct stat bound_metadata {};
    if (lstat(config_.socket_path.c_str(), &bound_metadata) != 0 || !S_ISSOCK(bound_metadata.st_mode)) {
        // `bind` just created this pathname below a validated private parent.  If it cannot be
        // re-observed, best-effort unlink prevents this failed half-bind from becoming a durable
        // restart blocker; no untrusted peer can replace this path in the validated parent.
        static_cast<void>(unlink(config_.socket_path.c_str()));
        return std::unexpected(make_error(ErrorCode::io_failure, "cannot prove ownership of bound broker socket"));
    }
    socket_device_ = bound_metadata.st_dev;
    socket_inode_ = bound_metadata.st_ino;
    owns_socket_path_ = true;
    if (chown(config_.socket_path.c_str(), config_.client_uid, static_cast<gid_t>(-1)) != 0 ||
        chmod(config_.socket_path.c_str(), 0600) != 0) {
        return std::unexpected(errno_error(ErrorCode::permission_denied, "protect broker socket"));
    }
    if (lstat(config_.socket_path.c_str(), &bound_metadata) != 0 || !S_ISSOCK(bound_metadata.st_mode) ||
        bound_metadata.st_dev != socket_device_ || bound_metadata.st_ino != socket_inode_ ||
        bound_metadata.st_uid != config_.client_uid || (bound_metadata.st_mode & 0777) != 0600) {
        return std::unexpected(make_error(ErrorCode::io_failure, "cannot prove protection of bound broker socket"));
    }
    if (listen(listen_fd_, 32) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "listen broker socket"));
    }
    return {};
}

Result<void> Broker::bind_operator_listener() {
    if (config_.operator_socket_path.string().size() >= sizeof(sockaddr_un::sun_path)) {
        return std::unexpected(make_error(ErrorCode::invalid_argument, "operator socket path exceeds AF_UNIX limit"));
    }
    if (const auto stale = reclaim_stale_listener_path(config_.operator_socket_path, "operator"); !stale) {
        return std::unexpected(stale.error());
    }
    operator_listen_fd_ = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (operator_listen_fd_ < 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "create operator SOCK_SEQPACKET"));
    }
    sockaddr_un address {};
    address.sun_family = AF_UNIX;
    std::strncpy(address.sun_path, config_.operator_socket_path.c_str(), sizeof(address.sun_path) - 1U);
    if (bind(operator_listen_fd_, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "bind operator socket"));
    }
    struct stat bound_metadata {};
    if (lstat(config_.operator_socket_path.c_str(), &bound_metadata) != 0 || !S_ISSOCK(bound_metadata.st_mode)) {
        // See the Bot endpoint equivalent: this is a just-created path under a verified private
        // parent, so a best-effort unlink avoids retaining an unprotected half-bind.
        static_cast<void>(unlink(config_.operator_socket_path.c_str()));
        return std::unexpected(make_error(ErrorCode::io_failure, "cannot prove ownership of bound operator socket"));
    }
    operator_socket_device_ = bound_metadata.st_dev;
    operator_socket_inode_ = bound_metadata.st_ino;
    owns_operator_socket_path_ = true;
    if (chown(config_.operator_socket_path.c_str(), config_.operator_uid, static_cast<gid_t>(-1)) != 0 ||
        chmod(config_.operator_socket_path.c_str(), 0600) != 0) {
        return std::unexpected(errno_error(ErrorCode::permission_denied, "protect operator socket"));
    }
    if (lstat(config_.operator_socket_path.c_str(), &bound_metadata) != 0 || !S_ISSOCK(bound_metadata.st_mode) ||
        bound_metadata.st_dev != operator_socket_device_ || bound_metadata.st_ino != operator_socket_inode_ ||
        bound_metadata.st_uid != config_.operator_uid || (bound_metadata.st_mode & 0777) != 0600) {
        return std::unexpected(make_error(ErrorCode::io_failure, "cannot prove protection of bound operator socket"));
    }
    if (listen(operator_listen_fd_, 16) != 0) {
        return std::unexpected(errno_error(ErrorCode::io_failure, "listen operator socket"));
    }
    return {};
}

Result<std::unique_ptr<Broker::SessionLease>> Broker::acquire_session(
    const std::string& runtime_key,
    const std::string& activation_id) {
    const auto runtime_id = domain::RuntimeId::parse(runtime_key);
    if (!runtime_id) {
        return std::unexpected(transport_error(runtime_id.error()));
    }
    const auto typed_activation_id = domain::ActivationId::parse(activation_id);
    if (!typed_activation_id) {
        return std::unexpected(transport_error(typed_activation_id.error()));
    }
    std::shared_ptr<RuntimeSession> session;
    const auto activate_session = [&]() -> Result<std::shared_ptr<RuntimeSession>> {
        auto created = std::make_shared<RuntimeSession>(*runtime_id, *typed_activation_id);
        const auto establish = [this, &runtime_key, &activation_id, &created]() -> Result<void> {
            auto activation_lease = quota_.acquire_activation_lease(runtime_key);
            if (!activation_lease) {
                return std::unexpected(activation_lease.error());
            }
            auto cgroup = prepare_runtime_cgroup(config_.sandbox, runtime_key);
            if (!cgroup) {
                return std::unexpected(cgroup.error());
            }
            if (const auto reclaimed = reclaim_dead_task_layers(config_.sandbox, quota_, *activation_lease, runtime_key); !reclaimed) {
                close_fd(cgroup->supervisor_procs_fd);
                close_fd(cgroup->procs_fd);
                close_fd(cgroup->kill_fd);
                close_fd(cgroup->events_fd);
                return std::unexpected(reclaimed.error());
            }
            const auto layer = prepare_task_layer(config_.sandbox, quota_, *activation_lease, activation_id);
            if (!layer) {
                close_fd(cgroup->supervisor_procs_fd);
                close_fd(cgroup->procs_fd);
                close_fd(cgroup->kill_fd);
                close_fd(cgroup->events_fd);
                if (const auto killed = kill_runtime_cgroup(config_.sandbox, runtime_key); !killed) {
                    return std::unexpected(make_error(
                        ErrorCode::invocation_in_doubt,
                        "runtime cgroup could not be proven empty after task-layer preparation failure"));
                }
                if (const auto reclaimed = reclaim_dead_task_layers(config_.sandbox, quota_, *activation_lease, runtime_key); !reclaimed) {
                    return std::unexpected(make_error(
                        ErrorCode::invocation_in_doubt,
                        "runtime task-layer preparation failed and transient staging recovery could not be proven"));
                }
                return std::unexpected(layer.error());
            }
            std::array<int, 2> control{-1, -1};
            const auto close_controls = [&]() noexcept {
                close_fd(control[0]);
                close_fd(control[1]);
                close_fd(cgroup->supervisor_procs_fd);
                close_fd(cgroup->procs_fd);
                close_fd(cgroup->kill_fd);
                close_fd(cgroup->events_fd);
            };
            const auto abort_prelaunch = [&](const Error& error) -> Result<void> {
                close_controls();
                const auto killed = kill_runtime_cgroup(config_.sandbox, runtime_key);
                const auto cleaned = cleanup_task_layer(config_.sandbox, quota_, *activation_lease, *layer);
                if (!killed || !cleaned) {
                    return std::unexpected(make_error(
                        ErrorCode::invocation_in_doubt,
                        "pre-launch activation cgroup/staging cleanup could not be proven"));
                }
                return std::unexpected(error);
            };
            if (socketpair(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0, control.data()) != 0) {
                const Error error = errno_error(ErrorCode::io_failure, "create runtime control channels");
                return abort_prelaunch(error);
            }
            if (const auto socket_configured = configure_control_socket(control[0], std::chrono::seconds(5)); !socket_configured ||
                !configure_control_socket(control[1], std::chrono::seconds(5))) {
                const Error error = socket_configured
                    ? make_error(ErrorCode::io_failure, "configure supervisor control socket")
                    : socket_configured.error();
                return abort_prelaunch(error);
            }
            auto launched = launch_runtime(*layer, *cgroup, control[1]);
            close_fd(control[1]);
            close_fd(cgroup->supervisor_procs_fd);
            close_fd(cgroup->procs_fd);
            close_fd(cgroup->kill_fd);
            close_fd(cgroup->events_fd);
            if (!launched) {
                close_fd(control[0]);
                if (const auto killed = kill_runtime_cgroup(config_.sandbox, runtime_key); !killed) {
                    return std::unexpected(make_error(
                        ErrorCode::invocation_in_doubt,
                        "runtime launch failed and cgroup cleanup could not be proven"));
                }
                if (launched.error().code != ErrorCode::invocation_in_doubt) {
                    if (const auto cleaned = cleanup_task_layer(config_.sandbox, quota_, *activation_lease, *layer); !cleaned) {
                        return std::unexpected(make_error(
                            ErrorCode::invocation_in_doubt,
                            "runtime launch failed and activation staging cleanup could not be proven"));
                    }
                }
                return std::unexpected(launched.error());
            }
            const auto abort_launch = [&](const Error& error) -> Result<void> {
                close_fd(launched->release_fd);
                close_fd(control[0]);
                const auto cancelled = cancel_launch(launched->launch_id);
                const auto killed = kill_runtime_cgroup(config_.sandbox, runtime_key);
                const auto exited = wait_launcher_exit(launched->launcher_pidfd, std::chrono::seconds(5));
                if (!cancelled || !killed || !exited) {
                    return std::unexpected(make_error(
                        ErrorCode::invocation_in_doubt,
                        "runtime activation cleanup could not prove launcher and cgroup termination"));
                }
                if (const auto cleaned = cleanup_task_layer(config_.sandbox, quota_, *activation_lease, *layer); !cleaned) {
                    return std::unexpected(make_error(
                        ErrorCode::invocation_in_doubt,
                        "runtime activation staging cleanup could not be proven"));
                }
                return std::unexpected(error);
            };
            // Commit first: helper then closes only its retained duplicate, while this broker-held
            // FD keeps PID 1 gated. A lost commit acknowledgement is therefore cancellable before
            // any untrusted mount/setup work can run.
            if (const auto committed = commit_launch(launched->launch_id); !committed) {
                return abort_launch(committed.error());
            }
            const char release = 'R';
            ssize_t released = -1;
            do {
                released = write(launched->release_fd, &release, 1U);
            } while (released < 0 && errno == EINTR);
            if (released != 1) {
                return abort_launch(errno_error(ErrorCode::child_failure, "release committed runtime PID 1"));
            }
            close_fd(launched->release_fd);
            // The helper retains its own release-pipe duplicate until this explicit acknowledgement.
            // This closes the launch transaction only after PID 1 can actually leave its gate.
            if (const auto release_acknowledged = release_launch(launched->launch_id); !release_acknowledged) {
                return abort_launch(release_acknowledged.error());
            }
            created->launcher_pid = launched->launcher_pid;
            created->pid1_pid = launched->pid1_pid;
            created->launcher_pidfd = std::exchange(launched->launcher_pidfd, -1);
            created->control_fd = control[0];
            created->layer = *layer;
            created->activation_lease.emplace(std::move(*activation_lease));
            created->last_used = std::chrono::steady_clock::now();
            return {};
        };
        const auto cleanup_established = [this, &runtime_key, &created]() -> Result<void> {
            if (created->control_fd >= 0) {
                const auto shutdown = encode_frame(MessageKind::shutdown, {});
                if (shutdown) {
                    static_cast<void>(configure_control_socket(created->control_fd, std::chrono::seconds(1)));
                    static_cast<void>(send_frame(created->control_fd, *shutdown));
                }
            }
            if (const auto killed = kill_runtime_cgroup(config_.sandbox, runtime_key); !killed) {
                return std::unexpected(killed.error());
            }
            if (const auto exited = wait_launcher_exit(created->launcher_pidfd, std::chrono::seconds(5)); !exited) {
                return std::unexpected(exited.error());
            }
            if (!created->activation_lease.has_value()) {
                return std::unexpected(make_error(ErrorCode::internal, "established runtime session lost its activation lease"));
            }
            if (const auto cleaned = cleanup_task_layer(config_.sandbox, quota_, *created->activation_lease, created->layer); !cleaned) {
                return std::unexpected(cleaned.error());
            }
            return {};
        };
        BrokerRuntimeActivationPort port(
            created->runtime.id(),
            created->activation,
            establish,
            cleanup_established);
        application::RuntimeActivationService lifecycle;
        if (const auto activated = lifecycle.activate(created->runtime, created->activation, port); !activated) {
            if (port.native_error().has_value()) {
                return std::unexpected(*port.native_error());
            }
            return std::unexpected(transport_error(activated.error()));
        }
        return created;
    };
    for (;;) {
        bool activate = false;
        {
            std::lock_guard map_lock(state_->sessions_mutex);
            if (state_->launch_unknown.contains(runtime_key)) {
                return std::unexpected(make_error(ErrorCode::invocation_in_doubt, "runtime launch remains quarantined after an unknown helper outcome"));
            }
            const auto existing = state_->sessions.find(runtime_key);
            if (existing != state_->sessions.end()) {
                session = existing->second;
                static_cast<void>(session->dispatch_references.fetch_add(1U, std::memory_order_acq_rel));
            } else if (!state_->activating.try_emplace(runtime_key, *typed_activation_id).second) {
                return std::unexpected(make_error(ErrorCode::busy, "runtime activation is already in progress"));
            } else {
                activate = true;
            }
        }
        if (activate) {
            const auto created = activate_session();
            std::lock_guard map_lock(state_->sessions_mutex);
            state_->activating.erase(runtime_key);
            if (!created) {
                if (created.error().code == ErrorCode::invocation_in_doubt) {
                    state_->launch_unknown.insert(runtime_key);
                }
                return std::unexpected(created.error());
            }
            const auto [iterator, inserted] = state_->sessions.emplace(runtime_key, *created);
            if (!inserted) {
                return std::unexpected(make_error(ErrorCode::internal, "runtime session appeared while activation reservation was held"));
            }
            session = iterator->second;
            static_cast<void>(session->dispatch_references.fetch_add(1U, std::memory_order_acq_rel));
        }
        auto lease = std::make_unique<SessionLease>(session, std::unique_lock(session->mutex));
        if (lease->session->poisoned) {
            return std::unexpected(make_error(ErrorCode::child_failure, "runtime session is poisoned pending cgroup cleanup"));
        }
        if (lease->session->activation == *typed_activation_id) {
            return lease;
        }
        // A new RuntimeProcess is a new activation. The per-runtime execution lease is held by
        // the caller, so retire the idle old PID 1 deterministically instead of returning busy.
        const auto retired = retire_session(runtime_key, lease->session);
        if (!retired) {
            return std::unexpected(retired.error());
        }
        lease.reset();
        session.reset();
    }
}

Result<ExecutionResult> Broker::dispatch(const ExecuteRequest& request) {
    const auto lease = acquire_session(request.runtime_key, request.activation_id);
    if (!lease) {
        return std::unexpected(lease.error());
    }
    RuntimeSession& session = *(*lease)->session;
    const auto fail_session = [&](const Error& error) -> Result<ExecutionResult> {
        if (const auto retired = retire_session(request.runtime_key, (*lease)->session); !retired) {
            return std::unexpected(retired.error());
        }
        return std::unexpected(error);
    };
    if (const auto deadline = configure_control_socket(session.control_fd, request.timeout + std::chrono::seconds(5)); !deadline) {
        return fail_session(deadline.error());
    }
    const auto payload = encode_execute_request(request);
    if (!payload) {
        return std::unexpected(payload.error());
    }
    const auto outbound = encode_frame(MessageKind::execute, *payload);
    if (!outbound) {
        return std::unexpected(outbound.error());
    }
    if (const auto executing = session.runtime.begin_execution(session.activation); !executing) {
        return fail_session(transport_error(executing.error()));
    }
    if (const auto sent = send_frame(session.control_fd, *outbound); !sent) {
        return fail_session(sent.error());
    }
    const auto inbound = receive_frame(session.control_fd);
    if (!inbound) {
        return fail_session(inbound.error());
    }
    const auto frame = decode_frame(*inbound);
    if (!frame) {
        return fail_session(frame.error());
    }
    if (frame->kind == MessageKind::error) {
        const auto error = decode_error(frame->payload);
        if (!error) {
            return fail_session(error.error());
        }
        return fail_session(*error);
    }
    if (frame->kind != MessageKind::result) {
        return fail_session(make_error(ErrorCode::protocol_violation, "supervisor returned non-result frame"));
    }
    const auto result = decode_execution_result(frame->payload);
    if (!result || result->request_id != request.request_id || result->replayed) {
        return fail_session(make_error(ErrorCode::protocol_violation, "invalid supervisor result identity"));
    }
    if (const auto finished = session.runtime.finish_execution(session.activation); !finished) {
        return fail_session(transport_error(finished.error()));
    }
    session.last_used = std::chrono::steady_clock::now();
    return *result;
}

Result<RuntimeStatusResult> Broker::read_runtime_status(const RuntimeStatusRequest& request) const {
    if (const auto valid = validate_runtime_status_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    const auto runtime = domain::RuntimeId::parse(request.runtime_key);
    const auto activation = domain::ActivationId::parse(request.activation_id);
    if (!runtime || !activation) {
        const domain::Error& error = !runtime ? runtime.error() : activation.error();
        return std::unexpected(transport_error(error));
    }
    const application::RuntimeStatusQuery query(*runtime, *activation);
    BrokerRuntimeStatusPort port([this](const application::RuntimeStatusQuery& observed_query)
        -> domain::Result<application::RuntimeStatus> {
        std::shared_ptr<RuntimeSession> session;
        std::optional<domain::ActivationId> activating_owner;
        bool launch_quarantined = false;
        {
            // Do not take a session mutex while holding sessions_mutex: retirement takes the
            // inverse (session -> map) order after slow cleanup. Copying the shared_ptr keeps the
            // session alive after this map observation without adding a dispatch reference.
            std::lock_guard map_lock(state_->sessions_mutex);
            const auto existing = state_->sessions.find(observed_query.runtime().value());
            if (existing != state_->sessions.end()) {
                session = existing->second;
            } else if (const auto activating = state_->activating.find(observed_query.runtime().value());
                       activating != state_->activating.end()) {
                activating_owner = activating->second;
            } else {
                launch_quarantined = state_->launch_unknown.contains(observed_query.runtime().value());
            }
        }
        if (!session) {
            const domain::RuntimeState state = launch_quarantined
                ? domain::RuntimeState::failed
                : activating_owner.has_value()
                ? domain::RuntimeState::activating
                : domain::RuntimeState::dormant;
            const auto snapshot = domain::RuntimeSnapshot::create(
                observed_query.runtime(),
                state,
                std::move(activating_owner));
            if (!snapshot) {
                return std::unexpected(snapshot.error());
            }
            return application::RuntimeStatus::create(
                observed_query,
                *snapshot,
                false,
                std::nullopt,
                config_.idle_ttl,
                0U,
                launch_quarantined);
        }

        std::lock_guard session_lock(session->mutex);
        const domain::RuntimeSnapshot snapshot = session->runtime.snapshot();
        std::optional<std::chrono::milliseconds> idle_for;
        if (snapshot.state() == domain::RuntimeState::ready) {
            const auto now = std::chrono::steady_clock::now();
            const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - session->last_used);
            idle_for = std::max(std::chrono::milliseconds::zero(), elapsed);
        }
        // "alive" means healthy and reusable, not merely that an untrusted PID might still exist
        // during a failed cleanup. `cleanup_pending` is the explicit signal for that latter case.
        const bool supervisor_alive = !session->poisoned &&
            snapshot.state() != domain::RuntimeState::failed &&
            !launcher_exited(session->launcher_pidfd);
        return application::RuntimeStatus::create(
            observed_query,
            snapshot,
            supervisor_alive,
            idle_for,
            config_.idle_ttl,
            static_cast<std::uint64_t>(session->dispatch_references.load(std::memory_order_acquire)),
            session->poisoned);
    });
    application::RuntimeStatusService service;
    const auto observed = service.inspect(query, port);
    if (!observed) {
        return std::unexpected(transport_error(observed.error()));
    }
    RuntimeStatusResult result{
        .runtime_key = observed->snapshot().runtime().value(),
        .state = observed->snapshot().state(),
        .active_activation_id = {},
        .handle_activation_matches = observed->handle_activation_matches(),
        .supervisor_alive = observed->supervisor_alive(),
        .idle_for = observed->idle_for(),
        .idle_ttl = observed->idle_ttl(),
        .borrowed_dispatches = observed->borrowed_dispatches(),
        .cleanup_pending = observed->cleanup_pending(),
    };
    if (observed->snapshot().active_activation().has_value()) {
        result.active_activation_id = observed->snapshot().active_activation()->value();
    }
    if (const auto valid = validate_runtime_status_result(result); !valid) {
        return std::unexpected(valid.error());
    }
    return result;
}

application::OperatorWorkspaceQueryResult<domain::OperatorWorkspaceStatus> Broker::status(
    const domain::RuntimeId& runtime) const {
    if (!state_) {
        return std::unexpected(application::make_operator_workspace_query_error(
            application::OperatorWorkspaceQueryErrorCode::unavailable,
            "operator workspace read model is unavailable"));
    }
    std::shared_ptr<RuntimeSession> session;
    domain::WorkspaceActivity activity{domain::WorkspaceActivity::inactive};
    {
        std::lock_guard map_lock(state_->sessions_mutex);
        const auto existing = state_->sessions.find(runtime.value());
        if (existing != state_->sessions.end()) {
            session = existing->second;
        } else if (state_->activating.contains(runtime.value())) {
            activity = domain::WorkspaceActivity::activating;
        } else if (state_->launch_unknown.contains(runtime.value())) {
            activity = domain::WorkspaceActivity::failed;
        }
    }
    if (session) {
        std::lock_guard session_lock(session->mutex);
        activity = session->poisoned
            ? domain::WorkspaceActivity::failed
            : workspace_activity_from_runtime_state(session->runtime.state());
    }

    const auto binding = quota_.find_ready_runtime(runtime.value());
    if (!binding) {
        if (binding.error().code == ErrorCode::not_found && activity == domain::WorkspaceActivity::inactive) {
            const auto status = domain::OperatorWorkspaceStatus::create(
                runtime,
                domain::WorkspacePersistence::absent,
                activity,
                std::nullopt);
            if (!status) {
                return std::unexpected(application::make_operator_workspace_query_error(
                    application::OperatorWorkspaceQueryErrorCode::inconsistent,
                    "inactive operator workspace status violates the domain contract"));
            }
            return *status;
        }
        if (binding.error().code == ErrorCode::not_found) {
            return std::unexpected(application::make_operator_workspace_query_error(
                application::OperatorWorkspaceQueryErrorCode::inconsistent,
                "active runtime has no persistent workspace binding"));
        }
        return std::unexpected(normalize_operator_read_error(binding.error()));
    }
    const auto quota_usage = quota_.read_workspace_quota_usage(*binding);
    if (!quota_usage) {
        return std::unexpected(normalize_operator_read_error(quota_usage.error()));
    }
    const auto status = domain::OperatorWorkspaceStatus::create(
        runtime,
        domain::WorkspacePersistence::ready,
        activity,
        *quota_usage);
    if (!status) {
        return std::unexpected(application::make_operator_workspace_query_error(
            application::OperatorWorkspaceQueryErrorCode::inconsistent,
            "ready operator workspace status violates the domain contract"));
    }
    return *status;
}

application::OperatorWorkspaceQueryResult<domain::WorkspaceListing> Broker::list(
    const domain::RuntimeId& runtime,
    const domain::OperatorWorkspacePath& path) const {
    const auto binding = quota_.find_ready_runtime(runtime.value());
    if (!binding) {
        return std::unexpected(normalize_operator_read_error(binding.error()));
    }
    OperatorWorkspaceReader reader;
    const auto listing = reader.list(*binding, path);
    if (!listing) {
        return std::unexpected(normalize_operator_read_error(listing.error()));
    }
    return *listing;
}

Result<PayloadResult> Broker::replay_payload(const PayloadReplayRequest& request) {
    if (const auto valid = validate_payload_replay_request(request); !valid) {
        return std::unexpected(valid.error());
    }
    const auto admission = admit_payload_replay_request(request);
    if (!admission) {
        return std::unexpected(admission.error());
    }
    // This first read is deliberately before quota/session work. A missing record is the one
    // normal signal for the Python import-intent workflow to download again, and it must leave
    // no journal, RuntimeSession, cgroup, mount, or staging side effect behind.
    const auto receipt = detail::resolve_payload_replay_receipt(journal_, request);
    if (!receipt) {
        return std::unexpected(receipt.error());
    }
    // The gate excludes an in-flight broker command or add_file stream from changing the same
    // persistent upper tree while its no-follow object is hashed. It does not activate, replace,
    // or otherwise inspect a RuntimeSession.
    const auto gate = execution_gate_.try_acquire(admission->runtime.value());
    if (!gate) {
        return std::unexpected(gate.error());
    }
    const auto binding = quota_.find_ready_runtime(admission->runtime.value());
    if (!binding) {
        return std::unexpected(make_error(
            ErrorCode::invocation_in_doubt,
            "completed file receipt exists but its persistent quota binding cannot be proven: " + binding.error().message));
    }
    if (const auto object = detail::verify_replayable_payload_object(*binding, request, *receipt); !object) {
        return std::unexpected(object.error());
    }
    return *receipt;
}

Result<void> Broker::dispatch_payload_stream(
    const int client_fd,
    const PayloadBeginRequest& request) {
    /** @brief 单个本机文件流 I/O 的最长等待时间 / Maximum wait for one local file-stream I/O. */
    constexpr auto kPayloadIoDeadline = std::chrono::seconds(30);
    // The caller owns RuntimeExecutionGate for the complete transfer. Re-read durable state
    // here, not only before gate acquisition, so a queued same-runtime request cannot seal a
    // duplicate after its predecessor completed or became in-doubt.
    const auto journal_decision = resolve_payload_journal(journal_, request);
    if (!journal_decision) {
        return std::unexpected(journal_decision.error());
    }
    if (journal_decision->has_value()) {
        return send_payload_result_frame(client_fd, **journal_decision);
    }
    const auto lease = acquire_session(request.runtime_key, request.activation_id);
    if (!lease) {
        return std::unexpected(lease.error());
    }
    RuntimeSession& session = *(*lease)->session;
    const auto fail_session = [&](const Error& error) -> Result<void> {
        if (const auto retired = retire_session(request.runtime_key, (*lease)->session); !retired) {
            return std::unexpected(retired.error());
        }
        return std::unexpected(error);
    };
    if (const auto client_deadline = configure_control_socket(client_fd, kPayloadIoDeadline); !client_deadline) {
        return std::unexpected(client_deadline.error());
    }
    if (const auto supervisor_deadline = configure_control_socket(session.control_fd, kPayloadIoDeadline); !supervisor_deadline) {
        return fail_session(supervisor_deadline.error());
    }
    if (const auto executing = session.runtime.begin_execution(session.activation); !executing) {
        return fail_session(transport_error(executing.error()));
    }
    bool lifecycle_executing = true;
    bool payload_active = false;
    std::size_t received_bytes = 0U;
    bool sealed = false;
    const auto finish_execution = [&]() -> Result<void> {
        if (!lifecycle_executing) {
            return {};
        }
        const auto finished = session.runtime.finish_execution(session.activation);
        if (!finished) {
            return std::unexpected(transport_error(finished.error()));
        }
        lifecycle_executing = false;
        session.last_used = std::chrono::steady_clock::now();
        return {};
    };
    const auto exchange_supervisor = [&](const MessageKind kind, const std::vector<std::byte>& payload) -> Result<Frame> {
        const auto outbound = encode_frame(kind, payload);
        if (!outbound) {
            return std::unexpected(outbound.error());
        }
        if (const auto sent = send_frame(session.control_fd, *outbound); !sent) {
            return std::unexpected(sent.error());
        }
        const auto inbound = receive_frame(session.control_fd);
        if (!inbound) {
            return std::unexpected(inbound.error());
        }
        const auto frame = decode_frame(*inbound);
        if (!frame) {
            return std::unexpected(frame.error());
        }
        if (frame->kind == MessageKind::error) {
            const auto error = decode_error(frame->payload);
            if (!error) {
                return std::unexpected(error.error());
            }
            return std::unexpected(*error);
        }
        return *frame;
    };
    const auto expect_ack = [&](const Frame& frame, const PayloadAckStage stage, const std::size_t expected_bytes) -> Result<PayloadAck> {
        if (frame.kind != MessageKind::payload_ack) {
            return std::unexpected(make_error(ErrorCode::protocol_violation, "supervisor returned a non-acknowledgement file frame"));
        }
        const auto acknowledgement = decode_payload_ack(frame.payload);
        if (!acknowledgement || acknowledgement->request_id != request.request_id || acknowledgement->stage != stage ||
            acknowledgement->received_bytes != expected_bytes) {
            return std::unexpected(make_error(ErrorCode::protocol_violation, "supervisor returned an invalid file acknowledgement"));
        }
        return *acknowledgement;
    };
    const auto abort_unpublished = [&]() -> Result<PayloadAck> {
        if (!payload_active) {
            return std::unexpected(make_error(ErrorCode::protocol_violation, "cannot abort an inactive file ingress"));
        }
        const PayloadControlRequest control{.request_id = request.request_id};
        const auto encoded = encode_payload_control_request(control);
        if (!encoded) {
            return std::unexpected(encoded.error());
        }
        const auto response = exchange_supervisor(MessageKind::payload_abort, *encoded);
        if (!response) {
            return std::unexpected(response.error());
        }
        const auto acknowledgement = expect_ack(*response, PayloadAckStage::aborted, received_bytes);
        if (!acknowledgement) {
            return std::unexpected(acknowledgement.error());
        }
        payload_active = false;
        if (const auto finished = finish_execution(); !finished) {
            return std::unexpected(finished.error());
        }
        return *acknowledgement;
    };
    const auto cleanup_unpublished = [&](const Error& cause) -> Result<void> {
        const auto aborted = abort_unpublished();
        if (!aborted) {
            return fail_session(make_error(
                ErrorCode::invocation_in_doubt,
                "unpublished file cleanup could not be proven: " + aborted.error().message));
        }
        return std::unexpected(cause);
    };
    const auto abandon_client = [&]() -> Result<void> {
        const auto aborted = abort_unpublished();
        if (!aborted) {
            return fail_session(make_error(
                ErrorCode::invocation_in_doubt,
                "client disconnected and unpublished file cleanup could not be proven: " + aborted.error().message));
        }
        return {};
    };

    const auto encoded_begin = encode_payload_begin_request(request);
    if (!encoded_begin) {
        return fail_session(encoded_begin.error());
    }
    const auto begin_response = exchange_supervisor(MessageKind::payload_begin, *encoded_begin);
    if (!begin_response) {
        return fail_session(make_error(
            ErrorCode::invocation_in_doubt,
            "file begin outcome is unknown: " + begin_response.error().message));
    }
    const auto begin_acknowledgement = expect_ack(*begin_response, PayloadAckStage::begun, 0U);
    if (!begin_acknowledgement) {
        return fail_session(make_error(
            ErrorCode::invocation_in_doubt,
            "file begin acknowledgement is invalid: " + begin_acknowledgement.error().message));
    }
    payload_active = true;
    if (const auto sent = send_payload_ack_frame(client_fd, *begin_acknowledgement); !sent) {
        return abandon_client();
    }

    for (;;) {
        const auto inbound = receive_frame(client_fd);
        if (!inbound) {
            return abandon_client();
        }
        const auto client_frame = decode_frame(*inbound);
        if (!client_frame) {
            return cleanup_unpublished(client_frame.error());
        }
        if (client_frame->kind == MessageKind::payload_chunk) {
            const auto chunk = decode_payload_chunk(client_frame->payload);
            if (!chunk || chunk->request_id != request.request_id || sealed || received_bytes > request.byte_size ||
                chunk->bytes.size() > request.byte_size - received_bytes) {
                return cleanup_unpublished(make_error(ErrorCode::protocol_violation, "invalid file chunk sequence"));
            }
            const auto encoded_chunk = encode_payload_chunk(*chunk);
            if (!encoded_chunk) {
                return cleanup_unpublished(encoded_chunk.error());
            }
            const auto response = exchange_supervisor(MessageKind::payload_chunk, *encoded_chunk);
            if (!response) {
                return cleanup_unpublished(response.error());
            }
            const std::size_t next_received_bytes = received_bytes + chunk->bytes.size();
            const auto acknowledgement = expect_ack(*response, PayloadAckStage::chunk_written, next_received_bytes);
            if (!acknowledgement) {
                return cleanup_unpublished(acknowledgement.error());
            }
            received_bytes = next_received_bytes;
            if (const auto sent = send_payload_ack_frame(client_fd, *acknowledgement); !sent) {
                return abandon_client();
            }
            continue;
        }
        if (client_frame->kind == MessageKind::payload_seal) {
            const auto control = decode_payload_control_request(client_frame->payload);
            if (!control || control->request_id != request.request_id || sealed) {
                return cleanup_unpublished(make_error(ErrorCode::protocol_violation, "invalid file seal sequence"));
            }
            const auto encoded = encode_payload_control_request(*control);
            if (!encoded) {
                return cleanup_unpublished(encoded.error());
            }
            const auto response = exchange_supervisor(MessageKind::payload_seal, *encoded);
            if (!response) {
                return cleanup_unpublished(response.error());
            }
            const auto acknowledgement = expect_ack(*response, PayloadAckStage::sealed, request.byte_size);
            if (!acknowledgement) {
                return cleanup_unpublished(acknowledgement.error());
            }
            sealed = true;
            received_bytes = request.byte_size;
            if (const auto sent = send_payload_ack_frame(client_fd, *acknowledgement); !sent) {
                return abandon_client();
            }
            continue;
        }
        if (client_frame->kind == MessageKind::payload_abort) {
            const auto control = decode_payload_control_request(client_frame->payload);
            if (!control || control->request_id != request.request_id) {
                return cleanup_unpublished(make_error(ErrorCode::protocol_violation, "invalid file abort sequence"));
            }
            const auto acknowledgement = abort_unpublished();
            if (!acknowledgement) {
                return fail_session(make_error(
                    ErrorCode::invocation_in_doubt,
                    "file abort could not be proven: " + acknowledgement.error().message));
            }
            static_cast<void>(send_payload_ack_frame(client_fd, *acknowledgement));
            return {};
        }
        if (client_frame->kind != MessageKind::payload_publish) {
            return cleanup_unpublished(make_error(ErrorCode::protocol_violation, "unexpected file-transfer frame"));
        }
        const auto control = decode_payload_control_request(client_frame->payload);
        if (!control || control->request_id != request.request_id || !sealed || received_bytes != request.byte_size) {
            return cleanup_unpublished(make_error(ErrorCode::protocol_violation, "invalid file publish sequence"));
        }
        // The pending marker is intentionally created only after PID 1 sealed+fdatasynced the
        // exact declared bytes, and immediately before the one irreversible rename operation.
        const auto begun = journal_.begin_payload(request);
        if (!begun) {
            const auto aborted = abort_unpublished();
            if (!aborted || begun.error().code != ErrorCode::already_exists) {
                return fail_session(make_error(
                    ErrorCode::invocation_in_doubt,
                    "file journal begin outcome is unknown: " + begun.error().message));
            }
            return std::unexpected(begun.error());
        }
        const auto encoded = encode_payload_control_request(*control);
        if (!encoded) {
            return fail_session(make_error(
                ErrorCode::invocation_in_doubt,
                "file journal is pending but publish encoding failed: " + encoded.error().message));
        }
        const auto response = exchange_supervisor(MessageKind::payload_publish, *encoded);
        if (!response) {
            return fail_session(make_error(
                ErrorCode::invocation_in_doubt,
                "file publish outcome is unknown: " + response.error().message));
        }
        if (response->kind != MessageKind::payload_result) {
            return fail_session(make_error(
                ErrorCode::invocation_in_doubt,
                "file publish returned an unexpected supervisor frame"));
        }
        const auto result = decode_payload_result(response->payload);
        const std::string expected_path = "/workspace/uploads/" + request.opaque_id + "/payload";
        if (!result || result->replayed || result->request_id != request.request_id || result->path != expected_path ||
            result->byte_size != request.byte_size || result->sha256 != request.sha256) {
            return fail_session(make_error(
                ErrorCode::invocation_in_doubt,
                "file publish returned an invalid receipt"));
        }
        payload_active = false;
        if (const auto completed = journal_.complete_payload(request, *result); !completed) {
            if (const auto finished = finish_execution(); !finished) {
                return fail_session(make_error(
                    ErrorCode::invocation_in_doubt,
                    "file published but lifecycle completion failed: " + finished.error().message));
            }
            return std::unexpected(make_error(
                ErrorCode::invocation_in_doubt,
                "file published but durable journal completion failed: " + completed.error().message));
        }
        if (const auto finished = finish_execution(); !finished) {
            return fail_session(finished.error());
        }
        static_cast<void>(send_payload_result_frame(client_fd, *result));
        return {};
    }
}

Result<void> Broker::serve_operator_client(const int client_fd) {
    ucred credentials {};
    socklen_t credential_size = sizeof(credentials);
    if (getsockopt(client_fd, SOL_SOCKET, SO_PEERCRED, &credentials, &credential_size) != 0 ||
        credential_size != sizeof(credentials) ||
        !is_authorized_operator_peer(credentials.uid, config_.operator_uid)) {
        return std::unexpected(make_error(ErrorCode::authentication_failed, "operator client UID is not authorized"));
    }
    if (const auto configured = configure_control_socket(client_fd, std::chrono::seconds(5)); !configured) {
        return std::unexpected(configured.error());
    }
    const auto wire = operator_protocol::receive_operator_frame(client_fd);
    if (!wire) {
        return wire.error().code == ErrorCode::io_failure ? Result<void>{} : std::unexpected(wire.error());
    }
    const auto frame = operator_protocol::decode_operator_frame(*wire);
    if (!frame) {
        if (const auto sent = send_operator_error_frame(
                client_fd,
                operator_protocol::OperatorErrorCode::protocol_violation);
            !sent) {
            return std::unexpected(sent.error());
        }
        return {};
    }
    application::OperatorWorkspaceQueryService service;
    if (frame->kind == operator_protocol::OperatorMessageKind::status_request) {
        const auto request = operator_protocol::decode_status_request(frame->payload);
        if (!request) {
            if (const auto sent = send_operator_error_frame(
                    client_fd,
                    operator_protocol::OperatorErrorCode::invalid_request);
                !sent) {
                return std::unexpected(sent.error());
            }
            return {};
        }
        const auto runtime = domain::RuntimeId::parse(request->runtime_key);
        if (!runtime) {
            if (const auto sent = send_operator_error_frame(
                    client_fd,
                    operator_protocol::OperatorErrorCode::invalid_request);
                !sent) {
                return std::unexpected(sent.error());
            }
            return {};
        }
        const auto status = service.status(*runtime, *this);
        if (!status) {
            if (const auto sent = send_operator_error_frame(client_fd, operator_error_code(status.error())); !sent) {
                return std::unexpected(sent.error());
            }
            return {};
        }
        return send_operator_status_frame(client_fd, *status);
    }
    if (frame->kind == operator_protocol::OperatorMessageKind::list_request) {
        const auto request = operator_protocol::decode_list_request(frame->payload);
        if (!request) {
            if (const auto sent = send_operator_error_frame(
                    client_fd,
                    operator_protocol::OperatorErrorCode::invalid_request);
                !sent) {
                return std::unexpected(sent.error());
            }
            return {};
        }
        const auto runtime = domain::RuntimeId::parse(request->runtime_key);
        const auto path = domain::OperatorWorkspacePath::parse(request->path);
        if (!runtime || !path) {
            if (const auto sent = send_operator_error_frame(
                    client_fd,
                    operator_protocol::OperatorErrorCode::invalid_request);
                !sent) {
                return std::unexpected(sent.error());
            }
            return {};
        }
        const auto listing = service.list(*runtime, *path, *this);
        if (!listing) {
            if (const auto sent = send_operator_error_frame(client_fd, operator_error_code(listing.error())); !sent) {
                return std::unexpected(sent.error());
            }
            return {};
        }
        return send_operator_list_frame(client_fd, *listing);
    }
    if (const auto sent = send_operator_error_frame(
            client_fd,
            operator_protocol::OperatorErrorCode::protocol_violation);
        !sent) {
        return std::unexpected(sent.error());
    }
    return {};
}

Result<void> Broker::serve_client(const int client_fd) {
    ucred credentials {};
    socklen_t credential_size = sizeof(credentials);
    if (getsockopt(client_fd, SOL_SOCKET, SO_PEERCRED, &credentials, &credential_size) != 0 ||
        credential_size != sizeof(credentials) || credentials.uid != config_.client_uid) {
        return std::unexpected(make_error(ErrorCode::authentication_failed, "broker client UID is not authorized"));
    }
    if (const auto configured = configure_control_socket(client_fd, std::chrono::seconds(5)); !configured) {
        return std::unexpected(configured.error());
    }
    for (bool one_request = true; one_request; one_request = false) {
        const auto wire = receive_frame(client_fd);
        if (!wire) {
            if (wire.error().code == ErrorCode::io_failure) {
                return {};
            }
            return std::unexpected(wire.error());
        }
        const auto frame = decode_frame(*wire);
        if (!frame) {
            if (const auto sent = send_error_frame(client_fd, frame.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (frame->kind == MessageKind::runtime_status) {
            const auto request = decode_runtime_status_request(frame->payload);
            if (!request) {
                if (const auto sent = send_error_frame(client_fd, request.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            const auto status = read_runtime_status(*request);
            if (!status) {
                if (const auto sent = send_error_frame(client_fd, status.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            if (const auto sent = send_runtime_status_frame(client_fd, *status); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (frame->kind == MessageKind::payload_replay) {
            const auto request = decode_payload_replay_request(frame->payload);
            if (!request) {
                if (const auto sent = send_error_frame(client_fd, request.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            const auto replay = replay_payload(*request);
            if (!replay) {
                if (const auto sent = send_error_frame(client_fd, replay.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            if (const auto sent = send_payload_result_frame(client_fd, *replay); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (frame->kind == MessageKind::payload_begin) {
            const auto request = decode_payload_begin_request(frame->payload);
            if (!request) {
                if (const auto sent = send_error_frame(client_fd, request.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            const auto admission = admit_payload_begin_request(*request);
            if (!admission) {
                if (const auto sent = send_error_frame(client_fd, admission.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            // As for execute, quota provisioning is an admission prerequisite: journal records
            // are allowed only under this runtime's verified control project.
            if (const auto quota_binding = quota_.ensure_runtime(admission->runtime.value()); !quota_binding) {
                if (const auto sent = send_error_frame(client_fd, quota_binding.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            const auto journal_decision = resolve_payload_journal(journal_, *request);
            if (!journal_decision) {
                if (const auto sent = send_error_frame(client_fd, journal_decision.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            if (journal_decision->has_value()) {
                if (const auto sent = send_payload_result_frame(client_fd, **journal_decision); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            // This lease spans begin/chunk/seal/publish, so a second command cannot observe a
            // half-written file or race the journal marker created immediately before rename.
            const auto lease = execution_gate_.try_acquire(request->runtime_key);
            if (!lease) {
                if (const auto sent = send_error_frame(client_fd, lease.error()); !sent) {
                    return std::unexpected(sent.error());
                }
                continue;
            }
            const auto transferred = dispatch_payload_stream(client_fd, *request);
            if (!transferred) {
                if (const auto sent = send_error_frame(client_fd, transferred.error()); !sent) {
                    // A closed client already triggered dispatch_payload_stream's abort path;
                    // preserve the transport error only for broker diagnostics.
                    return std::unexpected(sent.error());
                }
            }
            return {};
        }
        if (frame->kind != MessageKind::execute) {
            if (const auto sent = send_error_frame(client_fd, make_error(ErrorCode::protocol_violation, "broker accepts execute, file ingress, file replay, or runtime status only")); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        const auto request = decode_execute_request(frame->payload);
        if (!request) {
            if (const auto sent = send_error_frame(client_fd, request.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        const auto admission = admit_execute_request(*request);
        if (!admission) {
            if (const auto sent = send_error_frame(client_fd, admission.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        // The per-runtime control project must exist before any journal lookup/begin.  This
        // makes a missing/changed quota binding a storage admission failure rather than a
        // chance to create a global journal outside the runtime's control hard limit.
        if (const auto quota_binding = quota_.ensure_runtime(admission->runtime.value()); !quota_binding) {
            if (const auto sent = send_error_frame(client_fd, quota_binding.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        const auto journal_decision = resolve_execution_journal(journal_, *request);
        if (!journal_decision) {
            if (const auto sent = send_error_frame(client_fd, journal_decision.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (journal_decision->has_value()) {
            if (const auto sent = send_result_frame(client_fd, **journal_decision); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        const auto lease = execution_gate_.try_acquire(request->runtime_key);
        if (!lease) {
            if (const auto sent = send_error_frame(client_fd, lease.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        // A contender may have waited behind another same-runtime request. The only decision
        // that authorizes begin is this second, gate-protected durable observation.
        const auto gate_decision = resolve_execution_journal(journal_, *request);
        if (!gate_decision) {
            if (const auto sent = send_error_frame(client_fd, gate_decision.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (gate_decision->has_value()) {
            if (const auto sent = send_result_frame(client_fd, **gate_decision); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (const auto begun = journal_.begin(*request); !begun) {
            if (const auto sent = send_error_frame(client_fd, begun.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        const auto result = dispatch(*request);
        if (!result) {
            // Pending is intentionally retained: a crash or partially-started task is never retried unsafely.
            if (const auto sent = send_error_frame(client_fd, result.error()); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (const auto completed = journal_.complete(*request, *result); !completed) {
            if (const auto sent = send_error_frame(client_fd, make_error(ErrorCode::invocation_in_doubt, "result exists but durable journal completion failed")); !sent) {
                return std::unexpected(sent.error());
            }
            continue;
        }
        if (const auto sent = send_result_frame(client_fd, *result); !sent) {
            return std::unexpected(sent.error());
        }
    }
    return {};
}

void Broker::reap_expired_sessions() noexcept {
    if (!state_) {
        return;
    }
    const auto now = std::chrono::steady_clock::now();
    std::vector<std::pair<std::string, std::shared_ptr<RuntimeSession>>> candidates;
    {
        std::lock_guard map_lock(state_->sessions_mutex);
        candidates.reserve(state_->sessions.size());
        for (const auto& [runtime_key, session] : state_->sessions) {
            candidates.emplace_back(runtime_key, session);
        }
    }
    for (const auto& [runtime_key, session] : candidates) {
        if (session->dispatch_references.load(std::memory_order_acquire) != 0U) {
            continue;
        }
        std::unique_lock session_lock(session->mutex, std::try_to_lock);
        if (!session_lock.owns_lock()) {
            continue;
        }
        if (session->dispatch_references.load(std::memory_order_acquire) != 0U) {
            continue;
        }
        const bool dead = launcher_exited(session->launcher_pidfd);
        const bool expired = now - session->last_used >= config_.idle_ttl;
        if (!session->poisoned && !dead && !expired) {
            continue;
        }
        if (!dead) {
            const auto shutdown = encode_frame(MessageKind::shutdown, {});
            if (shutdown) {
                static_cast<void>(configure_control_socket(session->control_fd, std::chrono::seconds(1)));
                static_cast<void>(send_frame(session->control_fd, *shutdown));
            }
        }
        if (const auto retired = retire_session(runtime_key, session); !retired) {
            // Never drop tracking when cgroup.kill/cgroup.events/pidfd exit failed. A later
            // reaper pass retries the authoritative cleanup, while dispatch rejects poisoned.
            session->poisoned = true;
            session->last_used = now;
        }
    }
}

Result<void> Broker::serve_forever(const ReadyCallback ready_callback) {
    if (listen_fd_ < 0 || operator_listen_fd_ < 0) {
        return std::unexpected(make_error(ErrorCode::internal, "Bot or operator broker listener is not bound"));
    }
    /** @brief 已 accept client 所属 control-plane endpoint / Control-plane endpoint of one accepted client. */
    enum class ClientEndpoint : std::uint8_t {
        /** @brief Bot 专属 endpoint / Bot-exclusive endpoint. */
        bot,
        /** @brief 独立 operator endpoint / Independent operator endpoint. */
        operator_plane,
    };
    /** @brief 等待专属 worker 的已 accept client / Accepted client waiting for its dedicated worker pool. */
    struct QueuedClient final {
        /** @brief 已 accept 的 client FD / Accepted client FD. */
        int fd{-1};
    };
    std::mutex queue_mutex;
    /** @brief Bot worker 的专属唤醒条件 / Dedicated wake condition for Bot workers. */
    std::condition_variable bot_queue_ready;
    /** @brief operator worker 的专属唤醒条件 / Dedicated wake condition for operator workers. */
    std::condition_variable operator_queue_ready;
    std::deque<QueuedClient> queued_bot_clients;
    std::deque<QueuedClient> queued_operator_clients;
    bool stopping = false;
    // Do not share this pool with the Bot endpoint. A Bot connection can deliberately withhold
    // its first packet until the five-second I/O deadline, or run a much longer task dispatch;
    // queue priority cannot recover an operator worker after every shared worker is blocked.
    const auto bot_worker = [this,
                             &queue_mutex,
                             &bot_queue_ready,
                             &queued_bot_clients,
                             &stopping]() {
        for (;;) {
            QueuedClient client;
            {
                std::unique_lock queue_lock(queue_mutex);
                bot_queue_ready.wait(queue_lock, [&]() { return stopping || !queued_bot_clients.empty(); });
                if (queued_bot_clients.empty()) {
                    return;
                }
                client = queued_bot_clients.front();
                queued_bot_clients.pop_front();
            }
            const auto served = serve_client(client.fd);
            close_fd(client.fd);
            // An untrusted client can only lose its own short-lived connection, never a worker
            // or the broker. Errors are intentionally contained to this client.
            static_cast<void>(served);
        }
    };
    // This separately reserved pool is the liveness boundary for recovery and inspection. Its
    // work is one bounded WOP1 read-only request, and Bot-originated work cannot occupy it.
    const auto operator_worker = [this,
                                  &queue_mutex,
                                  &operator_queue_ready,
                                  &queued_operator_clients,
                                  &stopping]() {
        for (;;) {
            QueuedClient client;
            {
                std::unique_lock queue_lock(queue_mutex);
                operator_queue_ready.wait(queue_lock, [&]() { return stopping || !queued_operator_clients.empty(); });
                if (queued_operator_clients.empty()) {
                    return;
                }
                client = queued_operator_clients.front();
                queued_operator_clients.pop_front();
            }
            const auto served = serve_operator_client(client.fd);
            close_fd(client.fd);
            // A malformed or disconnected operator client can only lose its own bounded request.
            static_cast<void>(served);
        }
    };
    std::vector<std::thread> workers;
    workers.reserve(kMaxClientWorkers + kReservedOperatorWorkers);
    try {
        for (std::size_t index = 0; index < kMaxClientWorkers; ++index) {
            workers.emplace_back(bot_worker);
        }
        for (std::size_t index = 0; index < kReservedOperatorWorkers; ++index) {
            workers.emplace_back(operator_worker);
        }
    } catch (const std::system_error&) {
        {
            std::lock_guard queue_lock(queue_mutex);
            stopping = true;
        }
        bot_queue_ready.notify_all();
        operator_queue_ready.notify_all();
        for (std::thread& thread : workers) {
            if (thread.joinable()) {
                thread.join();
            }
        }
        return std::unexpected(make_error(ErrorCode::internal, "create bounded broker worker pool"));
    }
    /** @brief accept loop 或 readiness callback 的终止状态 / Terminal state from the accept loop or readiness callback. */
    Result<void> terminal{};
    if (ready_callback != nullptr) {
        terminal = ready_callback();
    }
    while (terminal.has_value()) {
        reap_expired_sessions();
        std::array<pollfd, 2> listening{
            pollfd{.fd = listen_fd_, .events = POLLIN, .revents = 0},
            pollfd{.fd = operator_listen_fd_, .events = POLLIN, .revents = 0},
        };
        const int ready = poll(listening.data(), static_cast<nfds_t>(listening.size()), 250);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            terminal = std::unexpected(errno_error(ErrorCode::io_failure, "poll broker listeners"));
            break;
        }
        if (ready == 0) {
            continue;
        }
        for (std::size_t index = 0U; index < listening.size(); ++index) {
            const short events = listening[index].revents;
            if ((events & (POLLERR | POLLHUP | POLLNVAL)) != 0) {
                terminal = std::unexpected(make_error(ErrorCode::io_failure, "broker listener became unusable"));
                break;
            }
            if ((events & POLLIN) == 0) {
                continue;
            }
            const int accepted = accept4(listening[index].fd, nullptr, nullptr, SOCK_CLOEXEC);
            if (accepted < 0) {
                if (errno == EINTR || errno == ECONNABORTED) {
                    continue;
                }
                terminal = std::unexpected(errno_error(ErrorCode::io_failure, "accept broker client"));
                break;
            }
            const ClientEndpoint endpoint = index == 0U ? ClientEndpoint::bot : ClientEndpoint::operator_plane;
            bool queued = false;
            {
                std::lock_guard queue_lock(queue_mutex);
                std::deque<QueuedClient>& queue = endpoint == ClientEndpoint::operator_plane
                    ? queued_operator_clients
                    : queued_bot_clients;
                const std::size_t limit = endpoint == ClientEndpoint::operator_plane
                    ? kMaxQueuedOperatorClients
                    : kMaxQueuedClients;
                if (queue.size() < limit) {
                    queue.push_back(QueuedClient{.fd = accepted});
                    queued = true;
                }
            }
            if (queued) {
                if (endpoint == ClientEndpoint::operator_plane) {
                    operator_queue_ready.notify_one();
                } else {
                    bot_queue_ready.notify_one();
                }
            } else {
                // Each endpoint has an independent bounded overload policy. A Bot flood cannot
                // consume operator queue capacity, and neither plane allocates an unbounded thread.
                close_fd(accepted);
            }
        }
        if (!terminal) {
            break;
        }
    }
    {
        std::lock_guard queue_lock(queue_mutex);
        stopping = true;
        for (const QueuedClient& client : queued_bot_clients) {
            close_fd(client.fd);
        }
        for (const QueuedClient& client : queued_operator_clients) {
            close_fd(client.fd);
        }
        queued_bot_clients.clear();
        queued_operator_clients.clear();
    }
    bot_queue_ready.notify_all();
    operator_queue_ready.notify_all();
    for (std::thread& thread : workers) {
        if (thread.joinable()) {
            thread.join();
        }
    }
    return terminal;
}

}  // namespace wspctl
