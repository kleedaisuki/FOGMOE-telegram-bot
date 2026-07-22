# 数据库控制面 CLI

`fogmoe-dbctl` 是外部 PostgreSQL 的显式控制面。机器人启动过程不会自动建库、迁移或提升权限。

## 架构

```text
cli.py                    组合根：构造 argparse、注册并分发命令
commands/bootstrap.py     建库、三类登录角色与初始授权用例
commands/migrate.py       Alembic 迁移、运行时与只读报表授权用例
commands/export_csv.py    通过受配置约束的连接原子导出表为 CSV
postgres.py               PostgreSQL DSN、标识符与连接原语
config.py                 项目路径、schema 拓扑与 config.json 解析
migrations/               Alembic 适配层与版本化 SQL
```

依赖方向保持单向：`cli -> commands -> postgres/config`。迁移适配层可以依赖共享 PostgreSQL 原语；任何控制面模块都不得依赖 `fogmoe_bot`。

## 配置边界

`fogmoe-dbctl` 只从仓库根目录的 `config.json`（JSONC）读取它所需的字段：
`database.endpoint`、`database.application`、`database.maintenance`、
`database.reporting`、`database.bootstrap` 与 `identity.administrator`。它拥有自己的窄配置
解析器，不调用 bot 或 Dashboard 的配置服务；三个程序只共享用户填写的同一个配置文件，
而不共享运行时配置对象。

`application`、`maintenance`、`reporting` 是三个不可复用的登录身份，配置模型会在执行任何
命令前强制其 `username` 两两不同。默认报表角色名为 `fogmoe-dashboard`。

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

- `bootstrap`：创建或收敛应用、维护、报表角色及数据库 owner。
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

## 角色与授权边界

`bootstrap` 始终把数据库 owner 校正为 `database.maintenance.username`；报表角色不能成为
数据库 owner。三个登录角色都显式设置为 `NOSUPERUSER`、`NOCREATEDB`、`NOCREATEROLE`、
`NOREPLICATION` 与 `NOBYPASSRLS`。报表角色额外设置
`default_transaction_read_only=on`，作为对象授权之外的第二层防线。

数据库级默认 `PUBLIC` 权限会被撤销，然后只显式授予：

- maintenance：`CONNECT`、`CREATE`、`TEMPORARY`，并持有数据库 owner；
- application：`CONNECT`、`TEMPORARY`；
- reporting：仅 `CONNECT`。

每次 `migrate` 后，报表权限都会先撤销再收敛到以下闭集：

- 每个应用 schema 的 `USAGE`；
- 已有表及未来由 maintenance 创建的表的 `SELECT`。

报表角色不会获得 schema/database `CREATE`、表 DML、`TRUNCATE` 或序列
`USAGE`/`SELECT`/`UPDATE`。`--skip-grants` 会同时跳过 application 与 reporting 的授权步骤。
