"""@brief PostgreSQL ``AttachmentImportIntent`` 聚合 adapter / PostgreSQL adapter for the ``AttachmentImportIntent`` aggregate.

此 adapter 的提交点严格早于 native ``RuntimeProcess.add_file``。它不持久化 Telegram
capability 或 payload bytes；它只把固定 source、scope、journal identity 与经校验的内容
摘要变成不可变恢复事实。/ This adapter's commit point is strictly before native
``RuntimeProcess.add_file``. It persists neither a Telegram capability nor payload bytes; it turns
only the fixed source, scope, journal identity, and verified content digest into an immutable
recovery fact.
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
from fogmoe_bot.application.assistant.workspace_attachment_intent import (
    WorkspaceAttachmentImportIntentStore,
    WorkspaceAttachmentIntentConflictError,
    WorkspaceAttachmentIntentUnavailableError,
)
from fogmoe_bot.domain.assistant.messages import CanonicalMessage, text_message
from fogmoe_bot.domain.conversation.identity import (
    CURRENT_USER_MESSAGE_SEMANTIC_KEY,
    ConversationId,
    ConversationMessageId,
)
from fogmoe_bot.domain.conversation.message import MessageRole
from fogmoe_bot.domain.conversation.payloads import JsonObject
from fogmoe_bot.domain.workspace.attachment import (
    AttachmentImportIntent,
    WorkspaceAttachmentImportState,
    workspace_attachment_import_state,
)
from fogmoe_bot.domain.workspace.runtime import (
    WorkspaceRequestHash,
    WorkspaceRequestId,
)
from fogmoe_bot.domain.workspace.scope import (
    GroupRuntimeScope,
    PersonalRuntimeScope,
    RuntimeScope,
    runtime_scope_parts,
)
from fogmoe_bot.infrastructure.database import db

from .workspace_attachment_receipts import _is_permanent_attachment_storage_error


class PostgresWorkspaceAttachmentImportIntentStore(
    WorkspaceAttachmentImportIntentStore
):
    """@brief 在 PostgreSQL 中读取及准备不可变附件导入意图 / Read and prepare immutable attachment-import intents in PostgreSQL."""

    async def find(
        self,
        command: DurableAssistantInferenceCommand,
    ) -> AttachmentImportIntent | None:
        """@brief 按 Turn 读取已提交 intent，不触发任何 native 行为 / Read a committed intent by Turn without triggering native behavior.

        @param command 已验证的 durable Assistant command / Validated durable Assistant command.
        @return 对应 immutable aggregate；不存在时为 None / Corresponding immutable aggregate, or None when absent.
        @raise WorkspaceAttachmentIntentConflictError durable command 与行语义漂移时抛出 /
            Raised when durable command and row semantics drift.
        @raise WorkspaceAttachmentIntentUnavailableError PostgreSQL 暂时不可用时抛出 /
            Raised when PostgreSQL is temporarily unavailable.
        """

        try:
            self._validate_command(command)
            row = await db.fetch_one(
                "SELECT conversation_id, source_message_id, scope_kind, scope_id, "
                "request_id, request_hash, runtime_path, byte_size, sha256 "
                "FROM workspace.attachment_import_intents "
                "WHERE turn_id = CAST(%s AS UUID)",
                (str(command.typed_turn_id),),
            )
            if row is None:
                return None
            intent = _intent_from_values(
                command=command,
                values=tuple(row),
            )
            self._validate_command_intent(command, intent)
            return intent
        except WorkspaceAttachmentIntentConflictError:
            raise
        except SQLAlchemyError as error:
            raise _intent_storage_error(error) from error

    async def prepare(
        self,
        command: DurableAssistantInferenceCommand,
        intent: AttachmentImportIntent,
    ) -> AttachmentImportIntent:
        """@brief 在 native 调用前原子准备唯一 intent / Atomically prepare the sole intent before native invocation.

        @param command 已验证的 durable Assistant command / Validated durable Assistant command.
        @param intent 已下载并验证内容后构造的候选 aggregate / Candidate aggregate built after download and content verification.
        @return 当前 Turn 的唯一 immutable aggregate / Sole immutable aggregate for the current Turn.
        @raise WorkspaceAttachmentIntentConflictError source、marker 或并发既有 intent 不兼容时抛出 /
            Raised when source, marker, or a concurrent existing intent is incompatible.
        @raise WorkspaceAttachmentIntentUnavailableError PostgreSQL 暂时不可用时抛出 /
            Raised when PostgreSQL is temporarily unavailable.
        @note source message 的行锁与 intent insert 在同一短事务内；事务返回前数据库 trigger
            已验证 pending canonical placeholder，因此调用方可在返回后安全进行 native side
            effect。/ The source-message row lock and intent insert share one short transaction;
            before it returns, database triggers have validated the pending canonical placeholder,
            so the caller may safely perform the native side effect afterwards.
        """

        try:
            self._validate_command_intent(command, intent)
            async with db.transaction() as connection:
                source_message_id, content = await self._lock_user_message(
                    command,
                    connection=connection,
                )
                if source_message_id != str(intent.source_message_id):
                    raise WorkspaceAttachmentIntentConflictError(
                        "Attachment import intent source message does not match durable ingress"
                    )
                existing = await self._load_intent(
                    command,
                    connection=connection,
                )
                if existing is not None:
                    self._validate_command_intent(command, existing)
                    if existing != intent:
                        raise WorkspaceAttachmentIntentConflictError(
                            "Attachment import intent immutable semantics changed on prepare"
                        )
                    return existing

                state = _validated_attachment_state(content)
                if state is not WorkspaceAttachmentImportState.PENDING:
                    raise WorkspaceAttachmentIntentConflictError(
                        "Attachment message is not pending when its import intent is prepared"
                    )
                scope_kind, scope_id = runtime_scope_parts(intent.scope)
                await db.execute(
                    "INSERT INTO workspace.attachment_import_intents ("
                    "turn_id, conversation_id, source_message_id, scope_kind, scope_id, "
                    "request_id, request_hash, runtime_path, byte_size, sha256) VALUES ("
                    "CAST(%s AS UUID), %s, CAST(%s AS UUID), %s, %s, %s, %s, %s, %s, %s)",
                    (
                        str(intent.turn_id),
                        str(intent.conversation_id),
                        str(intent.source_message_id),
                        scope_kind.value,
                        scope_id,
                        intent.request_id.value,
                        intent.request_hash.value,
                        intent.path,
                        intent.byte_size,
                        intent.sha256,
                    ),
                    connection=connection,
                )
                stored = await self._load_intent(command, connection=connection)
                if (
                    stored is None
                ):  # pragma: no cover - PostgreSQL INSERT is synchronous.
                    raise WorkspaceAttachmentIntentUnavailableError(
                        "Attachment import intent insert was not visible in its transaction"
                    )
                self._validate_command_intent(command, stored)
                if stored != intent:
                    raise WorkspaceAttachmentIntentConflictError(
                        "Attachment import intent changed during its prepare transaction"
                    )
                return stored
        except WorkspaceAttachmentIntentConflictError:
            raise
        except WorkspaceAttachmentIntentUnavailableError:
            raise
        except SQLAlchemyError as error:
            raise _intent_storage_error(error) from error

    @staticmethod
    def _validate_command(command: DurableAssistantInferenceCommand) -> None:
        """@brief 验证此 adapter 只服务当前附件 command / Validate that this adapter serves only a current-attachment command.

        @param command 待读取或准备的 durable command / Durable command to read or prepare.
        @return None / None.
        @raise WorkspaceAttachmentIntentConflictError command 没有当前附件时抛出 / Raised when the command has no current attachment.
        """

        if not isinstance(command, DurableAssistantInferenceCommand):
            raise TypeError(
                "Attachment import intent requires a durable Assistant command"
            )
        if command.current_turn_upload is None:
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent command has no current_turn_upload"
            )

    @classmethod
    def _validate_command_intent(
        cls,
        command: DurableAssistantInferenceCommand,
        intent: AttachmentImportIntent,
    ) -> None:
        """@brief 验证 aggregate 严格绑定到 durable source / Validate that an aggregate is strictly bound to its durable source.

        @param command 已验证 durable command / Validated durable command.
        @param intent 候选或已存储 aggregate / Candidate or stored aggregate.
        @return None / None.
        @raise WorkspaceAttachmentIntentConflictError scope、source message、path 或 request identity 漂移时抛出 /
            Raised when scope, source message, path, or request identity drifts.
        """

        cls._validate_command(command)
        if not isinstance(intent, AttachmentImportIntent):
            raise TypeError("Attachment import intent store requires a typed aggregate")
        reference = command.current_turn_upload
        if reference is None:  # pragma: no cover - narrowed by _validate_command.
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent command lost its upload reference"
            )
        expected_source_message_id = ConversationMessageId.for_turn(
            command.typed_turn_id,
            CURRENT_USER_MESSAGE_SEMANTIC_KEY,
        )
        expected_path = workspace_attachment_file_path(
            turn_id=command.typed_turn_id,
            reference=reference,
        )
        if (
            intent.turn_id != command.typed_turn_id
            or intent.conversation_id != command.typed_conversation_id
            or intent.source_message_id != expected_source_message_id
            or intent.scope != _runtime_scope_for(command)
            or intent.path != expected_path
        ):
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent crossed its durable turn, source, or Workspace scope"
            )

    async def _lock_user_message(
        self,
        command: DurableAssistantInferenceCommand,
        *,
        connection: AsyncConnection,
    ) -> tuple[str, JsonObject]:
        """@brief 锁定并验证当前 Turn 的唯一 pending user source / Lock and validate the sole pending user source of the current Turn.

        @param command 已验证 durable command / Validated durable command.
        @param connection 当前 PostgreSQL transaction connection / Current PostgreSQL transaction connection.
        @return source UUID 文本与严格 envelope / Source UUID text and strict envelope.
        @raise WorkspaceAttachmentIntentConflictError source 数量、placeholder 或 canonical message 漂移时抛出 /
            Raised when source cardinality, placeholder, or canonical message drifts.
        """

        rows = await db.fetch_all(
            "SELECT message_id, content FROM conversation.conversation_messages "
            "WHERE turn_id = CAST(%s AS UUID) AND conversation_id = %s "
            "AND role = 'user' FOR UPDATE",
            (str(command.typed_turn_id), str(command.typed_conversation_id)),
            connection=connection,
        )
        if len(rows) != 1:
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent requires exactly one current Turn user message"
            )
        source_message_id = str(rows[0][0])
        content = rows[0][1]
        if not isinstance(content, dict):
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent user message content is not a JSON object"
            )
        reference = command.current_turn_upload
        if reference is None:  # pragma: no cover - validated by prepare.
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent command lost its upload reference"
            )
        expected_path = workspace_attachment_file_path(
            turn_id=command.typed_turn_id,
            reference=reference,
        )
        expected = f'<workspace_file path="{expected_path}" />'
        if content.get("text") != expected:
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent user message text is not the fixed Workspace placeholder"
            )
        model_message = content.get("model_message")
        if not isinstance(model_message, Mapping):
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent user message has an invalid canonical model message"
            )
        try:
            canonical = CanonicalMessage.from_json(model_message)
        except (TypeError, ValueError) as error:
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent user message has an invalid canonical model message"
            ) from error
        if canonical != text_message(MessageRole.USER, expected):
            raise WorkspaceAttachmentIntentConflictError(
                "Attachment import intent user message model placeholder drifted"
            )
        return source_message_id, content

    async def _load_intent(
        self,
        command: DurableAssistantInferenceCommand,
        *,
        connection: AsyncConnection,
    ) -> AttachmentImportIntent | None:
        """@brief 在 prepare 事务内锁定同 Turn 的既有 aggregate / Lock an existing aggregate for the same Turn within a prepare transaction.

        @param command 当前 durable command / Current durable command.
        @param connection 当前 PostgreSQL transaction connection / Current PostgreSQL transaction connection.
        @return 已存储 aggregate；不存在时为 None / Stored aggregate, or None when absent.
        """

        row = await db.fetch_one(
            "SELECT conversation_id, source_message_id, scope_kind, scope_id, "
            "request_id, request_hash, runtime_path, byte_size, sha256 "
            "FROM workspace.attachment_import_intents "
            "WHERE turn_id = CAST(%s AS UUID) FOR UPDATE",
            (str(command.typed_turn_id),),
            connection=connection,
        )
        if row is None:
            return None
        return _intent_from_values(command=command, values=tuple(row))


def _intent_from_values(
    *,
    command: DurableAssistantInferenceCommand,
    values: tuple[object, ...],
) -> AttachmentImportIntent:
    """@brief 将固定 SQL row 投影为领域 aggregate / Project one fixed SQL row into a domain aggregate.

    @param command 行所属的已验证 durable command / Validated durable command owning the row.
    @param values 固定 SELECT 顺序的数据库字段 / Database fields in the fixed SELECT order.
    @return 强类型 ``AttachmentImportIntent`` aggregate / Strongly typed ``AttachmentImportIntent`` aggregate.
    @raise WorkspaceAttachmentIntentConflictError row 形状或基础字段不可信时抛出 /
        Raised when row shape or primitive fields are untrusted.
    """

    if len(values) != 9:  # pragma: no cover - fixed SELECT shape.
        raise WorkspaceAttachmentIntentConflictError(
            "Attachment import intent row has an unexpected shape"
        )
    try:
        scope = _scope_from_database(values[2], values[3])
        path = _database_text(values[6], label="runtime_path")
        opaque_id = _opaque_id_from_path(path)
        return AttachmentImportIntent(
            turn_id=command.typed_turn_id,
            conversation_id=ConversationId(
                _database_text(values[0], label="conversation_id")
            ),
            source_message_id=ConversationMessageId.parse(
                _database_text(values[1], label="source_message_id")
            ),
            scope=scope,
            opaque_id=opaque_id,
            request_id=WorkspaceRequestId(
                _database_text(values[4], label="request_id")
            ),
            request_hash=WorkspaceRequestHash(
                _database_text(values[5], label="request_hash")
            ),
            byte_size=_database_int(values[7], label="byte_size"),
            sha256=_database_text(values[8], label="sha256"),
        )
    except (TypeError, ValueError) as error:
        raise WorkspaceAttachmentIntentConflictError(
            "Attachment import intent row violates immutable aggregate semantics"
        ) from error


def _scope_from_database(scope_kind: object, scope_id: object) -> RuntimeScope:
    """@brief 从数据库 scope 字段重建强类型 Workspace scope / Rebuild a typed Workspace scope from database scope fields.

    @param scope_kind 数据库存储的 scope kind / Database-stored scope kind.
    @param scope_id 数据库存储的数值 scope ID / Database-stored numeric scope ID.
    @return 个人或整群强类型 scope / Personal or whole-group typed scope.
    @raise ValueError scope kind 或 ID 语义非法时抛出 / Raised when scope kind or ID semantics are invalid.
    """

    kind = _database_text(scope_kind, label="scope_kind")
    identifier = _database_int(scope_id, label="scope_id")
    if kind == "personal":
        return PersonalRuntimeScope(identifier)
    if kind == "group":
        return GroupRuntimeScope(identifier)
    raise ValueError("Attachment import intent scope_kind is invalid")


def _opaque_id_from_path(path: str) -> str:
    """@brief 从严格固定 runtime path 提取 attachment opaque ID / Extract the attachment opaque ID from one strict fixed runtime path.

    @param path 数据库保存的 runtime-internal payload path / Runtime-internal payload path stored by the database.
    @return 固定 attachment opaque ID / Fixed attachment opaque ID.
    @raise ValueError 路径不是当前附件固定树时抛出 / Raised when the path is outside the current-attachment fixed tree.
    """

    prefix = "/workspace/uploads/"
    suffix = "/payload"
    if not path.startswith(prefix) or not path.endswith(suffix):
        raise ValueError("Attachment import intent runtime_path is invalid")
    opaque_id = path[len(prefix) : -len(suffix)]
    if "/" in opaque_id:
        raise ValueError("Attachment import intent runtime_path is invalid")
    return opaque_id


def _database_text(value: object, *, label: str) -> str:
    """@brief 窄化一个数据库文本字段 / Narrow one database text field.

    @param value 数据库返回的候选值 / Candidate value returned by the database.
    @param label 面向开发者的字段名 / Developer-facing field name.
    @return 非空字符串 / Non-empty string.
    @raise TypeError 值不是非空字符串时抛出 / Raised when the value is not a non-empty string.
    """

    if not isinstance(value, str) or not value:
        raise TypeError(f"Attachment import intent {label} is not non-empty text")
    return value


def _database_int(value: object, *, label: str) -> int:
    """@brief 窄化一个数据库整数字段 / Narrow one database integer field.

    @param value 数据库返回的候选值 / Candidate value returned by the database.
    @param label 面向开发者的字段名 / Developer-facing field name.
    @return 严格整数 / Strict integer.
    @raise TypeError 值不是严格整数时抛出 / Raised when the value is not a strict integer.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Attachment import intent {label} is not an integer")
    return value


def _validated_attachment_state(content: JsonObject) -> WorkspaceAttachmentImportState:
    """@brief 从锁定 envelope 读取 fail-closed marker 状态 / Read the fail-closed marker state from a locked envelope.

    @param content 已锁定的 user-message JSON envelope / Locked user-message JSON envelope.
    @return 严格有效附件状态 / Strictly valid attachment state.
    @raise WorkspaceAttachmentIntentConflictError marker 缺失或畸形时抛出 / Raised when the marker is absent or malformed.
    """

    state = workspace_attachment_import_state(content)
    if state is None:
        raise WorkspaceAttachmentIntentConflictError(
            "Attachment import intent user message has an invalid workspace_attachment marker"
        )
    return state


def _runtime_scope_for(command: DurableAssistantInferenceCommand) -> RuntimeScope:
    """@brief 从 durable command 重建个人或整群 scope / Rebuild a personal or whole-group scope from a durable command.

    @param command 已验证 durable command / Validated durable command.
    @return 当前附件应使用的强类型 Workspace scope / Typed Workspace scope that the current attachment must use.
    @raise WorkspaceAttachmentIntentConflictError 群 command 缺少 group ID 时抛出 / Raised when a group command lacks its group ID.
    """

    if command.scope.is_group:
        group_id = command.scope.group_id
        if group_id is None:
            raise WorkspaceAttachmentIntentConflictError(
                "Group attachment import intent command has no group ID"
            )
        return GroupRuntimeScope(group_id)
    return PersonalRuntimeScope(command.user.user_id)


def _intent_storage_error(
    error: SQLAlchemyError,
) -> WorkspaceAttachmentIntentConflictError | WorkspaceAttachmentIntentUnavailableError:
    """@brief 将 PostgreSQL intent 错误映射为 application 错误 / Map a PostgreSQL intent error to an application error.

    @param error 已捕获 SQLAlchemy 数据库异常 / Captured SQLAlchemy database exception.
    @return 不可重试语义冲突或暂时存储不可用 / Non-retryable semantic conflict or temporary storage unavailability.
    """

    if _is_permanent_attachment_storage_error(error):
        return WorkspaceAttachmentIntentConflictError(
            "Workspace attachment import intent conflicts with durable database state"
        )
    return WorkspaceAttachmentIntentUnavailableError(
        "Workspace attachment import intent storage is temporarily unavailable"
    )


__all__ = ["PostgresWorkspaceAttachmentImportIntentStore"]
