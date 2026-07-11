# 数据库控制面 CLI

`fogmoe-dbctl` 是外部 PostgreSQL 的显式控制面。机器人启动过程不会自动建库、迁移或提升权限。

## 架构

```text
cli.py                    组合根：构造 argparse、注册并分发命令
commands/bootstrap.py     建库、角色与本地 psql 配置用例
commands/migrate.py       Alembic 迁移与运行时授权用例
commands/export_csv.py    通过 psql service 原子导出表为 CSV
postgres.py               PostgreSQL URL、标识符、pgpass、service 共享原语
config.py                 项目路径、schema 拓扑与环境配置
migrations/               Alembic 适配层与版本化 SQL
```

依赖方向保持单向：`cli -> commands -> postgres/config`。迁移适配层可以依赖共享 PostgreSQL 原语；任何控制面模块都不得依赖 `fogmoe_bot`。

## 添加子命令

1. 在 `commands/` 新建一个按业务能力命名的模块。
2. 实现 `configure_parser(subparsers)`，注册参数并通过 `set_defaults(handler=execute)` 指定处理函数。
3. 实现 `execute(args)`，仅编排该命令的用例；可复用的 PostgreSQL 规则放入 `postgres.py`。
4. 在 `cli.py` 的 `COMMAND_MODULES` 中显式注册模块。
5. 为命令注册、参数契约和副作用边界添加测试。

显式注册使帮助输出、命令顺序和发布内容保持确定。不要在命令模块中重新创建根解析器、读取 `sys.argv`，也不要通过 Python 子进程再次启动本项目 CLI。

## 兼容命令

- `bootstrap-postgres`，别名 `bootstrap`
- `migrate`，别名 `upgrade`、`run-migrations-as-role`
- `export-csv`，别名 `export`

导出仅接受 `schema.table`，不接受任意 SQL：

```bash
fogmoe-dbctl export-csv \
  --table conversation.chat_records \
  --output ./chat_records.csv
```

目标文件已存在时命令会拒绝覆盖；显式传入 `--force` 才会原子替换它。

所有数据库变更仍需由操作者显式运行。`--dry-run` 不显示数据库密码。
