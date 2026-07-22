# 数据库控制面 CLI

`fogmoe-dbctl` 是外部 PostgreSQL 的显式控制面。机器人启动过程不会自动建库、迁移或提升权限。

## 架构

```text
cli.py                    组合根：构造 argparse、注册并分发命令
commands/bootstrap.py     建库、三类受管登录角色与初始授权用例
commands/migrate.py       只编排迁移与授权的薄 CLI 用例
commands/access_policy.py 不可变的 schema、routine 与报表关系闭集
commands/access_sql.py    纯 PostgreSQL ACL 收敛与 guard SQL 渲染
commands/migration_execution.py Alembic 与 psql 副作用边界
commands/export_csv.py    通过受配置约束的连接原子导出表为 CSV
postgres.py               PostgreSQL DSN、标识符与连接原语
config.py                 dbctl 窄配置投影、路径与模型校验
migrations/               Alembic 适配层与版本化 SQL
```

依赖方向保持单向：`cli -> migrate -> (access_policy, access_sql, migration_execution) -> postgres/config`。
策略对象与 SQL 渲染不读取配置、不连接数据库；
只有 execution 边界可启动 Alembic 或 `psql`。迁移适配层可以依赖共享 PostgreSQL
原语；配置投影只可依赖中立的 `fogmoe_config.jsonc` 语法解码边界。任何控制面模块都不得
依赖 `fogmoe_bot`。

## 配置边界

`fogmoe-dbctl` 只从仓库根目录的 `config.json`（JSONC）读取它所需的字段：
`database.endpoint`、`database.application`、`database.maintenance`、
`database.reporting`、`database.bootstrap` 与 `identity.administrator`。它拥有自己的窄配置
投影、Pydantic 模型与公开错误，不调用 bot 或 Dashboard 的配置服务；三个程序只共享
`fogmoe_config.jsonc` 的严格 JSONC 语法解码器和用户填写的同一个文件，不共享语义配置模型
或运行时配置对象。

`application`、`maintenance`、`reporting` 是三个不可复用的受管登录身份；它们与
`bootstrap.system_user` 也不得复用。配置模型会在执行任何命令前强制四个角色名两两不同，
防止 bootstrap 把 PostgreSQL 管理员误收敛为低权限角色。默认报表角色名为
`fogmoe-dashboard`。

配置只来自根目录的 JSONC 文档。完整字段、默认值和说明见仓库根目录的
[`example.config.json`](../example.config.json)。

## 添加子命令

1. 在 `commands/` 新建一个按业务能力命名的模块。
2. 实现 `configure_parser(subparsers)`，注册参数并通过 `set_defaults(handler=execute)` 指定处理函数。
3. 实现 `execute(args, *, settings)`，仅编排该命令的用例；可复用的 PostgreSQL 规则放入 `postgres.py`。
4. 在 `cli.py` 的 `COMMAND_MODULES` 中显式注册模块。
5. 为命令注册、参数契约和副作用边界添加测试。

显式注册使帮助输出、命令顺序和发布内容保持确定。不要在命令模块中重新创建根解析器、读取 `sys.argv`，也不要通过 Python 子进程再次启动本项目 CLI。

## 命令

- `bootstrap`：创建或收敛应用、维护、报表角色及数据库 owner，并清理历史角色授权。
- `migrate`：执行 Alembic 迁移，并分别授予应用运行时权限与报表只读权限。
- `shell`：以维护身份启动交互式 `psql`。
- `export-csv`：导出一张受限的 `schema.table`。

导出仅接受 `schema.table`，不接受任意 SQL：

```bash
fogmoe-dbctl export-csv \
  --table conversation.conversation_messages \
  --output ./conversation_messages.csv
```

目标文件已存在时命令会拒绝覆盖；显式传入 `--force` 才会原子替换它。

所有数据库变更仍需由操作者显式运行。`--dry-run` 不显示数据库密码。

默认 `bootstrap` 专用于本机 PostgreSQL：它通过
`sudo -u database.bootstrap.system_user` 切换有效 OS 身份，不向 libpq 传入 host 或 user，
并清除调用者环境里的全部 `PG*` 变量。libpq 因此使用本地 Unix-domain socket，PostgreSQL
再以 peer authentication 将 OS 身份绑定到同名数据库管理员角色；这个路径不需要也不读取
`postgres` 数据库密码。数据库名与端口作为显式 `psql` 参数传入，不依赖 sudo 环境保留。

远程数据库、容器暴露的 TCP endpoint 或无 sudo 的临时测试集群必须显式使用
`bootstrap --no-sudo`；此时才采用 `database.endpoint` 中的 host、port 与 bootstrap 用户。
两种 transport 是互斥语义，不能在 sudo 路径中保留 `PGHOST=localhost`，因为 `localhost`
会强制 TCP 并绕过只适用于本地连接的 peer authentication。该边界对应 PostgreSQL 官方的
[libpq host 选择规则](https://www.postgresql.org/docs/current/libpq-connect.html)与
[peer authentication 约束](https://www.postgresql.org/docs/current/auth-peer.html)。

## 角色与授权边界

`bootstrap` 始终把数据库 owner 校正为 `database.maintenance.username`；报表角色不能成为
数据库 owner。三个登录角色都显式设置为 `NOSUPERUSER`、`NOCREATEDB`、`NOCREATEROLE`、
`NOINHERIT`、`NOREPLICATION` 与 `NOBYPASSRLS`。任一 membership 边以受管角色为被授予方
或 member 时，bootstrap 都会失败关闭（fail closed），不会隐式级联撤销集群级 membership。报表角色额外设置
`default_transaction_read_only=on`，作为对象授权之外的第二层防线。

application/reporting 必须是 FogMoe 专用角色，不得拥有数据库、表空间或目标数据库对象，
也不得持有 default ACL 或列级 ACL。bootstrap 对这些历史状态只做无破坏预检：发现后立即
失败并要求操作者显式转移 ownership 或撤销 ACL；它不会调用可能波及其他数据库 shared
objects 的 `REASSIGN OWNED`/`DROP OWNED`。新建环境及新增 reporting 角色仍应按
`bootstrap -> migrate` 顺序执行，后者负责把已知 schema、relation 和 routine 权限收敛到
闭集。

数据库级默认 `PUBLIC` 权限会被撤销，然后只显式授予：

- maintenance：`CONNECT`、`CREATE`、`TEMPORARY`，并持有数据库 owner；
- application：`CONNECT`、`TEMPORARY`；
- reporting：仅 `CONNECT`。

每次 `migrate` 后，授权会在一笔独立的 `psql --single-transaction` 事务中先撤销再收敛。
PostgreSQL 的 `PUBLIC` 是所有登录角色隐式继承的伪角色，不能由“受管角色没有直接 ACL”
推出不可访问。dbctl 因此还会动态枚举全部非系统用户 schema，撤销 `PUBLIC` 的 schema、
relation、列、序列、routine、type 与 large-object 权限，并以 catalog guard 验证没有残留；
未来 schema/table/sequence/routine/type 的默认权限也同步收紧，而且 guard 会检查所有
owner，不能由第三方角色的 per-schema default ACL 重开访问面。guard 还会通过
effective privilege 检查证明：任何非系统 schema 都没有 maintenance 之外的非超级
LOGIN 角色可以 `CREATE`。对 routine/function 与 type，`pg_default_acl` 缺行会回退到
PostgreSQL 内建的 `PUBLIC EXECUTE`/`PUBLIC USAGE`，因此 maintenance 的全局 `f`/`T`
覆盖行必须显式存在且不含 `PUBLIC`。application 只可直接执行
`observability.ensure_daily_partitions(date)` 与
`observability.drop_partitions_before(date)`，并显式获得业务 type 的 `USAGE`。

唯一例外是 bootstrap superuser 拥有的受信 `vector` extension 成员：maintenance 无权可靠修改
这些对象的 ACL，因此保留扩展自带的 type/routine `PUBLIC` ACL，但撤销 `PUBLIC` 对其所在
`public` schema 的 `USAGE`，只把 schema `USAGE` 直接授予 application。PostgreSQL 同时要求
schema 与对象权限，这使 application 可继续使用 `<=>`，reporting 却无法解析或调用 vector
对象。所有非扩展 routine 均不授予 application 或 reporting。
PostgreSQL 明确指出撤销 schema `USAGE` 不能使既有 session 已完成的名称解析失效；因此应用
0065 时必须先停止 Bot/Dashboard 并在迁移后建立新连接。新建的 reporting 角色天然没有这类
缓存，本仓库的上线流程也把迁移放在进程重启之前。

报表权限只收敛到以下闭集：

- `observability` schema 的 `USAGE`；
- `resources`、`log_records`、`spans`、`metric_points`、`pipeline_health`、
  `turn_latency` 与 `retrieval_queue_health` 的 `SELECT`。

`pipeline_health` 与 `retrieval_queue_health` 是由 maintenance 拥有的聚合观测读模型
（read model），只暴露队列计数、最旧就绪时间与过期 lease；Dashboard 不获得
`retrieval` 或 `user_profile` schema 权限，因此不能读取 embedding、队列 `last_error`、
用户 ID、画像 patch 或 metadata。新增 Dashboard 查询必须显式增加观测读模型和授权 allow-list；
未来表不会自动获得 `SELECT`。

报表角色不会获得 schema/database `CREATE`、表 DML、`TRUNCATE` 或序列
`USAGE`/`SELECT`/`UPDATE`，也不会获得 routine `EXECUTE`。`--skip-grants` 会同时跳过
application 与 reporting 的授权收敛；Alembic 迁移事务和后续授权事务彼此独立。
由于授权 allow-list 描述当前 schema head，`--revision` 指向非 `head` 时必须同时传
`--skip-grants`，避免向旧 revision 授予尚不存在的关系。

0065 会删除来源不可逆推出的历史 `PUBLIC` ACL，因此明确拒绝 downgrade；执行生产迁移前应
保留 dbctl 建议的逻辑备份，而不是用一个猜测性的 down migration 重新开放权限。
