# wspctl 部署：标准 OCI root 与特权 broker

`wspctld` 是 root-owned host control plane；Bot 只通过 `run/bot/wspctld.sock` 连接。
`run/operator` 是独立 root/operator endpoint，绝不能 bind 进 Bot container。workspace runtime
不是项目 `.venv`、宿主 Python 或宿主 `/usr` 的投影，而是按 OCI manifest digest 固定的完整镜像。

## 架构边界

```text
deploy/wspctl/image/Containerfile
        │  Buildah（daemonless）
        ▼
OCI image layout + sha256:<manifest>
        │  Skopeo --preserve-digests
        ▼
root-owned private OCI staging
        │  descriptor/config policy + umoci rootful unpack
        ▼
materialized rootfs
        │  native runtime contract + local rootfs seal
        ▼
artifacts/sha256/<hex>/rootfs
        │  digest-specific systemd mount unit
        │  readonly bind,nosuid,nodev
        ▼
images/sha256/<hex>/rootfs
        │
        └─ wspctld → mount namespace → pivot_root
```

OCI manifest digest 是唯一 image identity。tag 只允许作为构建输出中的临时 alias；broker CLI、
systemd environment 和 store path 都不接受 tag、短 hash、任意 generation 名或启动时 fingerprint。
本地 `rootfs_digest` 只用于检测物化结果被修改，不是第二套版本号。

## 安装与运行边界

开发 checkout 的完整 host control-plane 安装只有一个入口：

```bash
./installWspctl.sh
```

该安装器依次执行 OCI build、root-owned publication、host artifact/systemd unit 安装，并
enable/start `wspctld.service`。这是显式、可能要求 sudo 的部署操作。

`./runBot.sh start` 属于纯运行阶段：它只读取 Bot 配置中的 socket 路径，检查已经安装的
`wspctld.service` 与 Unix socket，然后启动无特权 Bot。它绝不构建 image、安装 host binary、
创建或挂载 XFS、修改 systemd，亦不会调用 sudo。缺少 broker 时应先运行安装器，而不是让
应用启动过程隐式改变 host。

## 构建 workspace OCI image

先安装明确的系统工具：

- Buildah：构建并导出 OCI layout；
- Skopeo：按 digest ingest/copy OCI graph；
- umoci：按 OCI layer、whiteout 和 opaque-directory 语义 rootful unpack。
- LXCFS 与 FUSE3：由 `wspctl-lxcfs.service` 提供独立的 cgroup-aware procfs 数据源；
  安装器要求 host 上已有 `/usr/bin/lxcfs` 和 `fusermount3`，不静默安装或复用发行版全局实例。

不提供 Docker、裸 tar、`debootstrap`、host Python 或 host `ldconfig` fallback。缺少工具会直接失败并
报告名字，因为静默换后端会改变 ownership、capability、whiteout 和 provenance 语义。

执行：

```bash
./scripts/build-wspctl-rootfs.sh
```

唯一 recipe 是 [`deploy/wspctl/image/Containerfile`](../deploy/wspctl/image/Containerfile)：

- builder/runtime 使用同一个 digest-pinned `python:3.14-slim-bookworm`；
- runtime/build APT 输入固定到 Debian snapshot；CMake/Ninja wheel 的 immutable URL 与 SHA-256
  固定在 `build-tools.lock`，构建时禁用 package index 与 dependency resolver；
- `wsp-systemd` 在同发行版/ABI 的 builder stage 编译；
- runtime stage 显式安装 Bash、coreutils、findutils、grep、sed、libcap、libseccomp 和 OpenSSL runtime；
- `site-packages` 清空，不复制项目 package、dashboard GUI、`.venv` 或 `src/`；
- image 内实际执行 Python imports 和 `ldd` smoke test；
- setuid/setgid bits 被移除，固定 mountpoints 和 supervisor entrypoint 写入 image config。

基础 image digest 固定符合 Docker/OCI 生产建议；Debian snapshot 进一步固定包解析。`SOURCE_DATE_EPOCH`
默认取当前 Git commit time，可由 operator/CI 显式设置。它只是可复现构建（reproducible build）的一个
输入；独立 clean build 仍必须比较最终 OCI manifest digest，不能把同一 store 的 cache hit 当证明。

## 验证式 ingest 与发布

执行：

```bash
./scripts/publish-wspctl-rootfs.sh
```

本地 build artifact 也按 digest 保存：

```text
.runtime/wspctl-rootfs/
├── sha256/<manifest-hex>/oci-layout/
└── current-image-digest
```

也可显式指定：

```bash
WSPCTL_IMAGE_DIGEST=sha256:<64-lowercase-hex> \
WSPCTL_IMAGE_REFERENCE=wspctl-runtime \
./scripts/publish-wspctl-rootfs.sh /absolute/path/to/oci-layout
```

发布器绝不直接 `sudo umoci unpack` 用户可写 layout。流程是：

1. operator 必须提供完整 OCI manifest digest；
2. Skopeo `--preserve-digests` 将 reference 复制进 artifact store 内的 root-owned private staging；
3. importer 从 staging 的 `oci-layout`、`index.json` 和 `blobs/sha256/*` 验证每个 descriptor 的
   media type、size 和 digest；
4. image config 必须为唯一 `linux/amd64`、layer/DiffID 数量一致、entrypoint 是固定
   `/usr/local/libexec/wspctl/wsp-systemd`，contract label 为 `3`；
5. umoci rootful 只读取这个 root-owned snapshot，正确应用 layers/whiteouts；
6. native sealer 验证 runtime contract、root ownership、regular-file set-id、xattr/file capability、special inode、
   symlink、固定入口和空 `site-packages`；
7. `renameat2(RENAME_NOREPLACE)` 原子发布，已有 digest 永不覆盖；
8. publisher 为 digest 写入 path-named systemd `.mount` unit，以真实 `ro,nosuid,nodev` bind mount
   发布；unit 被 enable，重启后会从 sealed CAS 自动恢复；
9. `wspctl-image --verify` 再验证一次；
10. 成功后才原子更新 `<work-root>/current-image-digest`。

import、mount、native verify 和 selection 由 root-owned `publish.lock` 串行化。新 mount/unit 任一步失败
都会回滚；既有 image、current selection 和正在运行的 broker 不改变。重复发布同一 digest 只验证
既有 CAS object 和 mount，不修改 readonly rootfs inode。

标准路径：

```text
.wspctl/
├── artifacts/sha256/<manifest-hex>/
│   ├── oci/                    # root-owned OCI snapshot
│   ├── runtime-config.json
│   ├── umoci-metadata/         # mtree 与 unpack provenance
│   └── rootfs/                 # sealed materialization
├── images/sha256/<manifest-hex>/rootfs/  # readonly mount
└── current-image-digest
```

## 安装开发 broker

正常安装只运行聚合入口：

```bash
./installWspctl.sh
```

聚合入口内部固定执行 `build-wspctl-rootfs.sh` → `publish-wspctl-rootfs.sh` →
`start-wspctld.sh`。最后一个脚本只负责 host broker/state/service 阶段，并会 enable unit；
它不会反向调用 image builder。

每次聚合安装都会把三个阶段的 stdout/stderr 实时显示并完整写入
`logs/wspctl_install_<timestamp>_<pid>.log`。日志以 `0600` 创建，失败时保留底层阶段的原始
退出码。readiness 分为两个互不替代的层次：

1. `wspctl-lxcfs.service` 先在 host mount namespace 建立
   `/run/fogmoe-wspctl-lxcfs/root`。`wspctld.service` 以 `Requires`、`BindsTo` 和 `After`
   绑定该专用实例；启动前验证它是 root-owned、不可由 group/world 写的 FUSE mount，并逐个读取
   全部核心映射节点。PSI 三节点按完整能力组协商：全有才映射，全无则遮蔽 `/proc/pressure`，
   部分存在则拒绝启动。实例退出会连带停止 broker，不会让新 Runtime 回退到宿主 procfs。
2. `wspctld.service` 使用 `Type=notify` 与 `NotifyAccess=main`。daemon 只有在 native preflight、
   Bot/operator listener 和全部 worker pool 均就绪后，才由 main PID 发送 `READY=1`；发送后立即
   清除 `NOTIFY_SOCKET`，runtime child 不会继承 service-manager notification capability。
   `systemctl start/restart` 因此不会把旧 generation 短暂残留的 socket inode 当成新 generation
readiness；30 秒 `TimeoutStartSec` 又让通知丢失或初始化卡死有界失败。
3. 安装器不再创建 health Runtime。镜像中的 Agent/passwd、`/workspace` lower、supervisor、
   动态库和基础工具由发布期 native seal 与每次启动前的 `wspctl-image --verify` 静态验证；
   service 阶段只检查 `Type=notify` readiness、Bot/operator socket 的 owner/mode，以及前后稳定的
   systemd `InvocationID`。这些条件和部署输入一一对应，不把 XFS quota、cgroup、OverlayFS、
   namespace 和一次 payload 执行的瞬时状态混入安装事务。

每个新 service invocation 只记录一次 readiness evidence。若外部操作恰好触发滚代，安装器最多
跟随三个 generation；稳定 generation 通过静态检查后，启动脚本原子记录 fingerprint 与已验证的
`InvocationID`。真实 Runtime 路径仍在正常请求及 privileged E2E 中验证，而不是用一个部署期
canary 占用 project ID、创建持久 workspace 或影响安装成败。

若 service 或 socket 检查失败，同一安装日志还包含 `systemctl status` 或最近 100 行 journal。
`./statusWspctl.sh` 会报告最近一次安装日志，并只读比较当前 `InvocationID` 与 readiness evidence；
它本身绝不执行 task。

安装器不推断 Linux、WSL 或容器环境，也不构造、改写或默认任何代理地址。需要代理访问 base
image、Debian snapshot 或 Python wheel 时，它直接读取调用环境中已经存在的大小写两组
`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY`。因此 WSL 应沿用 shell 中实际配置的
Windows host 地址，不假定 guest 的 `127.0.0.1`。

非特权 `curl` 直接继承当前环境；Buildah 显式启用 `--http-proxy=true`，读取同一组变量并传入
Containerfile 构建。跨 sudo 边界只 allowlist 这八个变量，值保持原样；root publisher 再以
同一 allowlist 构造 Skopeo/umoci 子进程环境。不要改成裸 `sudo -E`，否则无关凭据、agent
socket 和应用配置也可能进入 root 进程。

直接 host Bot 默认安装为当前 UID：

```bash
./installWspctl.sh
```

Compose Bot 安装使用固定 UID：

```bash
WSPCTL_CLIENT_UID=65532 ./installWspctl.sh
```

高级 operator 仍可用内部阶段选择已经发布的另一个 image：

```bash
WSPCTL_IMAGE_DIGEST=sha256:<64hex> ./scripts/start-wspctld.sh
```

启动脚本首次创建预分配 `32G` loopback XFS image，并以 `rw,prjquota` 挂载到
`./.wspctl/state`。可在首次创建前设置 `WSPCTL_LOOP_SIZE=48G`；既有 image 不自动 resize，
非 XFS image 不会被重新格式化。

开发安装器会检查 `/sys/fs/cgroup/system.slice/io.weight`。普通 Linux host 提供该控制文件时
默认使用 `WSPCTL_IO_WEIGHT=100`；部分 WSL2 kernel 只暴露 `io.max/io.latency` 而没有
`io.weight`，此时安装器明确记录 capability 降级并写入 `WSPCTL_IO_WEIGHT=0`。这只关闭相对
I/O QoS，不放松 memory、CPU、PIDs、namespace、seccomp 或 XFS project quota。operator 可用
`WSPCTL_IO_WEIGHT=0..10000 ./installWspctl.sh` 显式覆盖自动选择；在不支持 `io.weight` 的 host
上强制非零值仍会 fail closed。

`./runBot.sh start` 不再调用任何安装脚本。readiness 失败时它只提示
`./installWspctl.sh` 与 `./statusWspctl.sh`，不会在应用运行路径中提权修复 host。

## 生产 host control plane

生产工作根必须位于 root-owned、祖先不可 group/world-write 的绝对路径：

```bash
cmake -S . -B build/wspctl-prod \
  -DPython_EXECUTABLE="$PWD/.venv/bin/python" \
  -DWSPCTL_INSTALL_HOST_TOOLS=ON \
  -DWSPCTL_HOST_WORKDIR=/srv/fogmoe-wspctl \
  -DCMAKE_INSTALL_PREFIX=/usr/local
cmake --build build/wspctl-prod --parallel
sudo cmake --install build/wspctl-prod
```

生产发布使用同一个显式入口，但必须把 work root 指向上述 CMake 固定路径：

```bash
WSPCTL_WORK_ROOT=/srv/fogmoe-wspctl \
./scripts/publish-wspctl-rootfs.sh /absolute/path/to/oci-layout
```

该入口先把 publisher 与 native verifier 安装到 root-owned `/usr/local`，随后只允许
`/usr/bin/python3`、`/usr/bin/skopeo`、`/usr/bin/umoci`、systemd/util-linux 工具和
`/usr/local` 中 root-owned、group/world 不可写的已安装程序进入特权可信计算基（trusted
computing base, TCB）。它不会通过 `sudo` 执行 checkout `.venv`、checkout Python module、
checkout build artifact 或调用者 `PATH` 选出的工具。

checkout-local 模式必须显式设置 `WSPCTL_ALLOW_INSECURE_DEVELOPMENT_ROOT=ON`；它仅表示把
local developer 纳入 trusted control plane（TCB），不能用于多用户 production host。

systemd environment 使用：

```text
WSPCTL_IMAGES_ROOT=/srv/fogmoe-wspctl/images
WSPCTL_IMAGE_DIGEST=sha256:<64hex>
WSPCTL_STATE_ROOT=/srv/fogmoe-wspctl/state
WSPCTL_OPERATOR_SOCKET=/srv/fogmoe-wspctl/run/operator/wspctld.sock
```

`wspctld` 只接收 `--image-store` 与 `--image-digest`。它从强类型 digest 唯一派生
`images/sha256/<hex>/rootfs`；`--base-root` 与可变 `--supervisor` 已删除。固定 supervisor path 是
runtime contract 的组成部分，不再重复配置。

## XFS project quota

持久 OverlayFS upper layer 必须位于专用 XFS mount，启用 enforcing `prjquota` 或 `pquota`，不得使用
`pqnoenforce`。每个 Runtime 分配 control/workspace project-ID pair，同时设置 block hard limit
（`bhard`）和 inode hard limit（`ihard`），目录必须带 `PROJINHERIT`。完整恢复和容量模型见
[wspctl-xfs-project-quota.md](wspctl-xfs-project-quota.md)。

真实 quota 验收必须显式运行：

```bash
WSPCTL_REQUIRE_XFS_QUOTA_TESTS=1 \
ctest --test-dir build/wspctl-prod \
  -R '^wspctl.xfs_project_quota$' \
  --output-on-failure
```

普通开发环境的 skip 不是 quota 通过证明。

## Privileged E2E

验收环境必须提供 disposable XFS、delegated cgroup v2 parent、私有 socket parent，以及已经按新
`images/sha256/<hex>/rootfs` 结构发布的 readonly image。测试实际经过 broker、PID namespace、
OverlayFS upper layer、pivot_root、具名 `agent` 身份、旧 `nobody` workspace ownership 迁移、
Python/Bash execution、restart recovery 和 quota enforcement：

```bash
WSPCTL_REQUIRE_PRIVILEGED_E2E=1 \
WSPCTL_PRIVILEGED_E2E_XFS_MOUNT=/mnt/wspctl-xfs \
WSPCTL_PRIVILEGED_E2E_STATE_PARENT=/mnt/wspctl-xfs/ctest-state \
WSPCTL_PRIVILEGED_E2E_SOCKET_PARENT=/run/wspctl-ctest \
WSPCTL_PRIVILEGED_E2E_CGROUP_PARENT=/sys/fs/cgroup/wspctl-ctest \
WSPCTL_PRIVILEGED_E2E_IMAGES_ROOT=/srv/fogmoe-wspctl/images \
WSPCTL_PRIVILEGED_E2E_BASE_ROOT=/srv/fogmoe-wspctl/images/sha256/<hex>/rootfs \
WSPCTL_PRIVILEGED_E2E_LXCFS_ROOT=/run/fogmoe-wspctl-lxcfs/root \
WSPCTL_PRIVILEGED_E2E_XFS_PROJECT_ID_MIN=200000 \
WSPCTL_PRIVILEGED_E2E_XFS_PROJECT_ID_MAX=200199 \
ctest --test-dir build/wspctl-prod \
  -R '^wspctl\.privileged_e2e$' \
  --output-on-failure
```

## 观测、operator 与卸载

只读状态：

```bash
./statusWspctl.sh
tail -n 100 "$(find logs -maxdepth 1 -name 'wspctl_install_*.log' -printf '%T@ %p\n' \
  | sort -nr | head -n 1 | cut -d' ' -f2-)"
sudo wspctl status <runtime>
sudo wspctl workspace ls <runtime> /workspace
```

operator endpoint 来自 `run/operator`，Bot container 只看到只读的 `run/bot`。不要通过 group ACL 或
world-readable socket 合并两个身份。

卸载：

```bash
./uninstallWspctl.sh
```

默认停止本 checkout service、卸载 readonly images 和 loop device，但保留 XFS state、OCI artifacts
及业务 workspace。只有明确不再需要任何 workspace 时才运行：

```bash
./uninstallWspctl.sh --purge
```

`--purge` 会不可恢复地删除持久 workspace/state；image build/publish 本身不修改数据库，因此本次
OCI root 迁移没有 DB migration。

## 参考

- [OCI Image Layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
- [OCI Image Manifest](https://github.com/opencontainers/image-spec/blob/main/manifest.md)
- [OCI Image Config](https://github.com/opencontainers/image-spec/blob/main/config.md)
- [OCI Image Layer](https://github.com/opencontainers/image-spec/blob/main/layer.md)
- [Buildah](https://github.com/containers/buildah)
- [Skopeo](https://github.com/containers/skopeo)
- [umoci workflow](https://umo.ci/quick-start/workflow/)
- [Debian snapshot](https://snapshot.debian.org/)
- [Reproducible Builds: Increasing the Integrity of Software Supply Chains](https://arxiv.org/abs/2104.06020)
- [SLSA v1.2 build requirements](https://slsa.dev/spec/v1.2/build-requirements)
