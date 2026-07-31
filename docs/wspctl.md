# wspctl：可恢复的隔离 Workspace Runtime

`wspctl` 是 Bot 的本地代码执行边界，而不是另一个 HTTP 代码执行服务。它把持久的
Workspace 与一次性的进程 activation 分离：一个私聊用户或整个 Telegram 群聊对应一个
长期 `RuntimeRecord`，一次 `run_bash` 或 `add_file` 只取得该 Record 的短暂
`RuntimeProcess` handle。

这份文档定义安全契约；它比“容器能跑起来”更重要。这里刻意只解决**不可信执行
payload** 的隔离，而不把“可信 host 控制面已被攻破”伪装成 `wspctl` 能解决的问题。

## 现实威胁模型：唯一的不可信边界

本设计把以下内容一律当作不可信 payload：用户上传到 Workspace 的文件，及 Bot/Agent
生成并交给 `run_bash` 的 command、stdin、工作目录和参数。模型输出可能受 prompt injection
影响，所以“Bot 生成”不等于可信。

Bot 的业务代码、`wspctl._native`、`wspctld`、OCI image 内的 `wsp-systemd` 和宿主内核是
本问题预设的 trusted computing base（TCB，可信计算基）。我们仍对它们的输入做严格校验，
但不把“这些已被攻击者修改”纳入本版本的本质复杂度。

| 对不可信 payload 的安全性质 | 具体约束 |
| --- | --- |
| 作用域隔离 | 只可读写归属 Runtime 的 `/workspace`；不能看到 host、其他用户/群聊 Runtime，或由上传文件路径间接取得未授权 bind mount |
| 权限隔离 | Bot 不能传 mount、namespace、cgroup、host path 或 capability 参数；task 只有降权后的 uid/gid 与最小 `/dev`、`/proc` 视图 |
| 可用性隔离 | cgroup 的 memory/pids/CPU 预算、300 秒 timeout、输出上限和 PID 1 的 orphan 清理限制 fork bomb、输出洪泛和遗留子进程 |

用户提出的“恶意内核模块”不在这个 payload 的直接能力集合中：`sudo` 不是内核权限本身，真正
门槛是 `CAP_SYS_MODULE`。Bot/task 从未获得它，且 task 的 seccomp 再拒绝
`init_module`、`finit_module`、`delete_module`。因此这只是一个需要保持为真的、可测试的
部署不变量，不应膨胀为本系统的主要威胁分支。

## 设计证据与边界选择

这不是把几项 Linux feature 随意拼在一起。namespace、mount、cgroup v2、seccomp 和
PID 1 supervisor 分别缩小命名空间、文件、资源、系统调用与生命周期的攻击面；它们彼此
补充，却不产生“共享同一内核也等同于 VM”的结论。

- 内核的 [mount namespace 文档](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html)、
  [cgroup v2 文档](https://docs.kernel.org/admin-guide/cgroup-v2.html) 与
  [seccomp 文档](https://man7.org/linux/man-pages/man2/seccomp.2.html) 支持本设计的最小
  mount view、single-writer cgroup tree 和 `no_new_privs` + syscall deny-list 选择。
- [AWS 的 DDD/hexagonal architecture 指南](https://docs.aws.amazon.com/prescriptive-guidance/latest/hexagonal-architectures/overview.html)
  强调领域模型先于 database、外部 API 与 presentation 建立且不依赖它们；所以这里把
  `Runtime`、`ActivationId`、`AttachmentImportIntent` 等业务语义留在 domain/application，
  而把 Linux primitive、PostgreSQL 与 pybind transport 放在 infrastructure/presentation。
- Firecracker 的生产经验表明，为低启动开销工作负载使用 microVM 是可行的下一层隔离，
  但不是本版本的隐藏依赖；参见 [Firecracker: Lightweight Virtualization for Serverless
  Applications](https://www.usenix.org/conference/nsdi20/presentation/agache)。
- 更强的边界仍有自己的 host-facing attack surface。USENIX Security 2023 的
  [Attacks are Forwarded](https://www.usenix.org/conference/usenixsecurity23/presentation/xiao-jietao)
  研究了 microVM container 的 operation-forwarding attack。因此 `wspctl` 即使未来接入
  microVM，也必须保留最小、认证、无任意 FD 传递的 broker protocol 与严格资源限制。
- 近期的 [LITESHIELD](https://www.usenix.org/conference/atc25/presentation/manakkal)
  探索以 userspace microkernel（用户态微内核）缩小 guest-to-host syscall interface；它是值得
  跟踪的研究方向，不是当前 production dependency。当前实现选择成熟的 Linux primitive，
  并将复杂性集中在一个可审计的 host broker。

## 拓扑与权责

```text
Bot Python（无 CAP_SYS_ADMIN）
  └─ wspctl._native 的 RuntimeProcess（C++ pybind11 client）
       └─ Unix SOCK_SEQPACKET，SO_PEERCRED + 0600/0660 ACL
            └─ wspctld（host privilege broker）
                 ├─ RuntimeRecord、OCI manifest digest、activation cache、cgroup
                 └─ wsp-systemd（每个 Runtime 的 PID 1）
                      └─ task cgroup：bash / python / approved argv
```

- Bot 与 pybind extension 永远不持有 `CAP_SYS_ADMIN`，也不获得任意 mount、namespace、
  cgroup 或 base-root 参数。
- `wspctld` 是唯一特权主体。它从 root-owned 配置读取 socket、client UID、OCI manifest digest、
  state root 与 systemd delegated cgroup 根；这些绝不能由 Telegram 参数或 `config.json`
  传入。
- `wsp-systemd` 是新 PID namespace 的真正 PID 1：回收僵尸（zombie）、接收控制帧、转发
  stdout/stderr、处理 timeout 和清理 orphan。Bash/Python 绝不能充当 PID 1。

PID namespace 的 PID 1 退出时内核会终止其余进程，因此 supervisor 必须始终存活至 runtime
关闭，并持续 `waitid`/`waitpid` 回收子进程。参见
[pid_namespaces(7)](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)。

## 领域模型（Domain Model）与分层

`wspctl` 不是“把几组 syscall 包起来”的库。它首先维护一个长期 Workspace 的身份、一次
`RuntimeProcess` activation 的唯一所有权，以及命令副作用的可恢复语义；namespace、mount 和
cgroup 只是实现这些语义的 Linux 外设。下面的名称以当前 `src/wspctl` 实现为准，而不是未来
可能增加的用例名。

### 领域层：`Runtime` 聚合与值对象

`wspctl::domain::Runtime` 是聚合根（aggregate root）。它不保存 PID、socket FD、mount path 或
cgroup path；这些都是可失效的基础设施事实。它只保存持久 `RuntimeId`、当前状态、active owner
以及失败清理期间的私有 cleanup owner，并以类型与状态机约束“谁有权使用或清理这个 runtime”。

| 类型 | 表达的语义与验证 | 不能表达的东西 |
| --- | --- | --- |
| `RuntimeId` | host 侧持久 runtime key；只接受 canonical、小写 UUID | Telegram owner、PID、路径或 namespace handle |
| `ActivationId` | 一次 `RuntimeProcess` 激活的有界、wire-safe 标识；只允许当前 owner 驱动状态转换 | 永久 Workspace 身份或一个 Bot turn |
| `CommandId` | durable invocation 的有界、wire-safe 请求 ID | shell 文本、路径或可执行文件 |
| `Sha256Digest` | 恰好 64 个小写十六进制字符的 SHA-256 摘要 | 未验证的任意 hash 字符串 |
| `CommandIntent` | `RuntimeId`、`CommandId`、调用方语义 hash 和 control-plane canonical payload hash 的绑定 | 完整 transport DTO；`argv`、stdin 与 wire framing 属于外层 |
| `ExecutionBudget` | 正数 wall-clock 与正数合并输出字节上限 | Linux cgroup、RLIMIT 或 filesystem quota 配置 |
| `CommandJournalDecision` | 新执行、已完成回放、hash 冲突、既有结果未知四种确定性决定 | journal 文件格式或 fsync 实现 |

`Runtime` 的真实状态机如下；所有会改变既有 activation 的转换都要求完全相同的
`ActivationId`。这把“旧 handle 在新 activation 后仍可发命令”以及“错误调用方擦除失败
owner”的问题变成类型化的 `activation_mismatch`，而不是靠时间或 PID 猜测。

```text
dormant ─begin_activation(A)→ activating ─mark_ready(A)→ ready
ready ─begin_execution(A)→ executing ─finish_execution(A)→ ready
ready ─begin_stop(A)→ retiring ─finish_stop(A)→ dormant
activating/executing ─begin_stop(A)→ failed[cleanup=A] ─finish_stop(A)→ failed
retiring ─record_stop_failure(A)→ failed[cleanup=A]
failed[cleanup=A] ─begin_stop(A)→ failed[cleanup=A]（同一 owner 重试）
activating ─reject_activation(A)→ failed（establish 未成功，无 cleanup owner）
activating ─quarantine_activation(A)→ failed[quarantine]（establish 结果未知）
```

`failed` 是当前内存聚合的 fail-closed 终态，不是假装可恢复的 runtime。对外 snapshot 仍不
暴露 active activation；但外部资源尚未确认清理时，aggregate 会私有保存 cleanup owner，只有
同一 activation 能重试。broker 清理旧 session 后才以同一持久 `RuntimeId` 构造新的 aggregate；
它绝不把旧 PID 或旧 activation 当成可恢复状态。建立结果未知时，聚合另外保存
quarantine 事实，broker 的 `launch_unknown` recovery ledger 按 `RuntimeId` 禁止后续复用；
通用 `terminate` 不得猜测如何清理这个未知对象。

### 应用层：生命周期端口与补偿

`wspctl::application::RuntimeActivationPort` 是 outbound port（出站端口）：它只声明
`establish(RuntimeId, ActivationId)` 和 `terminate(RuntimeId, ActivationId)`，所以应用层既不知道
namespace/cgroup/mount/socket，也不能把这些 host capability 泄漏到领域层。

`RuntimeActivationService` 将领域转换与外部副作用编排为一个小的 use case（用例）：

```text
activate: begin_activation → port.establish → mark_ready
                     rejected_cleanly ↘ reject_activation
                     cleanup_required ↘ stop → port.terminate
                     outcome_unknown ↘ quarantine_activation
stop:     begin_stop → port.terminate → finish_stop
                            error ↘ record_stop_failure（保留 cleanup owner）
```

建立端口返回封闭的 `std::expected<void, RuntimeEstablishFailure>`，其失败值必须在
`rejected_cleanly`、`cleanup_required` 和 `outcome_unknown` 三种已证明处置中选一。
只有已知部分建立且通用终止效果可清理时才调用 `terminate`；结果未知时必须隔离。
任一步终止效果失败都会使 aggregate 进入 `failed` 并保留清理 ownership；若 `mark_ready`
后置检查失败，服务走同一 `stop` 路径作补偿（compensation）。实际 host adapter 是
`src/wspctl/src/infrastructure/broker.cpp` 内的 `BrokerRuntimeActivationPort`：它执行 cgroup、
OverlayFS、fork-server、PID 1 release 与清理，并保留 native `Error`，避免 domain 为 Linux
I/O 不确定性而反向依赖 infrastructure 错误类型。

### 基础设施与呈现边界

依赖倒置（dependency inversion）在这里不是抽象层数竞赛，而是把不稳定的 host 机制限制在
可替换边缘：

| 层 | 职责 | 当前实现边界 |
| --- | --- | --- |
| `domain` | 纯状态、不变量、值对象和错误分类 | 不包含 Linux、filesystem、socket、pybind header |
| `application` | 用例及其 port | 只依赖 `domain`；不认识具体 sandbox 或 broker |
| `infrastructure` | port 实现、journal、wire codec、runtime gate、immutable image、sandbox、supervisor 与特权 broker | Linux/文件系统/cgroup/seccomp 都留在此层；`protocol.hpp` 是 broker/PID 1 的内部控制 wire codec |
| `presentation` | 进程入口和 Bot-facing adapter | `UnixGatewayClient` 将 presentation DTO（data transfer object，DTO）转换为受验证的控制请求；pybind `RuntimeProcess` 与 `wspctld`/`wsp-systemd`/`wspctl-image` 的 `main` 都在这里 |

`UnixGatewayClient` 每次执行建立一个短 `AF_UNIX SOCK_SEQPACKET` 连接，校验 endpoint 的绝对路径、
长度与嵌入 NUL，并以 `SO_PEERCRED` 验证 root-owned broker。它没有 namespace、mount、cgroup 或
host capability。wire codec 仍在 infrastructure，是因为 broker 和 PID 1 共同使用它；presentation
只公开面向 Bot 的 DTO 与 gateway，避免 Python 直接获得特权控制协议的实现细节。

当前 C++ namespace 也反映这个边界：纯模型为 `wspctl::domain`，用例为
`wspctl::application`，Bot gateway 为 `wspctl::presentation`；基础设施的既有公开类型仍位于
`wspctl` namespace，但其物理目录和 CMake target 已明确标出 infrastructure 层，不能被
domain/application include。

### 最终物理 `src` 布局

实现采用 src-layout（源代码布局）并把 public header 与实现镜像放置；旧的
`src/wspctl/{broker,systemd,image,python,domain,application,infrastructure,presentation}` 平铺目录
不再存在。

```text
src/wspctl/
├── include/wspctl/
│   ├── domain/runtime.hpp
│   ├── application/runtime_activation.hpp
│   ├── infrastructure/
│   │   ├── broker.hpp
│   │   ├── common.hpp
│   │   ├── image.hpp
│   │   ├── journal.hpp
│   │   ├── protocol.hpp
│   │   ├── runtime_gate.hpp
│   │   ├── sandbox.hpp
│   │   ├── supervisor.hpp
│   │   └── xfs_project_quota.hpp
│   └── presentation/unix_gateway.hpp
├── src/
│   ├── domain/runtime.cpp
│   ├── application/runtime_activation.cpp
│   ├── infrastructure/
│   │   ├── broker.cpp
│   │   ├── image.cpp
│   │   ├── journal.cpp
│   │   ├── protocol.cpp
│   │   ├── runtime_gate.cpp
│   │   ├── sandbox.cpp
│   │   ├── supervisor.cpp
│   │   └── xfs_project_quota.cpp
│   └── presentation/
│       ├── unix_gateway.cpp
│       ├── broker/main.cpp
│       ├── image/main.cpp
│       ├── python/bindings.cpp
│       └── systemd/main.cpp
├── CMakeLists.txt
├── __init__.py
├── _native.pyi
└── py.typed
```

`CMakeLists.txt` 按 `wspctl_domain`、`wspctl_application`、`wspctl_infrastructure`、
`wspctl_presentation` 的顺序创建真实 target：前三者分别承载纯模型、用例和 host adapter，
最后一个是含 `unix_gateway.cpp` 的静态库（不是空的 `INTERFACE` 壳）。所有可执行入口都位于
`src/presentation/`；C++ 单元/集成测试和 Python contract 测试全部位于仓库根的 `ctest/`。

## 持久化模型与身份

`workspace.runtimes` 只保存可信 owner 到随机、不透明 runtime key 的一对一映射。
它不把 Telegram ID 直接用作宿主目录名。

| Scope | owner identity | 有意不包含 |
| --- | --- | --- |
| personal | authenticated `user_id` | chat ID、message ID、topic ID |
| group | authenticated group `chat_id` | sender ID、topic ID |

因此群内各 Topic 共享同一个 Workspace；这符合“一群一个 Runtime”，但不同 Topic 的 Bot
worker 可能并发到达。`wspctld` 必须按 runtime key 串行执行 task，不能让两个 OverlayFS
upper layer 或同一 `/workspace` 同时写入。

15 分钟（900 秒）是 broker 的 cache policy，而不是 `Runtime` 聚合中虚构的 `IdleGrace` 状态：
它从最后一个 `RuntimeProcess` lease 释放后开始计时，新控制请求取消 timer；到期时 broker 经
`RuntimeActivationService::stop` 终止 ready activation。broker 重启只执行**冷恢复**：重新验证
manifest、清理 stale activation、重挂 workspace overlay 并启动新的 PID 1。它不承诺恢复 PID、
内存或正在运行的命令；那是 CRIU 级别的另一项能力。

## 执行、journal 与恢复

`run_bash` 是 `workspace.exec` durable effect，而不是普通 read。每个请求包含：

```text
(runtime_key, activation_id, turn_id + invocation_id, request_hash,
 argv, cwd_relative, stdin, timeout, output_limit)
```

现有 PostgreSQL receipt 的顺序是“claim → 外部执行 → finalize”。若 Bot 在命令成功但
receipt finalize 前崩溃，单靠数据库会重放命令。为覆盖这个 crash gap，`wspctld` 在其
root-owned runtime state 下、且在把请求交给 `wsp-systemd` 前维护 fsync 的 command journal：

- 相同 `request_id + request_hash` 返回已固化的 stdout、stderr、exit status；不再执行；
- 相同 `request_id`、不同 hash 是冲突，fail closed；
- supervisor 在运行中崩溃的 journal 项标记为 `outcome_unknown`，不会自动重复可能已产生
  副作用的 Bash；Python 将它固化为当前 receipt 的终态 `outcome_unknown` 结果，而非释放
  claim 重试同一 invocation。后续 Agent turn 若仍需要动作，必须生成新的显式 invocation；
- stdout/stderr 有总字节上限与截断标记，不能以输出洪泛耗尽 broker 内存。当前协议采用
  单条 `AF_UNIX SOCK_SEQPACKET` frame，因此应用层也把可请求合并输出限制为 96 KiB（默认
  64 KiB）；broker 在启动时验证 socket buffer 能承载整个 128 KiB frame，不能把“1 MiB
  合法请求”推到 `EMSGSIZE` 后再留下 pending journal。

数据库 lease 必须严格大于 `run_bash` 的最大 300 秒 timeout 加上 cold activation、supervisor
清理与 receipt finalize 的余量。当前组合根取 8 分钟；workspace RPC 明确选择
`OUTSIDE_TRANSACTION`，不能把长时间 shell 放在 PostgreSQL transaction 中。

`InferenceWorker` 同时建立一次 attempt-local monotonic deadline，并把它作为**易失控制信息**
经 Adapter 与 `ToolExecutionContext` 传到 `run_bash` admission。若剩余时间小于“请求的原始
timeout + 15 秒 journal/cgroup/receipt 余量”，operation 固化
`not_started_deadline_exhausted`，且完全不触碰 native socket 或 journal。它不会把 300 秒
静默裁短为 117 秒，因为那会让同一 `request_id + request_hash` 在重试时代表不同的命令语义。
默认 inference attempt/lease 为 540/600 秒，可覆盖一次正常的模型→300 秒命令→模型收尾；多次
长命令仍由 deadline admission 明确拒绝，而不是由外层 cancellation 生成 pending journal。

### 当前 Turn 附件预处理：`RuntimeProcess.add_file`

`add_file` 不是 Agent tool，也不是 Telegram adapter 直接写入 workspace 的捷径。它是 Python
application 层 `RuntimeProcess` 的第二个能力，与 `run_bash` 共享同一 lazy activation cache、每个
Runtime 的串行锁和全局 admission。模型始终只得到 `run_bash`；因此它不能选择 host 路径、上传
目标、文件 ID，或绕过 runtime 边界。

当前 Telegram `photo`、`sticker`、`document`、`voice`、`audio`、`video`、`animation` 与
`video_note` 的正确时序相同：

```text
Telegram Update → durable Turn / CurrentTurnUploadReference + marker=pending
  →（该 Turn 的 inference 开始、Agent 之前）受限内存下载
  → RuntimeProcess.add_file(AddFileCommand)
  → native journal 的已验证 publish
  → PostgreSQL immutable attachment_import_receipt
  → 同一 DB transaction: marker pending → imported
  → 当前 user model message: <workspace_file path="…" />
  → Agent 只能通过 run_bash 使用该路径
```

`CurrentTurnUploadReference` 是已接受 Update 的受限下载 capability；它只在 application
preprocessor 使用。下载 adapter 复用已初始化的 Telegram Bot，先核验 provider 返回的 identity 和
声明大小，再在内存中复核实际长度及 SHA-256；它不创建临时文件，也不决定 workspace 路径。当前
上限为 8 MiB。`AddFileCommand` 只携带可信派生的个人/整群 scope、opaque ID、稳定 request ID、
完整语义 hash、长度、内容 SHA-256 和至多 64 KiB 的 bytes chunks。文件名、MIME、shebang、Telegram
`file_id` 都不参与目标路径或幂等语义。

durable ingress 在有附件时立即把审计 envelope 的 `content.text` 和 canonical `model_message`
都写成同一个固定 `<workspace_file …/>` 占位符，并在**同一 acceptance transaction** 写入
`workspace_attachment={"version": 1, "state": "pending"}`。这里的文本尚不是模型面对的事实：
普通 ContextWindow（上下文窗口）、compaction（压缩）、retrieval（检索）和 Profile source 对
`pending`、`unavailable`、未知版本或畸形 marker 一律 fail-closed（失败关闭）地隐藏整条行；caption
只在接受路由的短暂解析阶段存在，绝不作为第二条模型消息、WorkingMemory（工作记忆）或 Profile
Dreaming（画像归纳）的文本来源。

native `add_file` journal 的成功**不是**模型可见性的充分条件。Python 必须先核验 native 回执的
request ID、固定 path、字节数和 SHA-256，再用 PostgreSQL 的
`workspace.attachment_import_receipts` 插入不可变 receipt，并在**同一事务**把受控 source user
行从 `pending` 改为 `imported`。数据库 insert trigger（触发器）重新绑定 source message、Turn、
conversation、scope 与固定 path；deferred constraint trigger（延迟约束触发器）在 commit 时再次要求
source marker 已是 `imported`。因此“只写了 native journal”或“只写了看起来像路径的文本”都不能使
路径进入模型。首次 attempt 在 receipt 后显式注入一次当前 user placeholder；重试则去重已投影行后
再注入一次，避免重复。

已知 Telegram 网络/超时失败映射为可重试的 `NETWORK`；超限是 `INVALID_REQUEST`；身份漂移、非 bytes
响应和非 HTTPS provider 路径是终态 `PROVIDER` 契约失败；未知程序错误是 `INTERNAL`，不能伪装成网络
重试。可重试失败保持 `pending`，以 native journal 的同一 request ID 安全回放并重试数据库 publish；
**最终**失败时，fenced inference activity 先在其 transaction 中变为 `failed`，随后同一 transaction
只允许该附件严格 `pending → unavailable`。 `unavailable` 没有 receipt，永不显示路径，也不能在之后
提升为 `imported`；若 receipt 并发已先完成，条件更新零行并保留已见证的 `imported`。

native supervisor 只在该 Runtime 的 OverlayFS `/workspace/uploads/<opaque-id>/payload` 下以
受限 `openat2` 视图写入，`fdatasync` 后原子 publish，并把收据纳入同一类 durable journal。初始
mode 可以是 `0600`，这只是防止 host 控制面将上传内容作为 host executable 使用；**它不是**对
workspace 内执行的禁止。用户上传或 Bot 生成的文件可以在隔离 Workspace 中被 `chmod` 后由
`run_bash` 执行，仍受 namespace、cgroup、seccomp、PID 1 和 OverlayFS 约束。

模型可见的当前 user message 和 `current_user_text` 都是单个
`<workspace_file path="…" />`，但仅在上述 native+数据库双重见证完成后出现；它不会看到原
caption、filename、MIME、Telegram capability 或 bytes。若导入已提交但结果不可判定，activity 以
`partial_effect` 终结而不是重试同一 request ID；暂时的 Telegram/network 或 Runtime 不可用才允许
durable worker 重试。

`0070_workspace_attachment_model_boundary` 处理升级前的历史媒体；
`0071_workspace_attachment_import_receipts` 随后建立 immutable receipt/marker 状态机。两者之前的
每一条 direct-media、rollout marker 或 `current_turn_upload` 旧行都没有**数据库见证**的 `add_file`
receipt，因此 0071 把它们明确终结为 `unavailable`，而不是把看起来像 `<workspace_file>` 的字符串
当成可用路径。canonical
消息可以有多个 text/image part，首段占位符不能证明 bytes 已原子发布到 Runtime。迁移保留 append-only
audit（追加式审计）原文，却把该
附件 Turn 的 user、assistant 与 tool 全链打上 `exclude_from_assistant=true`。私聊中该 raw 内容可能已
被后续纯文本 Assistant 回复复述，因此迁移还会计算从附件 Turn 开始的后续 Assistant Turn 污染闭包；它们
一并从未来模型上下文移除，并删除受影响会话的全部 compaction 链、对应 episodic retrieval（情景检索）
投影、向量任务、Profile evidence/Dream/revision。
旧版 `fetch_group_context` 的结果没有逐读取 provenance（来源证明）：历史群媒体 caption 可能已进入一个
表面上只有文本的 Agent Turn，且无法可靠反向定位。因此迁移还会保守排除**所有历史群 Assistant Turn**
及其 compaction/retrieval；这故意牺牲旧群聊天模型历史，以保证该旁路不会从 assistant 回复或摘要回流。
私聊历史不因这条群旁路规则而被扩大清理。未来检索和画像 source 也把该 marker 当作整 Turn 排除条件。
迁移时必须停掉旧 Bot/worker、排空 inference、compaction、vector 和 Dream 队列；0071 的部署顺序是先
迁移、再启动带 receipt publisher 的新 Bot/worker，提交后重启新进程以清空内存 ContextWindow cache。

群消息观察器是另一条只读旁路，不能取得当前 Turn 的 import receipt。它会把任何历史或新入站群媒体
统一投影为 `<group_attachment />`（非可执行、非路径标记），而不是 `<workspace_file>`；`fetch_group_context`
只能返回这个标记。当前直接调用 Bot 的附件才拥有上述真实 workspace 路径。

## 文件系统与 OCI image identity

项目 `.venv`、宿主 Python 和宿主 `/usr` 都不是 workspace runtime。唯一构建定义是
[`deploy/wspctl/image/Containerfile`](../deploy/wspctl/image/Containerfile)：它以 digest 固定
Python 3.14 base 和 Debian snapshot，在相同 ABI 的 builder stage 编译 `wsp-systemd`，并在
runtime stage 由 Debian package manager 安装 Bash、GNU 工具和全部动态库。contract v3 还提供
`python3`、`curl`、`wget`、`git`、`jq`、`gcc`、`g++`、Node.js 24 LTS、pnpm 11、FFmpeg、
ImageMagick、SQLite、`htop`、`tree`、`neofetch` 与 OpenJDK 17（含 `java`/`javac`）。
Node.js/pnpm 与 builder wheel 一样先在 host 端按 SHA-256 验证，再作为本地 build context 输入；
Debian 包仍由固定 snapshot 解析。最终产物是标准 OCI image layout（OCI 镜像布局），不是临时
复制出的 Python 目录。

OCI image manifest digest 是唯一 identity。tag、短 hash、任意 generation 名和启动时计算的
fingerprint 都不会进入 broker。物化后的 rootfs 只保留第二个本地 seal，用来发现解包结果或磁盘内容
被修改；它不是另一套版本号。标准路径为
`<images-root>/sha256/<manifest-hex>/rootfs`。

runtime mount namespace 的顺序如下：

1. 立即把 `/` 设为递归 `MS_PRIVATE`，防止默认 shared mount 向 host 传播；
2. 用 `pivot_root` 建立只含授权对象的 rootfs；不把 `chroot` 当安全边界；
3. 将完整 OCI rootfs bind 并 remount 为 `ro,nosuid,nodev`；不暴露 host `/`、`/home`、
   `/proc`、`/sys`、`/run`、container socket 或宿主设备；
4. pivot 前在私有 `/run` 固定专用 LXCFS mount；pivot 后新挂完整 `/proc`，逐文件覆盖
   cgroup-aware 性能节点，再 detach 整个 `/run` staging；受限 `/dev` 与上限 1 GiB 的 `/tmp`
   仍为私有 tmpfs，且 `/tmp` 实际内存同时计入 Runtime memory cgroup；
5. 只将 `/workspace` 设为可持久化 OverlayFS 上层，当前每 Runtime hard limit 为 4 GiB；
   Python/GNU base 始终由只读 bind 覆盖；
6. activation 在 `pivot_root` 前把 merged workspace 收敛为 `agent:agent 0700`。迁移器不跟随
   symlink，接受新建的 `root:root`、旧版 `65534:65534` 与已迁移的 Agent inode，逐子树迁移且
   最后提交根 inode，所以中断后可以安全重试；其他 owner 一律失败关闭。

每个 Runtime 同时获得独立的 UTS、network、IPC、PID、mount 与 cgroup namespace。broker 在进入
新的 UTS namespace 后把 syscall hostname/domainname 固定为 `workspace`/`localdomain`；镜像内
`/etc/hostname`、`/etc/hosts` 与仅含说明的离线 `/etc/resolv.conf` 保持相同身份，不回显构建机或
宿主名称，`hostname -f` 稳定返回 `workspace.localdomain`。network namespace 不配置接口、地址、
route 或 resolver，payload 的 seccomp 还以
`EPERM` 拒绝 `AF_INET`、`AF_INET6`、`AF_NETLINK`、`AF_PACKET` 与 `AF_VSOCK` socket family；
`AF_UNIX` 保留给本地进程通信。这两个边界是互补的：network namespace 隔离网络状态，socket
filter 防止 payload 自行配置或探测该 namespace。

`/proc` 使用完整 procfs，不再使用 `hidepid` 或会删除所有顶层诊断节点的 `subset=pid`。PID
namespace 已经排除了宿主进程，因此 Agent 可以读取 `/proc/self`、Runtime PID 1 的 `comm`、
同一 Runtime 内的进程。`cpuinfo`、`diskstats`、`loadavg`、`meminfo`、`slabinfo`、`stat`、
`swaps` 与 `uptime` 是强制核心能力，由 wspctl 专用 LXCFS（Linux Containers Filesystem）按
发起读取的进程所在 cgroup 动态生成，并以逐文件 `ro,nosuid,nodev,noexec` bind 覆盖宿主
procfs。runtime cgroup 同时把 `cpuset.cpus` 固定为 `ceil(cpu.max quota / period)` 个 delegated
CPU（不超过宿主可用 CPU），并保留 `cpu.max` 作为时间份额硬上限；CPU 选择由 runtime key
稳定散列分散。因此 LXCFS 5.x 也会让编译器、JVM、Python、`htop` 看到与实际可并行调度范围一致的
CPU 数，而不是宿主总量；小于 1 CPU 的 quota 显示 1 CPU，但仍受 `cpu.max` 的小数份额约束。
`pressure/{cpu,io,memory}` 作为不可拆分的 PSI（Pressure Stall Information）能力组协商：
较新 LXCFS 完整提供三项时全部动态映射；LXCFS 5.0 等版本完全不提供时，整个 `/proc/pressure`
被遮蔽；只提供一部分视为损坏并 fail closed。任何情况都不会回落到宿主全局 PSI。
`version`、`filesystems` 与当前 network namespace 的 `/proc/net` 继续来自该 namespace 的
procfs，root-only 文件仍由标准 DAC/ptrace 权限保护。

没有可靠 cgroup 虚拟化语义的 host-global `vmstat`、`zoneinfo`、`vmallocinfo`、`softirqs`、
`schedstat`，以及 boot identity、内核符号/module/key、硬件与磁盘拓扑等节点，以不可读空 inode
做 bind mask；`/proc/bus`、`/proc/fs`、`/proc/irq` 与 `/proc/sys` 另做递归只读 bind。LXCFS 与
mask 来源都位于临时私有 `/run`，逐文件 bind 完成后立即 detach，Agent 看不到用于构造策略的
source path。LXCFS 缺失、不是 root-owned FUSE mount、任一核心动态节点缺失/返回空数据，或
可选能力组只出现部分节点时，broker fail closed，不退化为宿主数据或静态快照。
同一 Runtime 的 task 是一个信任域；若将来需要互不可信的并发 Agent，必须分配不同 Runtime，
不能把 procfs mount option 当成 task 间安全边界。宿主 sysfs 完全不挂载，因此
`/sys/class/hwmon` 等硬件传感器与设备拓扑不可见。

payload 使用镜像内持久命名的低权限 `agent`（UID/GID 65533），`HOME=/workspace`；不再使用
Linux 的特殊 overflow/nobody 身份 65534。identity 负责文件访问和稳定 ownership，cgroup 负责
整个 Runtime task subtree 的 CPU、memory、PID 与 I/O 资源边界，两者职责不同。
task child 先完成 Agent identity/capability/seccomp 收缩，再从 PID 1 固定的 workspace FD
逐分量解析 cwd，并通过 `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_MAGICLINKS|
RESOLVE_NO_XDEV)` 与 `fchdir` 进入目录。不存在的 cwd、symlink、mount crossing 或任何非
`/workspace` 路径都以 126 失败，不会退回 workspace root，也不再依赖无 DAC override 的 root
穿越 `agent:agent 0700`。

runtime contract 至少包含上述工具、`/bin/bash`、
`/usr/local/libexec/wspctl/wsp-systemd` 和 `/workspace`、`/proc`、`/dev`、`/tmp`、`/run`。
native sealer 同时验证平台、固定入口、上述静态身份文件、仅使用 `files` 的 NSS host lookup、
root ownership、regular file 的 set-id bit、file capability、特殊 inode、symlink 的容器根语义和
`site-packages` 为空。标准镜像中的绝对 symlink 按 pivot 后的容器根解释；
只有词法上逃出 rootfs 的链接才拒绝，不能用宿主 `Path.resolve()` 错解。

当前 `wspctl-image --verify` 只验证由受控发布流程生成的 manifest 和 rootfs digest，故意不接受
Bot 传入的目录来“现场制镜像”。用户上传文件也不在本 API 中以 host bind 的形式进入 runtime：
当前的 `RuntimeProcess.add_file` 将 bytes 复制到该 Runtime 的固定
`/workspace/uploads/<opaque-id>/payload`；它绝不把上传文件原路径、artifact store 或任意 host
directory 交给 mount policy。这里的 `payload` 仅是 native wire/journal 的内部名，不是 Agent tool
或用户可选文件名。

OverlayFS 的 `upperdir` 和 `workdir` 必须在同一文件系统且 `workdir` 初始为空；两个 runtime
不得共享或重叠它们。内核文档把重叠 upper/work 行为定义为未定义，见
[OverlayFS 文档](https://docs.kernel.org/filesystems/overlayfs.html)。不对不可信层启用
`metacopy=on`；使用 `metacopy=off,redirect_dir=nofollow,index=off` 的保守策略。

持久 workspace 还必须有真正的 per-Runtime filesystem hard limit（文件系统硬上限）；`memory.max`、
`pids.max` 与 IO 限速不能限制已落盘的 bytes 或 inodes。本项目的唯一目标后端是专用 XFS mount 上的
强制 project quota，使用每个 Runtime 的 control/workspace project-ID pair；完整的状态布局、恢复与
迁移门槛见 [XFS project quota 生产容量契约](wspctl-xfs-project-quota.md)。当前 native backend 已在
admission 前创建/读回 pair，并将 journal 放入 control project；没有 dedicated XFS、强制 project quota
或完整 hard-limit readback 时 broker 拒绝处理不可信 payload。默认 CTest 不伪造该内核语义；上线必须在
一次性 XFS mount 上运行 `wspctl.xfs_project_quota`，确认 bytes 与 inodes 两种超限都返回 `EDQUOT`。

## cgroup、seccomp 与信号

`wspctld.service` 应由 systemd 以 `Delegate=cpuset,cpu,memory,pids,io` 启动，并遵守 cgroup v2
“inner node 无进程、single writer”规则：broker 本身位于 `manager/` leaf，runtime 边界为
无进程父节点，supervisor 与每一个 task 位于 sibling leaf。

- unit 使用 `Type=notify`；`wspctld` 在 Bot/operator listener 和 bounded worker pools 全部
  建立后才发送 `READY=1`。因此旧 generation 的 stale Unix-socket pathname 不能让
  `systemctl restart` 提前返回；
- `NotifyAccess=main` 只接受 main PID 的 readiness，daemon 随即清除 `NOTIFY_SOCKET`，不把
  service-manager notification capability 传给 fork-server、supervisor 或 task；
- 部署验收不创建 Runtime。发布期 native seal 与启动前 `wspctl-image --verify` 静态核对
  contract-v3 的具名 Agent、`/workspace` lower、supervisor、动态库和全部基础工具；service
  阶段只验收 `Type=notify`、两类 socket metadata 与稳定 `InvocationID`。namespace、quota、
  OverlayFS、降权和 seccomp 属于真实请求路径，由 privileged E2E 与正常 Runtime 请求验证，
  不再作为安装事务的瞬时前置条件。

```text
wspctld.service
├─ manager/                         ← wspctld
└─ runtimes/<opaque-key>/            ← no processes
   ├─ supervisor/                    ← wsp-systemd PID 1
   └─ task/                          ← command descendants
```

broker 不会在异步边界把可复用的 host PID 写入 `supervisor/cgroup.procs`：fork-server launcher 在 fork
namespace PID 1 前通过预打开 FD 写入自身的 `0`，因此 PID 1 继承 supervisor leaf。task child 则由
`wsp-systemd` 在 fork 后、start barrier（启动屏障）放行前写入 task leaf；此时 parent 仍持有 child，
其 PID 不可能先被回收复用。

host service 的 `CAP_SETPCAP` 只是一项启动期 handoff capability（交接能力）。broker preflight
在发布 socket 前验证它存在；每个 `wsp-systemd` PID 1 随后用它锁定 `NOROOT`、
`NO_SETUID_FIXUP`、keep-caps 与 ambient-raise securebits，并按运行中内核的 capability 上界把
bounding set 收口为 `CAP_SETUID/CAP_SETGID/CAP_KILL`，最后先从 bounding set、再从
permitted/effective sets 丢掉 `CAP_SETPCAP`。因此 payload identity drop 仍可完成，但 PID 1
及其后代不能从 UID 0、ambient capability、file capability 或较新的未知 capability 恢复权限。
OCI sealer 对 file capability/set-id 的拒绝与 `PR_SET_NO_NEW_PRIVS` 是额外的独立防线。

broker 在每个 Runtime 的 task cgroup 上设定 `pids.max`、`memory.high`、`memory.max`、CPU/IO
限额；该边界覆盖命令 fork/exec 出来的完整进程子树，不要求 Agent 再感知或实现一套重复限制。timeout
先请求 task process group 正常退出；宽限后优先写 `cgroup.kill`，缺少该特性时以 pidfd 与
进程组信号回退，并由 PID 1 最终 reaping。cgroup v2 的 delegation 与 `cgroup.kill` 语义见
[Linux kernel cgroup v2 文档](https://docs.kernel.org/admin-guide/cgroup-v2.html) 和
[systemd cgroup delegation](https://systemd.io/CGROUP_DELEGATION/)。

payload 在 mount/rootfs 完成后必须 drop capabilities，设置 `PR_SET_NO_NEW_PRIVS`，再安装
角色化 seccomp filter。第一版允许 Bash/Python 正常 `fork`/`exec`，但拒绝
`unshare,setns,mount,umount,pivot_root,bpf,keyctl,perf_event_open,userfaultfd,ptrace` 及
创建任何 `CLONE_NEW*` namespace 的 clone flags。seccomp 仅缩小 syscall attack surface，
不是单独的 sandbox；详见
[seccomp filter 文档](https://docs.kernel.org/userspace-api/seccomp_filter.html)。

## 构建与测试

Python wheel 由 `scikit-build-core` 驱动 CMake；`pybind11` 是 build dependency，不能只放在
开发依赖中。开发构建示例：

```bash
uv sync --group dev
cmake -S . -B build -G Ninja -DPython_EXECUTABLE="$PWD/.venv/bin/python"
cmake --build build
ctest --test-dir build --output-on-failure
uv build
```

所有新增 Runtime 测试在 `ctest/`：纯 C++ 测试覆盖协议、journal、状态机、quota fail-closed
configuration 与 timeout；无特权 Python contract 测试由 CTest 调用；需要 overlay/cgroup delegation/
root broker 的 integration 测试必须显式 capability gate。特别是 `wspctl.xfs_project_quota` 默认 skip，
只有提供 disposable XFS mount 时才验证真实 `EDQUOT`，默认 CI 不得通过关闭隔离来“假装成功”。

## 发布迁移

迁移 `0069_workspace_runtimes` 先拒绝未排空的 inference activity 和任何未成功的旧
`execute_python_code` receipt，再创建 lazy owner→opaque-key 映射；不会预先为历史所有
用户/群组制造空目录。成功的历史 Judge0 receipt 仅作为审计历史保留，不能被新 catalog
重放。部署时先停止旧版本、排空队列、备份数据库、执行迁移、启动 `wspctld`，最后启动新
Bot；缺少 broker、base manifest、cgroup delegation 或 socket ACL 时 `run_bash` 必须报错而非
回退到宿主 `subprocess`。
