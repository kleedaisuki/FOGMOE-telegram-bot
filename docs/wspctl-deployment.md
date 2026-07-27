# wspctl 的主机 Broker 与 Docker Bot 部署

这一部署将权限边界拆成两个独立的故障域：`wspctld` 是 Linux 主机上 root-owned 的
systemd 服务；Docker Compose 中只有普通 Bot client。两者唯一共享的对象是一个 Unix
`SOCK_SEQPACKET` socket。不要将 broker 加入 `docker-compose.yml`，也不要给 Bot 容器
`privileged: true`、`CAP_SYS_ADMIN`、host PID namespace、Docker socket 或任意宿主目录。

Bot 容器的授权视图刻意很小：

| 对象 | Bot 是否可见 | 约束 |
| --- | --- | --- |
| 开发态 `./.wspctl/run/wspctld.sock`（容器内 `/app/.wspctl/run/wspctld.sock`） | 是 | 仅以只读目录 bind mount 暴露；socket 是 UID `65532` 的 `0600` 文件 |
| Docker/containerd socket | 否 | 不挂载 `/var/run/docker.sock`、`/run/containerd/containerd.sock` 或其父目录 |
| host cgroup control | 否（可写） | 不显式 bind mount `/sys/fs/cgroup`，更不允许可写 cgroup delegation；container runtime 的普通只读自身 cgroup metadata 不构成控制权 |
| host kernel/modules | 否（特权视图） | 不挂载 `/lib/modules`、`/usr/lib/modules` 或 `/dev/kvm`；container runtime 的普通受限 sysfs 视图不授予 module/control 权限 |
| host state/image roots | 否 | 不挂载 `WSPCTL_HOST_WORKDIR`、artifact store 或 broker 环境文件 |

本版本的 untrusted input（不可信输入）限定为用户上传内容以及 Bot/Agent 生成的 command payload；
host kernel、systemd、root-owned broker 配置和受控 image builder 是 trusted control plane（可信控制
平面）。Compose 同时设置 `cap_drop: [ALL]`、没有 `cap_add`，并启用 `no-new-privileges`；Bot 因此
不能 mount namespace、加载 kernel module、管理 cgroup 或自行启动有意义的 broker。主机被 root 级
攻破或可信控制平面被替换不属于 Bot 可缓解的威胁模型。

## 工作根：开发态视图与生产态边界

`wsp-systemd` 是 runtime 内的 PID 1 可执行程序；它的名字不变。这里调整的是仓库根目录曾经
过于泛化的 `./systemd/` **部署资产目录**：它们现在位于
[`deploy/wspctl/systemd/`](../deploy/wspctl/systemd/)，清楚表达“wspctl 的 host deployment
adapter”，而不是一个与任意服务混在一起的顶层目录。

开发者可让本机可见的 workspace 工件位于 checkout 相邻、被 `.gitignore` 排除的
`./.wspctl/`。为避免“看起来在本地”被误读为“可安全用于 production”，CMake 把它做成一个
**显式 opt-in**：

```bash
cmake -S . -B build/wspctl-dev \
  -DWSPCTL_INSTALL_HOST_TOOLS=ON \
  -DWSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON
```

此模式会向 native broker 传入 `--allow-insecure-dev-root`。socket 的所有权与 mode 不会放宽：broker
仍要求 socket parent 为 `root:root` 且不可 group/other 写，socket 仍为 UID `65532` 的 `0600`。该模式
只是明确把 local developer 纳入 trusted control plane，不能用于多用户 host 或 production。

生产构建不允许隐式选择该目录，必须给出绝对 host root：

```bash
cmake -S . -B build/wspctl-prod \
  -DWSPCTL_INSTALL_HOST_TOOLS=ON \
  -DWSPCTL_HOST_WORKDIR=/srv/fogmoe-wspctl
```

`WSPCTL_HOST_WORKDIR` 必须是绝对路径，且从 `/` 到该目录的每个祖先都应为 `root:root`、不可由
group/other 写。可按运营策略使用 `/srv`、`/var/lib` 或专用 volume；重要的是这个 ownership
invariant（所有权不变量），而非某一个目录名字。该值是 CMake 生成时固定进 unit 的 host control
plane，永远不从 Bot `config.json` 读取。

这里的 `WSPCTL_HOST_WORKDIR/state` 不是普通子目录：生产必须先把它作为**独立的 XFS mountpoint**
挂载，启用 `rw,prjquota`（或 `rw,pquota`）且不带 `pqnoenforce`。`images/`、artifact store、Bot
checkout 与其他服务 state 不得位于该 filesystem。生成的 unit 使用 `RequiresMountsFor=` 与
`ConditionPathIsMountPoint=` 要求这个 mount 已经存在；它不会为了“方便启动”在父文件系统上创建一个
同名目录。开发态若希望使用 `./.wspctl/`，也必须把 `./.wspctl/state` 单独挂成符合相同契约的 XFS，
否则 native preflight 应拒绝执行不可信 payload。

## 先决条件与身份契约

主机必须具备 Linux namespace、OverlayFS、cgroup v2 和 seccomp，并由 systemd 将
`cpu,memory,pids,io` 委派给 `wspctld`。`WSPCTL_INSTALL_HOST_TOOLS=ON` 的 CMake build 会从
[`deploy/wspctl/systemd/wspctld.service.in`](../deploy/wspctl/systemd/wspctld.service.in) 和
[`deploy/wspctl/systemd/wspctld.env.example.in`](../deploy/wspctl/systemd/wspctld.env.example.in)
生成实际文件至，例如 `build/wspctl-prod/deploy/wspctl/systemd/`；不要直接安装 `.in` 模板。
`cmake --install` 只把生成资产放进安装前缀的 `share/fogmoe-wspctl/systemd/`，不会静默创建
host work root 或激活 service。生产 operator 应像下面一样显式复制/链接 unit。将生成的
`wspctld.env.example` 复制为
`WSPCTL_HOST_WORKDIR/wspctld.env`，以 `root:root`、`0600` 保存，并把 generation 路径、资源
上限替换为实际值。`WSPCTL_SUPERVISOR` 是 `pivot_root` 后的 **image-internal** 路径，
不是 host 上 `wspctld.service` 的可执行文件路径；受控 image build 必须把 CMake 安装的
`wsp-systemd` 及其 ELF dependency closure 放到该位置。这个环境文件是 host control plane，
不是 Bot 的 `config.json`，绝不可 bind mount 进容器。

当前 broker 的认证契约是精确 UID，而不是组 ACL：它会将
`WSPCTL_SOCKET` 设为 owner `65532`、mode `0600`，并用 `SO_PEERCRED` 只接受 UID `65532`。
生成的 unit 以固定的 `ReadWritePaths=` 和 `ExecStartPre=install` 准备 root-owned `0711` socket
父目录；不要将 socket 改成 group/world writable 来“修复”连接问题。Compose 因而固定运行 Bot
为 `65532:65532`。

Bot `config.json` 只描述 **client-visible** socket 路径，绝不描述 `WSPCTL_HOST_WORKDIR`。默认
`.wspctl/run/wspctld.sock` 在容器中会相对于 `/app/config.json` 解析为
`/app/.wspctl/run/wspctld.sock`；production Compose 应只把其 `source` 改为
`/srv/fogmoe-wspctl/run`（或其他 root-owned host directory），仍挂到这个 target。因此通常无需
在 Bot JSONC 写 host `/srv/...` 路径；只有不经容器、直接运行 Bot client 时才配置 host 可见的绝对
socket 路径。

Bot 配置也必须让该 UID 读取。例如，若直接使用仓库根目录的 `config.json`：

```bash
build_dir=build/wspctl-prod
work_root=/srv/fogmoe-wspctl # Must exactly match -DWSPCTL_HOST_WORKDIR above.
sudo chown 65532:65532 ./config.json
sudo chmod 0600 ./config.json
sudo install -d -o root -g root -m 0711 "$work_root"
sudo install -o root -g root -m 0600 \
  "$build_dir/deploy/wspctl/systemd/wspctld.env.example" "$work_root/wspctld.env"
sudo install -o root -g root -m 0644 \
  "$build_dir/deploy/wspctl/systemd/wspctld.service" /etc/systemd/system/wspctld.service
sudo systemctl daemon-reload
```

此时不要启动 broker；先按下一节发布并挂载只读 generation。否则 `ExecStartPre` 与 broker
preflight 会有意拒绝启动。

在默认 rootful Docker 中，容器内数字 UID `65532` 会作为相同 UID 出现在 host Unix socket
凭据中。启用 Docker user-namespace remapping、rootless Docker 或额外 LSM policy 时，这个
映射可能改变；当前 broker 不接受映射后的第二个 UID。此类部署应保持 fail closed，直到已用
实际 `SO_PEERCRED` 和 broker 的固定 `--client-uid` 配置完成专门验证。

## 发布真正只读的 base generation

`wspctl-image` 不把 `chmod 0555` 当作 immutable image（不可变镜像）证明：它要求
`statvfs(WSPCTL_BASE_ROOT)` 带有真实的 `ST_RDONLY` mount flag。`WSPCTL_STATE_ROOT` 可以且
必须可写（journal 与 persistent workspace upper layer 在那里），但 `WSPCTL_BASE_ROOT` 绝不能
只是同一可写状态树中的普通目录。把 build pipeline 产出的 root-owned generation 放在独立的
artifact store，再将选中的 generation 用只读 bind mount 发布到 `images/`：

### 受控 rootfs 构建

不要把 `.venv`、`/usr/bin/bash` 或 `wsp-systemd` 在 broker 启动时 bind mount 进 runtime。受控
构建器 [`tools/build_wspctl_image.py`](../tools/build_wspctl_image.py) 只接受 operator 明确给出的
**绝对 host 路径**：项目 venv、需要兼容 editable 安装时的 source root、Bash、逐项选择的 GNU 基础
命令、已构建的 `wsp-systemd` 与 `wspctl-image`。这是 host 构建控制面的工作，输入由 operator 与项目
checkout 决定；它不属于 `run_bash` 的能力，也不向 workspace payload 暴露 builder API。构建器不会运行
`ldd`，而是通过 `readelf` 读取 ELF metadata，并以 `RPATH/RUNPATH` 和 `ldconfig` 递归复制 dynamic
loader（动态加载器）与共享库闭包（shared-library closure）。同 SONAME 的 multiarch cache entry 还会按
ELF class、endianness 与 `e_machine` 精确匹配：例如 x32 不能被误选为 x86-64 的 `libpthread`。遇到
host-specific absolute RPATH、外逃 symlink、未显式批准的 `.pth` source、不匹配 ABI 或不完整 closure
时会 fail closed。

构建器必须由 operator 以 root 运行：native verifier 要求 rootfs **每一个** inode 均为 UID 0。它在
artifact store 同一 filesystem 的私有 staging 目录写入；然后调用 `wspctl-image --seal`，使 C++ verifier
与构建阶段共用唯一的 manifest/digest 定义；最后使用 `renameat2(RENAME_NOREPLACE)` 原子发布。已有
generation 永不覆盖。下面的命令只是一份小 GNU allowlist；要增加工具时必须逐一审查并新增
`--gnu-command`，不能把宿主 `/usr/bin` 整体复制进去。

```bash
build_dir="$PWD/build/wspctl-prod"
artifact_store=/srv/fogmoe-wspctl-image-store
generation=2026-07-27-python314

sudo install -d -o root -g root -m 0700 "$artifact_store"
sudo "$PWD/.venv/bin/python" tools/build_wspctl_image.py \
  --generation "$generation" \
  --output-root "$artifact_store" \
  --venv "$PWD/.venv" \
  --python-source "$PWD/src" \
  --bash /usr/bin/bash \
  --gnu-command /usr/bin/env \
  --gnu-command /usr/bin/cat \
  --gnu-command /usr/bin/cp \
  --gnu-command /usr/bin/find \
  --gnu-command /usr/bin/grep \
  --gnu-command /usr/bin/ls \
  --gnu-command /usr/bin/mkdir \
  --gnu-command /usr/bin/rm \
  --gnu-command /usr/bin/sed \
  --gnu-command /usr/bin/tail \
  --gnu-command /usr/bin/tee \
  --gnu-command /usr/bin/touch \
  --gnu-command /usr/bin/wc \
  --wsp-systemd "$build_dir/src/wspctl/wsp-systemd" \
  --sealer "$build_dir/src/wspctl/wspctl-image" \
  --readelf /usr/bin/readelf \
  --ldconfig /usr/sbin/ldconfig
```

`--python-source` 不是隐式读取 checkout 的捷径：它只在 venv 的 editable `.pth` 明确引用该 source
时需要，且该 source 会被复制到 image 内的 `/opt/wspctl/python-source/`，`.pth` 也被重写为该
runtime-internal path。若 production venv 不含 editable package，应省略该选项。venv 的 absolute
interpreter symlink 会变为 rootfs-contained relative symlink；console script 的 shebang（解释器行）
改为 relocated venv path，而非 host 路径，因此仍看到 image 内的 site-packages。

builder 会创建最小 `/proc`、`/dev`、`/tmp`、`/run` 和 `/workspace` mountpoint。`/workspace` 与
`/tmp` 是 root-owned `01777`：前者给降权 task 在自己的 OverlayFS merged view 中创建文件，后者
也可作为 `pivot_root` 的 `put_old`；它们不是 host 目录，更不跨 Runtime 共享 writable upper layer。
除这两个经过精确验证的 sticky directory 外，builder 拒绝 group/world-writable、setuid/setgid 或
absolute/escaping symlink metadata。

若故意在 checkout 的 `./.wspctl/images` 做本机开发，请先以 root 创建该**终点**目录，并在同一次
构建中额外传入 `--allow-insecure-development-output`。`images` 本身仍须 `root:root` 且不可
group/other 写。它与 CMake 的 `-DWSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON` 一样，明确把 local
developer 纳入 TCB，绝不可用于多用户主机或 production。

```bash
generation=2026-07-27-example
work_root=/srv/fogmoe-wspctl # Must equal the CMake-configured WSPCTL_HOST_WORKDIR in production.
source_root=/srv/fogmoe-wspctl-image-store/$generation/rootfs # Output from the controlled builder above.
published_root=$work_root/images/$generation/rootfs

sudo test -d "$source_root"
sudo install -d -o root -g root -m 0700 "$published_root"
# 不要从未审计目录 bind mount；只发布 builder 成功返回的 immutable generation。
sudo mount --bind "$source_root" "$published_root"
sudo mount -o remount,bind,ro "$published_root"
findmnt --target "$published_root" --noheadings --output TARGET,OPTIONS
```

输出必须包含 `ro`。在 `$work_root/wspctld.env` 中将 `WSPCTL_BASE_ROOT` 指向
`published_root`，而不是 `source_root`。为了让 mount 跨重启存在，为该绝对路径创建并启用一个
对应的 systemd `.mount` unit（可用 `systemd-escape --path --suffix=mount "$published_root"` 得到
unit 名）；其 `[Mount]` 应将 source 指向 artifact store、`Where=` 指向 published path，并使用
`Options=bind,ro`，且必须在 `wspctld.service` 前启动。broker unit 的 `ExecStartPre` 会以
`findmnt` 再次检查 `ro`，因此遗漏或失效的 mount 会阻止启动，而不会在可写 base 上继续运行。

完成 mount 后才启动 broker：

```bash
sudo systemctl enable --now wspctld.service
sudo systemctl status wspctld.service
```

## 为每个 Workspace upperdir 配置硬容量上限

持久 `/workspace` OverlayFS upperdir 位于 `WSPCTL_STATE_ROOT`，因此 RAM 的 `memory.max`、CPU/IO
限速和 `pids.max` 都**不能**限制磁盘写入。没有每个 runtime 的真实 filesystem quota（文件系统配额）
时，恶意或失控的 `run_bash` 可以用 `fallocate`/大文件填满整个 host state filesystem；只给
`WSPCTL_HOST_WORKDIR` 一个全局磁盘大小不是隔离。

本项目只接受一个具体的 per-runtime hard quota backend（每个运行时硬配额后端）：专用 XFS filesystem
（文件系统）上的强制 XFS project quota（`prjquota`/`pquota`，不得使用 `pqnoenforce`）。每个 Runtime
有 control 与 workspace 两个 project ID，并同时设置 `bhard` 与 `ihard`；后端不可用、无法读回
assignment/limit、quota registry 不一致、activation cleanup 不确定或 admission budget 不足时，broker
必须 fail closed。完整的 layout、持久 registry、原子 provisioning、恢复与 operator preflight 契约见
[XFS project quota 生产容量契约](wspctl-xfs-project-quota.md)。

该 backend 已在 native 启动与 admission 路径实现：CLI 强制要求 `--quota-backend xfs_project_v1` 和
所有 XFS budget 参数，生成的 environment template 也包含它们。普通 ext4 目录、`du` 轮询、btrfs
qgroup 或“通用 quota plugin”都不是 fallback；缺少 dedicated XFS、project accounting/enforcement、
`PROJINHERIT` 或 hard-limit readback 时，服务退出而不是降级。

上线前在**与生产 state 分离的一次性 XFS test mount**上运行内核配额测试。不要把此测试指向正在使用的
production state mount：它会短暂创建真实 project ID、真实 quota tree，并故意触发 `EDQUOT`。例如，
CI 先创建/挂载一个空的 disposable XFS filesystem 后：

```bash
test_mount=/mnt/wspctl-xfs-quota-ctest
test_parent="$test_mount/ctest-parent"
sudo install -d -o root -g root -m 0700 "$test_parent"
sudo env \
  WSPCTL_REQUIRE_XFS_QUOTA_TESTS=1 \
  WSPCTL_XFS_QUOTA_TEST_MOUNT="$test_mount" \
  WSPCTL_XFS_QUOTA_TEST_PARENT="$test_parent" \
  ctest --test-dir build/wspctl-prod -R '^wspctl\.xfs_project_quota$' --output-on-failure
sudo rmdir "$test_parent"
```

`wspctl.xfs_project_quota` 先验证 XFS accounting/enforcement 与 project layout，然后在移除
`CAP_SYS_RESOURCE` 的 child 中分别把 inode 和 byte 写入推到 hard limit 之外；两项都必须得到 kernel
`EDQUOT`。默认没有上述环境变量时它显示为 skip，不能据此把生产验收标为通过。

## 从旧 Judge0 配置迁移

新版本不再接受 `integrations.code_execution`。在停止旧 Bot、排空 durable work 并备份数据库后，
对既有 `schema_version: 2` JSONC 配置运行显式的一次性迁移：

```bash
uv run python tools/migrate_config_v2_to_wspctl.py ./config.json --dry-run
uv run python tools/migrate_config_v2_to_wspctl.py ./config.json
```

该工具只删除该 retired member（退役成员），再通过公开的 `read_bot_settings` 验证生成结果；
只有验证成功才会替换文件。默认先写同目录、不可覆盖的
`.config.json.schema-v2-before-wspctl.bak`，且临时文件与目录项均 `fsync`。它拒绝符号链接、
硬链接、非 v2 文件、无效 JSONC 或已有同名备份；不会输出 Judge0 API key。已经不含该键的合法
配置是经过验证的幂等 no-op，不会生成空备份。备份路径已被 `.gitignore` 排除，但仍含历史密钥，
应按敏感文件保管。

随后执行数据库迁移并启动 broker；`0069_workspace_runtimes` 会拒绝未排空的旧不确定执行，
而 `0070_workspace_attachment_model_boundary` 还会拒绝未排空的 inference、context compaction、
retrieval vector 与 Profile Dream 队列。它会保留历史附件 audit 行、但排除每一条历史 direct-media Turn
及同一私聊会话中其后的 Assistant Turn 的未来模型使用；后者是防止 raw caption 已被纯文本回复转述后经
retrieval/Profile 回流。历史 `<workspace_file>` 文本不是 native `add_file` receipt，不能作为例外。它清除
可含 caption 的派生状态，并把历史群媒体收束为非可执行标记。由于旧 `fetch_group_context` 没有
逐读取 provenance，0070 还会保守排除所有历史群 Assistant Turn 及其 compaction/retrieval；这是防止
caption 已被模型转述后回流的必要代价，不影响私聊历史。不要跳过这些检查。迁移提交后必须重启新的
Bot worker，以清空任何内存中的旧 ContextWindow/summary：

```bash
fogmoe-dbctl --config ./config.json migrate
sudo systemctl restart wspctld.service
```

## 构建与运行 Bot 容器

Dockerfile 采用 multi-stage build：builder 安装 GCC、libseccomp/libcap/OpenSSL headers、CMake
和 Ninja，利用 scikit-build-core 构建含 pybind11 client 的 wheel；最终镜像只保留动态运行库、
wheel 和 Bot 源/静态资源。wheel build 显式设置 `WSPCTL_INSTALL_HOST_TOOLS=OFF`，因此 host-only
的 `wspctld`、`wsp-systemd`、`wspctl-image` 根本不会被安装到最终镜像。这样即使 Bot 被攻破，
也没有可被误启动的 privileged broker binary。

Compose 在开发态以只读方式把 `./.wspctl/run` 挂到 `/app/.wspctl/run`，而非单独 socket；Unix
socket 的 `connect(2)` 仍可工作，而容器无法在相邻路径创建、替换或 unlink 条目。生产 operator
可将 Compose source 改为其 root-owned absolute socket directory，但不得把 state/image root 送入
容器。它还使用只读 root filesystem、空
capability bounding set、`no-new-privileges`、受限 `/tmp`、非 root UID 和 PID 限额。socket、base
generation、state root 和 delegated cgroup 均不进入容器。

```bash
docker compose build bot
docker compose up -d bot
docker compose logs -f bot
```

上线前和每次 host upgrade 后至少检查：

```bash
sudo systemctl status wspctld.service
work_root=/srv/fogmoe-wspctl # Substitute the CMake-configured production work root.
sudo stat -c '%U %G %a %n' "$work_root/run" "$work_root/run/wspctld.sock"
docker compose exec bot id
docker compose exec bot test -S /app/.wspctl/run/wspctld.sock
docker compose exec bot python -c 'from wspctl import RuntimeProcess; print("native client import ok")'
```

预期容器身份为 `uid=65532 gid=65532`，socket mode 为 `600` 且 owner UID 为 `65532`。最后一条
只证明 wheel 中的 unprivileged client 可以导入，不会执行命令。真正的 `run_bash` 仍会在 broker
缺少 image proof、cgroup delegation 或安全 mount 条件时拒绝，而不是退回宿主 `subprocess`。

升级顺序应为：停止 Bot **及其 inference/retrieval/context/profile workers**、排空/备份、升级并验证
host generation、执行 migration、重启 `wspctld`、最后重建/启动 Bot；回滚则先停止 Bot，再用本地配置
备份和上一代已验证 image 恢复。不要在 broker 仍处理任务时删除 `WSPCTL_STATE_ROOT`，其中包含持久
workspace upper layer 和 command journal。
