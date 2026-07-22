"""@brief 0062 身份镜像退役的真实 PostgreSQL 迁移矩阵 / Real-PostgreSQL migration matrix for 0062 identity-mirror retirement."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from fogmoe_bot.domain.assistant.messages import CanonicalMessage, ToolCallPart
from fogmoe_bot.domain.context_window.compaction import compaction_source_digest
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_dbctl.commands import bootstrap, migration_execution
from fogmoe_dbctl.config import DbctlSettings
from fogmoe_dbctl.postgres import direct_psql_environment, quote_identifier

_POSTGRES_BIN = Path("/usr/lib/postgresql/16/bin")
"""@brief PostgreSQL 16 服务端工具目录 / PostgreSQL 16 server-tool directory."""

_VECTOR_CONTROL = Path("/usr/share/postgresql/16/extension/vector.control")
"""@brief pgvector 扩展可用性哨兵 / pgvector extension availability sentinel."""

_SUPERUSER = "fogmoe_test_superuser"
"""@brief 临时集群超级用户 / Ephemeral-cluster superuser."""

_APPLICATION_ROLE = "fogmoe_test_application"
"""@brief 临时应用角色 / Ephemeral application role."""

_MAINTENANCE_ROLE = "fogmoe_test_maintenance"
"""@brief 临时迁移 owner 角色 / Ephemeral migration-owner role."""

_REPORTING_ROLE = "fogmoe_test_reporting"
"""@brief 临时报表角色 / Ephemeral reporting role."""

_ADMIN_USER_ID = 1001
"""@brief 迁移模板注入的管理员 ID / Administrator ID injected into migrations."""

_TEST_PASSWORD = "fogmoe-ephemeral-test-only"
"""@brief 仅存在于临时集群的固定测试密码 / Fixed password used only inside the ephemeral cluster."""


@dataclass(frozen=True, slots=True)
class _EphemeralPostgres:
    """@brief 隔离 PostgreSQL 进程的连接坐标 / Connection coordinates for an isolated PostgreSQL process."""

    data_directory: Path
    socket_directory: Path
    log_path: Path
    port: int

    def settings(self, database: str) -> DbctlSettings:
        """@brief 为临时数据库构造显式 dbctl 投影 / Build an explicit dbctl projection for an ephemeral database.

        @param database 临时数据库名 / Ephemeral database name.
        @return 不读取部署配置的严格设置 / Strict settings that never read deployment configuration.
        """

        return DbctlSettings.model_validate(
            {
                "endpoint": {
                    "host": "127.0.0.1",
                    "port": self.port,
                    "name": database,
                },
                "application": {
                    "username": _APPLICATION_ROLE,
                    "password": _TEST_PASSWORD,
                },
                "maintenance": {
                    "username": _MAINTENANCE_ROLE,
                    "password": _TEST_PASSWORD,
                    "migration_schema": "infra",
                },
                "reporting": {
                    "username": _REPORTING_ROLE,
                    "password": _TEST_PASSWORD,
                },
                "bootstrap": {"system_user": _SUPERUSER},
                "administrator": {"user_id": _ADMIN_USER_ID},
            }
        )


def _require_ephemeral_postgres() -> None:
    """@brief 仅在显式启用且依赖完整时运行进程级测试 / Run process-level tests only when explicitly enabled and fully supported.

    @return None / None.
    """

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the migration matrix")
    required = (
        _POSTGRES_BIN / "initdb",
        _POSTGRES_BIN / "pg_ctl",
        _VECTOR_CONTROL,
    )
    if any(not path.is_file() for path in required) or shutil.which("psql") is None:
        pytest.skip("PostgreSQL 16 server tools, psql, and pgvector are required")


def _unused_loopback_port() -> int:
    """@brief 向内核申请当前空闲端口 / Ask the kernel for a currently unused port.

    @return 临时端口号 / Temporary port number.
    @note PostgreSQL 仅监听 loopback；先释放该端口再由临时进程绑定 / PostgreSQL listens only on loopback; the port is released before the ephemeral process binds it.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_checked(
    command: list[str],
    *,
    environment: dict[str, str] | None = None,
    sql: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """@brief 执行隔离测试子进程并保留失败诊断 / Run an isolated test subprocess while retaining failure diagnostics.

    @param command 不含密码的 argv / Password-free argv.
    @param environment 可选子进程环境 / Optional child environment.
    @param sql 可选标准输入 SQL / Optional SQL sent over standard input.
    @return 已完成进程 / Completed process.
    """

    return subprocess.run(
        command,
        input=sql,
        text=True,
        env=environment,
        check=True,
        capture_output=True,
    )


@contextmanager
def _postgres_cluster() -> Iterator[_EphemeralPostgres]:
    """@brief 启动并可靠清理私有 PostgreSQL 集群 / Start and reliably clean up a private PostgreSQL cluster.

    @return 临时集群上下文 / Ephemeral-cluster context.
    """

    _require_ephemeral_postgres()
    initdb = _POSTGRES_BIN / "initdb"
    pg_ctl = _POSTGRES_BIN / "pg_ctl"
    with tempfile.TemporaryDirectory(prefix="fogmoe-migration-test-") as root_name:
        root = Path(root_name)
        data_directory = root / "data"
        socket_directory = root / "socket"
        log_path = root / "postgres.log"
        socket_directory.mkdir(mode=0o700)
        port = _unused_loopback_port()
        cluster = _EphemeralPostgres(
            data_directory=data_directory,
            socket_directory=socket_directory,
            log_path=log_path,
            port=port,
        )
        _run_checked(
            [
                str(initdb),
                "--pgdata",
                str(data_directory),
                "--username",
                _SUPERUSER,
                "--auth-local=trust",
                "--auth-host=trust",
                "--encoding=UTF8",
                "--locale=C",
            ]
        )
        server_options = (
            f"-F -p {port} -k {socket_directory} "
            "-c listen_addresses='127.0.0.1' -c unix_socket_permissions=0700"
        )
        started = False
        try:
            _run_checked(
                [
                    str(pg_ctl),
                    "--pgdata",
                    str(data_directory),
                    "--log",
                    str(log_path),
                    "--options",
                    server_options,
                    "--wait",
                    "start",
                ]
            )
            started = True
            yield cluster
        finally:
            if started:
                _run_checked(
                    [
                        str(pg_ctl),
                        "--pgdata",
                        str(data_directory),
                        "--mode=fast",
                        "--wait",
                        "stop",
                    ]
                )


def _psql(
    cluster: _EphemeralPostgres,
    *,
    database: str,
    user: str,
    sql: str,
    password: str | None = None,
    tuples_only: bool = False,
) -> str:
    """@brief 通过私有 Unix socket 执行 SQL / Execute SQL through the private Unix socket.

    @param cluster 临时集群 / Ephemeral cluster.
    @param database 临时数据库名 / Ephemeral database name.
    @param user PostgreSQL 角色 / PostgreSQL role.
    @param sql 待执行 SQL / SQL to execute.
    @param password 可选临时密码 / Optional ephemeral password.
    @param tuples_only 是否只返回无格式元组 / Whether to return unformatted tuples only.
    @return psql 标准输出 / psql standard output.
    """

    command = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
    if tuples_only:
        command.extend(("--tuples-only", "--no-align"))
    environment = direct_psql_environment(
        host=str(cluster.socket_directory),
        port=cluster.port,
        database=database,
        user=user,
        password=password,
    )
    return _run_checked(command, environment=environment, sql=sql).stdout.strip()


def _bootstrap_database(cluster: _EphemeralPostgres, database: str) -> DbctlSettings:
    """@brief 用真实 dbctl bootstrap 创建测试库和分权角色 / Create a test database and separated roles through real dbctl bootstrap.

    @param cluster 临时集群 / Ephemeral cluster.
    @param database 新数据库名 / New database name.
    @return 对应数据库设置 / Settings for the database.
    """

    settings = cluster.settings(database)
    bootstrap.execute(
        argparse.Namespace(no_sudo=True, dry_run=False),
        settings=settings,
    )
    _psql(
        cluster,
        database=database,
        user=_SUPERUSER,
        sql="CREATE EXTENSION vector;",
    )
    return settings


def _clone_database(
    cluster: _EphemeralPostgres,
    *,
    template: str,
    database: str,
) -> DbctlSettings:
    """@brief 克隆无连接的 0061 模板库 / Clone the disconnected 0061 template database.

    @param cluster 临时集群 / Ephemeral cluster.
    @param template 0061 模板库名 / 0061 template database name.
    @param database 新场景库名 / New scenario database name.
    @return 新数据库设置 / Settings for the cloned database.
    """

    _psql(
        cluster,
        database="postgres",
        user=_SUPERUSER,
        sql=(
            f"CREATE DATABASE {quote_identifier(database)} "
            f"WITH TEMPLATE {quote_identifier(template)} "
            f"OWNER {quote_identifier(_MAINTENANCE_ROLE)};"
        ),
    )
    return cluster.settings(database)


def _maintenance_sql(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    sql: str,
) -> str:
    """@brief 以迁移 owner 执行业务夹具 SQL / Execute business-fixture SQL as the migration owner.

    @param cluster 临时集群 / Ephemeral cluster.
    @param settings 目标数据库设置 / Target database settings.
    @param sql 待执行 SQL / SQL to execute.
    @return psql 标准输出 / psql standard output.
    """

    return _psql(
        cluster,
        database=settings.endpoint.name,
        user=_MAINTENANCE_ROLE,
        password=_TEST_PASSWORD,
        sql=sql,
    )


def _scalar(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    sql: str,
) -> str:
    """@brief 读取单个迁移断言值 / Read one scalar migration assertion value.

    @param cluster 临时集群 / Ephemeral cluster.
    @param settings 目标数据库设置 / Target database settings.
    @param sql 标量查询 / Scalar query.
    @return 去除空白后的文本值 / Stripped textual value.
    """

    return _psql(
        cluster,
        database=settings.endpoint.name,
        user=_MAINTENANCE_ROLE,
        password=_TEST_PASSWORD,
        sql=sql,
        tuples_only=True,
    )


def _seed_users_and_wallets(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    users: tuple[tuple[int, str], ...],
) -> None:
    """@brief 建立零余额用户与完整 Bank 钱包 / Seed zero-balance users and complete Bank wallets.

    @param cluster 临时集群 / Ephemeral cluster.
    @param settings 目标数据库设置 / Target database settings.
    @param users ``(user_id, legacy_plan)`` 元组 / ``(user_id, legacy_plan)`` tuples.
    @return None / None.
    """

    values = ",\n".join(
        f"({user_id}, {user_id}, 'telegram', 'test-{user_id}', 0, 0, {plan!r})"
        for user_id, plan in users
    )
    _maintenance_sql(
        cluster,
        settings,
        f"""
        INSERT INTO identity.users (
          id, tg_uid, provider, name, coins, coins_paid, user_plan
        ) VALUES
          {values};

        INSERT INTO bank.accounts (
          account_key, account_scope, owner_id, token_bucket, system_kind, allow_negative
        )
        SELECT 'user:' || users.id::TEXT || ':' || bucket.name,
               'user', users.id, bucket.name, NULL, FALSE
        FROM identity.users AS users
        CROSS JOIN (VALUES ('free'::TEXT), ('paid'::TEXT)) AS bucket (name);

        INSERT INTO bank.account_balances (account_key, balance, version)
        SELECT account_key, 0, 0
        FROM bank.accounts
        WHERE account_scope = 'user';
        """,
    )


def _credit_paid_wallet(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    *,
    user_id: int,
    amount: int,
) -> None:
    """@brief 以平衡账本事实充值 paid 钱包 / Credit a paid wallet with a balanced ledger fact.

    @param cluster 临时集群 / Ephemeral cluster.
    @param settings 目标数据库设置 / Target database settings.
    @param user_id 收款用户 / Credited user.
    @param amount 正向金额 / Positive amount.
    @return None / None.
    """

    entry_id = f"00000000-0000-4000-8000-{user_id:012d}"
    _maintenance_sql(
        cluster,
        settings,
        f"""
        BEGIN;
        INSERT INTO bank.ledger_entries (
          entry_id, idempotency_key, reason, actor_id, metadata
        ) VALUES (
          '{entry_id}', 'test:paid:{user_id}', 'bank_issuance', NULL, '{{}}'::JSONB
        );
        INSERT INTO bank.ledger_postings (entry_id, line_no, account_key, delta)
        VALUES
          ('{entry_id}', 1, 'system:issuance', -{amount}),
          ('{entry_id}', 2, 'user:{user_id}:paid', {amount});
        COMMIT;
        """,
    )


def _seed_active_subscription(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    *,
    user_id: int,
) -> None:
    """@brief 建立由完整 Billing 外键链支撑的有效订阅 / Seed an active subscription backed by the complete Billing foreign-key chain.

    @param cluster 临时集群 / Ephemeral cluster.
    @param settings 目标数据库设置 / Target database settings.
    @param user_id 订阅所有者 / Subscription owner.
    @return None / None.
    """

    _maintenance_sql(
        cluster,
        settings,
        f"""
        INSERT INTO billing.products (
          product_id, code, display_name, kind, status, created_at
        ) VALUES (
          '10000000-0000-4000-8000-000000000001',
          'test.subscription', 'Test subscription', 'subscription', 'active',
          CURRENT_TIMESTAMP - INTERVAL '2 days'
        );
        INSERT INTO billing.offers (
          offer_id, product_id, product_kind, currency, price_units,
          entitlement_codes, created_at, subscription_period_seconds, status
        ) VALUES (
          '10000000-0000-4000-8000-000000000002',
          '10000000-0000-4000-8000-000000000001',
          'subscription', 'USD', 100, '[\"assistant.pro\"]'::JSONB,
          CURRENT_TIMESTAMP - INTERVAL '2 days', 2592000, 'active'
        );
        INSERT INTO billing.orders (
          order_id, buyer_id, product_id, offer_id, product_kind, currency,
          price_units, status, created_at, payment_provider, provider_payment_id,
          paid_at, fulfilled_at
        ) VALUES (
          '10000000-0000-4000-8000-000000000003', {user_id},
          '10000000-0000-4000-8000-000000000001',
          '10000000-0000-4000-8000-000000000002',
          'subscription', 'USD', 100, 'fulfilled',
          CURRENT_TIMESTAMP - INTERVAL '2 days', 'backoffice', 'test-payment',
          CURRENT_TIMESTAMP - INTERVAL '2 days',
          CURRENT_TIMESTAMP - INTERVAL '2 days'
        );
        INSERT INTO billing.subscriptions (
          subscription_id, owner_id, product_id, offer_id, source_order_id,
          current_order_id, period_starts_at, period_ends_at, status
        ) VALUES (
          '10000000-0000-4000-8000-000000000004', {user_id},
          '10000000-0000-4000-8000-000000000001',
          '10000000-0000-4000-8000-000000000002',
          '10000000-0000-4000-8000-000000000003',
          '10000000-0000-4000-8000-000000000003',
          CURRENT_TIMESTAMP - INTERVAL '1 day',
          CURRENT_TIMESTAMP + INTERVAL '1 day', 'active'
        );
        """,
    )


def _assert_failed_0062_is_atomic(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    *,
    expected_message: str,
) -> None:
    """@brief 断言 0062 拒绝数据且未留下部分 DDL / Assert that 0062 rejects data without leaving partial DDL.

    @param cluster 临时集群 / Ephemeral cluster.
    @param settings 目标数据库设置 / Target database settings.
    @param expected_message 预期 fail-closed 原因 / Expected fail-closed reason.
    @return None / None.
    """

    with pytest.raises(DBAPIError, match=expected_message):
        migration_execution.run_alembic(
            settings=settings,
            revision="0062_retire_identity_mirrors_and_legacy_media",
            dry_run=False,
        )

    assert (
        _scalar(cluster, settings, "SELECT version_num FROM infra.alembic_version;")
        == "0061_rebuild_assistant_scheduling"
    )
    assert (
        _scalar(
            cluster,
            settings,
            """
            SELECT string_agg(column_name, ',' ORDER BY ordinal_position)
            FROM information_schema.columns
            WHERE table_schema = 'identity'
              AND table_name = 'users'
              AND column_name IN ('coins', 'coins_paid', 'user_plan');
            """,
        )
        == "coins,coins_paid,user_plan"
    )
    assert (
        _scalar(
            cluster,
            settings,
            """
            SELECT (to_regclass('media.picture_request_receipts') IS NOT NULL)::INT
                 + (to_regclass('media.picture_offers') IS NOT NULL)::INT;
            """,
        )
        == "2"
    )
    assert (
        _scalar(
            cluster,
            settings,
            "SELECT count(*) FROM pg_trigger WHERE tgname = 'identity_users_money_projection_tr';",
        )
        == "1"
    )


def _seed_legacy_0068_messages(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    *,
    invalid_tool_arguments: bool,
) -> None:
    """@brief 写入 0068 前仍可被旧投影读取的 legacy Conversation row / Seed legacy Conversation rows still readable by the pre-0068 projection.

    @param cluster 临时集群 / Ephemeral cluster.
    @param settings 目标数据库设置 / Target database settings.
    @param invalid_tool_arguments 是否写入数组型 tool arguments 失败样本 / Whether to write an array-valued tool-arguments failure fixture.
    @return None / None.
    """

    if invalid_tool_arguments:
        _maintenance_sql(
            cluster,
            settings,
            """
            INSERT INTO conversation.conversation_turns (
              turn_id, conversation_id, state, source_kind, source_key,
              created_at, updated_at, completed_at
            ) VALUES (
              '81000000-0000-4000-8000-000000000001',
              'canonical-v2:invalid-tool-arguments',
              'delivered', 'scheduled.prompt', 'canonical-v2:invalid-tool-arguments',
              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            );
            INSERT INTO conversation.conversation_messages (
              message_id, conversation_id, sequence, turn_id, role, content,
              idempotency_key, created_at
            ) VALUES (
              '82000000-0000-4000-8000-000000000001',
              'canonical-v2:invalid-tool-arguments', 1,
              '81000000-0000-4000-8000-000000000001', 'assistant',
              '{"history_messages":[{"role":"assistant","content":null,"tool_calls":[{"id":"call-invalid","type":"function","function":{"name":"lookup","arguments":"[]"}}]}]}'::JSONB,
              'canonical-v2:invalid-tool-arguments:message', CURRENT_TIMESTAMP
            );
            """,
        )
        return

    _maintenance_sql(
        cluster,
        settings,
        """
        INSERT INTO conversation.conversation_turns (
          turn_id, conversation_id, state, source_kind, source_key,
          created_at, updated_at, completed_at
        ) VALUES (
          '81000000-0000-4000-8000-000000000002',
          'canonical-v2:legacy-projectable',
          'delivered', 'scheduled.prompt', 'canonical-v2:legacy-projectable',
          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        INSERT INTO conversation.conversation_messages (
          message_id, conversation_id, sequence, turn_id, role, content,
          idempotency_key, created_at
        ) VALUES
          (
            '82000000-0000-4000-8000-000000000002',
            'canonical-v2:legacy-projectable', 1,
            '81000000-0000-4000-8000-000000000002', 'user',
            '{"text":"legacy user text","content_kind":"plain","metadata":{"marker":"retained"}}'::JSONB,
            'canonical-v2:legacy-projectable:text', CURRENT_TIMESTAMP
          ),
          (
            '82000000-0000-4000-8000-000000000003',
            'canonical-v2:legacy-projectable', 2,
            '81000000-0000-4000-8000-000000000002', 'user',
            '{"kind":"opaque","opaque":{"answer":42}}'::JSONB,
            'canonical-v2:legacy-projectable:opaque', CURRENT_TIMESTAMP
          ),
          (
            '82000000-0000-4000-8000-000000000004',
            'canonical-v2:legacy-projectable', 3,
            '81000000-0000-4000-8000-000000000002', 'user',
            '{"role":"user","content":"embedded legacy user text","metadata":{"marker":"embedded"}}'::JSONB,
            'canonical-v2:legacy-projectable:embedded', CURRENT_TIMESTAMP
          ),
          (
            '82000000-0000-4000-8000-000000000005',
            'canonical-v2:legacy-projectable', 4,
            '81000000-0000-4000-8000-000000000002', 'assistant',
            '{"text":"legacy assistant text","task_kind":"assistant"}'::JSONB,
            'canonical-v2:legacy-projectable:assistant', CURRENT_TIMESTAMP
          ),
          (
            '82000000-0000-4000-8000-000000000006',
            'canonical-v2:legacy-projectable', 5,
            '81000000-0000-4000-8000-000000000002', 'tool',
            '{"tool_call_id":"call-tool","name":"lookup","content":"{\\"answer\\":42}"}'::JSONB,
            'canonical-v2:legacy-projectable:tool', CURRENT_TIMESTAMP
          ),
          (
            '82000000-0000-4000-8000-000000000007',
            'canonical-v2:legacy-projectable', 6,
            '81000000-0000-4000-8000-000000000002', 'user',
            '{"marker":"written-by-v2-runtime-before-migration","model_message":{"schema_version":2,"role":"user","parts":[{"type":"text","text":"already canonical user text"}],"policy":{"include_in_context":true},"meta":{"source":"runtime"}}}'::JSONB,
            'canonical-v2:legacy-projectable:already-v2', CURRENT_TIMESTAMP
          );

        INSERT INTO context_window.compactions (
          compaction_id, conversation_id, owner_user_id, epoch_floor_sequence,
          from_sequence, through_sequence, anchor_turn_id,
          predecessor_compaction_id, projection_version, source_digest,
          source_snapshot, source_row_count, source_token_count, status, version,
          attempt_count, next_attempt_at, claim_token, lease_expires_at,
          completion_token, summary_text, summary_token_count, summary_route_key,
          last_error, created_at, updated_at, completed_at
        ) VALUES (
          '83000000-0000-4000-8000-000000000001',
          'canonical-v2:legacy-projectable', 1001, 0, 1, 1,
          '81000000-0000-4000-8000-000000000002', NULL, 1,
          repeat('0', 64),
          '[{"role":"assistant","content":null,"tool_calls":[{"id":"call-digest","type":"function","function":{"name":"lookup","arguments":"{\\"ratio\\":0.000001,\\"whole\\":1.0,\\"large\\":1e20}"}}]}]'::JSON,
          1, 1, 'failed_final', 0, 0, NULL, NULL, NULL, NULL, NULL, NULL,
          NULL, 'fixture', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        );
        """,
    )


def test_0062_business_data_matrix_and_fresh_head_are_transactional() -> None:
    """@brief 在临时 PostgreSQL 中验证 0062 业务矩阵与 fresh head / Verify the 0062 business matrix and fresh head in ephemeral PostgreSQL.

    @return None / None.
    @note 测试只使用 mktemp 私有集群、私有 Unix socket 和随机 loopback 端口，既不读取 config.json 也不接触外部端点 / The test uses only a mktemp-private cluster, private Unix socket, and random loopback port; it neither reads config.json nor contacts an external endpoint.
    """

    with _postgres_cluster() as cluster:
        template_database = "fogmoe_test_0061_template"
        template_settings = _bootstrap_database(cluster, template_database)
        migration_execution.run_alembic(
            settings=template_settings,
            revision="0061_rebuild_assistant_scheduling",
            dry_run=False,
        )

        success = _clone_database(
            cluster,
            template=template_database,
            database="fogmoe_test_0062_success",
        )
        _seed_users_and_wallets(
            cluster,
            success,
            (
                (_ADMIN_USER_ID, "admin"),
                (2001, "free"),
                (2002, "paid"),
                (2003, "paid"),
            ),
        )
        _credit_paid_wallet(cluster, success, user_id=2002, amount=13)
        _seed_active_subscription(cluster, success, user_id=2003)
        _maintenance_sql(
            cluster,
            success,
            """
            INSERT INTO media.picture_offers (
              offer_id, source_id, sample_url, rating, requester_id, expires_at,
              state, charged_user_id, preview_cost, hd_cost, preview_confirm_by,
              preview_refunded, hd_refunded
            ) VALUES
              (
                '20000000-0000-4000-8000-000000000001', 'settled-preview',
                'https://example.invalid/preview', 'safe', 2001,
                CURRENT_TIMESTAMP + INTERVAL '1 hour', 'available', NULL, 1, 5,
                CURRENT_TIMESTAMP + INTERVAL '1 hour', TRUE, FALSE
              ),
              (
                '20000000-0000-4000-8000-000000000002', 'settled-hd',
                'https://example.invalid/refunded', 'safe', 2001,
                CURRENT_TIMESTAMP + INTERVAL '1 hour', 'refunded', 2001, 1, 5,
                CURRENT_TIMESTAMP + INTERVAL '1 hour', TRUE, TRUE
              );
            """,
        )
        migration_execution.run_alembic(
            settings=success,
            revision="0062_retire_identity_mirrors_and_legacy_media",
            dry_run=False,
        )
        assert (
            _scalar(cluster, success, "SELECT version_num FROM infra.alembic_version;")
            == "0062_retire_identity_mirrors_and_legacy_media"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = 'identity' AND table_name = 'users'
                  AND column_name IN ('coins', 'coins_paid', 'user_plan');
                """,
            )
            == "0"
        )
        assert (
            _scalar(
                cluster,
                success,
                "SELECT balance FROM bank.account_balances WHERE account_key = 'user:2002:paid';",
            )
            == "13"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT count(*) FROM billing.subscriptions
                WHERE owner_id = 2003 AND status = 'active'
                  AND period_starts_at <= CURRENT_TIMESTAMP
                  AND CURRENT_TIMESTAMP < period_ends_at;
                """,
            )
            == "1"
        )
        assert _scalar(cluster, success, "SELECT count(*) FROM identity.users;") == "4"
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT (to_regclass('media.picture_request_receipts') IS NULL)::INT
                     + (to_regclass('media.picture_offers') IS NULL)::INT;
                """,
            )
            == "2"
        )
        migration_execution.run_alembic(
            settings=success,
            revision="head",
            dry_run=False,
        )
        assert (
            _scalar(cluster, success, "SELECT version_num FROM infra.alembic_version;")
            == "0068_canonical_assistant_messages"
        )
        assert (
            _scalar(
                cluster,
                success,
                "SELECT balance FROM bank.account_balances WHERE account_key = 'user:2002:paid';",
            )
            == "13"
        )

        failure_cases: tuple[tuple[str, str, str], ...] = (
            (
                "money_mismatch",
                "identity and Bank balances differ",
                """
                SET session_replication_role = replica;
                UPDATE identity.users SET coins = 1 WHERE id = 3001;
                RESET session_replication_role;
                """,
            ),
            (
                "paid_without_evidence",
                "legacy paid/admin label lacks authoritative",
                "SELECT 1;",
            ),
            (
                "administrator_mismatch",
                "legacy paid/admin label lacks authoritative",
                "SELECT 1;",
            ),
            (
                "unsettled_hd",
                "charged or delivered HD offers require manual audit",
                """
                INSERT INTO media.picture_offers (
                  offer_id, source_id, sample_url, rating, requester_id, expires_at,
                  state, charged_user_id, preview_cost, hd_cost, preview_confirm_by,
                  preview_refunded, hd_refunded
                ) VALUES (
                  '30000000-0000-4000-8000-000000000001', 'unsettled-hd',
                  'https://example.invalid/unsettled', 'safe', 3001,
                  CURRENT_TIMESTAMP + INTERVAL '1 hour', 'charged', 3001, 1, 5,
                  CURRENT_TIMESTAMP + INTERVAL '1 hour', TRUE, FALSE
                );
                """,
            ),
        )
        for suffix, expected_message, corrupting_sql in failure_cases:
            settings = _clone_database(
                cluster,
                template=template_database,
                database=f"fogmoe_test_0062_{suffix}",
            )
            plan = "paid" if suffix == "paid_without_evidence" else "free"
            user_id = _ADMIN_USER_ID if suffix == "administrator_mismatch" else 3001
            _seed_users_and_wallets(cluster, settings, ((user_id, plan),))
            if suffix == "money_mismatch":
                _psql(
                    cluster,
                    database=settings.endpoint.name,
                    user=_SUPERUSER,
                    sql=corrupting_sql,
                )
            else:
                _maintenance_sql(cluster, settings, corrupting_sql)
            _assert_failed_0062_is_atomic(
                cluster,
                settings,
                expected_message=expected_message,
            )

        fresh = _bootstrap_database(cluster, "fogmoe_test_fresh_head")
        migration_execution.run_alembic(
            settings=fresh,
            revision="head",
            dry_run=False,
        )
        assert (
            _scalar(cluster, fresh, "SELECT version_num FROM infra.alembic_version;")
            == "0068_canonical_assistant_messages"
        )
        assert (
            _scalar(
                cluster,
                fresh,
                "SELECT count(*) FROM identity.users;",
            )
            == "0"
        )


def test_0068_converts_legacy_projectable_rows_and_rejects_non_object_tools() -> None:
    """@brief 在临时 PostgreSQL 验证 0068 的 fallback 与 tool 参数闭合性 / Verify 0068 fallback conversion and object-only tool arguments in ephemeral PostgreSQL.

    @return None / None.
    @note 测试只使用私有临时集群；成功样本验证 raw durable row 变为 canonical V2，失败样本验证迁移原子回滚。/
        The test uses only a private ephemeral cluster; the success fixture verifies raw durable rows become canonical V2, and the failure fixture verifies atomic rollback.
    """

    with _postgres_cluster() as cluster:
        template_database = "fogmoe_test_0067_canonical_template"
        template = _bootstrap_database(cluster, template_database)
        migration_execution.run_alembic(
            settings=template,
            revision="0067_close_schema_creator_and_default_gaps",
            dry_run=False,
        )

        success = _clone_database(
            cluster,
            template=template_database,
            database="fogmoe_test_0068_canonical_success",
        )
        _seed_legacy_0068_messages(
            cluster,
            success,
            invalid_tool_arguments=False,
        )
        migration_execution.run_alembic(
            settings=success,
            revision="0068_canonical_assistant_messages",
            dry_run=False,
        )

        assert (
            _scalar(
                cluster,
                success,
                "SELECT version_num FROM infra.alembic_version;",
            )
            == "0068_canonical_assistant_messages"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT count(*)
                FROM conversation.conversation_messages
            WHERE conversation_id = 'canonical-v2:legacy-projectable'
                  AND content #>> '{model_message,schema_version}' = '2'
                  AND content #>> '{model_message,policy,include_in_context}' = 'true'
                  AND content #> '{model_message,meta}' = '{}'::JSONB
                  AND content ->> 'history_format' = 'canonical-v2';
                """,
            )
            == "5"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT content #>> '{model_message,parts,0,text}'
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000002';
                """,
            )
            == "legacy user text"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT (
                  (content #>> '{model_message,parts,0,text}')::JSONB =
                  '{"kind":"opaque","opaque":{"answer":42}}'::JSONB
                  AND content @> '{"kind":"opaque","opaque":{"answer":42}}'::JSONB
                )::INT
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000003';
                """,
            )
            == "1"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT content #>> '{model_message,parts,0,text}'
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000004';
                """,
            )
            == "embedded legacy user text"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT (content #>> '{model_message,role}') || ':' ||
                  (content #>> '{model_message,parts,0,text}')
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000005';
                """,
            )
            == "assistant:legacy assistant text"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT (content #>> '{model_message,role}') || ':' ||
                  (content #>> '{model_message,parts,0,type}') || ':' ||
                  (content #>> '{model_message,parts,0,result,answer}')
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000006';
                """,
            )
            == "tool:tool_result:42"
        )
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT (content #>> '{model_message,parts,0,text}') || ':' ||
                  (content #>> '{model_message,meta,source}') || ':' ||
                  (content ->> 'marker')
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000007';
                """,
            )
            == "already canonical user text:runtime:written-by-v2-runtime-before-migration"
        )
        expected_snapshot_digest = compaction_source_digest(
            (
                CanonicalMessage(
                    MessageRole.ASSISTANT,
                    (
                        ToolCallPart(
                            "call-digest",
                            "lookup",
                            {
                                "ratio": 0.000001,
                                "whole": 1.0,
                                # PostgreSQL JSONB 会在写回 JSON 前以无指数记法规范化该整数 / PostgreSQL JSONB canonicalizes this integral value without exponent notation before writing it back to JSON.
                                "large": 100_000_000_000_000_000_000,
                            },
                        ),
                    ),
                ).to_json(),
            )
        )
        actual_snapshot_digest = _scalar(
            cluster,
            success,
            """
            SELECT source_digest
            FROM context_window.compactions
            WHERE compaction_id = '83000000-0000-4000-8000-000000000001';
            """,
        )
        actual_snapshot = _scalar(
            cluster,
            success,
            """
            SELECT source_snapshot::TEXT
            FROM context_window.compactions
            WHERE compaction_id = '83000000-0000-4000-8000-000000000001';
            """,
        )
        assert actual_snapshot_digest == expected_snapshot_digest, actual_snapshot
        assert (
            _scalar(
                cluster,
                success,
                """
                SELECT projection_version::TEXT || ':' ||
                  (source_snapshot::JSONB #>> '{0,schema_version}')
                FROM context_window.compactions
                WHERE compaction_id = '83000000-0000-4000-8000-000000000001';
                """,
            )
            == "1:2"
        )

        invalid = _clone_database(
            cluster,
            template=template_database,
            database="fogmoe_test_0068_canonical_invalid_tool",
        )
        _seed_legacy_0068_messages(
            cluster,
            invalid,
            invalid_tool_arguments=True,
        )
        with pytest.raises(DBAPIError, match="must decode to a JSON object"):
            migration_execution.run_alembic(
                settings=invalid,
                revision="0068_canonical_assistant_messages",
                dry_run=False,
            )
        assert (
            _scalar(
                cluster,
                invalid,
                "SELECT version_num FROM infra.alembic_version;",
            )
            == "0067_close_schema_creator_and_default_gaps"
        )
        assert (
            _scalar(
                cluster,
                invalid,
                """
                SELECT content #>> '{history_messages,0,tool_calls,0,function,arguments}'
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000001';
                """,
            )
            == "[]"
        )
        _maintenance_sql(
            cluster,
            invalid,
            """
            UPDATE conversation.conversation_messages
            SET content = jsonb_set(
              content,
              '{history_messages,0,tool_calls,0,function,arguments}',
              '[]'::JSONB
            )
            WHERE message_id = '82000000-0000-4000-8000-000000000001';
            """,
        )
        with pytest.raises(DBAPIError, match="must decode to a JSON object"):
            migration_execution.run_alembic(
                settings=invalid,
                revision="0068_canonical_assistant_messages",
                dry_run=False,
            )
        assert (
            _scalar(
                cluster,
                invalid,
                "SELECT version_num FROM infra.alembic_version;",
            )
            == "0067_close_schema_creator_and_default_gaps"
        )
        assert (
            _scalar(
                cluster,
                invalid,
                """
                SELECT jsonb_typeof(
                  content #> '{history_messages,0,tool_calls,0,function,arguments}'
                )
                FROM conversation.conversation_messages
                WHERE message_id = '82000000-0000-4000-8000-000000000001';
                """,
            )
            == "array"
        )
