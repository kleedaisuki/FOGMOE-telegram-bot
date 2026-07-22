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
            == "0067_close_schema_creator_and_default_gaps"
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
            == "0067_close_schema_creator_and_default_gaps"
        )
        assert (
            _scalar(
                cluster,
                fresh,
                "SELECT count(*) FROM identity.users;",
            )
            == "0"
        )
