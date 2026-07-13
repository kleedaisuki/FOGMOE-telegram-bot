# FogMoe 可观测性数据契约

FogMoe 将可观测性（observability）视为业务运行时的一部分：它记录用户可见结果、
durable pipeline、外部依赖、运行时饱和度与遥测自身健康，而不是仅收集 Python 日志。

## 信号分工

| 信号 | 目的 | 保留原则 |
|---|---|---|
| Metrics | SLO、告警、容量与成本趋势 | 业务 outcome 全量、低基数聚合 |
| Spans/Traces | 定位单次 Turn 的因果链与慢路径 | 失败和慢路径完整保留；普通路径按数据量配置保留 |
| Logs | 为失败提供人类可读上下文 | 带稳定 event name 与 correlation IDs |

所有信号写入独立、有界、异步的 exporter；遥测不可阻塞 Telegram、推理或投递热路径。

## 关联模型

每个 active span 建立 `trace_id`、`span_id` 与不可变关联属性。子 span 继承关联属性；
标准库日志在 producer thread 冻结 trace 与属性，再交由异步 listener 持久化。因此同一
Turn 内的日志可使用 `turn_id`、`update_id`、`activity_id` 或 `outbound_message_id` 下钻。

高基数 identity 仅能位于 span/log attributes：

- `fogmoe.turn.id`
- `fogmoe.update.id`
- `fogmoe.activity.id`
- `fogmoe.outbound.id`

它们禁止作为 metric 标签。Metric 只允许有限枚举，例如 `outcome`、`pipeline.stage`、
`gen_ai.provider.name`、`gen_ai.request.model`、`gen_ai.tool.name`、`message.kind`。

## 业务指标

核心 counter 使用 `fogmoe.<area>.outcomes`，通过 `outcome` 表示 `success`、`failure`、
`timeout`、`retry` 或 `dropped`：

- `fogmoe.inbox.outcomes`
- `fogmoe.inference.outcomes`
- `fogmoe.outbox.outcomes`
- `fogmoe.llm.outcomes`
- `fogmoe.tool.outcomes`
- `fogmoe.retrieval.outcomes`
- `fogmoe.user_profile.outcomes`
- `fogmoe.dependency.outcomes`

`fogmoe.pipeline.lease.recoveries` 使用 `pipeline.stage` 区分 inbox、inference、outbox 与
retrieval 与 `user_profile.dreaming`。
runtime sampler 每 15 秒补充 mailbox、telemetry queue、exporter、RSS、累计 CPU、FD、
event-loop lag 与系统一分钟负载。Telemetry buffer 还按 `log`、`span`、`metric` 三类
分别暴露累计 accepted/dropped；因此可以区分“业务流量很高”与“日志或 Trace 被队列丢弃”。

## 外部依赖与数据库

LLM、Agent tool、外部读取、图像生成与语音生成均产生 client span 和 dependency outcome。
数据库 span 遵循 OpenTelemetry database client 语义：记录 PostgreSQL、低基数操作、可安全
提取的 `schema.table` target 与 batch 信息，绝不记录 SQL 文本、参数或凭据。

Dashboard 的总览和健康趋势排除数据库 client span，避免频繁轮询把用户体验延迟淹没；
Operations 页面仍保留这些 span 以供数据库排障。

## Retrieval 性能契约

Retrieval 使用封闭的低基数 operation 名称形成完整耗时链：

- `retrieval.projection.batch`：非空 Conversation Source 批次形成 Passage；
- `retrieval.embedding.batch`：durable claim 到 fenced completion 的批次处理；
- `retrieval.embedding.request`：OpenAI-compatible Provider HTTP 请求；
- `retrieval.recall`：Assistant 语义召回端到端；
- `retrieval.query.embedding`：Query embedding；
- `retrieval.search`：租户过滤后的 pgvector exact search。
- `memory.working.retrieve`：每次实际模型 Query 的 WorkingMemory 换入总耗时；checkpoint
  replay 不产生该 span。

空 polling 不产生 span 或 metric point，避免闲置实例污染吞吐与延迟分位数。非空工作额外
记录 `fogmoe.retrieval.batch.size`、`fogmoe.retrieval.source.discovery.duration` 和
`fogmoe.retrieval.vector.claim.duration`；duration 单位遵循 OpenTelemetry 约定使用秒。
Dashboard 的 Retrieval 页面将这些时间窗信号（包括 WorkingMemory 换入）与
`retrieval.passage_vectors` 的实时状态
组合，呈现每个 Embedding Space 的 pending、processing、retry、completed、failed、最老
积压年龄与过期 lease。查询使用稳定 operation/metric 精确集合，使分区索引可用于时间窗
聚合，禁止用前缀模糊扫描扩大监控成本。

## Dashboard 与数据保留

Dashboard 使用独立只读连接。Metrics 页面按完整 attributes 分组，因而能分别显示例如
`outcome=success` 与 `outcome=failure`，不会把它们错误合并。日志表显示 Event、Trace 和
Turn。Overview 的 durable pipeline 同时呈现 Dreaming job 的 pending、processing、retry、
failed-final、最老 ready 时间与过期 lease；Operations/AI/Metrics 页面分别展示 evidence
projection、Dreaming/model latency、provider usage 与 outcome。

生产部署应采用分层保留：错误/慢 Trace 保留较久；普通 Trace 与 INFO 日志按容量采样；
metrics 先高精度保留再降采样。保留期限、采样率与 SLO 阈值属于部署配置，而非业务代码。
