"""@brief PostgreSQL 当前 Workspace 附件 receipt adapter / PostgreSQL current-Workspace attachment receipt adapter.

此 adapter 是 native 文件 publish 与 Conversation 模型可见性之间的唯一事务边界：同一事务
插入不可变 receipt 并把受控消息 marker 从 pending 变为 imported。/ This adapter is the
sole transactional boundary between native file publication and Conversation model visibility:
one transaction inserts an immutable receipt and changes the controlled message marker from
pending to imported.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from fogmoe_bot.application.assistant.current_turn_upload import (
    workspace_attachment_file_path,
)
from fogmoe_bot.application.assistant.inference_command import (
    DurableAssistantInferenceCommand,
)
from fogmoe_bot.application.assistant.workspace_attachment_receipt import (
    WorkspaceAttachmentImportReceipt,
    WorkspaceAttachmentReceiptConflictError,
    WorkspaceAttachmentReceiptStore,
    WorkspaceAttachmentReceiptUnavailableError,
)
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, text_message
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.workspace.attachment import (
    WORKSPACE_ATTACHMENT_FIELD,
    WorkspaceAttachmentImportState,
    workspace_attachment_import_state,
)
from fogmoe_bot.domain.workspace.scope import (
    GroupRuntimeScope,
    PersonalRuntimeScope,
    RuntimeScope,
    runtime_scope_parts,
)
from fogmoe_bot.infrastructure.database import db


_PERMANENT_RECEIPT_SQLSTATE_PREFIXES = frozenset({"22", "23", "42"})
"""@brief 表示不可重试 receipt 语义/数据错误的 PostgreSQL SQLSTATE 类别 / PostgreSQL SQLSTATE classes denoting non-retryable receipt semantic or data errors."""

_PERMANENT_RECEIPT_SQLSTATES = frozenset({"55000"})
"""@brief 不属于通用类别、但必须终结为冲突的 receipt SQLSTATE / Receipt SQLSTATEs outside general classes that must terminate as conflicts."""


def _postgres_sqlstate(error: SQLAlchemyError) -> str | None:
    """@brief 从 SQLAlchemy/driver 异常提取规范 PostgreSQL SQLSTATE / Extract a canonical PostgreSQL SQLSTATE from a SQLAlchemy/driver exception.

    @param error SQLAlchemy 暴露的数据库异常 / Database exception exposed by SQLAlchemy.
    @return 五字符规范 SQLSTATE；driver 未提供时返回 ``None`` / Canonical five-character SQLSTATE, or ``None`` when the driver does not provide one.
    @note asyncpg 使用 ``sqlstate``，部分 DBAPI 驱动使用 ``pgcode``；两者都只来自
        已捕获的 driver 异常，绝不解析错误文本。/ asyncpg uses ``sqlstate`` while some
        DBAPI drivers use ``pgcode``; both are read only from the captured driver exception and
        error text is never parsed.
    """

    candidates = (error, getattr(error, "orig", None), error.__cause__)
    for candidate in candidates:
        if candidate is None:
            continue
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if (
                isinstance(value, str)
                and len(value) == 5
                and value.isascii()
                and value.isalnum()
            ):
                return value.upper()
    return None


def _receipt_storage_error(
    error: SQLAlchemyError,
) -> (
    WorkspaceAttachmentReceiptConflictError | WorkspaceAttachmentReceiptUnavailableError
):
    """@brief 将 PostgreSQL receipt 异常分类为永久冲突或临时不可用 / Classify a PostgreSQL receipt error as a permanent conflict or temporary unavailability.

    @param error 已捕获的 SQLAlchemy 数据库异常 / Captured SQLAlchemy database exception.
    @return 可安全呈现给 application 重试策略的受限异常 / Restricted exception safe to present to the application retry policy.
    @note 约束/数据/SQL 语义错误与 ``55000`` 表示 durable 事实不再能与当前命令相容，
        不能伪装成网络故障重放 native journal。连接中断、serialization/deadlock 等其他
        driver 错误仍保守归为暂时不可用。/ Constraint, data, and SQL-semantic errors plus
        ``55000`` mean durable facts no longer agree with the current command, so they must not
        replay the native journal as a network failure. Connection loss, serialization/deadlock,
        and other driver errors remain conservatively temporarily unavailable.
    """

    if _is_permanent_attachment_storage_error(error):
        return WorkspaceAttachmentReceiptConflictError(
            "Workspace attachment receipt conflicts with durable database state"
        )
    return WorkspaceAttachmentReceiptUnavailableError(
        "Workspace attachment receipt storage is temporarily unavailable"
    )


def _is_permanent_attachment_storage_error(error: SQLAlchemyError) -> bool:
    """@brief 判断 PostgreSQL 附件持久化故障是否为不可重试语义冲突 / Determine whether a PostgreSQL attachment-persistence failure is a non-retryable semantic conflict.

    @param error 已捕获的 SQLAlchemy 数据库异常 / Captured SQLAlchemy database exception.
    @return 约束、数据、SQL 语义或状态冲突时为 True / ``True`` for constraint, data, SQL-semantic, or state conflicts.
    @note intent 与 receipt 共享这个分类，但各自在 application 层保留自己的错误类型；这样
        retry policy 不会把“已写 native 后的约束冲突”伪装成连接故障。/ Intent and receipt
        share this classification while retaining their own application error types; retry policy
        therefore cannot disguise a post-native constraint conflict as a connection failure.
    """

    sqlstate = _postgres_sqlstate(error)
    return sqlstate is not None and (
        sqlstate in _PERMANENT_RECEIPT_SQLSTATES
        or sqlstate[:2] in _PERMANENT_RECEIPT_SQLSTATE_PREFIXES
    )


class PostgresWorkspaceAttachmentReceiptStore(WorkspaceAttachmentReceiptStore):
    """@brief 将受限 native 文件 receipt 原子见证到 PostgreSQL / Atomically witness constrained native file receipts in PostgreSQL."""

    async def record_import(
        self,
        command: DurableAssistantInferenceCommand,
        receipt: WorkspaceAttachmentImportReceipt,
    ) -> None:
        """@brief 插入 receipt 并发布对应 pending 附件行 / Insert a receipt and publish its matching pending attachment row.

        @param command 已验证的 durable Assistant command / Validated durable Assistant command.
        @param receipt 已由 native 成功和 application 协议核验的 receipt / Receipt already validated against native success and the application protocol.
        @return None / None.
        @raise WorkspaceAttachmentReceiptConflictError command、message、已有 receipt 或 marker
            语义漂移时抛出 / Raised when command, message, existing receipt, or marker
            semantics drift.
        @raise WorkspaceAttachmentReceiptUnavailableError PostgreSQL 暂时不可用时抛出 /
            Raised when PostgreSQL is temporarily unavailable.
        @note 先锁消息再插入 receipt，数据库 trigger 会再次验证 pending/path 关系；随后
            marker update 与 insert 同一事务提交。/ The method locks the message before
            inserting the receipt; a database trigger validates pending/path ownership again,
            and the marker update commits in the same transaction as the insert.
        """

        try:
            self._validate_command_receipt(command, receipt)
            async with db.transaction() as connection:
                message_id, content = await self._lock_user_message(
                    command,
                    connection=connection,
                )
                state = _validated_attachment_state(content)
                if state is WorkspaceAttachmentImportState.UNAVAILABLE:
                    raise WorkspaceAttachmentReceiptConflictError(
                        "Attachment row is permanently unavailable and cannot be published"
                    )
                existing = await self._load_receipt(
                    receipt,
                    connection=connection,
                )
                if state is WorkspaceAttachmentImportState.IMPORTED:
                    if existing is None:
                        raise WorkspaceAttachmentReceiptConflictError(
                            "Imported attachment message has no durable receipt"
                        )
                    _validate_existing_receipt(existing, receipt, message_id=message_id)
                    return
                if state is not WorkspaceAttachmentImportState.PENDING:
                    raise WorkspaceAttachmentReceiptConflictError(
                        "Attachment message is not in a publishable pending state"
                    )

                if existing is None:
                    scope_kind, scope_id = runtime_scope_parts(receipt.scope)
                    await db.execute(
                        "INSERT INTO workspace.attachment_import_receipts ("
                        "turn_id, conversation_id, source_message_id, scope_kind, scope_id, "
                        "request_id, request_hash, runtime_path, byte_size, sha256) VALUES ("
                        "CAST(%s AS UUID), %s, CAST(%s AS UUID), %s, %s, %s, %s, %s, %s, %s)",
                        (
                            str(receipt.turn_id),
                            str(receipt.conversation_id),
                            message_id,
                            scope_kind.value,
                            scope_id,
                            receipt.request_id.value,
                            receipt.request_hash.value,
                            receipt.path,
                            receipt.byte_size,
                            receipt.sha256,
                        ),
                        connection=connection,
                    )
                    existing = await self._load_receipt(
                        receipt,
                        connection=connection,
                    )
                    if (
                        existing is None
                    ):  # pragma: no cover - PostgreSQL INSERT is synchronous.
                        raise WorkspaceAttachmentReceiptUnavailableError(
                            "Attachment receipt insert was not visible in its transaction"
                        )
                _validate_existing_receipt(existing, receipt, message_id=message_id)

                updated = await db.execute(
                    "UPDATE conversation.conversation_messages SET content = jsonb_set("
                    "content, '{workspace_attachment,state}', '\"imported\"'::JSONB, false) "
                    "WHERE message_id = CAST(%s AS UUID) "
                    "AND content #>> '{workspace_attachment,version}' = '1' "
                    "AND content #>> '{workspace_attachment,state}' = 'pending'",
                    (message_id,),
                    connection=connection,
                )
                if updated != 1:
                    raise WorkspaceAttachmentReceiptConflictError(
                        "Attachment pending marker changed during receipt publication"
                    )
        except WorkspaceAttachmentReceiptConflictError:
            raise
        except WorkspaceAttachmentReceiptUnavailableError:
            raise
        except SQLAlchemyError as error:
            raise _receipt_storage_error(error) from error

    @staticmethod
    def _validate_command_receipt(
        command: DurableAssistantInferenceCommand,
        receipt: WorkspaceAttachmentImportReceipt,
    ) -> None:
        """@brief 验证 receipt 恰属于该 durable 当前附件 / Validate that a receipt belongs exactly to this durable current attachment.

        @param command 已验证 durable command / Validated durable command.
        @param receipt native 成功导出的 application receipt / Application receipt derived from native success.
        @return None / None.
        @raise WorkspaceAttachmentReceiptConflictError command 与 receipt 不属于同一附件时抛出 /
            Raised when command and receipt do not belong to the same attachment.
        """

        if not isinstance(command, DurableAssistantInferenceCommand):
            raise TypeError("Attachment receipt requires a durable Assistant command")
        if not isinstance(receipt, WorkspaceAttachmentImportReceipt):
            raise TypeError("Attachment receipt requires a typed import receipt")
        reference = command.current_turn_upload
        if reference is None:
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment receipt command has no current_turn_upload"
            )
        if (
            receipt.turn_id != command.typed_turn_id
            or receipt.conversation_id != command.typed_conversation_id
            or receipt.scope != _runtime_scope_for(command)
        ):
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment receipt crossed its durable turn or workspace scope"
            )
        expected_path = workspace_attachment_file_path(
            turn_id=command.typed_turn_id,
            reference=reference,
        )
        if receipt.path != expected_path:
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment receipt path does not match the durable upload reference"
            )

    async def _lock_user_message(
        self,
        command: DurableAssistantInferenceCommand,
        *,
        connection: AsyncConnection,
    ) -> tuple[str, JsonObject]:
        """@brief 锁定并验证当前 Turn 唯一 user 消息 / Lock and validate the sole current-Turn user message.

        @param command 已验证 durable command / Validated durable command.
        @param connection 当前 PostgreSQL transaction connection / Current PostgreSQL transaction connection.
        @return 消息 UUID 文本与严格 JSON envelope / Message UUID text and strict JSON envelope.
        @raise WorkspaceAttachmentReceiptConflictError 消息数量、角色、placeholder 或 marker
            不符合 ingress 合约时抛出 / Raised when message cardinality, role, placeholder, or
            marker violates the ingress contract.
        """

        rows = await db.fetch_all(
            "SELECT message_id, content FROM conversation.conversation_messages "
            "WHERE turn_id = CAST(%s AS UUID) AND conversation_id = %s "
            "AND role = 'user' FOR UPDATE",
            (str(command.typed_turn_id), str(command.typed_conversation_id)),
            connection=connection,
        )
        if len(rows) != 1:
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment receipt requires exactly one current Turn user message"
            )
        message_id = str(rows[0][0])
        content = rows[0][1]
        if not isinstance(content, dict):
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment user message content is not a JSON object"
            )
        reference = command.current_turn_upload
        if reference is None:  # pragma: no cover - validated by record_import.
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment receipt command lost its upload reference"
            )
        expected_path = workspace_attachment_file_path(
            turn_id=command.typed_turn_id,
            reference=reference,
        )
        expected = f'<workspace_file path="{expected_path}" />'
        if content.get("text") != expected:
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment user message text is not the fixed Workspace placeholder"
            )
        model_message = content.get("model_message")
        if not isinstance(model_message, Mapping):
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment user message has an invalid canonical model message"
            )
        try:
            canonical = CanonicalMessage.from_json(model_message)
        except (TypeError, ValueError) as error:
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment user message has an invalid canonical model message"
            ) from error
        if canonical != text_message(MessageRole.USER, expected):
            raise WorkspaceAttachmentReceiptConflictError(
                "Attachment user message model placeholder drifted"
            )
        return message_id, content

    async def _load_receipt(
        self,
        receipt: WorkspaceAttachmentImportReceipt,
        *,
        connection: AsyncConnection,
    ) -> tuple[object, ...] | None:
        """@brief 锁定同 Turn 的既有 immutable receipt / Lock an existing immutable receipt for the same Turn.

        @param receipt 待见证 receipt / Receipt to witness.
        @param connection 当前 PostgreSQL transaction connection / Current PostgreSQL transaction connection.
        @return 既有字段元组；无记录时为 None / Existing field tuple, or ``None`` when absent.
        """

        row = await db.fetch_one(
            "SELECT conversation_id, source_message_id, scope_kind, scope_id, request_id, "
            "request_hash, runtime_path, byte_size, sha256 "
            "FROM workspace.attachment_import_receipts "
            "WHERE turn_id = CAST(%s AS UUID) FOR UPDATE",
            (str(receipt.turn_id),),
            connection=connection,
        )
        return tuple(row) if row is not None else None


def _validated_attachment_state(content: JsonObject) -> WorkspaceAttachmentImportState:
    """@brief 读取 fail-closed 的受控 marker 状态 / Read the controlled marker state fail-closed.

    @param content 已锁定消息 JSON envelope / Locked message JSON envelope.
    @return 严格有效状态 / Strictly valid state.
    @raise WorkspaceAttachmentReceiptConflictError marker 缺失或畸形时抛出 /
        Raised when the marker is missing or malformed.
    """

    if WORKSPACE_ATTACHMENT_FIELD not in content:
        raise WorkspaceAttachmentReceiptConflictError(
            "Attachment user message has no workspace_attachment marker"
        )
    state = workspace_attachment_import_state(content)
    if state is None:
        raise WorkspaceAttachmentReceiptConflictError(
            "Attachment user message has an invalid workspace_attachment marker"
        )
    return state


def _validate_existing_receipt(
    values: tuple[object, ...],
    receipt: WorkspaceAttachmentImportReceipt,
    *,
    message_id: str,
) -> None:
    """@brief 验证同 Turn receipt 的不可变重放语义 / Validate immutable replay semantics of a same-Turn receipt.

    @param values 锁定数据库行的规范字段 / Canonical fields from the locked database row.
    @param receipt 本次 native 成功事实 / Native-success fact for this attempt.
    @param message_id 当前已锁定 user message ID / Current locked user-message identifier.
    @return None / None.
    @raise WorkspaceAttachmentReceiptConflictError 任意受保护字段漂移时抛出 /
        Raised when any protected field drifts.
    """

    if len(values) != 9:  # pragma: no cover - fixed SELECT shape.
        raise WorkspaceAttachmentReceiptConflictError(
            "Attachment receipt row has an unexpected shape"
        )
    scope_kind, scope_id = runtime_scope_parts(receipt.scope)
    expected = (
        str(receipt.conversation_id),
        message_id,
        scope_kind.value,
        scope_id,
        receipt.request_id.value,
        receipt.request_hash.value,
        receipt.path,
        receipt.byte_size,
        receipt.sha256,
    )
    actual = (
        str(values[0]),
        str(values[1]),
        str(values[2]),
        _database_int(values[3], field="scope_id"),
        str(values[4]),
        str(values[5]),
        str(values[6]),
        _database_int(values[7], field="byte_size"),
        str(values[8]),
    )
    if actual != expected:
        raise WorkspaceAttachmentReceiptConflictError(
            "Attachment receipt immutable semantics changed on replay"
        )


def _database_int(value: object, *, field: str) -> int:
    """@brief 严格读取 PostgreSQL 整数字段 / Strictly read a PostgreSQL integer field.

    @param value 数据库 driver 返回的候选值 / Candidate value returned by the database driver.
    @param field 用于诊断的字段名 / Field name used for diagnostics.
    @return 经类型校验的整数 / Type-checked integer.
    @raise WorkspaceAttachmentReceiptConflictError 值不是实际整数时抛出 /
        Raised when the value is not an actual integer.
    @note ``bool`` 是 Python ``int`` 的子类，必须显式拒绝，以免畸形 driver 值被错误地
        接受。/ ``bool`` is a Python ``int`` subclass and must be rejected explicitly so a
        malformed driver value cannot be accepted accidentally.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkspaceAttachmentReceiptConflictError(
            f"Attachment receipt {field} is not an integer"
        )
    return value


def _runtime_scope_for(command: DurableAssistantInferenceCommand) -> RuntimeScope:
    """@brief 从 command 重建其个人或整群 runtime scope / Rebuild the personal-or-whole-group runtime scope of a command.

    @param command 已验证 durable Assistant command / Validated durable Assistant command.
    @return 个人或整群 Workspace scope / Personal or whole-group Workspace scope.
    @raise WorkspaceAttachmentReceiptConflictError 群命令缺少 group ID 时抛出 /
        Raised when a group command lacks its group identifier.
    """

    if command.scope.is_group:
        group_id = command.scope.group_id
        if group_id is None:
            raise WorkspaceAttachmentReceiptConflictError(
                "Group attachment receipt command has no group ID"
            )
        return GroupRuntimeScope(group_id)
    return PersonalRuntimeScope(command.user.user_id)


__all__ = ["PostgresWorkspaceAttachmentReceiptStore"]
