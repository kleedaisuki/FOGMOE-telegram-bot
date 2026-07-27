"""@brief 0071 Workspace 附件 receipt 的真实 PostgreSQL CTest / Real-PostgreSQL CTest for 0071 Workspace attachment receipts.

此测试故意不 mock PostgreSQL trigger、deferred constraint trigger 或 Alembic 事务。
它只在 ``FOGMOE_TEST_POSTGRES=1`` 时启动一个 ``mkdtemp`` 私有 PostgreSQL 16 集群；普通
CTest 以 77 跳过，不能把静态 SQL 断言误当作运行时验证。/ This test deliberately does
not mock PostgreSQL triggers, deferred constraint triggers, or Alembic transactions. It starts
a ``mkdtemp``-private PostgreSQL 16 cluster only with ``FOGMOE_TEST_POSTGRES=1``; ordinary CTest
skips with 77 and must not mistake static SQL assertions for runtime validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from alembic import command as alembic_command
from alembic.config import Config

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""@brief 仓库根目录 / Repository root directory."""

_SOURCE_ROOT = _PROJECT_ROOT / "src"
"""@brief src-layout Python 根目录 / src-layout Python root directory."""

if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from fogmoe_dbctl.commands import bootstrap, migration_execution  # noqa: E402
from fogmoe_dbctl.commands.migration_execution import (  # noqa: E402
    maintenance_database_url,
)
from fogmoe_dbctl.config import DbctlSettings  # noqa: E402
from fogmoe_dbctl.postgres import direct_psql_environment, quote_literal  # noqa: E402

_POSTGRES_BIN = Path("/usr/lib/postgresql/16/bin")
"""@brief PostgreSQL 16 服务端工具目录 / PostgreSQL 16 server-tool directory."""

_VECTOR_CONTROL = Path("/usr/share/postgresql/16/extension/vector.control")
"""@brief pgvector extension 控制文件 / pgvector extension control file."""

_SUPERUSER = "fogmoe_ctest_attachment_superuser"
"""@brief 临时集群 bootstrap 超级用户 / Ephemeral-cluster bootstrap superuser."""

_APPLICATION_ROLE = "fogmoe_ctest_attachment_application"
"""@brief 临时应用登录角色 / Ephemeral application login role."""

_MAINTENANCE_ROLE = "fogmoe_ctest_attachment_maintenance"
"""@brief 临时迁移 owner 角色 / Ephemeral migration-owner role."""

_REPORTING_ROLE = "fogmoe_ctest_attachment_reporting"
"""@brief 临时报表登录角色 / Ephemeral reporting login role."""

_DATABASE = "fogmoe_ctest_attachment_receipts"
"""@brief 每个私有集群中的测试数据库名 / Test database name inside each private cluster."""

_PASSWORD = "fogmoe-ctest-attachment-only"
"""@brief 仅用于临时角色的固定密码 / Fixed password used only by ephemeral roles."""

_ADMIN_USER_ID = 4242
"""@brief 个人 Workspace scope 的测试 user ID / Test user ID for the personal Workspace scope."""

_TRACEPARENT = "00-00000000000000000000000000000000-0000000000000000-01"
"""@brief 符合 inference 活动约束的固定 traceparent / Fixed traceparent satisfying the inference-activity constraint."""


@dataclass(frozen=True, slots=True)
class _EphemeralPostgres:
    """@brief 私有 PostgreSQL 进程的连接坐标 / Connection coordinates for a private PostgreSQL process."""

    data_directory: Path
    """@brief initdb 创建的数据目录 / Data directory created by initdb."""
    socket_directory: Path
    """@brief 私有 Unix socket 目录 / Private Unix-socket directory."""
    log_path: Path
    """@brief PostgreSQL 日志路径 / PostgreSQL log path."""
    port: int
    """@brief 随机 loopback TCP 端口 / Random loopback TCP port."""

    def settings(self, database: str) -> DbctlSettings:
        """@brief 构造不读取部署配置的 dbctl 设置 / Build dbctl settings without reading deployment configuration.

        @param database 临时数据库名 / Ephemeral database name.
        @return 角色隔离且连接显式的严格设置 / Strict settings with separated roles and an explicit connection.
        """

        return DbctlSettings.model_validate(
            {
                "endpoint": {"host": "127.0.0.1", "port": self.port, "name": database},
                "application": {
                    "username": _APPLICATION_ROLE,
                    "password": _PASSWORD,
                },
                "maintenance": {
                    "username": _MAINTENANCE_ROLE,
                    "password": _PASSWORD,
                    "migration_schema": "infra",
                },
                "reporting": {
                    "username": _REPORTING_ROLE,
                    "password": _PASSWORD,
                },
                "bootstrap": {"system_user": _SUPERUSER},
                "administrator": {"user_id": _ADMIN_USER_ID},
            }
        )


@dataclass(frozen=True, slots=True)
class _AttachmentFixture:
    """@brief 单个 pending 当前附件的最小 durable fixture / Minimal durable fixture for one pending current attachment."""

    label: str
    """@brief 场景唯一标签 / Scenario-unique label."""
    turn_id: str
    """@brief 当前 Turn UUID 文本 / Current Turn UUID text."""
    activity_id: str
    """@brief 对应 inference activity UUID 文本 / Matching inference-activity UUID text."""
    message_id: str
    """@brief 唯一 source user message UUID 文本 / Sole source-user-message UUID text."""
    conversation_id: str
    """@brief 所属会话 / Owning conversation."""
    source_message_id: int
    """@brief Telegram current upload/source message ID / Telegram current-upload/source message ID."""
    runtime_path: str
    """@brief 固定 Workspace 内 payload 路径 / Fixed payload path inside the Workspace."""


def _postgres_gate_reason() -> str | None:
    """@brief 返回 PostgreSQL 集成测试的跳过原因 / Return the skip reason for the PostgreSQL integration test.

    @return 不应运行时的可读原因；满足所有条件时为 None / Human-readable reason when the
        test must not run, or None when every prerequisite is available.
    """

    if os.environ.get("FOGMOE_TEST_POSTGRES") != "1":
        return "set FOGMOE_TEST_POSTGRES=1 to run real PostgreSQL attachment tests"
    required = (
        _POSTGRES_BIN / "initdb",
        _POSTGRES_BIN / "pg_ctl",
        _VECTOR_CONTROL,
    )
    if any(not path.is_file() for path in required) or shutil.which("psql") is None:
        return "PostgreSQL 16 server tools, pgvector, and psql are required"
    return None


_POSTGRES_GATE_REASON = _postgres_gate_reason()
"""@brief import 时计算的 CTest 门控状态 / CTest gate state computed at import time."""


def _unused_loopback_port() -> int:
    """@brief 向内核申请一个空闲 loopback 端口 / Ask the kernel for an unused loopback port.

    @return 调用时可用的 TCP 端口 / TCP port available at probe time.
    @note 端口探测与 PostgreSQL bind 之间天然有小竞态；私有测试只在本机 loopback 使用它，
        pg_ctl 失败会保留诊断而不会连接外部数据库。/ There is inherently a small race
        between probing and PostgreSQL bind; this private test uses it only on loopback, and a
        pg_ctl failure retains diagnostics rather than connecting to an external database.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_checked(
    command: list[str],
    *,
    environment: Mapping[str, str] | None = None,
    sql: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """@brief 运行私有 PostgreSQL 子进程并保留失败输出 / Run a private PostgreSQL subprocess while retaining failure output.

    @param command 不包含密码的 argv / Password-free argv.
    @param environment 可选显式环境 / Optional explicit environment.
    @param sql 可选标准输入 SQL / Optional SQL supplied over standard input.
    @return 已成功完成的子进程结果 / Successfully completed subprocess result.
    @raise subprocess.CalledProcessError initdb、pg_ctl 或 psql 失败时抛出 /
        Raised when initdb, pg_ctl, or psql fails.
    """

    return subprocess.run(
        command,
        input=sql,
        text=True,
        env=dict(environment) if environment is not None else None,
        check=True,
        capture_output=True,
    )


@contextmanager
def _postgres_cluster() -> Iterator[_EphemeralPostgres]:
    """@brief 启动并清理一个隔离 PostgreSQL 集群 / Start and clean up an isolated PostgreSQL cluster.

    @return 只监听随机 loopback 端口、使用 mkdtemp 数据与 socket 目录的集群 /
        Cluster listening only on a random loopback port with mkdtemp data and socket directories.
    @note 调用方门控已验证 server tools。清理只由 ``TemporaryDirectory`` 删除它自己创建的
        根目录；不会触及 operator 的 PostgreSQL state。/ The caller gate has already
        verified server tools. Cleanup deletes only the root created by ``TemporaryDirectory``
        and never touches operator PostgreSQL state.
    """

    initdb = _POSTGRES_BIN / "initdb"
    pg_ctl = _POSTGRES_BIN / "pg_ctl"
    with tempfile.TemporaryDirectory(
        prefix="fogmoe-attachment-receipt-ctest-"
    ) as root_name:
        root = Path(root_name)
        socket_directory = root / "socket"
        socket_directory.mkdir(mode=0o700)
        cluster = _EphemeralPostgres(
            data_directory=root / "data",
            socket_directory=socket_directory,
            log_path=root / "postgres.log",
            port=_unused_loopback_port(),
        )
        _run_checked(
            [
                str(initdb),
                "--pgdata",
                str(cluster.data_directory),
                "--username",
                _SUPERUSER,
                "--auth-local=trust",
                "--auth-host=trust",
                "--encoding=UTF8",
                "--locale=C",
            ]
        )
        server_options = (
            f"-F -p {cluster.port} -k {cluster.socket_directory} "
            "-c listen_addresses='127.0.0.1' -c unix_socket_permissions=0700"
        )
        started = False
        try:
            _run_checked(
                [
                    str(pg_ctl),
                    "--pgdata",
                    str(cluster.data_directory),
                    "--log",
                    str(cluster.log_path),
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
                        str(cluster.data_directory),
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
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """@brief 用显式临时凭据执行 SQL / Execute SQL with explicit ephemeral credentials.

    @param cluster 私有集群 / Private cluster.
    @param database 目标数据库 / Target database.
    @param user PostgreSQL 角色 / PostgreSQL role.
    @param sql 输入 SQL / Input SQL.
    @param password 可选密码 / Optional password.
    @param tuples_only 是否只输出无格式元组 / Whether to output unformatted tuples only.
    @param check 是否把非零退出升级为异常 / Whether to raise for a non-zero exit.
    @return psql 结果；预期拒绝场景可返回非零结果 / psql result; expected-rejection
        scenarios may return a non-zero result.
    """

    command = ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1"]
    if tuples_only:
        command.extend(("--tuples-only", "--no-align"))
    environment = direct_psql_environment(
        host="127.0.0.1",
        port=cluster.port,
        database=database,
        user=user,
        password=password,
    )
    return subprocess.run(
        command,
        input=sql,
        text=True,
        env=environment,
        check=check,
        capture_output=True,
    )


def _scalar(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    sql: str,
) -> str:
    """@brief 查询一个维护角色可见的标量 / Query one scalar visible to the maintenance role.

    @param cluster 私有集群 / Private cluster.
    @param settings 显式 dbctl 设置 / Explicit dbctl settings.
    @param sql 单行标量查询 / One-row scalar query.
    @return 去除外围空白后的 psql 输出 / psql output stripped of surrounding whitespace.
    """

    result = _psql(
        cluster,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=_PASSWORD,
        sql=sql,
        tuples_only=True,
    )
    return result.stdout.strip()


def _bootstrap_database(
    cluster: _EphemeralPostgres,
) -> DbctlSettings:
    """@brief 经真实 dbctl bootstrap 创建角色隔离测试库 / Create the role-separated test database through real dbctl bootstrap.

    @param cluster 私有 PostgreSQL 集群 / Private PostgreSQL cluster.
    @return 已建库、尚未迁移的 dbctl 设置 / dbctl settings for a created but unmigrated database.
    """

    settings = cluster.settings(_DATABASE)
    bootstrap.execute(
        argparse.Namespace(no_sudo=True, dry_run=False),
        settings=settings,
    )
    _psql(
        cluster,
        database=_DATABASE,
        user=_SUPERUSER,
        sql="CREATE EXTENSION vector;",
    )
    return settings


def _uuid_for(label: str, kind: str) -> str:
    """@brief 从场景标签派生稳定测试 UUID / Derive a stable test UUID from a scenario label.

    @param label 测试场景标签 / Test-scenario label.
    @param kind 该场景内的实体种类 / Entity kind inside the scenario.
    @return 符合 PostgreSQL UUID 输入格式的稳定 UUID / Stable UUID acceptable to PostgreSQL.
    """

    return str(uuid5(NAMESPACE_URL, f"fogmoe:ctest:attachment-receipt:{label}:{kind}"))


def _valid_current_upload_request(source_message_id: int) -> dict[str, object]:
    """@brief 构造能授权附件转移的最小 request / Construct the minimal request authorizing an attachment transition.

    @param source_message_id Telegram source/current message ID / Telegram source/current message ID.
    @return 含精确 scope/current-upload 关联的 JSON 对象 / JSON object with an exact
        scope/current-upload relationship.
    """

    return {
        "current_turn_upload": {"source_message_id": source_message_id},
        "scope": {"is_group": False, "message_id": source_message_id},
        "user": {"user_id": _ADMIN_USER_ID},
    }


def _runtime_path_for(label: str) -> str:
    """@brief 由测试标签生成固定 Runtime payload 路径 / Derive the fixed Runtime payload path from a test label.

    @param label 场景唯一标签 / Scenario-unique label.
    @return 符合 receipt 约束的 Runtime 内 payload 路径 / Runtime-internal payload path satisfying the receipt constraint.
    """

    return (
        "/workspace/uploads/attachment-"
        + hashlib.sha256(f"{label}:path".encode("utf-8")).hexdigest()
        + "/payload"
    )


def _seed_pending_attachment(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    *,
    label: str,
    activity_status: str,
    request: Mapping[str, object],
    model_message: Mapping[str, object] | None = None,
) -> _AttachmentFixture:
    """@brief 写入一个旧或新 pending 附件 Turn / Write one legacy or new pending attachment Turn.

    @param cluster 私有集群 / Private cluster.
    @param settings 显式 dbctl 设置 / Explicit dbctl settings.
    @param label 场景唯一标签 / Scenario-unique label.
    @param activity_status 仅允许 pending 或 failed 的活动状态 / Activity status, limited to
        pending or failed.
    @param request 当前 durable request / Current durable request.
    @param model_message 可选故意偏离的 canonical model message；缺省时生成精确占位符 /
        Optional deliberately divergent canonical model message; when omitted, an exact
        placeholder is generated.
    @return 具有唯一 user source 行的 pending 附件 fixture / Pending attachment fixture with a
        sole user-source row.
    @raise ValueError 测试错误地请求其他 activity 状态时抛出 / Raised when the test asks for
        another activity state.
    @note 此 helper 只在迁移 owner 下插入固定测试数据；没有用户输入参与 SQL 组装。/
        This helper inserts fixed test data only under the migration owner; no user input takes
        part in SQL construction.
    """

    if activity_status not in {"pending", "failed"}:
        raise ValueError("fixture activity_status must be pending or failed")
    turn_id = _uuid_for(label, "turn")
    activity_id = _uuid_for(label, "activity")
    message_id = _uuid_for(label, "message")
    source_message_id = 70_000 + len(label)
    runtime_path = _runtime_path_for(label)
    fixture = _AttachmentFixture(
        label=label,
        turn_id=turn_id,
        activity_id=activity_id,
        message_id=message_id,
        conversation_id=f"ctest-attachment-receipt:{label}",
        source_message_id=source_message_id,
        runtime_path=runtime_path,
    )
    placeholder = f'<workspace_file path="{fixture.runtime_path}" />'
    canonical_model_message = (
        {
            "schema_version": 2,
            "role": "user",
            "parts": [{"type": "text", "text": placeholder}],
            "policy": {"include_in_context": True},
            "meta": {},
        }
        if model_message is None
        else dict(model_message)
    )
    content = {
        "text": placeholder,
        "model_message": canonical_model_message,
        "workspace_attachment": {"version": 1, "state": "pending"},
    }
    next_attempt_at = "CURRENT_TIMESTAMP" if activity_status == "pending" else "NULL"
    sql = f"""
        INSERT INTO conversation.conversation_turns (
          turn_id, conversation_id, state, source_kind, source_key
        ) VALUES (
          {quote_literal(fixture.turn_id)}::UUID,
          {quote_literal(fixture.conversation_id)},
          'waiting_inference',
          'ctest.attachment-receipt',
          {quote_literal(fixture.label)}
        );

        INSERT INTO conversation.inference_activities (
          activity_id, turn_id, conversation_id, request, status, next_attempt_at, traceparent
        ) VALUES (
          {quote_literal(fixture.activity_id)}::UUID,
          {quote_literal(fixture.turn_id)}::UUID,
          {quote_literal(fixture.conversation_id)},
          {quote_literal(json.dumps(dict(request), sort_keys=True))}::JSONB,
          {quote_literal(activity_status)},
          {next_attempt_at},
          {_TRACEPARENT!r}
        );

        INSERT INTO conversation.conversation_messages (
          message_id, conversation_id, sequence, turn_id, role, content, idempotency_key
        ) VALUES (
          {quote_literal(fixture.message_id)}::UUID,
          {quote_literal(fixture.conversation_id)},
          1,
          {quote_literal(fixture.turn_id)}::UUID,
          'user',
          {quote_literal(json.dumps(content, sort_keys=True))}::JSONB,
          'current-user'
        );
    """
    _psql(
        cluster,
        database=settings.endpoint.name,
        user=settings.maintenance.username,
        password=_PASSWORD,
        sql=sql,
    )
    return fixture


def _receipt_insert_sql(fixture: _AttachmentFixture) -> str:
    """@brief 构造一个与 fixture 精确对应的 receipt INSERT / Construct a receipt INSERT exactly matching a fixture.

    @param fixture pending 当前附件 fixture / Pending current-attachment fixture.
    @return 可在显式事务中执行的单条 INSERT / One INSERT executable inside an explicit transaction.
    """

    request_hash = hashlib.sha256(
        f"{fixture.label}:request".encode("utf-8")
    ).hexdigest()
    content_sha256 = hashlib.sha256(
        f"{fixture.label}:content".encode("utf-8")
    ).hexdigest()
    return f"""
        INSERT INTO workspace.attachment_import_receipts (
          turn_id, conversation_id, source_message_id, scope_kind, scope_id,
          request_id, request_hash, runtime_path, byte_size, sha256
        ) VALUES (
          {quote_literal(fixture.turn_id)}::UUID,
          {quote_literal(fixture.conversation_id)},
          {quote_literal(fixture.message_id)}::UUID,
          'personal',
          {_ADMIN_USER_ID},
          {quote_literal(fixture.turn_id + ":attachment-import")},
          {quote_literal(request_hash)},
          {quote_literal(fixture.runtime_path)},
          3,
          {quote_literal(content_sha256)}
        );
    """


def _intent_insert_sql(fixture: _AttachmentFixture) -> str:
    """@brief 构造一个与 fixture 精确对应、在 native side effect 前插入的 intent INSERT / Construct an intent INSERT exactly matching a fixture before the native side effect.

    @param fixture pending 当前附件 fixture / Pending current-attachment fixture.
    @return 可在显式事务中执行的单条 INSERT / One INSERT executable inside an explicit transaction.
    """

    request_hash = hashlib.sha256(
        f"{fixture.label}:request".encode("utf-8")
    ).hexdigest()
    content_sha256 = hashlib.sha256(
        f"{fixture.label}:content".encode("utf-8")
    ).hexdigest()
    return f"""
        INSERT INTO workspace.attachment_import_intents (
          turn_id, conversation_id, source_message_id, scope_kind, scope_id,
          request_id, request_hash, runtime_path, byte_size, sha256
        ) VALUES (
          {quote_literal(fixture.turn_id)}::UUID,
          {quote_literal(fixture.conversation_id)},
          {quote_literal(fixture.message_id)}::UUID,
          'personal',
          {_ADMIN_USER_ID},
          {quote_literal(fixture.turn_id + ":attachment-import")},
          {quote_literal(request_hash)},
          {quote_literal(fixture.runtime_path)},
          3,
          {quote_literal(content_sha256)}
        );
    """


def _marker_update_sql(fixture: _AttachmentFixture, state: str) -> str:
    """@brief 构造将 fixture marker 转到一个状态的 SQL / Construct SQL transitioning a fixture marker to one state.

    @param fixture pending 当前附件 fixture / Pending current-attachment fixture.
    @param state 目标状态，仅由测试传入 imported 或 unavailable / Target state, supplied by
        the test only as imported or unavailable.
    @return 单条受 trigger 保护的 UPDATE / One trigger-protected UPDATE.
    """

    if state not in {"imported", "unavailable"}:
        raise ValueError("test marker update state must be imported or unavailable")
    return f"""
        UPDATE conversation.conversation_messages
        SET content = jsonb_set(
          content,
          '{{workspace_attachment,state}}',
          {quote_literal(json.dumps(state))}::JSONB,
          false
        )
        WHERE message_id = {quote_literal(fixture.message_id)}::UUID;
    """


def _marker_state(
    cluster: _EphemeralPostgres,
    settings: DbctlSettings,
    fixture: _AttachmentFixture,
) -> str:
    """@brief 读取 fixture 的 durable marker 状态 / Read a fixture's durable marker state.

    @param cluster 私有集群 / Private cluster.
    @param settings 显式 dbctl 设置 / Explicit dbctl settings.
    @param fixture 当前附件 fixture / Current-attachment fixture.
    @return marker 内的状态字符串 / State string inside the marker.
    """

    return _scalar(
        cluster,
        settings,
        "SELECT content #>> '{workspace_attachment,state}' "
        "FROM conversation.conversation_messages "
        f"WHERE message_id = {quote_literal(fixture.message_id)}::UUID;",
    )


def _run_irreversible_downgrade(settings: DbctlSettings) -> None:
    """@brief 通过真实 Alembic downgrade 尝试回退 0071 / Attempt to downgrade 0071 through real Alembic.

    @param settings 显式 dbctl 设置 / Explicit dbctl settings.
    @return None / None.
    @note 0071 的 downgrade 必须失败；调用方负责断言异常与数据库版本均保持不变。/
        The 0071 downgrade must fail; the caller asserts both the exception and unchanged
        database version.
    """

    configuration = Config(str(_PROJECT_ROOT / "alembic.ini"))
    configuration.attributes["database_url"] = maintenance_database_url(settings)
    configuration.attributes["migration_schema"] = settings.maintenance.migration_schema
    configuration.attributes["admin_user_id"] = settings.administrator.user_id
    configuration.attributes["application_role"] = settings.application.username
    alembic_command.downgrade(
        configuration,
        "0070_workspace_attachment_model_boundary",
    )


def _exception_text(error: BaseException) -> str:
    """@brief 收集异常链的可断言文本 / Collect assertable text from an exception chain.

    @param error 最外层 Alembic/SQLAlchemy 异常 / Outermost Alembic/SQLAlchemy exception.
    @return 去重且以换行连接的链上消息 / Deduplicated chain messages joined with newlines.
    """

    messages: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        if message:
            messages.append(message)
        current = current.__cause__ or current.__context__
    return "\n".join(messages)


@unittest.skipIf(_POSTGRES_GATE_REASON is not None, _POSTGRES_GATE_REASON or "")
class WorkspaceAttachmentReceiptPostgresTests(unittest.TestCase):
    """@brief 0070→0071 和 receipt 状态机的真实数据库契约 / Real-database contract for 0070→0071 and the receipt state machine."""

    def _assert_rejected(
        self,
        cluster: _EphemeralPostgres,
        settings: DbctlSettings,
        sql: str,
        *,
        expected: str,
    ) -> None:
        """@brief 断言触发器拒绝 SQL 并带有目标诊断 / Assert that a trigger rejects SQL with the intended diagnostic.

        @param cluster 私有集群 / Private cluster.
        @param settings 显式 dbctl 设置 / Explicit dbctl settings.
        @param sql 预期被拒绝的 SQL / SQL expected to be rejected.
        @param expected stderr 中必须出现的稳定诊断片段 / Stable diagnostic fragment required in stderr.
        @return None / None.
        """

        result = _psql(
            cluster,
            database=settings.endpoint.name,
            user=settings.maintenance.username,
            password=_PASSWORD,
            sql=sql,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(expected, result.stderr)

    def test_0071_receipt_state_machine_and_irreversible_downgrade(self) -> None:
        """@brief 验证 legacy terminalization、receipt 原子性、非法边与不可逆回退 / Verify legacy terminalization, receipt atomicity, illegal edges, and irreversible rollback.

        @return None / None.
        """

        with _postgres_cluster() as cluster:
            settings = _bootstrap_database(cluster)
            migration_execution.run_alembic(
                settings=settings,
                revision="0070_workspace_attachment_model_boundary",
                dry_run=False,
            )

            legacy = _seed_pending_attachment(
                cluster,
                settings,
                label="legacy-pending",
                activity_status="failed",
                request=_valid_current_upload_request(70_000 + len("legacy-pending")),
            )

            migration_execution.run_alembic(
                settings=settings,
                revision="0071_workspace_attachment_import_receipts",
                dry_run=False,
            )
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT version_num FROM infra.alembic_version;",
                ),
                "0071_workspace_attachment_import_receipts",
            )
            self.assertEqual(_marker_state(cluster, settings, legacy), "unavailable")
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT count(*) FROM workspace.attachment_import_receipts "
                    f"WHERE turn_id = {quote_literal(legacy.turn_id)}::UUID;",
                ),
                "0",
            )

            receipt_only = _seed_pending_attachment(
                cluster,
                settings,
                label="receipt-only",
                activity_status="pending",
                request=_valid_current_upload_request(70_000 + len("receipt-only")),
            )
            self._assert_rejected(
                cluster,
                settings,
                "BEGIN;\n" + _receipt_insert_sql(receipt_only) + "\nCOMMIT;",
                expected="receipt must commit with its source marker imported",
            )
            self.assertEqual(_marker_state(cluster, settings, receipt_only), "pending")
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT count(*) FROM workspace.attachment_import_receipts "
                    f"WHERE turn_id = {quote_literal(receipt_only.turn_id)}::UUID;",
                ),
                "0",
            )

            wrong_canonical_label = "wrong-canonical-model-message"
            wrong_canonical_placeholder = (
                f'<workspace_file path="{_runtime_path_for(wrong_canonical_label)}" />'
            )
            wrong_canonical = _seed_pending_attachment(
                cluster,
                settings,
                label=wrong_canonical_label,
                activity_status="pending",
                request=_valid_current_upload_request(
                    70_000 + len(wrong_canonical_label)
                ),
                model_message={
                    "schema_version": 2,
                    "role": "user",
                    "parts": [
                        {"type": "text", "text": wrong_canonical_placeholder},
                        {
                            "type": "text",
                            "text": "raw caption that must never become model-visible",
                        },
                    ],
                    "policy": {"include_in_context": True},
                    "meta": {},
                },
            )
            self._assert_rejected(
                cluster,
                settings,
                _receipt_insert_sql(wrong_canonical),
                expected="exact pending source placeholder and canonical model message",
            )
            self.assertEqual(
                _marker_state(cluster, settings, wrong_canonical), "pending"
            )

            witnessed = _seed_pending_attachment(
                cluster,
                settings,
                label="receipt-and-imported",
                activity_status="pending",
                request=_valid_current_upload_request(
                    70_000 + len("receipt-and-imported")
                ),
            )
            _psql(
                cluster,
                database=settings.endpoint.name,
                user=settings.maintenance.username,
                password=_PASSWORD,
                sql=(
                    "BEGIN;\n"
                    + _receipt_insert_sql(witnessed)
                    + "\n"
                    + _marker_update_sql(witnessed, "imported")
                    + "\nCOMMIT;"
                ),
            )
            self.assertEqual(_marker_state(cluster, settings, witnessed), "imported")
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT count(*) FROM workspace.attachment_import_receipts "
                    f"WHERE turn_id = {quote_literal(witnessed.turn_id)}::UUID;",
                ),
                "1",
            )

            no_receipt = _seed_pending_attachment(
                cluster,
                settings,
                label="imported-without-receipt",
                activity_status="pending",
                request=_valid_current_upload_request(
                    70_000 + len("imported-without-receipt")
                ),
            )
            self._assert_rejected(
                cluster,
                settings,
                _marker_update_sql(no_receipt, "imported"),
                expected="imported marker requires its matching durable receipt",
            )
            self.assertEqual(_marker_state(cluster, settings, no_receipt), "pending")

            malformed_failed = _seed_pending_attachment(
                cluster,
                settings,
                label="malformed-failed-upload",
                activity_status="failed",
                request={
                    "current_turn_upload": {},
                    "scope": {
                        "is_group": False,
                        "message_id": 70_000 + len("malformed-failed-upload"),
                    },
                    "user": {"user_id": _ADMIN_USER_ID},
                },
            )
            self._assert_rejected(
                cluster,
                settings,
                _marker_update_sql(malformed_failed, "unavailable"),
                expected="unavailable marker requires final attachment failure without a receipt",
            )
            self.assertEqual(
                _marker_state(cluster, settings, malformed_failed), "pending"
            )

            mismatched_failed_label = "mismatched-failed-upload"
            mismatched_failed_source_message_id = 70_000 + len(mismatched_failed_label)
            mismatched_failed = _seed_pending_attachment(
                cluster,
                settings,
                label=mismatched_failed_label,
                activity_status="failed",
                request={
                    "current_turn_upload": {
                        "source_message_id": mismatched_failed_source_message_id + 1
                    },
                    "scope": {
                        "is_group": False,
                        "message_id": mismatched_failed_source_message_id,
                    },
                    "user": {"user_id": _ADMIN_USER_ID},
                },
            )
            self._assert_rejected(
                cluster,
                settings,
                _marker_update_sql(mismatched_failed, "unavailable"),
                expected="unavailable marker requires final attachment failure without a receipt",
            )
            self.assertEqual(
                _marker_state(cluster, settings, mismatched_failed), "pending"
            )

            nonpositive_failed = _seed_pending_attachment(
                cluster,
                settings,
                label="nonpositive-failed-upload",
                activity_status="failed",
                request={
                    "current_turn_upload": {"source_message_id": 0},
                    "scope": {"is_group": False, "message_id": 0},
                    "user": {"user_id": _ADMIN_USER_ID},
                },
            )
            self._assert_rejected(
                cluster,
                settings,
                _marker_update_sql(nonpositive_failed, "unavailable"),
                expected="unavailable marker requires final attachment failure without a receipt",
            )
            self.assertEqual(
                _marker_state(cluster, settings, nonpositive_failed), "pending"
            )

            nonfailed = _seed_pending_attachment(
                cluster,
                settings,
                label="nonfailed-valid-upload",
                activity_status="pending",
                request=_valid_current_upload_request(
                    70_000 + len("nonfailed-valid-upload")
                ),
            )
            self._assert_rejected(
                cluster,
                settings,
                _marker_update_sql(nonfailed, "unavailable"),
                expected="unavailable marker requires final attachment failure without a receipt",
            )
            self.assertEqual(_marker_state(cluster, settings, nonfailed), "pending")

            final_failure = _seed_pending_attachment(
                cluster,
                settings,
                label="valid-failed-upload",
                activity_status="failed",
                request=_valid_current_upload_request(
                    70_000 + len("valid-failed-upload")
                ),
            )
            _psql(
                cluster,
                database=settings.endpoint.name,
                user=settings.maintenance.username,
                password=_PASSWORD,
                sql=_marker_update_sql(final_failure, "unavailable"),
            )
            self.assertEqual(
                _marker_state(cluster, settings, final_failure), "unavailable"
            )

            with self.assertRaises(Exception) as captured:
                _run_irreversible_downgrade(settings)
            self.assertIn("0071 is irreversible", _exception_text(captured.exception))
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT version_num FROM infra.alembic_version;",
                ),
                "0071_workspace_attachment_import_receipts",
            )
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT to_regclass('workspace.attachment_import_receipts') IS NOT NULL;",
                ),
                "t",
            )

    def test_0072_intent_backfill_receipt_gate_and_unavailable_fence(self) -> None:
        """@brief 0072 回填既有 receipt，要求新 receipt 有 intent，并阻止 prepared source 终结 unavailable / 0072 backfills existing receipts, requires an intent for new receipts, and prevents a prepared source becoming unavailable.

        @return None / None.
        @note 这个真实数据库测试与 application recovery test 互补：这里证明 DB gate 会把
            ``AttachmentImportIntent`` 保留为 native/receipt 两侧的 durable bridge，而不是只
            依赖 Python 的重试顺序。/ This real-database test complements the application
            recovery test: it proves the DB gate retains ``AttachmentImportIntent`` as the durable
            bridge between native and receipt rather than relying only on Python retry order.
        """

        with _postgres_cluster() as cluster:
            settings = _bootstrap_database(cluster)
            migration_execution.run_alembic(
                settings=settings,
                revision="0071_workspace_attachment_import_receipts",
                dry_run=False,
            )

            legacy_receipt = _seed_pending_attachment(
                cluster,
                settings,
                label="0072-backfilled-receipt",
                activity_status="pending",
                request=_valid_current_upload_request(
                    70_000 + len("0072-backfilled-receipt")
                ),
            )
            _psql(
                cluster,
                database=settings.endpoint.name,
                user=settings.maintenance.username,
                password=_PASSWORD,
                sql=(
                    "BEGIN;\n"
                    + _receipt_insert_sql(legacy_receipt)
                    + "\n"
                    + _marker_update_sql(legacy_receipt, "imported")
                    + "\nCOMMIT;"
                ),
            )

            prepared_before_upgrade = _seed_pending_attachment(
                cluster,
                settings,
                label="0072-prepared-success",
                activity_status="pending",
                request=_valid_current_upload_request(
                    70_000 + len("0072-prepared-success")
                ),
            )

            migration_execution.run_alembic(
                settings=settings,
                revision="0072_workspace_attachment_import_intents",
                dry_run=False,
            )
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT version_num FROM infra.alembic_version;",
                ),
                "0072_workspace_attachment_import_intents",
            )
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT count(*) FROM workspace.attachment_import_intents "
                    f"WHERE turn_id = {quote_literal(legacy_receipt.turn_id)}::UUID;",
                ),
                "1",
            )
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT request_hash FROM workspace.attachment_import_intents "
                    f"WHERE turn_id = {quote_literal(legacy_receipt.turn_id)}::UUID;",
                ),
                hashlib.sha256(
                    f"{legacy_receipt.label}:request".encode("utf-8")
                ).hexdigest(),
            )

            no_intent = _seed_pending_attachment(
                cluster,
                settings,
                label="0072-receipt-without-intent",
                activity_status="pending",
                request=_valid_current_upload_request(
                    70_000 + len("0072-receipt-without-intent")
                ),
            )
            self._assert_rejected(
                cluster,
                settings,
                _receipt_insert_sql(no_intent),
                expected="exact previously prepared import intent",
            )
            self.assertEqual(_marker_state(cluster, settings, no_intent), "pending")

            _psql(
                cluster,
                database=settings.endpoint.name,
                user=settings.maintenance.username,
                password=_PASSWORD,
                sql=(
                    "BEGIN;\n"
                    + _intent_insert_sql(prepared_before_upgrade)
                    + "\n"
                    + _receipt_insert_sql(prepared_before_upgrade)
                    + "\n"
                    + _marker_update_sql(prepared_before_upgrade, "imported")
                    + "\nCOMMIT;"
                ),
            )
            self.assertEqual(
                _marker_state(cluster, settings, prepared_before_upgrade), "imported"
            )

            prepared_failed = _seed_pending_attachment(
                cluster,
                settings,
                label="0072-prepared-failed",
                activity_status="failed",
                request=_valid_current_upload_request(
                    70_000 + len("0072-prepared-failed")
                ),
            )
            _psql(
                cluster,
                database=settings.endpoint.name,
                user=settings.maintenance.username,
                password=_PASSWORD,
                sql=_intent_insert_sql(prepared_failed),
            )
            self._assert_rejected(
                cluster,
                settings,
                _marker_update_sql(prepared_failed, "unavailable"),
                expected="without a receipt or prepared intent",
            )
            self.assertEqual(
                _marker_state(cluster, settings, prepared_failed), "pending"
            )

            with self.assertRaises(Exception) as captured:
                _run_irreversible_downgrade(settings)
            self.assertIn("0072 is irreversible", _exception_text(captured.exception))
            self.assertEqual(
                _scalar(
                    cluster,
                    settings,
                    "SELECT to_regclass('workspace.attachment_import_intents') IS NOT NULL;",
                ),
                "t",
            )


if __name__ == "__main__":
    if _POSTGRES_GATE_REASON is not None:
        print(f"SKIP: {_POSTGRES_GATE_REASON}", file=sys.stderr)
        raise SystemExit(77)
    unittest.main()
