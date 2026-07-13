# Context Window、Retrieval 与 User Profile 架构决策

状态：Accepted

## 1. 决策背景

系统当前把会话消息、模型输入投影、token 预算、压缩任务、累计摘要、旧永久记录、
付费可见额度和 Assistant 检索统一表达为 `conversation.retention_segments`。这些能力
共享数据来源，但不共享变化原因与业务不变量。继续扩展统一 `RetentionSegment` 会让
上下文窗口策略、永久记忆产品语义和历史迁移形状互相渗透。

本决策采用五个已实现边界：

1. `conversation` 是不可变事实日志，拥有 Turn、Message、Reset 和 durable workflow。
2. `context_window` 是推理资源管理，拥有 token budget、history projection、checkpoint、
   compaction lifecycle、lease/fencing 和 provider-neutral summary。
3. `retrieval` 是独立底层能力，拥有 embedding space、passage、异步向量形成、检索与
   provenance；不理解 Assistant、Context 或 Memory 产品语义。
4. `memory` 是 Retrieval 的产品 Consumer，拥有强隔离的 `PersonalMemoryScope | GroupMemoryScope`
   和一次模型 Query 有效的 `WorkingMemory`；它与 `ContextState` 没有对象关系。
5. `user_profile` 是长期语义状态，拥有 evidence log、版本化 Profile、Dreaming queue、
   provenance、lease/fencing 与修正扩展位。Profile 更新由独立后台机制负责，不由
   compaction 触发。

## 2. 依赖方向

```text
domain.context_window ----> domain.conversation (identity/message/payload only)
domain.retrieval ---------> domain.temporal only
domain.memory ------------> domain.temporal only
domain.user_profile ------> domain.temporal only

application.context_window -> domain.context_window + domain.conversation
application.retrieval ------> domain.retrieval
application.memory ---------> application.retrieval + domain.memory
application.user_profile ---> domain.user_profile + narrow runtime/telemetry ports
application.assistant -----> application ports + domain DTO

infrastructure ------------> application ports + domain models
presentation --------------> application use cases
```

禁止：

- `conversation` domain 导入 `context_window`、`retrieval` 或 `user_profile`；
- `retrieval` domain 导入 Conversation、Context Window、provider SDK 或数据库；
- `memory` domain 导入 Context、Conversation、Retrieval 或 infrastructure；
- Assistant Memory tool 暴露 provider/database aggregate 或允许模型指定 scope；
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

`ContextState` 是有显式 UUID identity 的可变实体，只表达当前会话投影。WorkingMemory
不是它的字段、子对象或历史消息。真正调用模型前，AgentLoop 才把两者共同投影为 provider
messages；调用结束后 WorkingMemory 即失效。因此 WorkingMemory 不进入 ContextState、
Conversation message ledger、checkpoint request hash 或 compaction 输入。

### 3.3 Retrieval

回答“哪些历史证据与当前 Query 相关”。一个 embedding space 显式冻结 model、dimension、
query instruction 与 passage format version；不同空间的向量不可比较。Conversation 的完整
Assistant Turn 是当前自然 indexing unit，形成带 `scope_kind + scope_id`、event time 和
source Turn provenance 的 passage。私聊形成 `personal(user_id)` 域；群聊形成
`group(group_id)` 域。个人域与所有群隔离，每个群域又与其他群隔离。累计 compaction
summary 永不进入检索语料。

物理表额外保存只在 personal 域非空的 `personal_user_id` 外键，并用 CHECK 强制它等于
`scope_id`；删除 identity user 会级联清除个人 passage/vector，但群域不会因某位成员删除
而消失。该列只负责 erasure lifecycle，不参与检索路由。

向量形成由固定 worker、lease/fencing、有限 retry 与 reconciliation 驱动；Provider I/O
不发生在数据库事务中。查询必须先用 `scope_kind + scope_id + corpus_id + format_version` 过滤，
再执行 exact cosine ordering。当前单用户语料很小，精确检索具有完整 recall，不创建
HNSW/IVFFlat；只有评测与规模证明必要时才改变物理策略。

### 3.4 User Profile 与 Dreaming

回答“系统跨会话对用户持有什么当前认识，以及为什么可以相信、修正或删除它”。目标
模型是有 provenance 的当前 User Profile，而不是 compaction summary 集合。

Dreaming 是 Runtime 所有的后台 consolidation workflow：唯一 coordinator 从已完成私聊
Assistant Turn 幂等投影 immutable evidence，并为到期且存在新证据的用户形成精确 source
set；固定数量 model consumers 每次只领取一个 job，在数据库事务外调用专用模型，返回严格 JSON patch，再通过
revision compare-and-swap（CAS）与 fencing token 提交。模型只允许 UPSERT/DELETE 有稳定键的
fact、preference、goal 与 interaction-style claim；每个操作必须引用当前批次 event ID。

```text
Conversation completed Turn
        | idempotent projection
        v
evidence_events -> frozen Dream job -> MODEL(Profile_i + Events + Metadata)
                                      -> validated patch + reducer
                                      -> Profile_(i+1)
```

一个 Dream job 同时受事件数和送模字符预算约束。原始用户/Assistant 文本完整保存在 evidence
log；Assistant 回应只是解释上下文，不是用户事实证据，送模时可有界截断。Profile revisions
在没有显式 `/resetprofile` erasure 时 append-only，`profiles.current_revision` 只指向当前版本。
`dream_sources` 是 job 的组成部分，
随 job 或 evidence 生命周期级联清理，不拥有第二份独立消费状态。

Profile 的读取边界是 Turn acceptance。acceptance 在同一短事务中读取一个 committed Profile
snapshot，序列化进 schema-versioned durable inference command。推理首次执行、provider fallback、
retry 与 crash recovery 都只消费该冻结快照，禁止在对话中途重新读取 Profile。后台 Dreaming
即使此时提交新 revision，也只影响之后接受的 Turn。

Profile 当前预留用户纠正、删除和敏感信息策略的扩展位，但不让模型写入 secrets、credentials、
财务余额、权限、医疗诊断、protected/sensitive traits 或面向 Assistant 的命令。所有 Profile
内容在 Context 中标记为 `untrusted_derived_data`。

四个状态不得互相替代：

```text
Context State      = 当前推理所需的 checkpoint summary + recent tail
Episodic Retrieval = 从 Conversation 原始历史检索出的相关证据
User Profile       = 由独立机制维护的当前用户认识
WorkingMemory      = 当前模型 Query 从 Retrieval 临时换入的相关历史证据
```

### 3.5 用户可见的状态管理命令

命令按被管理状态而不是含糊的“记忆”一词划分：

| 命令 | 唯一作用域 | 不会改变 |
|---|---|---|
| `/clear` | 当前 Conversation 的 Context 可见性边界，并取消其中活动推理 | Retrieval、User Profile |
| `/resetmem` | `personal(user_id)` 的 Retrieval passages/vector | Context、群记忆、User Profile |
| `/resetgroup` | 当前 `group(group_id)` 的 Retrieval passages/vector；仅 owner/administrator | 个人记忆、其他群、Context、User Profile |
| `/resetprofile` | 当前用户的 Profile revision、Dream job 与请求前 evidence | Context、Retrieval、Conversation |
| `/regen` | 把当前用户标记为立即可由 Dreaming 消费 | 已提交 Profile、Context、Retrieval |

Retrieval 与 Profile 的清除都不是裸 `DELETE`。每个隔离域维护单调
`forgotten_through`，source discovery 与最终 projection commit 都执行该边界；投影和遗忘共享
transaction-level advisory lock（事务级咨询锁），因此删除前已读出的旧来源也不能在命令提交后
复活。命令确认 outbox 同时充当 result receipt，重放不会误删首次请求之后的新 passage/evidence。

`/resetgroup` 的 Telegram `getChatMember` 结果属于可变外部状态。首次权限判断以 Update ID
first-writer-wins 冻结；at-least-once inbox 重放只读取该事实，不会因管理员身份后来变化而让
同一命令产生不同副作用。

## 4. 迁移策略

项目不保留旧 Python import 或 repository facade。迁移按快速失败切片执行：

1. `0040` 拆开 Conversation、Context Window 与历史 Memory 形状；
2. 停止运行时 `compaction -> memory.records` 投影；
3. `0041` 要求已启用 pgvector，建立独立 `retrieval` schema；
4. 验证所有 legacy archive 都有 Conversation 来源后删除 `memory.records`；
5. 删除旧 Memory Python packages 与两个 permanent-record Assistant tools；
6. 从已完成的私聊 Assistant Turn 自愈 backfill passages 与 1024 维向量；
7. `0042` 删除无产品语义的用户 Memory quota、商店 SKU 与空 `memory` schema；
8. `0043` 建立独立 `user_profile` schema、Dreaming queue 与 append-only revisions，并删除
   旧的 `assistant.ai_user_affection`/impression 路径；
9. acceptance command schema 升级为 v2，直接冻结 Profile snapshot；不保留旧 inference
   command 的运行时兼容分支。
10. `0044` 丢弃无法证明私聊/群聊归属的旧检索投影，以 Conversation canonical log 重新投影，
    并将物理隔离键改为 `scope_kind + scope_id`。
11. `0045` 为 Retrieval/Profile 建立遗忘边界，并冻结破坏性群命令的外部授权决定。

数据迁移允许一次性 backfill，不维持应用层双写。跨上下文派生使用同事务 intent/outbox
和幂等 consumer，避免把两个 aggregate 包装成伪原子大对象。

## 5. 检索与安全

当前 embedding space 使用 `qwen/qwen3-embedding-8b`、1024 维、cosine distance、
query-side instruction 与 passage renderer v1。OpenAI-compatible adapter 严格验证 batch
index、维度、有限性和非零向量；模型/指令/renderer 改变必须创建新 space 并蓝绿 backfill。

AgentLoop 在每次真正的模型调用前使用未经 rewrite 的当前用户文本重新检索若干条
WorkingMemory，并用独立 system message 明示其 `query_only`、`compactable=false` 和
`untrusted_historical_data` 语义。换入受独立的 16384-token 硬预算约束，最后一条 passage
必要时二分截断并显式标记。checkpoint replay 没有模型调用，因而不检索。隔离的 translation
task 不属于会话 Agent query，显式禁用 WorkingMemory。

Assistant 另暴露标准 `search_memory` 自然语言工具。scope 只能从 durable
`ToolExecutionContext` 派生，模型参数中没有 user/group ID。该工具每次 fresh 执行，不写
durable result receipt；tool call、结果和 recalled content 只驻留当前 Agent turn，最终不会
进入 ContextState、Conversation history、runtime events 或 compaction。返回值保留 passage、
source、事件时间、content 与 cosine distance，并有独立 4096-token 回填上限；Agent 可用更
具体的 Query 再次分页，而不是一次换入无界历史。

Retrieval evidence 与 User Profile content 永远视为不可信数据。形成和读取必须保留 provenance、scope isolation、
删除/隔离状态，并在注入模型时使用数据边界，防止持久化 prompt injection。

## 6. 验收标准

- `domain.conversation` 不包含 retention/compaction/retrieval/profile 模型；
- `domain.retrieval` 不依赖产品来源或 infrastructure；
- context-window persistence 不读取或写入 `memory.records`；
- compaction 完成只改变 checkpoint 状态，不形成 Memory/User Profile；
- User Profile 由独立 evidence/revision/Dreaming lifecycle 维护，且不依赖 compaction；
- 每个 Dream source set 精确、输入有界，模型调用发生在事务外；
- Dream completion 使用 lease、fencing 与 Profile revision CAS 拒绝迟到结果；
- acceptance 恰好读取一次 committed Profile 并冻结进 durable command；推理不重新读取；
- Profile claim 必须具有当前 evidence provenance，并作为不可信派生数据注入 Context；
- `memory.records` 与旧 Memory Python facade 不存在；
- `memory` schema、旧用户 quota 与商店 SKU 不存在；
- Assistant Memory operation 只依赖 typed WorkingMemory port；
- WorkingMemory 与 ContextState 无对象关系，只在 provider 投影边界共同组成模型输入；
- 每次真实模型 Query fresh retrieve；checkpoint replay 不检索；
- personal/group 与 group/group 隔离同时受类型、SQL predicate 和真实 PostgreSQL 测试约束；
- `/clear`、`/resetmem`、`/resetgroup`、`/resetprofile` 与 `/regen` 不共享含糊删除语义；
- 遗忘边界同时约束 source discovery 与 projection commit，旧数据不能被后台 worker 复活；
- `/resetgroup` 只接受 durable owner/administrator 决定，同一 Update 重放不重新解释权限；
- `search_memory` 结果不缓存、不落 Conversation 且不参与 compaction；
- passage projection 与 vector completion 具有 idempotency、lease/fencing 和真实 PostgreSQL 契约；
- exact search 在 SQL 中先执行 scope/corpus 过滤，跨个人/群及跨群命中数必须为零；
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
- [Generative Agents, UIST 2023](https://dl.acm.org/doi/10.1145/3586183.3606763)：将观察
  与周期性 reflection/consolidation 分离，为后台 Dreaming 提供了可比较的研究范式。
- [Mem0, 2025](https://arxiv.org/abs/2504.19413)：显式提取、更新与删除长期状态，相比
  无界历史或统一摘要更适合低延迟生产系统；其评测结论仍需以本系统数据复验。
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
- [PostgreSQL `SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html)：
  queue-like 多消费者可跳过已锁行，但必须与 durable status、lease 和 fencing 一起使用。
- [PostgreSQL Explicit Locking](https://www.postgresql.org/docs/current/explicit-locking.html)：
  transaction-level advisory lock 适合表达数据库本身不知道的应用互斥域，并随事务自动释放。
- [Telegram Bot API `getChatMember`](https://core.telegram.org/bots/api#getchatmember)：
  查询群成员权限；对其他用户只有 Bot 为管理员时才保证可用。
