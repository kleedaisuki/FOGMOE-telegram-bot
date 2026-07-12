# FogMoe Dashboard

**fogmoe-dashboard** 是 observability schema 的内建分析客户端。它不是任意 SQL
代理，而是一组参数化、有界、可审计的诊断查询。交互终端和 Python API 使用同一组
不可变返回模型，因此两者不会逐渐产生不同语义。

## 连接与安全边界

连接优先级如下：

1. 显式 --database-url；
2. 项目 .env 中的 DATABASE_URL；
3. var/psql/pg_service.conf 中的 fogmoe_automation service 与对应 pgpass。

Dashboard 连接设置 default_transaction_read_only=on，并具有独立连接池、默认五秒
statement timeout、一秒 lock timeout、最多 1,000 行的 API limit，以及最长 90 天的
查询窗口。即使 automation role 拥有写权限，Dashboard 自身仍不能意外修改生产数据。

~~~bash
fogmoe-dashboard \
  --config-dir ./var/psql \
  --service fogmoe_automation \
  --timeout 10 \
  --window 24h \
  overview
~~~

## 内建视图

| 视图 | 问题 |
|---|---|
| overview | 系统请求量、错误率、p50/p95/p99、token 和 pipeline 是否健康？ |
| pipeline | inbox、inference、outbox 的积压、重试、最终失败、过期 lease 是多少？ |
| spans | 哪个操作最慢、调用最多或错误最多？ |
| errors | 最近的错误 span 和 error log 按时间合并后是什么？ |
| logs | 某严重度或 logger 的结构日志是什么？ |
| traces | 最近或含错误的 trace 有哪些？ |
| trace | 一条 trace 的父子 waterfall 与关联日志是什么？ |
| metrics | Gauge/Counter 在窗口内的最新值、范围和平均值是什么？ |
| ai | 各 provider/model 的调用、错误、token 与延迟如何？ |
| latency | Turn 端到端、推理、投递延迟及最慢 Turn 如何？ |
| resources | 哪些服务实例在何时启动、停止，当前是否存活？ |
| watch | overview 的实时刷新结果如何？ |

~~~bash
fogmoe-dashboard --window 1h spans --name chat
fogmoe-dashboard --window 24h logs --severity error --limit 200
fogmoe-dashboard --window 6h traces --errors-only
fogmoe-dashboard --window 7d ai
fogmoe-dashboard --window 24h latency --limit 50
~~~

watch 在终端使用 Rich Live 原地刷新；JSON 模式输出 JSON Lines，适合流式脚本：

~~~bash
fogmoe-dashboard --window 15m watch --interval 5
fogmoe-dashboard --format json --window 15m watch --interval 5 | jq -c '.data'
~~~

## 脚本接口

公共 API 是异步的，避免隐藏 event loop 或在脚本中建立无法关闭的连接池：

~~~python
from datetime import timedelta

from fogmoe_dashboard import DashboardClient, TimeWindow


async def inspect() -> None:
    window = TimeWindow.last(timedelta(hours=6))
    async with DashboardClient.from_environment() as dashboard:
        overview = await dashboard.overview(window)
        slow_operations = await dashboard.spans(window, limit=20)
        failures = await dashboard.errors(window, limit=100)

    print(overview.span_error_rate)
    print(slow_operations[0] if slow_operations else "no spans")
    print(len(failures))
~~~

也可显式使用 URL：

~~~python
dashboard = DashboardClient.from_database_url(
    "postgresql://user:password@localhost/fogmoe",
    pool_size=2,
    command_timeout=3,
)
~~~

公开方法均返回 fogmoe_dashboard.domain.models 中的 frozen、slotted dataclass，
包括 Overview、SpanStats、ErrorEvent、TraceDetail、MetricStats、GenAiStats、
TurnLatencyStats 与 ResourceInstance。

## 自由 SQL shell

预定义 Dashboard 刻意不接受任意 SQL。需要临时分析、expanded output、watch、copy
或查询计划时，使用完整的 PostgreSQL 客户端：

~~~bash
fogmoe-dbctl shell
fogmoe-dbctl shell --no-psqlrc
~~~

该命令设置 PGSERVICE=fogmoe_automation、PGSERVICEFILE、PGPASSFILE 和
PGAPPNAME=fogmoe-dbctl-shell，前台继承当前 TTY，并把 psql 的退出状态原样返回。
数据库参数默认且显式为 fogmoe；host、port 和 role 仍来自 service。密码不会出现
在 argv、日志或 PGPASSWORD 中。
