# Context Window、Retrieval 与 User Profile 架构决策

状态：Accepted

## 1. 决策背景

系统当前把会话消息、模型输入投影、token 预算、压缩任务、累计摘要、旧永久记录、
付费可见额度和 Assistant 检索统一表达为 `conversation.retention_segments`。这些能力
共享数据来源，但不共享变化原因与业务不变量。继续扩展统一 `RetentionSegment` 会让
上下文窗口策略、永久记忆产品语义和历史迁移形状互相渗透。

本决策采用三个已实现边界与一个保留扩展边界：

1. `conversation` 是不可变事实日志，拥有 Turn、Message、Reset 和 durable workflow。
2. `context_window` 是推理资源管理，拥有 token budget、history projection、checkpoint、
   compaction lifecycle、lease/fencing 和 provider-neutral summary。
3. `retrieval` 是独立底层能力，拥有 embedding space、passage、异步向量形成、检索与
   provenance；当前 Consumer 是 Conversation episodic history。
4. User Profile 是保留的逻辑扩展边界，未来拥有 profile scope、provenance、修正、
   遗忘和用户可见策略；当前没有 `memory` package 或 schema。Profile 更新由独立机制
   负责，不由 compaction 触发。

## 2. 依赖方向

```text
domain.context_window ----> domain.conversation (identity/message/payload only)
domain.retrieval ---------> domain.temporal only

application.context_window -> domain.context_window + domain.conversation
application.retrieval ------> domain.retrieval
application.assistant -----> application ports + domain DTO

infrastructure ------------> application ports + domain models
presentation --------------> application use cases
```

禁止：

- `conversation` domain 导入 `context_window`、`retrieval` 或未来 `memory`；
- `retrieval` domain 导入 Conversation、Context Window、provider SDK 或数据库；
- Assistant retrieval tool 暴露 provider/database aggregate；
- context-window checkpoint 充当可修改的用户事实；
- compaction completion 创建或更新 Memory/User Profile；
- Memory/User Profile 的生命周期、更新频率或内容形状依赖 compaction 阈值；
- package root 重导出类型以伪造兼容 facade；
- 为每个类型创建只有 `__init__.py` 的空壳子包。

Retrieval 使用不透明 `source_kind + source_id` 保存 provenance，不导入来源 aggregate。
跨上下文行为通过窄 Protocol 和不可变 DTO 表达。

## 3. 核心语义

### 3.1 Conversation

回答“发生过什么”。Message ledger 是上游事实来源；reset 只改变后续上下文可见边界，
不暗示永久记忆删除。

### 3.2 Context Window

回答“本次推理能够安全携带什么”。Compaction checkpoint 是可重建的派生 artifact，
其 identity 由 conversation epoch、连续 sequence range 和 projection version 决定。
`CompactionSummary` 的唯一产品语义是替换过长的当前 Context State：推理使用“累计旧历史
摘要 + 未压缩 recent tail”，而不是继续携带完整旧前缀。它不是长期记忆、用户事实或
User Profile 的形成输入。

checkpoint 的状态机只允许：

```text
pending -> processing -> completed
                      -> retry_wait -> processing
                      -> failed_final
```

任何迟到 worker 必须被 fencing token 拒绝。token budget 与 projection algorithm version
属于该上下文，不属于 memory。

### 3.3 Retrieval

回答“哪些历史证据与当前 Query 相关”。一个 embedding space 显式冻结 model、dimension、
query instruction 与 passage format version；不同空间的向量不可比较。Conversation 的完整
私聊 Turn 是当前自然 indexing unit，形成带 owner、event time 和 source Turn provenance
的 passage。累计 compaction summary 永不进入检索语料。

向量形成由固定 worker、lease/fencing、有限 retry 与 reconciliation 驱动；Provider I/O
不发生在数据库事务中。查询必须先用 `owner_user_id + corpus_id + format_version` 过滤，
再执行 exact cosine ordering。当前单用户语料很小，精确检索具有完整 recall，不创建
HNSW/IVFFlat；只有评测与规模证明必要时才改变物理策略。

### 3.4 Memory / User Profile 扩展位

回答“系统跨会话对用户持有什么当前认识，以及为什么可以相信、修正或删除它”。目标
模型是有 provenance 的 User Profile，而不是 compaction summary 集合。Profile 的更新
需要独立的形成、冲突处理、supersession 和用户纠正机制；本决策只保留扩展边界，不预先
规定其触发频率、提取模型或内部 schema。

当前不实现 User Profile 更新机制，也不保留用 compaction summary 伪装的 Memory record。
旧 `memory.records` 在验证每条 archive 都已有 Conversation 来源后删除；历史读取由
Conversation episodic retrieval 取代。未来 Profile 形成独立 ADR、schema 与 worker，
不得复用 compaction lifecycle 或 passage-vector state machine。

三个状态不得互相替代：

```text
Context State      = 当前推理所需的 checkpoint summary + recent tail
Episodic Retrieval = 从 Conversation 原始历史检索出的相关证据
User Profile       = 由独立机制维护的当前用户认识
```

## 4. 迁移策略

项目不保留旧 Python import 或 repository facade。迁移按快速失败切片执行：

1. `0040` 拆开 Conversation、Context Window 与历史 Memory 形状；
2. 停止运行时 `compaction -> memory.records` 投影；
3. `0041` 要求已启用 pgvector，建立独立 `retrieval` schema；
4. 验证所有 legacy archive 都有 Conversation 来源后删除 `memory.records`；
5. 删除旧 Memory Python packages 与两个 permanent-record Assistant tools；
6. 从已完成的私聊 Assistant Turn 自愈 backfill passages 与 1024 维向量；
7. `0042` 删除无产品语义的用户 Memory quota、商店 SKU 与空 `memory` schema；
8. User Profile 通过后续独立决策演进，不复用 retrieval 或 compaction lifecycle。

数据迁移允许一次性 backfill，不维持应用层双写。跨上下文派生使用同事务 intent/outbox
和幂等 consumer，避免把两个 aggregate 包装成伪原子大对象。

## 5. 检索与安全

当前 embedding space 使用 `qwen/qwen3-embedding-8b`、1024 维、cosine distance、
query-side instruction 与 passage renderer v1。OpenAI-compatible adapter 严格验证 batch
index、维度、有限性和非零向量；模型/指令/renderer 改变必须创建新 space 并蓝绿 backfill。

Assistant 只暴露 `recall_conversation_history` 自然语言工具；旧摘要分页和正则扫描工具已
删除。返回值保留 source Turn、事件时间、excerpt 与 cosine distance。检索内容始终视为
不可信历史数据，不获得 system/instruction 权限。

Retrieval evidence 与未来 User Profile content 永远视为不可信数据。形成和读取必须保留 provenance、scope isolation、
删除/隔离状态，并在注入模型时使用数据边界，防止持久化 prompt injection。

## 6. 验收标准

- `domain.conversation` 不包含 retention/compaction/retrieval/profile 模型；
- `domain.retrieval` 不依赖产品来源或 infrastructure；
- context-window persistence 不读取或写入 `memory.records`；
- compaction 完成只改变 checkpoint 状态，不形成 Memory/User Profile；
- User Profile 保留独立扩展边界，且不依赖 compaction lifecycle；
- `memory.records` 与旧 Memory Python facade 不存在；
- `memory` schema、旧用户 quota 与商店 SKU 不存在；
- Assistant retrieval operation 只依赖 typed recall port；
- passage projection 与 vector completion 具有 idempotency、lease/fencing 和真实 PostgreSQL 契约；
- exact search 在 SQL 中先执行 owner/corpus 过滤，跨用户命中数必须为零；
- compaction 状态机、fencing、digest、连续 range 与 token budget 测试全部保留；
- reset、forget、correct、erase 是不同命令；
- mypy、pyright、ruff、全量 pytest 与 AST architecture gates 通过。

## 7. 参考依据

- [Martin Fowler, *Bounded Context*](https://martinfowler.com/bliki/BoundedContext.html)：
  大模型必须以显式边界和关系保持内部一致性。
- [Python 3.14 typing](https://docs.python.org/3.14/library/typing.html) 与
  [dataclasses](https://docs.python.org/3.14/library/dataclasses.html)：使用 nominal value
  object、frozen slotted dataclass 与 exhaustive union 表达状态空间。
- [MemGPT](https://arxiv.org/abs/2310.08560)：上下文窗口和外部长期存储是不同 memory tier。
- [LongMemEval, ICLR 2025](https://openreview.net/forum?id=pZiyCaVuti)：长期记忆需要信息
  提取、多 session 推理、时间推理、更新与拒答，不能以累计摘要代替完整产品语义。
- [Qwen3-Embedding-8B model card](https://huggingface.co/Qwen/Qwen3-Embedding-8B)：
  query instruction、MRL 与 32--4096 可变输出维度。
- [pgvector](https://github.com/pgvector/pgvector)：exact search、cosine operator、过滤与
  ANN recall/performance 取舍。
- [OpenRouter Embeddings API](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings)：
  OpenAI-compatible batch input、dimensions、input type 与 response index contract。
- [LangGraph Memory Overview](https://docs.langchain.com/oss/python/concepts/memory)：
  thread-scoped short-term state 与 namespaced long-term store 分离。
- [OWASP Agent Memory Guard](https://owasp.org/www-project-agent-memory-guard/)：
  持久记忆是跨 session poisoning 攻击面。
