# 雾萌娘（改） - 多功能 Telegram 机器人

<div align="center">

![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)
![Python](https://img.shields.io/badge/python-3.14-green.svg)
![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-blue.svg)

一个功能丰富、可扩展的 Telegram 机器人，集成 AI 聊天、经济系统、娱乐游戏和群组管理功能。

</div>

---

## Fork 说明

本项目由 **foemoe** 上游项目 fork 而来，并在此基础上演进。请保留原项目的许可证与署名；当前仓库的改动、配置和部署说明以本 README 为准。

## ✨ 功能特性

### 🤖 AI 智能聊天
- **多模型支持**：通过 LiteLLM 集成 OpenAI、OpenRouter、Google Gemini、Azure OpenAI、智谱 AI
- **个性化对话**：可爱、中二、傲娇的"雾萌娘"人设
- **上下文记忆**：支持长期对话记忆和个性化印象
- **好感度系统**：根据互动调整回复风格

### 💰 银行、经济与权益
- **账本式双钱包**：`Free`（免费金币）与 `Paid legacy`（历史付费金币）分别记账；所有资金移动都有平衡的双重记账分录、幂等回执和审计原因。
- **Free 是唯一流通钱包**：活动奖励、群组贡献与可验证随机活动只使用 `Free`；`Paid legacy` 仅保留既有历史余额，不会通过充值、订阅或活动继续售卖、发行或混入流通。
- **银行申请而非人工充值**：私聊中的 `/request_tokens <数量> <用途>` 用于申请免费金币；`/recharge` 是同一申请流程的简写，**不是**手工充值、卡密兑换或付款确认。
- **产品权益与订阅**：原生支付在受控渠道验证后只授予产品权益（entitlement）或订阅（subscription），从不把支付金额兑换为任一金币钱包。
- **移除投机兑换入口**：不提供 `$FOGMOE` 买入、兑换或 swap，也不提供卡密充值路径。

### 🎲 可验证活动与共建玩法
- **可验证随机活动**：`/chance` 创建并公开服务端承诺（commitment），再由用户提交客户端种子（client seed）结算；只押注 `Free`，在结算前展示精确负期望值（negative expected value, EV）与规则集指纹。
- **个人冒险**：仅私聊的 `/adventure` 提供角色、每日探索、材料制作和收藏图鉴，个人进度不会与群组资产混用。
- **群组小镇**：仅群聊的 `/town` 提供群金库、协作项目与成员 `Free` 贡献；群管理员管理项目，成员共同建设。
- **其他轻量互动**：御神签、个人冒险与群组代币图表继续独立运行；未在当前命令表中声明的命令会统一引导至 `/help`，不会回落到任何历史实现。

### 👥 群组管理
- **新成员验证**：防止机器人和垃圾账号
- **垃圾消息控制**：智能检测和过滤垃圾内容
- **举报系统**：用户可举报不当消息给管理员
- **关键词自动回复**：自定义关键词触发回复
- **代币图表**：查看加密货币价格图表

### 🛠️ 实用工具
- **中英互译**：快速翻译功能
- **音乐搜索**：搜索并获取音乐资源
- **随机图片**：获取二次元图片
- **邀请系统**：邀请好友获得奖励
- **任务系统**：完成任务获得金币

---

## 🚀 快速开始

### 环境要求

- **Python**: 3.14
- **PostgreSQL**: 15 或更高版本
- **操作系统**: Linux / macOS / Windows

### 安装依赖

```bash
# 克隆项目
git clone https://github.com/kleedaisuki/FOGMOE-telegram-bot.git
cd FOGMOE-telegram-bot

# 安装 Python 依赖和命令行入口
python -m pip install -e .
```

### 配置文件（JSONC）

```bash
# 复制带完整说明的 JSONC 模板
cp example.config.json config.json
chmod 600 config.json

# 编辑 config.json，填入你的配置
nano config.json
```

`config.json` 是唯一的运行时配置入口；它使用 JSON with Comments（JSONC），可在对象
与字段前写说明性注释。不要把真实密钥提交到仓库。全部字段、默认值和逐项说明见
[`example.config.json`](example.config.json)；README 只展示最常用的 AI 路由片段，避免成为
第二份会漂移的配置规范。

AI 调用统一通过 LiteLLM SDK。provider 和模型位于 `ai` 下，路由策略与 provider 凭据分开：

```jsonc
{
  "ai": {
    "routing": {
      // 按顺序尝试聊天 provider。
      "chat": {
        "provider_order": ["openai", "openrouter", "siliconflow"]
      },
      "summary": { "provider": "openai" },
      "translation": { "provider": "openai" }
    },
    "providers": {
      "openai": {
        "api_key": "替换为真实密钥",
        "models": { "chat": "gpt-4o" }
      }
    }
  }
}
```

### 数据库设置

```bash
# 本地 PostgreSQL：根据 config.json 中的 database 配置创建数据库与角色
fogmoe-dbctl bootstrap

# 已有外部数据库：填写 config.json 的 database.endpoint 后直接运行迁移
fogmoe-dbctl migrate

# 通过 config.json 中受约束的数据库连接导出一张表；不会接受任意 SQL
fogmoe-dbctl export-csv --table conversation.conversation_messages --output ./conversation_messages.csv
```
数据库迁移由 `fogmoe-dbctl` 显式管理，机器人启动时不会自动迁移外部数据库。
CLI 的分层结构和子命令扩展约定见 [`docs/dbctl.md`](docs/dbctl.md)。

### 可观测性

迁移 `0039_observability` 建立 `observability` schema。运行时以 W3C
`traceparent` 串联 durable inbox、inference activity 与 transactional outbox，异步批量写入：

- `observability.log_records`：脱敏后的结构化日志；
- `observability.spans`：inbox、LLM、tool、outbox 与 Retrieval 操作耗时和错误；
- `observability.metric_points`：mailbox、exporter、lease、token 与 Retrieval 批次指标；
- `observability.pipeline_health`：当前积压、重试、最终失败和过期 lease；
- `observability.turn_latency`：Turn 各阶段和端到端时延。

日志仍同时进入本地轮转文件，PostgreSQL 故障不会阻塞 Telegram 或推理热路径。
遥测使用独立的单连接 `asyncpg` pool、有界队列和批次级指数退避；每日分区由受限的
`SECURITY DEFINER` 函数创建，默认保留 30 天。常用诊断查询：

```sql
SELECT *, CURRENT_TIMESTAMP - oldest_ready_at AS oldest_age
FROM observability.pipeline_health;

SELECT span_name,
       count(*) AS calls,
       count(*) FILTER (WHERE status_code = 'error') AS errors,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ns / 1e6) AS p95_ms
FROM observability.spans
WHERE started_at >= CURRENT_TIMESTAMP - INTERVAL '1 hour'
GROUP BY span_name;

SELECT occurred_at, severity_text, event_name, body
FROM observability.log_records
WHERE trace_id = decode(:trace_id_hex, 'hex')
ORDER BY occurred_at;
```

内建数据分析 CLI 提供 RED/USE 总览、pipeline、span 聚合、统一错误流、结构日志、
trace waterfall、metric、GenAI token/延迟、Turn 延迟及实例生命周期视图：

~~~bash
fogmoe-dashboard --window 1h overview
fogmoe-dashboard --window 24h spans --limit 20
fogmoe-dashboard --window 24h retrieval
fogmoe-dashboard --window 6h traces --errors-only
fogmoe-dashboard trace 0123456789abcdef0123456789abcdef
fogmoe-dashboard --window 15m watch --interval 2

# 稳定 JSON 适合 jq、定时任务或其他脚本消费
fogmoe-dashboard --format json --window 1h errors | jq '.data'
~~~

原生 Qt GUI 提供健康趋势、KPI、交互筛选、trace master-detail waterfall、自动刷新和
跨 logs/traces drill-down。GUI 是可选依赖，不会把 Qt runtime 带进 bot 的服务器部署：

~~~bash
pip install -e '.[dashboard-gui]'
fogmoe-dashboard-gui --config ./config.json --window 6h --auto-refresh 10
~~~

需要自由查询时，可通过 `fogmoe-dbctl` 打开前台 psql；连接参数只从根目录
`config.json` 读取：

~~~bash
fogmoe-dbctl shell
~~~

Python 异步 API、全部视图参数和生产安全边界见 [Dashboard 文档](docs/dashboard.md)。

### 启动机器人

```bash
# 方式一：直接运行命令行入口
fogmoe-bot

# 方式二：使用脚本（后台运行）
chmod +x runBot.sh
./runBot.sh
```

### 停止机器人

```bash
# 查找进程
ps -ef | grep python3

# 终止进程
kill <PID>

# 或使用脚本
# 编辑 runBot.sh 查看命令
```

`runBot.sh stop` 默认最多等待 40 秒，让运行时完成分阶段排空；部署环境可通过
`BOT_STOP_TIMEOUT_SECONDS` 调整脚本的强制终止上限。

---

## 📦 部署指南

### 使用虚拟环境（推荐）

```bash
# 创建虚拟环境
python -m venv .venv

# 激活虚拟环境
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
```

---

## ⚙️ 配置说明

### 必需配置

#### 获取必要的 API 密钥
在根目录 `config.json` 中配置必需项；不要把真实密钥提交到仓库。该文件使用 JSONC，
所有可配置字段、默认值及说明均在 `example.config.json` 中；首次配置可执行
`cp example.config.json config.json`。

---

## 📖 使用说明

先在私聊执行 `/me` 注册，然后按场景使用下列入口：

| 场景 | 可用位置 | 命令 |
|---|---|---|
| 钱包与免费金币申请 | **仅私聊** | `/bank`；`/request_tokens <数量> <用途>`；`/recharge <数量> <用途>`（仅为申请别名） |
| 权益、订单与订阅 | **仅私聊** | `/billing`；`/billing_order <报价ID> [续费订阅ID]`；`/refund <订单ID> <原因>`；`/subscription_cancel <订阅ID>` |
| 个人冒险 | **仅私聊** | `/adventure`；`/adventure_create <名称>`；`/adventure_explore <woodland|quarry|shore>`；`/adventure_craft <配方>`；`/adventure_collection` |
| 群组小镇 | **仅群聊/超级群** | `/town`；`/town overview`；`/town project <类型> <金币> <项目名>`；`/town contribute <免费金币> [项目ID]`；`/town complete <项目ID>` |
| 可验证随机活动 | 私聊、群聊或超级群 | `/chance <规则> <免费金币押注>`（big/small/odd/even，以及高方差的 any-triple、triple-1 至 triple-6）；`/chance_seed <轮次UUID> <客户端种子>`；`/chance_show <轮次UUID>` |

`/town` 与 `/adventure` 是刻意隔离的两种范围：前者以 Telegram 群为共同资产边界，后者以私聊用户为个人进度边界；不要在群里使用 `/adventure`，也不要在私聊使用 `/town`。

下单只会创建待付款订单。只有受控的原生支付渠道提交并通过验证的支付事件，才会推进订单并由后台履约为权益或订阅；用户不能通过命令自报“已付款”。这份说明不表示 Telegram Stars 已在任何部署中自动启用；若未来启用该渠道，仍必须完成受控渠道验证后才可履约。未声明的历史命令不会得到兼容处理，而是统一返回当前帮助入口。

更多管理员操作见 [部署管理员使用说明](docs/admin-commands.md)，运行时边界见 [架构说明](docs/runtime-architecture.md)。


## 🐳 Docker 部署（仅 Python，外部 PostgreSQL）

无需在容器内运行 PostgreSQL，只容器化机器人。

1. 复制 `example.config.json` 为 `config.json`，填好 Telegram、AI 与 PostgreSQL 配置。Docker
   会把它以只读方式挂载到 `/app/config.json`；将 `database.endpoint.host` 设为容器可访问的
   外部数据库地址（Docker Desktop 上的宿主机 PostgreSQL 可使用 `host.docker.internal`；Linux
   Docker Engine 则使用可路由的宿主机 IP 或网关地址）。
2. 构建镜像：
   ```bash
   docker compose build bot
   ```
3. 后台运行：
   ```bash
   docker compose up -d bot
   ```
4. 查看日志：`docker compose logs -f bot`。Compose 默认用 `fogmoe-runtime` named volume 持久化
   文件日志、待投递媒体 artifact 与跨进程限流状态；如需直接查看宿主机文件，可把该挂载改成
   `./logs:/app/logs`。Compose 的 40 秒停止宽限覆盖运行时默认 30 秒分阶段排空窗口。
5. 更新代码并重建/重启容器：
   ```bash
   git pull --ff-only && docker compose up -d --build bot
   ```

   如需同时刷新基础镜像：
   ```bash
   git pull --ff-only && docker compose build --pull bot && docker compose up -d bot
   ```

   如果服务器上的 Docker 需要 root 权限，把 `docker` 改成 `sudo docker` 即可。

> 默认镜像基于 `python:3.14-slim`，入口命令为 `fogmoe-bot`，仅依赖外部 PostgreSQL。


### 使用的主要技术

- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) - Telegram Bot API 封装
- [LiteLLM](https://github.com/BerriAI/litellm) - 统一 AI provider 调用层
- [OpenAI](https://openai.com/) - AI 服务
- [Google Gemini](https://ai.google.dev/) - AI 聊天模型
- [Azure OpenAI](https://azure.microsoft.com/en-us/products/ai-services/openai-service) - AI 服务
- [智谱 AI](https://open.bigmodel.cn/) - 中文 AI 模型
- [PostgreSQL](https://www.postgresql.org/) - 数据库

### 核心代码分层

- `src/fogmoe_bot/domain/`：纯领域状态、不变量和值类型，不依赖 Telegram、SQLAlchemy 或 HTTP SDK
- `src/fogmoe_bot/application/runtime/`：有界 keyed mailbox 与统一后台服务生命周期
- `src/fogmoe_bot/application/conversation/`：durable inbox、Turn、inference activity 与 outbox 工作流
- `src/fogmoe_bot/domain/context_window/`、`application/context_window/`：token budget、history projection、checkpoint 与 durable compaction
- `src/fogmoe_bot/domain/retrieval/`、`application/retrieval/`：embedding space、episodic passage、durable vector workflow 与 typed semantic recall
- `src/fogmoe_bot/domain/observability/`：W3C trace identity 与不可变 typed signals
- `src/fogmoe_bot/application/observability/`：有界 buffer、span scope、export/runtime metrics 生命周期
- `src/fogmoe_bot/infrastructure/observability/`：结构日志、脱敏、独立 PostgreSQL batch sink 与进程装配
- `src/fogmoe_bot/application/assistant/`：provider-neutral Agent、严格推理命令和类型化工具目录
- `src/fogmoe_bot/presentation/telegram/`：Telegram Update 映射、显式路由和薄适配器
- `src/fogmoe_bot/infrastructure/database/`：PostgreSQL UoW、lease/fencing 与 bounded-context adapters
- `src/fogmoe_bot/infrastructure/assistant/`：LLM/HTTP/media 工具适配与 composition
- `src/fogmoe_dbctl/`：独立数据库控制面；机器人运行时不会隐式执行迁移

执行模型、状态所有权、幂等边界和迁移决策详见
[`docs/runtime-architecture.md`](docs/runtime-architecture.md)。
Memory 与 Context Window 的显式边界、数据所有权和迁移策略详见
[`docs/memory-architecture.md`](docs/memory-architecture.md)。

---

## 🤝 贡献指南

开发依赖与本地门禁：

```bash
python -m pip install -e '.[dev]'
python -m compileall -q src tests
ruff check .
pyright
mypy --strict src/fogmoe_bot src/fogmoe_dbctl
pytest -q
```

涉及迁移、事务、lease/fencing 或幂等 receipt 的改动，还必须在独立的临时
PostgreSQL 实例上覆盖 upgrade、downgrade/re-up 与对应真实语义测试。
完整命令和测试分层见 [`docs/testing-guidelines.md`](docs/testing-guidelines.md)。

---

## 📄 许可证

### AGPL-3.0 License

本项目采用 **GNU Affero General Public License v3.0** 开源协议。

**这意味着：**

⚠️ **您必须：**
- **开源您的修改**：如果您修改了本软件并通过网络提供服务，您必须公开源代码
- **保持相同许可证**：衍生作品必须使用相同的 AGPL-3.0 许可证
- **声明更改**：明确标注您所做的修改
- **提供源代码访问**：向所有通过网络与软件交互的用户提供源代码

🔴 **重要提示：**
- 如果您在服务器上运行修改版本的本软件，并通过网络向用户提供服务（例如作为 Telegram Bot），您**必须**向这些用户提供完整的源代码
- 这是 AGPL 覆盖网络使用场景的主要要求

详细许可证内容请查看 [LICENSE](LICENSE) 文件。

### 第三方许可证

本项目使用的第三方库遵循各自的许可证：
- 依赖库请查看 `pyproject.toml`

---

## 🔒 安全与隐私

### 数据安全
- 所有敏感配置集中保存在根目录 `config.json`（JSONC）中，并应限制为仅部署账户可读
- 数据库密码不会硬编码在代码中
- 支持加密存储用户数据

### 隐私保护
- 仅存储必要的用户信息（用户ID、用户名）
- 聊天记录用于提供服务，不会被滥用
- 遵守 Telegram 服务条款和隐私政策

---

## 📊 项目统计

![GitHub stars](https://img.shields.io/github/stars/fogmoe/telegram-bot?style=social)
![GitHub forks](https://img.shields.io/github/forks/fogmoe/telegram-bot?style=social)
![GitHub issues](https://img.shields.io/github/issues/fogmoe/telegram-bot)
![GitHub pull requests](https://img.shields.io/github/issues-pr/fogmoe/telegram-bot)

---

<div align="center">

**如果这个项目对您有帮助，请给个 ⭐ Star！**

Made with ❤️ by FOGMOE

</div>
