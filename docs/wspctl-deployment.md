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

## 构建 workspace OCI image

先安装明确的系统工具：

- Buildah：构建并导出 OCI layout；
- Skopeo：按 digest ingest/copy OCI graph；
- umoci：按 OCI layer、whiteout 和 opaque-directory 语义 rootful unpack。

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
   `/usr/local/libexec/wspctl/wsp-systemd`，contract label 为 `2`；
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

## 启动开发 broker

首次顺序：

```bash
./scripts/build-wspctl-rootfs.sh
./scripts/publish-wspctl-rootfs.sh
./scripts/start-wspctld.sh
```

`start-wspctld.sh` 可以构建 host `wspctld`、`wspctl-image`、`wspctl` 和 Python client，但不会调用
Buildah、Skopeo、umoci、uv、readelf、ldconfig 或任何 rootfs builder。缺少已发布 image 时，它会在
任何 `systemctl start/restart` 前失败，并打印唯一的 build/publish 命令。

直接 host Bot 默认使用当前 UID：

```bash
./scripts/start-wspctld.sh
```

Compose Bot 使用固定 UID：

```bash
WSPCTL_CLIENT_UID=65532 ./scripts/start-wspctld.sh
```

选择已发布的另一个 image：

```bash
WSPCTL_IMAGE_DIGEST=sha256:<64hex> ./scripts/start-wspctld.sh
```

启动脚本首次创建预分配 `32G` loopback XFS image，并以 `rw,prjquota` 挂载到
`./.wspctl/state`。可在首次创建前设置 `WSPCTL_LOOP_SIZE=48G`；既有 image 不自动 resize，
非 XFS image 不会被重新格式化。

`./runBot.sh start` 会先调用启动脚本，日志写入 `logs/wspctld_<timestamp>.log`。控制面失败时 Bot
不会启动，终端同时显示完整日志路径；若 systemd broker preflight 失败，启动脚本还会附上最近
100 行 journal，native broker 会输出具体拒绝原因而不是只有泛化状态。

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
OverlayFS upper layer、pivot_root、Python/Bash execution、restart recovery 和 quota enforcement：

```bash
WSPCTL_REQUIRE_PRIVILEGED_E2E=1 \
WSPCTL_PRIVILEGED_E2E_XFS_MOUNT=/mnt/wspctl-xfs \
WSPCTL_PRIVILEGED_E2E_STATE_PARENT=/mnt/wspctl-xfs/ctest-state \
WSPCTL_PRIVILEGED_E2E_SOCKET_PARENT=/run/wspctl-ctest \
WSPCTL_PRIVILEGED_E2E_CGROUP_PARENT=/sys/fs/cgroup/wspctl-ctest \
WSPCTL_PRIVILEGED_E2E_IMAGES_ROOT=/srv/fogmoe-wspctl/images \
WSPCTL_PRIVILEGED_E2E_BASE_ROOT=/srv/fogmoe-wspctl/images/sha256/<hex>/rootfs \
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
