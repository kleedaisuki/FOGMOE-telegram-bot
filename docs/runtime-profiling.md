# 运行时 Profiling、诊断与优化 Runbook

> 本文是一份可复现的生产诊断方法，而不是一次性能数字的展示。
> 所有示例都禁止输出密码、DSN、Bot token、业务 ID、主机地址或消息内容。

## 1. 目标、边界与停止条件

运行时优化要同时回答四个互不替代的问题：

1. **用户语义是否正确**：pipeline 是否积压、lease 是否过期、错误是否增加；
2. **资源成本在哪里**：进程 CPU/RSS、数据库事务、表访问和外部调用各占多少；
3. **成本由什么机制产生**：真实业务、空闲轮询、连接探活、恢复扫描还是观测写入；
4. **改动是否保留正确性**：并发上限、fencing、重试、关停排空和权限边界是否仍成立。

不要以“CPU 降了”替代后三项。把 durable work 漏掉、把恢复频率降到不可接受，或让
Dashboard 复用维护账号，都能制造漂亮但错误的图表。

本 Runbook 的安全边界是：

- 先执行只读检查；`EXPLAIN ANALYZE` 只用于已确认的 `SELECT`，因为 `ANALYZE` 会真实执行语句；
- 不在共享生产实例调用 `pg_stat_reset()`，而是读取累计计数器的前后差值；
- 不把真实 `config.json`、异常中的 DSN 或任何 secret 复制到报告；
- PostgreSQL 集成测试只允许显式的隔离测试数据库，测试辅助代码会 fail closed；
- 不用 application 角色越权查询迁移元数据，也不把 maintenance 角色临时塞给 Dashboard；
- schema/ACL 变更先 `--dry-run`，需要集群角色权限时由有资质的操作者执行；
- 进程必须由既有 supervisor 拥有。临时 shell 启动成功不等于服务能在 shell 退出后存活。

完成条件不是“所有计数都为零”，而是：正确性指标健康、成本可解释、目标 SLO 未退化、
重复样本支持改进，并且剩余风险被明确记录。

## 2. 从外向内的 MECE 证据链

采用四层互斥且覆盖完整的证据链。下一层用于解释上一层，不能跳过用户语义直接改代码。

| 层 | 主要问题 | 首选证据 | 常见误判 |
|---|---|---|---|
| 控制面 | 配置、迁移、角色和 ACL 是否可执行 | dbctl dry-run、迁移 head、角色 flags、effective ACL | 把认证失败当成 Dashboard 查询 bug |
| 应用语义 | pipeline、错误、资源心跳是否健康 | Dashboard overview/pipeline/errors/resources | 只看进程存活，不看过期 lease |
| 进程/数据库 | 空闲成本和事务组成是什么 | `/proc`、`pg_stat_database`、`pg_stat_user_tables` | 把 index scan 次数当成 SQL 次数 |
| 代码机制 | 哪个 loop/checkout/query 产生计数 | 静态频率预算、官方契约、定向测试、`EXPLAIN` | 看到相关性就直接宣布因果 |

### 2.1 控制面预检

~~~bash
.venv/bin/fogmoe-dbctl --config ./config.json bootstrap --dry-run
.venv/bin/fogmoe-dbctl --config ./config.json migrate --dry-run
~~~

dry-run 必须做到两件事：显示将执行的角色、数据库和 ACL 操作；始终遮蔽密码且不修改
PostgreSQL。机器人启动过程不隐式 bootstrap 或 migrate。

正式环境的身份是三条不同职责：

- application：Bot 业务 DML；
- maintenance：迁移与受控运维；
- reporting：Dashboard 的封闭只读关系集合。

三者及 bootstrap 管理身份必须两两不同。若 reporting 角色尚不存在，Dashboard 应返回
简洁的非零退出状态；不要改成 maintenance 身份“先跑起来”。完整的角色和 ACL 契约见
[dbctl 文档](dbctl.md)。

### 2.2 应用语义基线

~~~bash
.venv/bin/fogmoe-dashboard --config ./config.json --window 1h overview
.venv/bin/fogmoe-dashboard --config ./config.json pipeline
.venv/bin/fogmoe-dashboard --config ./config.json --window 1h errors --limit 100
.venv/bin/fogmoe-dashboard --config ./config.json --window 1h resources --limit 100
~~~

至少记录：

- 各 stage 的 pending、processing、retry、failed-final、oldest-ready 与 expired-lease；
- error span/log 数及首次出现时间；
- 当前资源心跳是否在配置的 stale window 内；
- 样本窗口内是否存在真实用户流量。

`failed-final` 是历史事实，不应为了让 overview 变绿而直接删除。诊断重点是它是否继续增长、
是否对应当前故障，以及业务是否需要显式补偿。

### 2.3 进程 CPU 与 RSS

Linux `/proc/<pid>/stat` 的 `utime`、`stime` 是累计 CPU tick。定义：

$$
U_{1c}=\frac{(\Delta u+\Delta s)/H}{\Delta t}\times 100\%
$$

其中：

- \(U_{1c}\) 是相对单个逻辑 CPU 的使用率；
- \(\Delta u\) 与 \(\Delta s\) 分别是采样窗口内 user/system CPU tick 增量；
- \(H\) 是 `getconf CLK_TCK` 返回的每秒 tick 数；
- \(\Delta t\) 是单调时钟测得的窗口秒数。

读取 PID 前先执行 `./runBot.sh status`。该脚本会同时核对 PID 与 Linux 进程启动时刻，避免
PID reuse。`.runtime/fogmoe-bot.pid` 包含的不只是 PID，不能把整行当作整数。

RSS 是时点值，不是累计量；至少记录窗口前后值。短窗 RSS 不变只能说明没有明显增长，不能
证明不存在泄漏。需要泄漏结论时扩大时间尺度，并关联 workload、GC 与对象分配 profiler。

### 2.4 PostgreSQL 事务率

令：

$$
R_{tx}=\frac{\Delta C+\Delta R}{\Delta t}
$$

其中 \(R_{tx}\) 是每秒事务结束数，\(\Delta C\) 是 `xact_commit` 增量，\(\Delta R\) 是
`xact_rollback` 增量，\(\Delta t\) 是采样秒数。

在 `fogmoe-dbctl shell --no-psqlrc` 的同一 session 中可以无写入地测量：

~~~sql
SELECT xact_commit AS commits, xact_rollback AS rollbacks
FROM pg_stat_database
WHERE datname = current_database()
\gset before_

\! sleep 10

SELECT xact_commit - :before_commits AS commit_delta,
       xact_rollback - :before_rollbacks AS rollback_delta,
       (xact_commit - :before_commits
        + xact_rollback - :before_rollbacks) / 10.0 AS transactions_per_second
FROM pg_stat_database
WHERE datname = current_database();
~~~

采样查询自身也会产生很小的开销，因此报告窗口长度、查询次数和连接方式。不要清零共享计数器。
PostgreSQL 统计视图的更新与 snapshot 行为见
[官方 monitoring statistics 文档](https://www.postgresql.org/docs/current/monitoring-stats.html)。

### 2.5 表级归因

在同一窗口读取 `pg_stat_user_tables` 的前后差值：

~~~sql
SELECT schemaname,
       relname,
       seq_scan,
       idx_scan,
       n_tup_ins,
       n_tup_upd,
       n_tup_del
FROM pg_stat_user_tables
ORDER BY schemaname, relname;
~~~

关键限制：`seq_scan + idx_scan` 是 relation scan 次数，不是 SQL 或事务次数。Nested Loop
（嵌套循环）的一条 SQL 可以对内表发起数百次 index scan。正确用法是：

1. 先用 `pg_stat_database` 确认总成本；
2. 再用表级增量定位 bounded context；
3. 阅读 worker 的循环频率与一轮调用的 repository 方法数；
4. 最后对特定 `SELECT` 使用 `EXPLAIN (ANALYZE, BUFFERS)` 验证执行计划。

执行计划字段、buffer 计数与 `ANALYZE` 会实际执行语句的边界见
[PostgreSQL EXPLAIN](https://www.postgresql.org/docs/current/sql-explain.html)。

### 2.6 静态频率预算

为每个空闲 worker 写出一个简单预算：

$$
B=\sum_{i=1}^{n} w_i\,q_i\,f_i
$$

其中 \(B\) 是预期业务数据库操作数/秒；\(w_i\) 是第 \(i\) 类 loop 的并发数；
\(q_i\) 是每轮数据库操作数；\(f_i\) 是每秒轮数。它不是测量值，而是用来验证测量是否
符合代码结构。

例如，两个 consumer 各自每 0.5 秒 claim 一次时，空闲 claim 预算是
\(2\times1\times2=4\) 次/秒。若表级统计接近 4/s，而数据库总事务接近 8/s，就应寻找
每次 checkout 额外执行的一条语句，而不是继续微调业务 SQL。

## 3. 本次案例：从 133 tx/s 到约 16 tx/s

以下数字仅是一个部署的短时空闲案例，不是产品 SLO 或跨机器 benchmark。三个阶段均使用
相同 10 秒口径；最终阶段额外重复两个窗口。背景 telemetry、采样查询和偶发外部流量仍是
干扰项。

| 阶段 | 单核 CPU | RSS | 事务率 | 关键现象 |
|---|---:|---:|---:|---|
| 初始基线 | 7.99% | 286.2 MiB | 132.71/s | 高频固定轮询和每次 checkout 探活叠加 |
| 第一轮 | 3.40% | 280.1 MiB | 38.36/s | durable pipeline 改为有界 adaptive polling |
| 第二轮 | 2.10–2.20% | 279.9 MiB | 15.69–15.98/s | 去掉 checkout ping，拆分恢复，verification 单 producer |

最终三个窗口的事务率中位数为约 15.98/s，CPU 中位数约 2.20%。相对初始短窗，事务率下降
约 88%，单核 CPU 下降约 72%。这只是说明改动在该 workload 下有效；不能由此推出峰值
吞吐、p99 用户延迟或另一部署的节省比例。

### 3.1 第一轮：空轮询不是业务 retry

原实现把低延迟基础轮询间隔同时当作永久空闲间隔。多个 durable worker 即使队列为空，仍
反复建立短事务。修复后 `AdaptivePollingPolicy` 只管理可丢失的进程内空闲状态：

~~~text
base -> 2 × base -> 4 × base -> ... -> max
             work found -> base
~~~

等待使用最多 10% 的向下 jitter，使并发 worker 不在同一时刻苏醒，同时不突破配置声明的
最大延迟。它不替代 durable `next_attempt_at`、lease、业务 retry 或 fencing。

AWS 的生产经验建议对 retry 使用 capped exponential backoff 与 jitter，以避免同步重试放大
故障；本项目只借用其去同步化原则，并把业务 retry 与空闲 polling 分开建模。
[AWS Builders' Library](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)

### 3.2 第二轮归因：为什么 commit 与 rollback 近似 1:1

第一轮后，一个 10 秒窗口约有 188 次 commit 与 196 次 rollback；表级业务操作预算约为
18–19/s，但数据库总事务约 38/s。两条独立证据指向同一个横切机制：SQLAlchemy
`pool_pre_ping=True` 会在**每次 checkout**执行 driver ping 或 `SELECT 1`，而连接归还时
默认 reset-on-return 会 rollback 打开的事务。

修复不是隐藏 rollback 指标，而是选择 SQLAlchemy 官方定义的 optimistic disconnect
handling（乐观断连处理）：

- 关闭每次 checkout pre-ping；
- 保留 `pool_recycle`；
- 失效连接的首次操作允许失败一次，Engine 随后 invalidate 旧 pool；
- durable worker 通过既有 retry、lease recovery 与幂等/fencing 恢复。

最终短窗 rollback 降到约 0.5–0.7/s，证明先前近 1:1 关系不是业务 rollback。这个选择适合
当前长驻 PostgreSQL 和可恢复工作流；若某个同步、不可重放请求要求 checkout 必须透明成功，
应重新评估，而不是全局复制该设置。SQLAlchemy 对 pessimistic/optimistic 两种策略、
pre-ping 开销和 pool invalidation 的契约见
[官方 pooling 文档](https://docs.sqlalchemy.org/en/20/core/pooling.html#dealing-with-disconnects)。

### 3.3 恢复扫描与业务领取必须独立

lease recovery 的频率由故障恢复目标决定，claim polling 的频率由业务延迟目标决定；把两者
塞进每一轮会同时浪费事务并产生 head-of-line blocking（队首阻塞）。

本次统一使用 `LeaseRecoveryCadence`：首次启动立即恢复，随后间隔为
`min(lease / 2, 5s)`。deadline 在查询前推进，因此恢复查询失败不会形成 tight loop。
Admin 与 Scheduling 使用独立、命名的 `TaskGroup` 子任务；恢复阻塞或短暂失败不拖住正常
claim，正常 shutdown 仍等待结构化子任务退出。

Python 3.14 的 `TaskGroup` 提供结构化并发（structured concurrency）：子任务不从所有者
生命周期逃逸，非取消异常会取消 siblings 并聚合传播；正确的 coroutine 仍必须在 cleanup 后
重新抛出 `CancelledError`。[Python 3.14 asyncio Task 文档](https://docs.python.org/3.14/library/asyncio-task.html#task-groups)

### 3.4 单 producer 消除 verification 惊群

Verification 原有两个 consumer 各自执行数据库 claim，空闲时约 4 claim/s。重构后：

~~~text
one claim producer
        |
bounded queue + capacity tokens
        |
fixed consumers
~~~

单次 claim 仍受 `claim_limit` 约束；排队中与执行中的 claim 总量不超过
`worker_count × claim_limit`；实际外部副作用并发不超过 `worker_count`。正常关停先停止新
claim，再 `queue.join()` 排空已领取工作。无调用且会广播唤醒所有 worker 的 `wake()` 被删除。
空闲 verification 业务数据库操作由约 4.2/s 降到约 2.2/s，同时保留 0.5 秒到期检查 SLO。

### 3.5 timeout、lease 与 circuit breaker

外部调用型 workflow 强制表达：

~~~text
provider timeout < whole-attempt timeout < durable lease
~~~

provider timeout 只包单个外部请求；attempt timeout 包含 fallback 与本地处理；lease 必须覆盖
整个尝试，否则合法 owner 可能在结束前被回收。Circuit Breaker 的 Half-Open 状态只允许一个
probe，避免 provider 恢复时所有等待者同时放行。Microsoft 的模式说明也要求 Half-Open 只让
有限请求试探恢复。[Azure Circuit Breaker pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker)

Google 的 *The Tail at Scale* 说明大型 fan-out 系统为何必须控制尾延迟；它支持“所有外部
工作都要有 deadline”的方向，但不直接决定本项目的 polling 间隔或 lease 数值。
[Dean 与 Barroso，CACM 2013](https://research.google/pubs/the-tail-at-scale/)

## 4. 配置、类型与分层如何防止回归

Bot、Dashboard 与 dbctl 读取同一个用户 `config.json`，但分别拥有窄 Pydantic projection；
它们只共享 `fogmoe_config.jsonc` 的严格语法 decoder，不共享万能配置对象。decoder：

- 接受严格 JSON 加 `//`、`/* ... */` 注释；
- 拒绝 duplicate key、NaN/Infinity、浮点溢出和 JSON5 扩展；
- 保留原始错误位置；
- 不在 ValidationError 中回显 secret 输入。

Bot 类型约束 adaptive `max >= base`、Dashboard `stale >= 3 × heartbeat`，并验证所有嵌套
deadline。真实 `config.json` 含 secret，通常不进入 Git；新增必需字段时必须同时更新：

1. 对应 executable 的窄 projection；
2. `example.config.json` 的公开说明；
3. 部署中的真实 `config.json`；
4. 配置契约测试。

“example 能通过”不代表部署文件已更新；“Bot 能读”也不代表 Dashboard/dbctl projection
有效。收尾应显式验证三者。

数据库层只有 `database/db.py` 拥有 Engine、连接与 transaction。普通读取 API 不接受
`for_update: bool`；要求锁的操作必须接收 `AsyncConnection`，使“没有事务却请求锁”在类型
签名上不可表达。跨多张事实表的 scheduled profile 读取使用同一
`REPEATABLE READ, READ ONLY` transaction，避免把多时点事实拼成不存在的快照。
PostgreSQL transaction mode 的约束见
[SET TRANSACTION](https://www.postgresql.org/docs/current/sql-set-transaction.html)。

## 5. Dashboard、dbctl 与 ACL 故障的正确处理

Dashboard 认证失败时，CLI 应输出一行运维错误并以状态 2 退出，不倾倒 asyncpg traceback。
这改善的是错误边界，不会创建缺失角色。

当 live audit 显示以下任一状态时，代码侧工作已经到达集群权限边界：

- reporting 角色不存在；
- 受管旧角色仍为 `INHERIT`；
- 数据库 `PUBLIC` 仍有 `CONNECT` 或 `TEMPORARY`；
- maintenance 是 `NOCREATEROLE`，无法自行修复角色。

不要索取管理员密码、绕过 peer/sudo 或授予 maintenance 更宽权限。由集群管理员在受控 TTY
按顺序执行：

~~~bash
./runBot.sh stop
.venv/bin/fogmoe-dbctl --config ./config.json bootstrap
.venv/bin/fogmoe-dbctl --config ./config.json migrate
./runBot.sh start
.venv/bin/fogmoe-dashboard --config ./config.json overview
~~~

先停应用是因为 schema/ACL 变更对既有 session 的名称解析和权限缓存不能被当作即时失效。
PostgreSQL 也明确说明 `LISTEN` 首次建立存在启动竞态：未来若用 LISTEN/NOTIFY 替代 bounded
polling，应先提交 `LISTEN`，再检查数据库状态，之后才依赖通知。
[PostgreSQL LISTEN](https://www.postgresql.org/docs/current/sql-listen.html)

## 6. Migration 与业务数据

迁移验证至少包含：

- 从空 PostgreSQL + 必需 extension 迁到 head；
- 从包含历史业务行的上一 revision 升级；
- backfill 后再添加/验证 constraint，最后才删除旧列或表；
- 权威事实与旧镜像逐行对账；无法证明映射时 fail closed；
- application/reporting effective privilege 的正反断言；
- 测试数据库名和 endpoint 隔离检查。

“不用兼容旧实现”允许删除旧 API 和无意义表，不等于可以丢失已有业务事实。代码兼容层可以
一次删除，数据迁移仍必须有确定的来源、映射、验证和失败策略。

## 7. Retrieval：当前不是瓶颈，但存在 O(history) 风险

最终样本中，Retrieval source discovery 每轮对约 153 个历史 Turn 与 forgetting boundary
发起 index probe。只读 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 的一次样本约为：

- execution 1.122 ms；
- 823 shared-buffer hits；
- 153 个 history candidate/Turn probe；
- 153 个 forgetting-boundary probe；
- 返回 0 个未投影来源。

所以它目前不是端到端延迟瓶颈，不能只凭“O(history)”立即增加新表。但成本随历史线性增长，
应设置触发条件：执行时间、shared hits 或历史行数持续跨越预算后，才引入 completion-time
projection intent，并在同一 inference completion transaction 写入。迁移需要为历史 completed
Turn 回填 intent，保留 `(source, format_version)` 幂等 identity，并证明 forget boundary 与
重投影语义不变。

这与 incremental view maintenance（增量视图维护）的研究方向一致：复用更新增量，而不是
不断重算历史查询。DBToaster 展示了更一般的高阶 delta 技术；本项目未来只需要较简单的 durable
intent，不应照搬完整研究系统。[Ahmad 等，PVLDB 2012](https://www.vldb.org/pvldb/vol5/p968_yanifahmad_vldb2012.pdf)

Beldi 研究展示了 stateful workflow 中 log、transaction 与重放语义的组合价值；其 serverless
runtime 与本项目部署模型不同，因此这里采用的只是 durable intent、幂等与恢复原则，而不是
引入另一套 workflow engine。[Zhang 等，OSDI 2020](https://www.usenix.org/conference/osdi20/presentation/zhang-haoran)

## 8. 失败案例与认知陷阱

| 失败案例 | 原因 | 正确做法 |
|---|---|---|
| 集成测试误连 live DB | 只检查“URL 存在”，未证明隔离 | 拒绝生产 endpoint，并要求测试数据库命名契约 |
| shell 中 `start` 后进程消失 | 执行工具回收 orphan，不是应用 crash | 复用部署 supervisor/tmux/systemd owner，再查应用日志 |
| 用 UTC literal 查询本地重启时间 | aware datetime 的时区语义写错 | 使用 `ZoneInfo` 或数据库 `CURRENT_TIMESTAMP - interval` |
| application 查询迁移表失败 | least privilege 正常生效 | 用 maintenance 进行控制面只读检查 |
| Dashboard traceback | driver 异常未在 CLI 边界映射 | 只捕获预期数据库/配置/OS 错误，保留程序 bug traceback |
| 只看 total tx/s | 无法区分业务、ping 与 telemetry | commit/rollback、表级增量、代码预算三方互证 |
| 看到 index scan 很大就改 schema | Nested Loop 会放大 scan 计数 | 先看 EXPLAIN 时间/buffer，再做增长实验 |
| 降低 recovery 与 claim 到同一慢频率 | 恢复与用户延迟目标混淆 | 独立 cadence，各自拥有测试和 SLO |
| 吞掉 `CancelledError` | TaskGroup 无法可靠 shutdown | cleanup 后原样传播 cancellation |
| 直接删除 failed-final/history | 把证据当噪声 | 先解释增长与补偿策略，保留审计事实 |

## 9. 每次优化的复测清单

1. 固定 workload、配置、进程 owner、预热时间和窗口长度；记录是否有真实流量。
2. 至少三个窗口，报告中位数与范围，不只报告最佳一次。
3. 记录 CPU、RSS、commit、rollback、表级增量、pipeline、errors 和 heartbeat。
4. 对静态预算与动态计数的差异提出假设，再用一项独立证据证伪或确认。
5. 运行定向并发/租约测试，再运行 Ruff、mypy、Pyright 和全量 pytest。
6. 对 PostgreSQL migration 使用隔离实例验证 fresh + historical upgrade。
7. 受控重启后检查日志、进程身份、心跳、积压、过期 lease 和重启后错误。
8. 再跑 dbctl dry-run；若需要新权限，停在明确的管理员边界。
9. 更新本 Runbook 中的方法或风险，不把部署 secret/业务数据写入 Git。

本次最终质量门禁为 908 passed、17 skipped；Ruff、mypy（519 个源文件）和 Pyright 均无
问题。测试数量会随项目变化，后来者应记录自己的完整输出，而不是把这里的数字当作固定阈值。

## 10. 结论应如何写

一份可信的 profiling 结论应包含：观察、假设、独立证据、改动、正确性代价、重复复测和剩余
风险。避免“某库很慢”“异步更快”或“事务太多”这种不可证伪描述。

本次结论是暂时且有边界的：主要空闲成本已经由 adaptive polling、optimistic disconnect、
独立 recovery cadence 和单 claim producer 消除；当前 pipeline 与重启后错误健康。Retrieval
历史发现是可测的增长风险，不是当前瓶颈；reporting/PUBLIC ACL 收敛需要集群管理员，而不是
进一步扩大应用权限。
