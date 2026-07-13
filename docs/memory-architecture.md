# Memory 与 Context Window 架构决策

状态：Accepted

## 1. 决策背景

系统当前把会话消息、模型输入投影、token 预算、压缩任务、累计摘要、旧永久记录、
付费可见额度和 Assistant 检索统一表达为 `conversation.retention_segments`。这些能力
共享数据来源，但不共享变化原因与业务不变量。继续扩展统一 `RetentionSegment` 会让
上下文窗口策略、永久记忆产品语义和历史迁移形状互相渗透。

本决策采用三个显式限界上下文（Bounded Context）：

1. `conversation` 是不可变事实日志，拥有 Turn、Message、Reset 和 durable workflow。
2. `context_window` 是推理资源管理，拥有 token budget、history projection、checkpoint、
   compaction lifecycle、lease/fencing 和 provider-neutral summary。
3. `memory` 是跨会话产品知识，拥有 scope、record、provenance、检索、修正、遗忘和
   用户可见策略。

## 2. 依赖方向

```text
domain.context_window ----> domain.conversation (identity/message/payload only)
domain.memory ------------> domain.conversation (source reference identity only)

application.context_window -> domain.context_window + domain.conversation
application.memory --------> domain.memory
application.assistant -----> application ports + domain DTO

infrastructure ------------> application ports + domain models
presentation --------------> application use cases
```

禁止：

- `conversation` domain 导入 `context_window` 或 `memory`；
- `memory` domain 导入 compaction aggregate；
- Assistant memory tool 暴露数据库 aggregate；
- context-window checkpoint 充当可修改的用户事实；
- package root 重导出类型以伪造兼容 facade；
- 为每个类型创建只有 `__init__.py` 的空壳子包。

共享 `ConversationId`、`TurnId` 与 provider-neutral JSON 是 source identity，不代表聚合
所有权共享。跨上下文行为通过窄 Protocol 和不可变 DTO 表达。

## 3. 核心语义

### 3.1 Conversation

回答“发生过什么”。Message ledger 是上游事实来源；reset 只改变后续上下文可见边界，
不暗示永久记忆删除。

### 3.2 Context Window

回答“本次推理能够安全携带什么”。Compaction checkpoint 是可重建的派生 artifact，
其 identity 由 conversation epoch、连续 sequence range 和 projection version 决定。
checkpoint 的状态机只允许：

```text
pending -> processing -> completed
                      -> retry_wait -> processing
                      -> failed_final
```

任何迟到 worker 必须被 fencing token 拒绝。token budget 与 projection algorithm version
属于该上下文，不属于 memory。

### 3.3 Memory

回答“系统跨会话知道什么，以及为什么可以相信、展示或删除它”。Memory record 至少
表达稳定 identity、namespace/scope、kind、结构化 content、source provenance、event
time、生命周期状态和创建时间。语义事实更新通过 supersession 表达，不覆盖来源。

旧永久 snapshot 是 `legacy_archive` memory record；它不是 compaction job 的特殊状态。
累计 checkpoint 可作为 memory formation 的输入来源，但两者拥有独立 identity 与
lifecycle。

## 4. 迁移策略

项目不保留旧 Python import 或 repository facade。迁移按快速失败切片执行：

1. 先移动 domain/application 所有权并由 AST gate 锁定依赖；
2. 拆分 PostgreSQL compaction store 与 memory reader；
3. 新建 `memory` schema，迁移 legacy archives；
4. 将 compaction completion 通过幂等 formation intent 投影为现有产品可见记录；
5. shadow-read 验证 record ID、quota、排序和工具 JSON 语义后切换；
6. 删除 `retention` 名称、kind union、nullable legacy shape 与旧表字段。

数据迁移允许一次性 backfill，不维持应用层双写。跨上下文派生使用同事务 intent/outbox
和幂等 consumer，避免把两个 aggregate 包装成伪原子大对象。

## 5. 检索与安全

第一阶段使用可索引的 PostgreSQL metadata/full-text 查询；只有在固定评测集证明必要后
才加入 embedding、hybrid retrieval 或 reranker。禁止对任意数量的大型 snapshot 使用
无超时 Python 正则扫描。

Memory content 永远视为不可信数据。形成和读取必须保留 provenance、scope isolation、
删除/隔离状态，并在注入模型时使用数据边界，防止持久化 prompt injection。

## 6. 验收标准

- `domain.conversation` 不再包含 retention/compaction/permanent-memory 模型；
- context-window 与 memory 没有 aggregate 级依赖；
- Assistant memory ports 返回 memory DTO，而不是 persistence aggregate；
- compaction 状态机、fencing、digest、连续 range 与 token budget 测试全部保留；
- 旧用户可见 record ID、quota、排序和工具响应语义有迁移测试；
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
- [LangGraph Memory Overview](https://docs.langchain.com/oss/python/concepts/memory)：
  thread-scoped short-term state 与 namespaced long-term store 分离。
- [OWASP Agent Memory Guard](https://owasp.org/www-project-agent-memory-guard/)：
  持久记忆是跨 session poisoning 攻击面。
