# FogMoe Dashboard

FogMoe Dashboard 是 observability schema 的内建分析客户端。它不是任意 SQL
代理，而是一组参数化、有界、可审计的诊断查询。原生 Qt GUI、交互终端和 Python
API 使用同一套封闭查询语言与不可变返回模型，因此三个入口不会逐渐产生不同语义。

## 原生 GUI

GUI 作为可选依赖安装，避免在仅运行 bot 或 CLI 的服务器上引入约 100 MiB 的 Qt
runtime：

~~~bash
pip install -e '.[dashboard-gui]'
fogmoe-dashboard-gui --window 6h
fogmoe-dashboard-gui --window 24h --auto-refresh 10
~~~

它包含七个面向排障问题组织的工作区：

| 工作区 | 交互与可视化 |
|---|---|
| 总览 | KPI、吞吐/错误率/p95 时间序列、durable pipeline 饱和度 |
| 操作 | 精确 span-name 筛选、p50/p95/p99 对比、完整 RED 表 |
| 事件与日志 | severity/logger 筛选、统一错误流、双击下钻 trace |
| Traces | error-only 筛选、master-detail、waterfall、关联日志与 attributes |
| Metrics | 精确 metric 筛选、latest/average/min–max 范围图、Counter rate |
| 可靠性与依赖 | inbox/inference/outbox/LLM/tool/dependency outcome、lease recovery 与遥测健康 |
| AI 与 Turn | provider/model token 与 p95、Turn 分阶段延迟及 slow Turns |
| Resources | service/version/environment/instance 生命周期 |

全局窗口可选 15 分钟至 30 天；手动刷新和 2–300 秒自动刷新使用相同语义。每次刷新
产生单调的 generation，旧 generation 即使较晚返回也不会覆盖新状态。查询期间界面
仍可导航、滚动和复制数据。

Metrics 按 metric 名称、种类、单位和完整低基数 attributes 分组。例如同一个
`fogmoe.outbox.outcomes` 会分别显示 `outcome=success`、`outcome=retry` 和
`outcome=dropped`，不会把不同结果混为一个平均值。总览与健康趋势刻意排除 PostgreSQL
client spans；数据库详细操作仍在“操作”视图中可查，避免高频数据库轮询掩盖用户 Turn
的端到端延迟。完整的数据契约见 [可观测性文档](observability.md)。

### GUI 并发与层次边界

~~~text
Qt widgets / Matplotlib canvas
        │ DashboardQuery + immutable DashboardResult
        ▼
application.execute_query ──► Dashboard use cases / repository port
        │
        ▼
single QThread + single persistent asyncio loop ──► asyncpg read-only pool
~~~

Qt 主线程只处理 widget；后台 `QThread` 独占一个持久 `asyncio` event loop 和 asyncpg
连接池。这样既不跨 event loop 使用连接，也不让 PostgreSQL I/O 阻塞 GUI。页面只声明
强类型 `DashboardQuery`，不接触连接、SQL 或线程。`ObjectTableModel[T]` 保留原始领域
对象，显示文本只是 Qt role 的投影，而不是第二份字符串数据模型。

图表采用 Matplotlib 的原生 `QtAgg` canvas。Plotly 的交互 renderer 依赖浏览器中的
plotly.js；在桌面 Qt 内通常还要引入 Qt WebEngine/Chromium。当前数据量受查询上限约束，
原生 canvas 已提供更小的部署面和一致的 Qt 生命周期；若未来需要百万点 WebGL 或跨浏览器
共享，再把 Plotly/Dash 作为独立 web presentation，而不是塞进本地窗口。

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
TurnLatencyStats、HealthPoint 与 ResourceInstance。

## 设计依据

- Qt 官方 [Model/View Programming](https://doc.qt.io/qtforpython-6.10/overviews/qtwidgets-model-view-programming.html)
  将数据、视图与 delegate 分离；本项目用 `QAbstractTableModel` 直接投影不可变领域对象。
- Matplotlib 官方 [Embed in Qt](https://matplotlib.org/stable/gallery/user_interfaces/embedding_in_qt_sgskip.html)
  说明 `FigureCanvasQTAgg` 的原生嵌入方式。
- Davidson、Wall 与 Mace 的 IEEE TVCG 研究
  [A Qualitative Interview Study of Distributed Tracing Visualisation](https://doi.org/10.1109/TVCG.2023.3241596)
  指出现有 tracing 工具在 sensemaking 上的不足；这里用摘要筛选、waterfall、logs 与
  attributes 同屏联动，而不是把 waterfall 当作孤立终点。
- Plotly 官方 [renderers 文档](https://plotly.com/python/renderers/) 明确交互图由
  plotly.js 在浏览器执行；因此当前 native desktop 路径不承担浏览器 runtime。

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
