# 测试与质量门禁

测试围绕可观察行为和架构不变量组织。领域规则使用小型单元测试；事务、锁序、幂等回执、
lease/fencing 和迁移往返使用真实 PostgreSQL 测试；Telegram、LLM 与 HTTP 在默认测试中使用
窄端口替身，不访问真实网络。

## 分层重点

- `domain/`：状态转移、值对象、计算规则和非法状态。
- `application/`：用例编排、重放、超时、取消和端口契约。
- `infrastructure/`：SQL 原子性、迁移、外部协议解析和资源边界。
- `presentation/telegram/`：Update 映射、路由互斥、callback 协议和用户可见渲染。
- `test_package_boundaries.py` 与 `test_domain_dependencies.py`：依赖方向、已删除旧路径和阻塞
  I/O 准入边界。

测试应断言结果和不变量，避免复刻实现。修复并发或恢复缺陷时，回归测试必须覆盖导致缺陷的
交错、重放或取消时机；仅测试正常路径不足以证明修复。

## 本地门禁

从仓库根目录运行：

```bash
.venv/bin/python -m compileall -q src tests
ruff check .
ruff format --check .
.venv/bin/pyright
.venv/bin/mypy --strict src/fogmoe_bot src/fogmoe_dbctl
.venv/bin/pytest -q
git diff --check
```

开发依赖通过项目虚拟环境安装：

```bash
uv pip install --python .venv/bin/python -e '.[dev]'
```

## 显式集成测试

真实 PostgreSQL 测试默认跳过。使用隔离测试库并显式开启：

```bash
FOGMOE_TEST_POSTGRES=1 .venv/bin/pytest -q \
  $(rg -l 'FOGMOE_TEST_POSTGRES' tests | sort)
```

迁移变更还必须在空库验证 `fresh → head`，并从父版本执行一次
`upgrade → downgrade → upgrade`；测试结束后删除隔离实例。真实 provider 连通性测试仅在人工
诊断时使用 `RUN_ENV_API_CONNECTIVITY_TESTS=1`，不得成为普通测试或 CI 的隐式网络依赖。
