"""@brief 治理工作流真实 PostgreSQL 契约 / Real-PostgreSQL contract for the moderation workflow."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import TextClause

from fogmoe_bot.domain.moderation.aggregate import (
    GroupModeration,
    StaleModerationVersion,
)
from fogmoe_bot.domain.moderation.effects import (
    ModerationEffectId,
    ModerationEffectKind,
    SpamEnforcementPlan,
)
from fogmoe_bot.domain.moderation.models import (
    ChatId,
    EnforcementFailureMode,
    MessageId,
    ModerationCommandReceiptConflict,
    RuleKind,
    UserId,
)
from fogmoe_bot.domain.moderation.reporting import (
    ReportKey,
    ReportRegistration,
)
from fogmoe_bot.infrastructure.database import connection as db_connection
from fogmoe_bot.infrastructure.database.moderation.effects import (
    PostgresModerationEffectRepository,
)
from fogmoe_bot.infrastructure.database.moderation.group import (
    PostgresModerationGroupRepository,
)
from fogmoe_bot.infrastructure.database.moderation.reports import (
    PostgresModerationReportRepository,
)
from fogmoe_bot.infrastructure.database.repositories.verification_repository import (
    PostgresVerificationRepository,
)
from fogmoe_dbctl.postgres import read_service, service_sqlalchemy_url


PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""


def _postgres_url() -> str:
    """@brief 读取显式测试 DSN 或本地 automation service / Read an explicit test DSN or local automation service.

    @return SQLAlchemy asyncpg URL / SQLAlchemy asyncpg URL.
    """

    explicit = os.environ.get("FOGMOE_TEST_DATABASE_URL")
    if explicit:
        return explicit
    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        pytest.skip("set FOGMOE_TEST_POSTGRES=1 to run the real PostgreSQL contract")
    config_dir = PROJECT_ROOT / "var/psql"
    if not (config_dir / "pg_service.conf").is_file():
        pytest.skip("local PostgreSQL service configuration is unavailable")
    return service_sqlalchemy_url(read_service(config_dir, "fogmoe_automation"))


def _statement(
    sql: str,
    params: Iterable[object] | None,
) -> tuple[TextClause, dict[str, object]]:
    """@brief 将 legacy ``%s`` 参数转换为 SQLAlchemy named binds / Convert legacy ``%s`` parameters to SQLAlchemy named binds.

    @param sql SQL 文本 / SQL text.
    @param params 位置参数 / Positional parameters.
    @return statement 与 bind map / Statement and bind map.
    """

    values = tuple(params or ())
    parts = sql.split("%s")
    if len(parts) - 1 != len(values):
        raise ValueError("SQL placeholder count mismatch")
    rendered = [parts[0]]
    bindings: dict[str, object] = {}
    for index, value in enumerate(values):
        name = f"p{index}"
        rendered.extend((f":{name}", parts[index + 1]))
        bindings[name] = value
    return text("".join(rendered)), bindings


def _bind_repository_io(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
) -> None:
    """@brief 将仓储数据库端口绑定到测试 engine / Bind repository database ports to the test engine.

    @param monkeypatch pytest monkeypatch / pytest monkeypatch.
    @param engine 真实 PG engine / Real PostgreSQL engine.
    @return None / None.
    """

    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncConnection]:
        """@brief 创建真实短事务 / Create a real short transaction.

        @return 活动连接 iterator / Active-connection iterator.
        """

        async with engine.begin() as connection:
            yield connection

    async def fetch_one(
        sql: str,
        params: Iterable[object] | None = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> Any:
        """@brief 执行并读取首行 / Execute and read the first row.

        @param sql SQL 文本 / SQL text.
        @param params 位置参数 / Positional parameters.
        @param connection 可选活动连接 / Optional active connection.
        @return 首行或 None / First row or None.
        """

        statement, bindings = _statement(sql, params)
        if connection is not None:
            return (await connection.execute(statement, bindings)).first()
        async with engine.connect() as owned:
            row = (await owned.execute(statement, bindings)).first()
            await owned.rollback()
            return row

    async def execute(
        sql: str,
        params: Iterable[object] | None = None,
        *,
        connection: AsyncConnection | None = None,
    ) -> int:
        """@brief 执行写语句 / Execute a write statement.

        @param sql SQL 文本 / SQL text.
        @param params 位置参数 / Positional parameters.
        @param connection 可选活动连接 / Optional active connection.
        @return 影响行数 / Affected row count.
        """

        statement, bindings = _statement(sql, params)
        if connection is not None:
            result = await connection.execute(statement, bindings)
            return result.rowcount
        async with engine.begin() as owned:
            result = await owned.execute(statement, bindings)
            return result.rowcount

    monkeypatch.setattr(db_connection, "transaction", transaction)
    monkeypatch.setattr(db_connection, "fetch_one", fetch_one)
    monkeypatch.setattr(db_connection, "execute", execute)


def test_moderation_migration_declares_occ_idempotency_and_p2_state() -> None:
    """@brief 迁移声明 OCC、effect、警告和举报不变量 / Migration declares OCC, effect, warning, and report invariants."""

    sql = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0021_moderation_workflow.sql"
    ).read_text(encoding="utf-8")

    assert "ADD COLUMN version BIGINT NOT NULL DEFAULT 0" in sql
    assert "CREATE TABLE moderation.member_warning_windows" in sql
    assert "CREATE TABLE moderation.effects" in sql
    assert "UNIQUE (source_update_id, kind)" in sql
    assert "CREATE TABLE moderation.reports" in sql
    assert "UNIQUE (chat_id, message_id, reporter_id)" in sql

    toggle_sql = (
        PROJECT_ROOT
        / "src/fogmoe_dbctl/migrations/sql/postgresql/0034_toggle_command_receipts.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE moderation.toggle_command_receipts" in toggle_sql
    assert "idempotency_key VARCHAR(200) PRIMARY KEY" in toggle_sql
    assert "request_payload JSONB NOT NULL" in toggle_sql
    assert "enabled BOOLEAN NOT NULL" in toggle_sql


def test_real_postgres_occ_effect_and_report_races(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief 双连接竞争只提交一个配置版本、一个警告和一个举报 / Dual-connection races commit one configuration version, warning, and report."""

    async def scenario() -> None:
        """@brief 执行真实 PG 并发场景 / Execute the real PostgreSQL concurrency scenario.

        @return None / None.
        """

        engine = create_async_engine(_postgres_url(), poolclass=NullPool)
        _bind_repository_io(monkeypatch, engine)
        groups = PostgresModerationGroupRepository()
        effects_repository = PostgresModerationEffectRepository()
        reports_repository = PostgresModerationReportRepository()
        seed = uuid4().int % 1_000_000_000
        chat_id = ChatId(-8_000_000_000_000 - seed)
        user_id = UserId(seed + 1)
        message_id = MessageId(seed + 2)
        update_id = 8_000_000_000_000 + seed
        try:
            empty = await groups.load_group(chat_id)
            assert empty == GroupModeration.empty(chat_id)
            first = empty.toggle(UserId(41))
            second = empty.toggle(UserId(42))
            outcomes = await asyncio.gather(
                groups.save_group(first, expected_version=0, actor_id=41),
                groups.save_group(second, expected_version=0, actor_id=42),
                return_exceptions=True,
            )
            assert sum(outcome is None for outcome in outcomes) == 1
            assert (
                sum(isinstance(outcome, StaleModerationVersion) for outcome in outcomes)
                == 1
            )
            committed = await groups.load_group(chat_id)
            assert committed.version == 1

            toggle_key = f"telegram-update:{update_id}:moderation.spam-toggle"
            toggle_results = await asyncio.gather(
                groups.toggle_group(
                    chat_id,
                    actor_id=41,
                    idempotency_key=toggle_key,
                ),
                groups.toggle_group(
                    chat_id,
                    actor_id=41,
                    idempotency_key=toggle_key,
                ),
            )
            assert [result.enabled for result in toggle_results] == [False, False]
            assert {result.replayed for result in toggle_results} == {False, True}
            toggled = await groups.load_group(chat_id)
            assert toggled.version == 2
            assert toggled.policy.enabled is False
            with pytest.raises(ModerationCommandReceiptConflict):
                await groups.toggle_group(
                    chat_id,
                    actor_id=42,
                    idempotency_key=toggle_key,
                )

            verification = PostgresVerificationRepository()
            verify_key = (
                f"telegram-update:{update_id + 10}:moderation.verification-toggle"
            )
            verify_results = await asyncio.gather(
                verification.toggle_group(
                    chat_id,
                    group_name="Test Group",
                    actor_id=UserId(41),
                    idempotency_key=verify_key,
                ),
                verification.toggle_group(
                    chat_id,
                    group_name="Test Group",
                    actor_id=UserId(41),
                    idempotency_key=verify_key,
                ),
            )
            assert [result.enabled for result in verify_results] == [True, True]
            assert {result.replayed for result in verify_results} == {False, True}
            assert await verification.group_enabled(chat_id)

            disable_key = (
                f"telegram-update:{update_id + 11}:moderation.verification-toggle"
            )
            disabled = await verification.toggle_group(
                chat_id,
                group_name="Test Group",
                actor_id=UserId(41),
                idempotency_key=disable_key,
            )
            first_replay = await verification.toggle_group(
                chat_id,
                group_name="Test Group",
                actor_id=UserId(41),
                idempotency_key=verify_key,
            )
            assert disabled.enabled is False
            assert first_replay.enabled is True and first_replay.replayed
            assert not await verification.group_enabled(chat_id)

            base_time = datetime.now(UTC)
            plan = SpamEnforcementPlan(
                effect_id=ModerationEffectId.for_update(
                    update_id,
                    ModerationEffectKind.SPAM_ENFORCEMENT,
                ),
                update_id=update_id,
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                matched_text="spam",
                rule_kind=RuleKind.LITERAL,
                failure_mode=EnforcementFailureMode.FAIL_CLOSED,
            )
            effects = await asyncio.gather(
                effects_repository.reserve_effect(
                    plan,
                    now=base_time,
                    warning_window=timedelta(hours=1),
                ),
                effects_repository.reserve_effect(
                    plan,
                    now=base_time,
                    warning_window=timedelta(hours=1),
                ),
            )
            assert effects[0] == effects[1]
            assert effects[0].warning_count == 1

            second_plan = SpamEnforcementPlan(
                effect_id=ModerationEffectId.for_update(
                    update_id + 1,
                    ModerationEffectKind.SPAM_ENFORCEMENT,
                ),
                update_id=update_id + 1,
                chat_id=chat_id,
                user_id=user_id,
                message_id=MessageId(int(message_id) + 1),
                matched_text="spam again",
                rule_kind=RuleKind.LITERAL,
                failure_mode=EnforcementFailureMode.FAIL_CLOSED,
            )
            second_effect = await effects_repository.reserve_effect(
                second_plan,
                now=base_time + timedelta(minutes=1),
                warning_window=timedelta(hours=1),
            )
            assert second_effect.warning_count == 2

            reset_plan = SpamEnforcementPlan(
                effect_id=ModerationEffectId.for_update(
                    update_id + 2,
                    ModerationEffectKind.SPAM_ENFORCEMENT,
                ),
                update_id=update_id + 2,
                chat_id=chat_id,
                user_id=user_id,
                message_id=MessageId(int(message_id) + 2),
                matched_text="spam after reset",
                rule_kind=RuleKind.LITERAL,
                failure_mode=EnforcementFailureMode.FAIL_CLOSED,
            )
            reset_effect = await effects_repository.reserve_effect(
                reset_plan,
                now=base_time + timedelta(hours=2),
                warning_window=timedelta(hours=1),
            )
            assert reset_effect.warning_count == 1

            report_key = ReportKey(chat_id, message_id, user_id)
            reports = await asyncio.gather(
                reports_repository.register_report(
                    report_key,
                    now=base_time,
                    deduplication_window=timedelta(hours=1),
                ),
                reports_repository.register_report(
                    report_key,
                    now=base_time,
                    deduplication_window=timedelta(hours=1),
                ),
            )
            assert set(reports) == {
                ReportRegistration.ACCEPTED,
                ReportRegistration.DUPLICATE,
            }
            after_window = await reports_repository.register_report(
                report_key,
                now=base_time + timedelta(hours=2),
                deduplication_window=timedelta(hours=1),
            )
            assert after_window is ReportRegistration.ACCEPTED
        finally:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "DELETE FROM moderation.toggle_command_receipts WHERE chat_id = :chat_id"
                    ),
                    {"chat_id": int(chat_id)},
                )
                await connection.execute(
                    text(
                        "DELETE FROM moderation.group_verification WHERE group_id = :chat_id"
                    ),
                    {"chat_id": int(chat_id)},
                )
                await connection.execute(
                    text("DELETE FROM moderation.reports WHERE chat_id = :chat_id"),
                    {"chat_id": int(chat_id)},
                )
                await connection.execute(
                    text("DELETE FROM moderation.effects WHERE chat_id = :chat_id"),
                    {"chat_id": int(chat_id)},
                )
                await connection.execute(
                    text(
                        "DELETE FROM moderation.member_warning_windows WHERE chat_id = :chat_id"
                    ),
                    {"chat_id": int(chat_id)},
                )
                await connection.execute(
                    text(
                        "DELETE FROM moderation.group_spam_keywords WHERE group_id = :chat_id"
                    ),
                    {"chat_id": int(chat_id)},
                )
                await connection.execute(
                    text(
                        "DELETE FROM moderation.group_keywords WHERE group_id = :chat_id"
                    ),
                    {"chat_id": int(chat_id)},
                )
                await connection.execute(
                    text(
                        "DELETE FROM moderation.group_spam_control WHERE group_id = :chat_id"
                    ),
                    {"chat_id": int(chat_id)},
                )
            await engine.dispose()

    asyncio.run(scenario())
