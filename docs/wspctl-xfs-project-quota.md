# wspctl：XFS project quota 的生产容量契约

这是当前 native quota backend（原生配额后端）的部署与实现契约，不是“可以先用 `du` 顶着”的
临时建议。`run_bash` 面向不可信 payload；若 persistent OverlayFS upper layer 没有由内核执行的
byte 和 inode hard limit（硬上限），它就能以大文件、空文件、whiteout 或目录项耗尽 host state
filesystem。当前 broker 已把 XFS-only preflight、持久 project-pair registry、`PROJINHERIT`、
`Q_XSETQLIM`/读回和 runtime-local journal 接入启动路径：没有满足该契约的 mount，`Broker::create`
与新 Runtime admission 都会 fail closed（失败关闭）。

默认 CTest 不会假装在普通开发目录验证内核配额；`wspctl.xfs_project_quota` 只有在 operator/CI 明确
提供一次性 dedicated XFS mount 时运行，并实际要求 byte 与 inode 两种超限都得到 `EDQUOT`。生产上线
前必须让这一条 privileged integration test（特权集成测试）在目标内核和挂载配置上通过；它不是可以
用 ext4、`du` 或一次“看起来能写文件”的 smoke test 代替的门槛。

这是 **runtime writable storage（运行时可写存储）** 的资源边界：它限制一个 Runtime 能消耗的 XFS
blocks 和 inodes，不试图把 host/Bot 控制面是否可信重新定义为 quota 问题。即使 host 的 builder、broker
和 Bot 业务代码都已在 TCB，彼此独立的 payload workspace 仍需要这个可验证的容量隔离边界。

本版本只选择一个具体后端：**dedicated XFS filesystem（专用 XFS 文件系统）+ 强制 project quota
（`prjquota` 或 `pquota`）**。不接受 ext4、btrfs qgroup、普通 user/group quota、`du` 轮询、定时
cleanup 或“通用 quota plugin”作为等价替代。XFS 的 `pquota/prjquota` mount option 会开启 project
quota accounting 与 enforcement；`pqnoenforce` 只统计而不强制，必须拒绝。内核文档见
[XFS mount options](https://www.kernel.org/doc/html/latest/admin-guide/xfs.html)，而
[`xfs_quota(8)`](https://man7.org/linux/man-pages/man8/xfs_quota.8.html) 明确 project quota 可限制
目录树的 block 与 inode 使用量。

## 不变量与非目标

| 不变量 | 生产含义 |
| --- | --- |
| 专用 mount | `WSPCTL_STATE_ROOT` 必须就是或严格位于一个专用 XFS `rw,prjquota`/`rw,pquota` mount；images、Bot checkout、Docker data、其他服务 state 不得共用该 filesystem。 |
| 两个 project | 每个 opaque Runtime key 有一对持久 ID：`control_project_id` 管 control/journal/staging，`workspace_project_id` 管 upper 与 OverlayFS work。ID `0` 不可用，因为 XFS 不对 project ID 0 强制 limit。 |
| 配额树闭合 | 每个 project root 都设置 project ID 与 `PROJINHERIT`；任何新 inode 继承同一 ID。目录 project inheritance 是 XFS project quota 的必要语义，参见 [XFS inode flags](https://man7.org/linux/man-pages/man2/ioctl_xfs_fsgetxattr.2.html)。 |
| 双 hard limit | 每个 project 同时有 `bhard`（bytes/block hard limit）和 `ihard`（inode hard limit）；只设 bytes 不能防空文件/目录/whiteout 洪泛。 |
| 全局 admission | broker 持久化预留 ledger；新 Runtime 只有在 bytes 与 inodes 的全局预算都还能容纳其两个 hard limit 时才创建。project quota 本身不把空间预留给未来 Runtime。 |
| 失败关闭 | 任一 mount、project assignment、inherit flag、hard-limit readback、registry consistency 或 cleanup 检查失败时，Runtime 不得 Ready，也不得退回到无配额 upperdir。 |

`du` 不满足这里的任何 hard enforcement 语义：它只观察已经写入的目录树，和 allocation 并发竞速，
也无法成为 kernel allocation path 的拒绝点；hardlink、OverlayFS metadata 与 inode exhaustion 还会让
事后统计更不可信。`io.max`/`io.weight` 是吞吐控制，不是磁盘容量控制；`memory.max` 也不限制文件
系统写入。

## 目标 state layout 与两个 project ID

目标 layout 刻意把两类可增长状态分开：

```text
WSPCTL_STATE_ROOT/                         # dedicated XFS mount
├── quota-registry/                         # root-owned, fsync'd allocator/ledger; not payload-visible
│   ├── lock
│   ├── next-id
│   └── runtimes/<hashed-runtime-key>       # (runtime_key, control_id, workspace_id, state)
└── runtimes/<hashed-runtime-key>/
    ├── control/                            # control_project_id, PROJINHERIT
    │   ├── journal/                        # durable command receipts
    │   └── mounts/<activation>/
    │       ├── root/
    │       └── workspace-lower/
    └── workspace/                          # workspace_project_id, PROJINHERIT
        ├── upper/                          # persistent OverlayFS upperdir; activation 后 agent:agent 0700
        └── work/<activation>/              # fresh, empty OverlayFS workdir
```

`workspace/upper` 与 `workspace/work/<activation>` 必须留在同一 XFS filesystem，满足 OverlayFS
对 `upperdir`/`workdir` 的同一 filesystem 要求；它们也必须使用**同一个** workspace project ID，
这样 workdir 的临时 copy-up 和 upperdir 的持久内容一起受同一个硬上限约束。`control/` 使用另一 ID，
避免 command journal/staging 的小而关键状态被大 workspace 写入挤掉。

native layout 已使用这棵树；`Journal` 不再创建 global `WSPCTL_STATE_ROOT/journal/`，且 task layer
不再使用 `runtimes/<key>/workspace-upper` 或顶层 `mounts/`。这也是一个显式 migration gate（迁移闸门）：
preflight 一旦发现上述任一旧路径便拒绝启动，绝不静默删除、移动或把旧 upperdir 误标为新 project。

本轮**没有**把旧 Overlay upperdir 原地转换成新 project pair 的自动迁移器。安全的受支持流程是停止
broker、备份/归档旧 state、在新 dedicated XFS mount 上建立空的 root-owned state root，然后让 broker
创建新的 registry 和 Runtime trees。旧 workspace 内容若要恢复，必须由 root-owned 的后续迁移工具在
离线状态下逐 inode 验证 project ID、`PROJINHERIT`、limit readback 与 journal receipt 一致性；在该工具
存在之前，手工 copy 旧目录后直接启动是不可接受的，因为 registry 无法证明其 project identity。

这里的“旧 layout/project pair 迁移”与 v2→v3 的 **owner migration** 不同：如果 Runtime 已经处于
当前受验证的 XFS layout 和 project ID 下，activation 会在排他锁与空 task cgroup 内，把 upper 中
`root:root`、旧 `65534:65534` 和部分已迁移的 inode 可重入地收敛到 `agent:agent`。它不会移动目录、
改 project ID 或绕过 quota readback；发现第四种 owner 时失败关闭。

一个 Runtime 的 ID pair 在 root-owned、fsync 的 registry 中保存为 `(runtime_key,
control_project_id, workspace_project_id, state)`，而不是从 Telegram ID、路径或截断 hash 推导。建议从
一个非零、偶数对齐的受限 range 单调分配：`control = first + 2*n`，`workspace = control + 1`。只有在
递归清理成功、两个 tree 已解除 project control、并且 quota usage 被证明为零后，才可设计受控复用。
当前实现更保守：ID 从不自动复用；任何崩溃/不确定状态进入 quarantine（隔离池），不能猜测释放。

## 必需的 host 配置字段

这些字段由当前 `wspctld` CLI 强制要求，并由 root-owned systemd environment file 提供；它们不应进入
Bot `config.json`，也不应由 Telegram、Agent 或 Python runtime 覆盖。

```ini
# Only accepted production backend; absence/other value means fail closed.
WSPCTL_QUOTA_BACKEND=xfs_project_v1

# Existing dedicated XFS mount with rw,prjquota (or rw,pquota), not pqnoenforce.
WSPCTL_XFS_QUOTA_MOUNT=/srv/fogmoe-wspctl/state
WSPCTL_STATE_ROOT=/srv/fogmoe-wspctl/state

# Nonzero inclusive project-ID range.  It contains complete control/workspace pairs.
WSPCTL_XFS_PROJECT_ID_MIN=100000
WSPCTL_XFS_PROJECT_ID_MAX=199999

# Per Runtime hard limits.  Both values of each pair are mandatory.
WSPCTL_RUNTIME_CONTROL_HARD_BYTES=16777216
WSPCTL_RUNTIME_CONTROL_HARD_INODES=8192
WSPCTL_RUNTIME_WORKSPACE_HARD_BYTES=4294967296
WSPCTL_RUNTIME_WORKSPACE_HARD_INODES=131072

# Persisted admission reservations must stay below usable XFS capacity.
WSPCTL_XFS_GLOBAL_ADMISSION_BYTES=53687091200
WSPCTL_XFS_GLOBAL_ADMISSION_INODES=6291456
WSPCTL_XFS_SYSTEM_RESERVE_BYTES=4294967296
WSPCTL_XFS_SYSTEM_RESERVE_INODES=262144
```

Preflight 必须验证以下关系，而不是静默 clamp：

```text
project_id_min > 0
project_id_max >= project_id_min + 1
number_of_ids is even
control_hard_bytes > 0 ∧ control_hard_inodes > 0
workspace_hard_bytes > 0 ∧ workspace_hard_inodes > 0
global_admission_bytes + system_reserve_bytes ≤ usable_XFS_bytes
global_admission_inodes + system_reserve_inodes ≤ usable_XFS_inodes
```

对一个新 Runtime，持久 ledger 采用保守预留：

```text
reserve_bytes = control_hard_bytes + workspace_hard_bytes
reserve_inodes = control_hard_inodes + workspace_hard_inodes

admit ⇔ used_reservations + reserve ≤ global_admission_budget
```

其中 bytes 与 inodes 必须分别成立。这样即使所有已接纳 Runtime 同时冲向 `bhard`/`ihard`，仍保留
`SYSTEM_RESERVE` 给 XFS metadata、broker cleanup 和 operator recovery。达到预算时返回明确的
`quota_admission_exhausted`，而不是创建一个没有 project limit 的 Runtime。长期不活跃 Runtime 的
显式 archival/deletion 是释放 reservation 的唯一正常路径。

## 原子 provisioning 与恢复顺序

native backend 的顺序是：

1. 在全局 quota registry lock 下读取并恢复 reservation ledger；发现重复 key、半写 pair、未知 project
   或已超预算，立即拒绝服务而不是自动修复。
2. 为新 key 预留两个 ID 和 bytes/inodes；将 `allocating` state fsync 到 registry。
3. 在 dedicated XFS 下创建尚未对 payload 可见的 `control/`、`workspace/` roots；设置 project ID、
   `PROJINHERIT` 与两个 `bhard`/`ihard`，然后通过内核/`xfs_quota` 等价 API 读回验证。
4. 创建 journal、upper 和每次 activation 的 workdir；逐个读回 project ID、`PROJINHERIT` 与 hard
   limit。仅此时把 Runtime 记为 `ready` 并允许 Overlay mount。
5. 任一步失败：已持久化的 `allocating` record 或部分 tree 进入 `quarantined`；不会猜测清理成功、不会
   回收 pair、更不会创建无 quota upperdir。operator 必须显式恢复或在新 state root 重建。

同一过程也适用于 broker restart：registry 记录的是 durable identity；PID、cgroup 和 mount 是易失的。
恢复时不能给已有 Runtime 换 project pair，也不能因为某个 `xfs_quota` 命令报错而忽略配额继续启动。

## Operator preflight 与验证命令

下面命令用于 operator 验证，不是给 Bot 的 tool。路径/ID 取自 root-owned broker config 和 registry，
不得由 chat input 拼接。

```bash
quota_mount=/srv/fogmoe-wspctl/state
state_root=/srv/fogmoe-wspctl/state

# Expected: FSTYPE xfs; OPTIONS contains prjquota or pquota, never pqnoenforce.
findmnt --noheadings --output TARGET,FSTYPE,OPTIONS --target "$quota_mount"
test "$(findmnt --noheadings --output FSTYPE --target "$quota_mount" | tr -d ' ')" = xfs
findmnt --noheadings --output OPTIONS --target "$quota_mount" | grep -E '(^|,)(prjquota|pquota)(,|$)'
if findmnt --noheadings --output OPTIONS --target "$quota_mount" | grep -Eq '(^|,)pqnoenforce(,|$)'; then
  echo 'project quota enforcement is disabled' >&2
  exit 1
fi

# Expected: project quota accounting and enforcement are ON.
sudo xfs_quota -x -c state "$quota_mount"
```

对已经创建的 Runtime（示例 ID 仅作说明）执行：

```bash
control_id=100000
workspace_id=100001
runtime_root="$state_root/runtimes/<hashed-runtime-key>"

# Reports block/inode usage and hard limits for both projects.
sudo xfs_quota -x -c "quota -p -b -i -N $control_id $workspace_id" "$quota_mount"
sudo xfs_quota -x -c "report -p -h" "$quota_mount"

# Checks project-ID/inheritance consistency recursively; empty output means the tree is consistent.
sudo xfs_quota -x -c "project -c -p $runtime_root/control $control_id" "$quota_mount"
sudo xfs_quota -x -c "project -c -p $runtime_root/workspace $workspace_id" "$quota_mount"

# Directly displays each directory's project ID; xfs_io is an operator diagnostic, not broker logic.
sudo xfs_io -c lsproj "$runtime_root/control"
sudo xfs_io -c lsproj "$runtime_root/workspace"
```

`xfs_quota -p` 能以 numeric project ID 和 `-p path` 操作而无需修改全局 `/etc/projects`/`/etc/projid`；
这降低了多个服务竞争全局映射文件的风险。其命令格式与 project tree consistency check 见
[xfs_quota(8)](https://man7.org/linux/man-pages/man8/xfs_quota.8.html)，而
[`projects(5)`](https://man7.org/linux/man-pages/man5/projects.5.html) 也说明静态 mapping 文件不是必需的。

## 明确拒绝的“看似可用”方案

- **`du` / periodic scan**：仅事后观测，不能阻止 allocation，也不提供 inode hard limit。
- **仅 global disk volume limit**：一个 Runtime 仍可耗尽整个 volume，破坏其他 Runtime 和 broker journal。
- **只配 `bhard`**：空文件、目录与 Overlay whiteout 仍能耗尽 inode。
- **`pqnoenforce`、soft limit/grace-only policy**：不满足攻击路径上的 hard boundary。
- **ext4 / generic quota fallback**：本设计没有为它们定义等价的 project-tree/inheritance/verification 契约；
  native preflight 必须拒绝，而不是以“best effort”继续。
- **复用 project ID 而不验证清理**：stale inode 可以把新用户的 workspace 计入旧配额，或反过来造成
  quota bypass/错误拒绝。

这份约束故意比“能配置 xfs_quota”严格：真正需要保护的是每个长期 Runtime 的可用性边界，而不是
某一次 shell 的临时目录大小。
