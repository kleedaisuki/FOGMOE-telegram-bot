"""@brief Workspace Python 分层与 native adapter 的 CTest 合约 / CTest contract for Workspace Python layers and native adapter."""

from __future__ import annotations

import asyncio
import ast
import importlib
import sys
import threading
import unittest
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from unittest import mock
from uuid import UUID

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""@brief 仓库根目录 / Repository root directory."""

_SOURCE_ROOT = _PROJECT_ROOT / "src"
"""@brief Python src-layout 根目录 / Python src-layout root directory."""

_WORKSPACE_DOMAIN_ROOT = _SOURCE_ROOT / "fogmoe_bot" / "domain" / "workspace"
"""@brief Workspace 领域层源码目录 / Workspace domain-layer source directory."""

_WORKSPACE_APPLICATION_ROOT = _SOURCE_ROOT / "fogmoe_bot" / "application" / "workspace"
"""@brief Workspace 应用层源码目录 / Workspace application-layer source directory."""

if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from fogmoe_bot.application.assistant.tool_runtime import (  # noqa: E402
    ToolEffectRequest,
    ToolExecutionContext,
)
from fogmoe_bot.application.assistant.tools.catalog import (  # noqa: E402
    DEFAULT_TOOL_CATALOG,
    InvalidToolArguments,
)
from fogmoe_bot.application.workspace.errors import (  # noqa: E402
    WorkspaceInvocationOutcomeUnknownError,
    WorkspaceRuntimeProtocolError,
    WorkspaceRuntimeUnavailableError,
)
from fogmoe_bot.application.workspace.models import (  # noqa: E402
    AddFileCommand,
    RunBashCommand,
    RunBashResult,
    WorkspaceRelativePath,
)
from fogmoe_bot.domain.conversation.identity import (  # noqa: E402
    ConversationId,
    DeliveryStreamId,
    TurnId,
)
from fogmoe_bot.domain.conversation.payloads import JsonObject  # noqa: E402
from fogmoe_bot.domain.workspace.runtime import (  # noqa: E402
    WorkspaceRequestHash,
    WorkspaceRequestId,
    WorkspaceRuntime,
    WorkspaceRuntimeKey,
)
from fogmoe_bot.domain.workspace.scope import (  # noqa: E402
    GroupRuntimeScope,
    PersonalRuntimeScope,
    runtime_scope_parts,
)
from fogmoe_bot.infrastructure.assistant.tool_operations.workspace import (  # noqa: E402
    execute_run_bash,
)
from fogmoe_bot.infrastructure.database import db as database_module  # noqa: E402
from fogmoe_bot.infrastructure.workspace.lifecycle import (  # noqa: E402
    RuntimeProcessLifecycle,
)
from fogmoe_bot.infrastructure.workspace.registry import (  # noqa: E402
    PostgresWorkspaceRuntimeRegistry,
)
from fogmoe_bot.infrastructure.workspace.wspctl import (  # noqa: E402
    _FairRuntimeAdmissionScheduler,
    _NativeClientLifecycleOffloadGate,
    WspctlRuntimeProcessFactory,
    WspctlRuntimeProcess,
)


class _StaticRegistry:
    """@brief 固定 runtime key 的内存 registry / In-memory registry with one fixed runtime key."""

    def __init__(self, key: WorkspaceRuntimeKey) -> None:
        """@brief 保存固定 key / Store the fixed key.

        @param key 测试 runtime key / Test runtime key.
        @return None / None.
        """

        self.key = key
        self.scopes: list[PersonalRuntimeScope | GroupRuntimeScope] = []

    async def resolve(
        self,
        scope: PersonalRuntimeScope | GroupRuntimeScope,
    ) -> WorkspaceRuntime:
        """@brief 记录 scope 并返回固定绑定 / Record the scope and return the fixed binding.

        @param scope 待解析 scope / Scope to resolve.
        @return 对应固定 key 的 runtime / Runtime with the fixed key.
        """

        self.scopes.append(scope)
        return WorkspaceRuntime(scope=scope, key=self.key)


class _KeyedRegistry:
    """@brief 按强类型 scope 返回不同 runtime key 的内存 registry / In-memory registry returning distinct runtime keys by typed scope."""

    def __init__(
        self,
        keys: Mapping[PersonalRuntimeScope | GroupRuntimeScope, WorkspaceRuntimeKey],
    ) -> None:
        """@brief 保存测试 scope/key 映射 / Store the test scope/key mapping.

        @param keys 强类型 scope 到 runtime key 的完整映射 / Complete mapping from typed scope to runtime key.
        @return None / None.
        """

        self._keys = dict(keys)
        """@brief 测试期间不变的 scope/key 映射 / Immutable-for-test scope/key mapping."""

    async def resolve(
        self,
        scope: PersonalRuntimeScope | GroupRuntimeScope,
    ) -> WorkspaceRuntime:
        """@brief 解析一个明确登记的 scope / Resolve one explicitly registered scope.

        @param scope 请求的 runtime scope / Requested runtime scope.
        @return 具有对应 key 的 runtime / Runtime with the corresponding key.
        @raise AssertionError 测试遗漏 scope 时抛出 / Raised when a test omitted a scope.
        """

        try:
            key = self._keys[scope]
        except KeyError as error:
            raise AssertionError("test scope was not registered") from error
        return WorkspaceRuntime(scope=scope, key=key)


class _CrossScopeRegistry:
    """@brief 故意违反 aggregate ownership 的 registry 替身 / Registry double deliberately violating aggregate ownership."""

    def __init__(self, runtime: WorkspaceRuntime) -> None:
        """@brief 保存将错误返回的 aggregate / Store the aggregate that will be returned incorrectly.

        @param runtime 错误 scope 的 runtime aggregate / Runtime aggregate with the wrong scope.
        @return None / None.
        """

        self._runtime = runtime
        """@brief 测试所需的错误 aggregate / Wrong aggregate used by the test."""

    async def resolve(
        self,
        scope: PersonalRuntimeScope | GroupRuntimeScope,
    ) -> WorkspaceRuntime:
        """@brief 忽略请求 scope，模拟损坏的 registry / Ignore the requested scope to simulate a corrupt registry.

        @param scope 被忽略的请求 scope / Requested scope, deliberately ignored.
        @return 错误 aggregate / Wrong aggregate.
        """

        del scope
        return self._runtime


class _FakeNativeProcess:
    """@brief 可记录、可阻塞的 native RuntimeProcess 替身 / Recording, optionally blocking native RuntimeProcess double."""

    def __init__(
        self,
        *,
        block: bool = False,
        result_overrides: Mapping[str, object] | None = None,
        execute_error: Exception | None = None,
        file_result_overrides: Mapping[str, object] | None = None,
        file_error: Exception | None = None,
    ) -> None:
        """@brief 初始化 native process 替身 / Initialize the native-process double.

        @param block 是否等待 ``release`` event / Whether to wait for the ``release`` event.
        @param result_overrides 覆盖默认结果的字段 / Fields overriding the default result.
        @param execute_error 调用时要抛出的原生异常 / Native exception raised on execution.
        @param file_result_overrides 覆盖默认文件收据的字段 / Fields overriding the default file receipt.
        @param file_error 文件写入时要抛出的原生异常 / Native exception raised during file ingress.
        @return None / None.
        """

        self.block = block
        self.result_overrides = dict(result_overrides or {})
        self.execute_error = execute_error
        self.file_result_overrides = dict(file_result_overrides or {})
        self.file_error = file_error
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.file_calls: list[dict[str, object]] = []
        self.close_count = 0

    def execute(
        self,
        argv: list[str],
        stdin: str = "",
        cwd: str = "",
        timeout_ms: int = 0,
        output_limit: int = 0,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 记录 native 调用并返回可预测结果 / Record a native call and return a predictable result.

        @param argv native argv / Native argv.
        @param stdin 标准输入 / Standard input.
        @param cwd runtime 目录 / Runtime directory.
        @param timeout_ms deadline / Deadline.
        @param output_limit 输出预算 / Output budget.
        @param request_id 请求标识 / Request identifier.
        @param request_hash 请求摘要 / Request digest.
        @return native 协议结果 / Native protocol result.
        """

        self.calls.append(
            (
                argv,
                {
                    "stdin": stdin,
                    "cwd": cwd,
                    "timeout_ms": timeout_ms,
                    "output_limit": output_limit,
                    "request_id": request_id,
                    "request_hash": request_hash,
                },
            )
        )
        self.entered.set()
        if self.execute_error is not None:
            raise self.execute_error
        if self.block:
            if not self.release.wait(timeout=3.0):
                raise TimeoutError("test native process was not released")
        result: dict[str, object] = {
            "stdout": "ok",
            "stderr": "",
            "exit_code": 0,
            "timed_out": False,
            "truncated": False,
            "replayed": False,
            "request_id": request_id,
        }
        result.update(self.result_overrides)
        return result

    def add_file(
        self,
        opaque_id: str,
        chunks: Iterable[bytes],
        byte_size: int,
        sha256: str,
        request_id: str = "",
        request_hash: str = "",
    ) -> Mapping[str, object]:
        """@brief 记录文件写入并返回可预测收据 / Record file ingress and return a predictable receipt.

        @param opaque_id 固定 uploads 子目录 ID / Fixed uploads-subdirectory ID.
        @param chunks 原样消费的 binary chunks / Binary chunks consumed without interpretation.
        @param byte_size 声明完整字节数 / Declared total byte count.
        @param sha256 声明完整 SHA-256 / Declared complete SHA-256.
        @param request_id 请求标识 / Request identifier.
        @param request_hash 请求摘要 / Request digest.
        @return native 文件收据 / Native file receipt.
        """

        consumed_chunks = list(chunks)
        self.file_calls.append(
            {
                "opaque_id": opaque_id,
                "chunks": consumed_chunks,
                "byte_size": byte_size,
                "sha256": sha256,
                "request_id": request_id,
                "request_hash": request_hash,
            }
        )
        self.entered.set()
        if self.file_error is not None:
            raise self.file_error
        if self.block:
            if not self.release.wait(timeout=3.0):
                raise TimeoutError("test native file ingress was not released")
        result: dict[str, object] = {
            "request_id": request_id,
            "replayed": False,
            "path": f"/workspace/uploads/{opaque_id}/payload",
            "byte_size": byte_size,
            "sha256": sha256,
        }
        result.update(self.file_result_overrides)
        return result

    def close(self) -> None:
        """@brief 记录 client close / Record a client close.

        @return None / None.
        """

        self.close_count += 1


class _KeyedFactory:
    """@brief 按 runtime key 返回独立 fake process 的 factory / Factory returning a distinct fake process per runtime key."""

    def __init__(
        self,
        processes: Mapping[WorkspaceRuntimeKey, _FakeNativeProcess],
    ) -> None:
        """@brief 保存 key/process 映射 / Store the key/process mapping.

        @param processes runtime key 到 fake process 的映射 / Mapping from runtime key to fake process.
        @return None / None.
        """

        self._processes = dict(processes)
        """@brief 测试期间不变的 key/process 映射 / Immutable-for-test key/process mapping."""

    def create(
        self,
        key: WorkspaceRuntimeKey,
        activation_id: str,
    ) -> _FakeNativeProcess:
        """@brief 返回该 key 的唯一 fake process / Return the unique fake process for a key.

        @param key runtime key / Runtime key.
        @param activation_id 新 client 唯一绑定的 activation / Activation uniquely bound to the new client.
        @return 对应 fake process / Corresponding fake process.
        @raise AssertionError 测试遗漏 key 时抛出 / Raised when a test omitted a key.
        """

        del activation_id
        try:
            return self._processes[key]
        except KeyError as error:
            raise AssertionError("test runtime key was not registered") from error


class _FakeFactory:
    """@brief 返回单一 fake process 的 RuntimeProcess factory / RuntimeProcess factory returning one fake process."""

    def __init__(self, process: _FakeNativeProcess) -> None:
        """@brief 保存 fake process / Store the fake process.

        @param process fake native process / Fake native process.
        @return None / None.
        """

        self.process = process
        self.created_keys: list[WorkspaceRuntimeKey] = []
        """@brief 所有 native client 创建请求的 runtime key / Runtime keys of all native-client creation requests."""
        self.created_activation_ids: list[str] = []
        """@brief 所有由 runner 生成且绑定到新 client 的 activation / Activations generated by the runner and bound to new clients."""

    def create(
        self,
        key: WorkspaceRuntimeKey,
        activation_id: str,
    ) -> _FakeNativeProcess:
        """@brief 记录创建请求并返回 fake process / Record a create request and return the fake process.

        @param key runtime key / Runtime key.
        @param activation_id 新 client 唯一绑定的 activation / Activation uniquely bound to the new client.
        @return fake process / Fake process.
        """

        self.created_keys.append(key)
        self.created_activation_ids.append(activation_id)
        return self.process


class _BlockingFactory:
    """@brief 可阻塞创建阶段的 RuntimeProcess factory / RuntimeProcess factory with a blockable creation phase."""

    def __init__(self, process: _FakeNativeProcess) -> None:
        """@brief 保存将被延迟返回的 process / Store the process returned after a delay.

        @param process fake native process / Fake native process.
        @return None / None.
        """

        self.process = process
        self.entered = threading.Event()
        self.release = threading.Event()

    def create(
        self,
        key: WorkspaceRuntimeKey,
        activation_id: str,
    ) -> _FakeNativeProcess:
        """@brief 阻塞直到测试放行，再返回 process / Block until the test releases, then return the process.

        @param key runtime key / Runtime key.
        @param activation_id 新 client 唯一绑定的 activation / Activation uniquely bound to the new client.
        @return fake process / Fake process.
        """

        del key, activation_id
        self.entered.set()
        if not self.release.wait(timeout=3.0):
            raise TimeoutError("test RuntimeProcess factory was not released")
        return self.process


class _SelectiveBlockingFactory:
    """@brief 仅阻塞一个 runtime key 的并发创建 factory / Concurrent creation factory that blocks only one runtime key.

    @param processes key 到 fake native client 的完整映射 / Complete mapping from keys to fake native clients.
    @param blocked_key 首次创建时等待测试放行的 key / Key whose first creation waits for the test to release it.
    """

    def __init__(
        self,
        *,
        processes: Mapping[WorkspaceRuntimeKey, _FakeNativeProcess],
        blocked_key: WorkspaceRuntimeKey,
    ) -> None:
        """@brief 保存 factory 的并发控制状态 / Store the factory's concurrency-control state.

        @param processes key 到 process 的映射 / Mapping from keys to processes.
        @param blocked_key 被阻塞的 key / Blocked key.
        @return None / None.
        """

        self._processes = dict(processes)
        """@brief 测试 process 映射 / Test-process mapping."""
        self._blocked_key = blocked_key
        """@brief 需要等待放行的 key / Key that waits for release."""
        self.blocked_entered = threading.Event()
        """@brief 阻塞 factory 已进入的信号 / Signal that the blocked factory call entered."""
        self.release = threading.Event()
        """@brief 测试放行阻塞创建的信号 / Signal releasing the blocked creation."""
        self.created_keys: list[WorkspaceRuntimeKey] = []
        """@brief 所有实际 factory 调用的 key / Keys of all actual factory calls."""

    def create(
        self,
        key: WorkspaceRuntimeKey,
        activation_id: str,
    ) -> _FakeNativeProcess:
        """@brief 为 key 返回 process；只阻塞指定 key / Return a process for a key, blocking only the selected one.

        @param key 待创建的 runtime key / Runtime key to create.
        @param activation_id 新 client 唯一绑定的 activation / Activation uniquely bound to the new client.
        @return 对应 fake process / Corresponding fake process.
        @raise AssertionError key 没有配置 process 时抛出 / Raised when no process is configured for the key.
        """

        del activation_id
        self.created_keys.append(key)
        if key == self._blocked_key:
            self.blocked_entered.set()
            if not self.release.wait(timeout=3.0):
                raise TimeoutError("test blocked RuntimeProcess factory was not released")
        try:
            return self._processes[key]
        except KeyError as error:
            raise AssertionError("test runtime key was not registered") from error


class _RecordingRuntimeProcess:
    """@brief 记录工具映射输出的 RuntimeProcess / RuntimeProcess recording tool-mapping output."""

    def __init__(
        self,
        *,
        exit_code: int | None,
        timed_out: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        """@brief 配置返回结果 / Configure the returned result.

        @param exit_code 命令退出码 / Command exit code.
        @param timed_out 是否超时 / Whether the command timed out.
        @param outcome_unknown 是否报告不可判定的 journal outcome /
            Whether to report an indeterminate journal outcome.
        @return None / None.
        """

        self.exit_code = exit_code
        self.timed_out = timed_out
        self.outcome_unknown = outcome_unknown
        self.commands: list[RunBashCommand] = []

    async def run_bash(self, command: RunBashCommand) -> RunBashResult:
        """@brief 记录命令并回传完成结果 / Record a command and return a completed result.

        @param command 应用层命令 / Application-layer command.
        @return 规范结果 / Canonical result.
        """

        self.commands.append(command)
        if self.outcome_unknown:
            raise WorkspaceInvocationOutcomeUnknownError(command.request_id)
        return RunBashResult(
            stdout="stdout",
            stderr="stderr",
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            truncated=False,
            replayed=False,
            request_id=command.request_id,
        )


class _NativeRuntimeProcessError(RuntimeError):
    """@brief pybind 结构化 native 错误替身 / Double for a structured pybind native error."""

    def __init__(self, code: str) -> None:
        """@brief 保存机器错误码 / Store the machine error code.

        @param code native binding 的稳定错误码 / Stable error code from the native binding.
        @return None / None.
        """

        self.code = code
        super().__init__(code)


class _FakeTransaction:
    """@brief registry 单测使用的异步事务上下文 / Asynchronous transaction context used by registry unit tests."""

    async def __aenter__(self) -> object:
        """@brief 返回哨兵 connection / Return a sentinel connection.

        @return connection sentinel / Connection sentinel.
        """

        return object()

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> bool:
        """@brief 不抑制测试异常 / Do not suppress test exceptions.

        @param exception_type 异常类型 / Exception type.
        @param exception 异常值 / Exception value.
        @param traceback traceback / Traceback.
        @return 始终为 ``False`` / Always ``False``.
        """

        del exception_type, exception, traceback
        return False


def _command(
    *,
    scope: PersonalRuntimeScope | GroupRuntimeScope | None = None,
    request_id: str = "step:0:call:0",
) -> RunBashCommand:
    """@brief 构造一个最小有效 Bash 命令 / Construct one minimally valid Bash command.

    @param scope 可选 runtime scope / Optional runtime scope.
    @param request_id 可选稳定请求标识 / Optional stable request identifier.
    @return 已验证 Bash 命令 / Validated Bash command.
    """

    return RunBashCommand(
        scope=scope or PersonalRuntimeScope(101),
        command="printf ok",
        request_id=WorkspaceRequestId(request_id),
        request_hash=WorkspaceRequestHash("a" * 64),
        stdin="input",
        working_directory=WorkspaceRelativePath("project"),
        timeout_seconds=30,
        output_limit_bytes=65_536,
    )


def _file_command(
    *,
    scope: PersonalRuntimeScope | GroupRuntimeScope | None = None,
    opaque_id: str = "opaque42",
    request_id: str = "payload:0:call:0",
    chunks: Iterable[bytes] = (b"hello ", b"world"),
) -> AddFileCommand:
    """@brief 构造一个最小有效文件写入命令 / Construct one minimally valid file-ingress command.

    @param scope 可选 runtime scope / Optional runtime scope.
    @param opaque_id 固定 uploads 子目录 ID / Fixed uploads-subdirectory ID.
    @param request_id 可选稳定请求标识 / Optional stable request identifier.
    @param chunks 一次性 binary chunks / Single-consumption binary chunks.
    @return 已验证文件写入命令 / Validated file-ingress command.
    """

    return AddFileCommand(
        scope=scope or PersonalRuntimeScope(101),
        opaque_id=opaque_id,
        chunks=chunks,
        byte_size=11,
        sha256="b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        request_id=WorkspaceRequestId(request_id),
        request_hash=WorkspaceRequestHash("b" * 64),
    )


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float = 1.0,
) -> None:
    """@brief 等待异步调度状态满足谓词 / Wait until asynchronous scheduling state satisfies a predicate.

    @param predicate 应返回当前是否已满足条件的无副作用谓词 / Side-effect-free predicate reporting whether the condition now holds.
    @param timeout_seconds 最大等待时间 / Maximum wait time.
    @return None / None.
    @raise AssertionError 超时仍未满足条件时抛出 / Raised when the condition is still false at timeout.
    """

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for asynchronous test state")
        await asyncio.sleep(0.01)


def _absolute_imports(path: Path) -> set[str]:
    """@brief 读取一个 Python 源文件的绝对 import 根 / Read absolute import roots from one Python source file.

    @param path 待解析的 Python 文件 / Python file to parse.
    @return 出现在 ``import`` 或绝对 ``from`` 中的模块名集合 /
        Module names appearing in ``import`` or absolute ``from`` statements.
    @note 相对 import 只能留在同一 layer 的 package 内，不构成跨 layer 向外依赖，因此
        此测试刻意不把它们计入。/ Relative imports stay within the same layer's package and
        do not constitute outward cross-layer dependencies, so this test intentionally omits them.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _tool_request(
    *,
    group_id: int | None,
    message_thread_id: int | None,
    execution_deadline_monotonic: float | None = None,
    timeout_seconds: int | None = None,
) -> ToolEffectRequest:
    """@brief 构造映射测试所需的 ``run_bash`` effect / Construct a ``run_bash`` effect for mapping tests.

    @param group_id 群聊 ID，私聊时为 None / Group ID, or None for a private chat.
    @param message_thread_id 可选 topic ID / Optional topic ID.
    @param execution_deadline_monotonic 可选 attempt 单调截止点 / Optional attempt monotonic deadline.
    @param timeout_seconds 可选命令 timeout / Optional command timeout.
    @return 已认证工具 effect / Authenticated tool effect.
    """

    turn_id = TurnId.new()
    context = ToolExecutionContext(
        turn_id=turn_id,
        conversation_id=ConversationId("conversation:test"),
        delivery_stream_id=DeliveryStreamId("telegram:test"),
        user_id=101,
        chat_id=group_id if group_id is not None else 101,
        is_group=group_id is not None,
        group_id=group_id,
        message_id=10,
        message_thread_id=message_thread_id,
        execution_deadline_monotonic=execution_deadline_monotonic,
    )
    arguments: JsonObject = {
        "command": "printf mapped",
        "working_directory": "project",
    }
    if timeout_seconds is not None:
        arguments["timeout_seconds"] = timeout_seconds
    return ToolEffectRequest(
        context=context,
        invocation_id="step:0:call:0",
        provider_call_id="provider-call",
        tool_name="run_bash",
        effect_kind="workspace.exec",
        mutating=True,
        arguments=arguments,
        request_hash="b" * 64,
    )


class WorkspaceLayerTests(unittest.IsolatedAsyncioTestCase):
    """@brief 强类型 scope、registry 和 native adapter 的异步合约 / Async contracts for typed scopes, registry, and native adapter."""

    def test_scope_rejects_conversation_and_topic_substitution(self) -> None:
        """@brief Runtime scope 只接受用户或整群 ID / Runtime scope accepts only user or whole-group IDs.

        @return None / None.
        """

        self.assertEqual(runtime_scope_parts(PersonalRuntimeScope(8))[1], 8)
        self.assertEqual(runtime_scope_parts(GroupRuntimeScope(-100_200))[1], -100_200)
        with self.assertRaises(TypeError):
            runtime_scope_parts(ConversationId("chat:-100_200:topic:9"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            PersonalRuntimeScope(True)
        with self.assertRaises(ValueError):
            GroupRuntimeScope(0)

    def test_catalog_rejects_multibyte_stdin_over_native_byte_budget(self) -> None:
        """@brief catalog 以 UTF-8 字节而非 Unicode 字符数保护 native stdin / Catalog protects native stdin by UTF-8 bytes rather than Unicode character count.

        @return None / None.
        """

        result = DEFAULT_TOOL_CATALOG.validate(
            "run_bash",
            {
                "command": "true",
                # 21,846 characters are below Pydantic's 65,536-character cap but encode to
                # 65,538 bytes, exceeding the native protocol's 64 KiB byte budget.
                "stdin": "€" * 21_846,
            },
        )
        self.assertIsInstance(result, InvalidToolArguments)

    def test_catalog_rejects_command_and_stdin_that_native_cannot_transport(self) -> None:
        """@brief catalog 在 receipt 前拒绝 native argv/NUL 不可表示的 payload / Catalog rejects payloads unrepresentable by native argv/NUL before a receipt.

        @return None / None.
        """

        oversized_command = DEFAULT_TOOL_CATALOG.validate(
            "run_bash",
            {
                # 5,462 Unicode characters fit the character bound but encode to 16,386 bytes,
                # exceeding wspctl's 16 KiB per-argv-element transport limit.
                "command": "€" * 5_462,
            },
        )
        nul_command = DEFAULT_TOOL_CATALOG.validate(
            "run_bash", {"command": "printf ok\x00"}
        )
        nul_stdin = DEFAULT_TOOL_CATALOG.validate(
            "run_bash", {"command": "printf ok", "stdin": "input\x00"}
        )

        self.assertIsInstance(oversized_command, InvalidToolArguments)
        self.assertIsInstance(nul_command, InvalidToolArguments)
        self.assertIsInstance(nul_stdin, InvalidToolArguments)

    async def test_registry_uses_workspace_schema_and_uuid_key(self) -> None:
        """@brief Registry 使用 immutable workspace.runtimes 映射 / Registry uses the immutable workspace.runtimes mapping.

        @return None / None.
        """

        key = WorkspaceRuntimeKey.parse("b845ae6a-92cc-4ca7-b257-1067b7b27b32")
        calls: list[tuple[str, tuple[object, ...]]] = []

        async def fake_execute(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> int:
            """@brief 记录 INSERT / Record INSERT.

            @param sql SQL 文本 / SQL text.
            @param params 参数 / Parameters.
            @param connection transaction connection / Transaction connection.
            @return 一行影响数 / One affected row.
            """

            del connection
            calls.append((sql, params))
            return 1

        async def fake_fetch_one(
            sql: str,
            params: tuple[object, ...],
            *,
            connection: object,
        ) -> tuple[UUID] | None:
            """@brief 返回固定 UUID row / Return a fixed UUID row.

            @param sql SQL 文本 / SQL text.
            @param params 参数 / Parameters.
            @param connection transaction connection / Transaction connection.
            @return runtime-key row / Runtime-key row.
            """

            del connection
            calls.append((sql, params))
            return (key.value,)

        with (
            mock.patch.object(database_module, "transaction", lambda: _FakeTransaction()),
            mock.patch.object(database_module, "execute", fake_execute),
            mock.patch.object(database_module, "fetch_one", fake_fetch_one),
        ):
            runtime = await PostgresWorkspaceRuntimeRegistry().resolve(
                GroupRuntimeScope(-100_777)
            )

        self.assertEqual(runtime.key, key)
        self.assertIsInstance(runtime.scope, GroupRuntimeScope)
        self.assertIn("INSERT INTO workspace.runtimes", calls[0][0])
        self.assertIn("ON CONFLICT DO NOTHING", calls[0][0])
        self.assertIn("SELECT runtime_key FROM workspace.runtimes", calls[1][0])
        self.assertEqual(calls[0][1][0:2], ("group", -100_777))

    async def test_runner_rejects_a_registry_binding_for_another_scope(self) -> None:
        """@brief runner 不会把其他 scope 的 opaque key 交给 native / Runner never passes another scope's opaque key to native.

        @return None / None.
        """

        requested_scope = PersonalRuntimeScope(101)
        other_scope = PersonalRuntimeScope(202)
        process = _FakeNativeProcess()
        factory = _FakeFactory(process)
        runner = WspctlRuntimeProcess(
            registry=_CrossScopeRegistry(
                WorkspaceRuntime(scope=other_scope, key=WorkspaceRuntimeKey.new())
            ),
            process_factory=factory,
        )

        with self.assertRaises(WorkspaceRuntimeUnavailableError):
            await runner.run_bash(
                _command(scope=requested_scope, request_id="scope:mismatch")
            )
        self.assertEqual(factory.created_keys, [])
        await runner.close()

    async def test_runner_is_lazy_cached_and_binds_native_idempotency_fields(self) -> None:
        """@brief Runner 懒创建一次 client，并传递稳定去重字段 / Runner lazily creates one client and passes stable deduplication fields.

        @return None / None.
        """

        key = WorkspaceRuntimeKey.new()
        process = _FakeNativeProcess()
        factory = _FakeFactory(process)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(key),
            process_factory=factory,
            idle_ttl_seconds=0.05,
        )
        result_one = await runner.run_bash(_command())
        result_two = await runner.run_bash(_command(request_id="step:0:call:1"))

        self.assertTrue(result_one.succeeded)
        self.assertTrue(result_two.succeeded)
        self.assertEqual(factory.created_keys, [key])
        self.assertEqual(len(factory.created_activation_ids), 1)
        self.assertTrue(factory.created_activation_ids[0].startswith("activation:"))
        self.assertEqual(process.calls[0][0][0:4], ["/bin/bash", "--noprofile", "--norc", "-c"])
        self.assertEqual(process.calls[0][1]["cwd"], "/workspace/project")
        self.assertNotIn("activation_id", process.calls[0][1])
        self.assertNotIn("activation_id", process.calls[1][1])
        self.assertEqual(process.calls[0][1]["request_id"], "step:0:call:0")
        self.assertEqual(process.calls[0][1]["request_hash"], "a" * 64)

        await asyncio.sleep(0.12)
        self.assertEqual(process.close_count, 1)

    async def test_add_file_reuses_cache_and_forwards_only_typed_ingress_fields(self) -> None:
        """@brief add_file 复用已缓存 handle，并仅向 native 转发受限 ingress 字段 / add_file reuses the cached handle and forwards only constrained ingress fields to native.

        @return None / None.
        """

        key = WorkspaceRuntimeKey.new()
        process = _FakeNativeProcess()
        factory = _FakeFactory(process)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(key),
            process_factory=factory,
        )
        result = await runner.add_file(_file_command())

        self.assertEqual(factory.created_keys, [key])
        self.assertEqual(len(factory.created_activation_ids), 1)
        self.assertEqual(result.path, "/workspace/uploads/opaque42/payload")
        self.assertEqual(len(process.file_calls), 1)
        self.assertEqual(process.calls, [])
        call = process.file_calls[0]
        self.assertEqual(call["opaque_id"], "opaque42")
        self.assertEqual(call["chunks"], [b"hello ", b"world"])
        self.assertEqual(call["byte_size"], 11)
        self.assertEqual(
            call["sha256"],
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        )
        self.assertEqual(call["request_id"], "payload:0:call:0")
        self.assertEqual(call["request_hash"], "b" * 64)
        await runner.close()

    async def test_add_file_and_run_bash_never_mutate_one_workspace_concurrently(self) -> None:
        """@brief add_file 与 run_bash 必须共享同一 per-runtime lock / add_file and run_bash must share one per-runtime lock.

        @return None / None.
        """

        process = _FakeNativeProcess(block=True)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=_FakeFactory(process),
            max_concurrent_executions=2,
        )
        file_task = asyncio.create_task(runner.add_file(_file_command()))
        bash_task: asyncio.Task[RunBashResult] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(process.entered.wait, 1.0))
            bash_task = asyncio.create_task(runner.run_bash(_command()))
            await asyncio.sleep(0.02)
            self.assertEqual(len(process.file_calls), 1)
            self.assertEqual(process.calls, [])

            process.release.set()
            file_result = await asyncio.wait_for(file_task, timeout=1.0)
            bash_result = await asyncio.wait_for(bash_task, timeout=1.0)
            self.assertEqual(file_result.path, "/workspace/uploads/opaque42/payload")
            self.assertTrue(bash_result.succeeded)
            self.assertEqual(len(process.calls), 1)
        finally:
            process.release.set()
            await asyncio.gather(
                file_task,
                *(task for task in (bash_task,) if task is not None),
                return_exceptions=True,
            )
            await runner.close()

    async def test_file_invocation_in_doubt_preserves_terminal_semantics(self) -> None:
        """@brief 文件写入 outcome 不可判定时不得伪装成可重试 unavailable / An indeterminate file-ingress outcome must not masquerade as retryable unavailability.

        @return None / None.
        """

        process = _FakeNativeProcess(
            file_error=_NativeRuntimeProcessError("invocation_in_doubt")
        )
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=_FakeFactory(process),
        )
        with self.assertRaises(WorkspaceInvocationOutcomeUnknownError):
            await runner.add_file(_file_command())
        await runner.close()

    async def test_slow_client_creation_does_not_block_another_runtime_key(self) -> None:
        """@brief 一个 key 的慢 client 创建不串行化另一个 key / A slow client creation for one key does not serialize another key.

        @return None / None.
        """

        slow_scope = PersonalRuntimeScope(101)
        fast_scope = PersonalRuntimeScope(102)
        slow_key = WorkspaceRuntimeKey.new()
        fast_key = WorkspaceRuntimeKey.new()
        slow_process = _FakeNativeProcess()
        fast_process = _FakeNativeProcess()
        factory = _SelectiveBlockingFactory(
            processes={slow_key: slow_process, fast_key: fast_process},
            blocked_key=slow_key,
        )
        runner = WspctlRuntimeProcess(
            registry=_KeyedRegistry({slow_scope: slow_key, fast_scope: fast_key}),
            process_factory=factory,
        )
        slow = asyncio.create_task(
            runner.run_bash(_command(scope=slow_scope, request_id="create:slow"))
        )
        fast: asyncio.Task[RunBashResult] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(factory.blocked_entered.wait, 1.0))
            fast = asyncio.create_task(
                runner.run_bash(_command(scope=fast_scope, request_id="create:fast"))
            )
            self.assertTrue(await asyncio.to_thread(fast_process.entered.wait, 1.0))
            self.assertEqual(factory.created_keys.count(slow_key), 1)
            self.assertEqual(factory.created_keys.count(fast_key), 1)

            factory.release.set()
            await asyncio.wait_for(asyncio.gather(slow, fast), timeout=1.0)
        finally:
            factory.release.set()
            await asyncio.gather(
                slow,
                *(task for task in (fast,) if task is not None),
                return_exceptions=True,
            )
            await runner.close()

    async def test_concurrent_same_key_creation_is_coalesced(self) -> None:
        """@brief 同 key 的并发惰性创建只调用一次 native factory / Concurrent lazy creation for one key calls the native factory once.

        @return None / None.
        """

        scope = PersonalRuntimeScope(101)
        key = WorkspaceRuntimeKey.new()
        process = _FakeNativeProcess()
        factory = _SelectiveBlockingFactory(
            processes={key: process},
            blocked_key=key,
        )
        runner = WspctlRuntimeProcess(
            registry=_KeyedRegistry({scope: key}),
            process_factory=factory,
        )
        first = asyncio.create_task(
            runner.run_bash(_command(scope=scope, request_id="create:one"))
        )
        second: asyncio.Task[RunBashResult] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(factory.blocked_entered.wait, 1.0))
            second = asyncio.create_task(
                runner.run_bash(_command(scope=scope, request_id="create:two"))
            )
            await asyncio.sleep(0.02)
            self.assertEqual(factory.created_keys, [key])

            factory.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)
            self.assertEqual(factory.created_keys, [key])
            self.assertEqual(len(process.calls), 2)
        finally:
            factory.release.set()
            await asyncio.gather(
                first,
                *(task for task in (second,) if task is not None),
                return_exceptions=True,
            )
            await runner.close()

    async def test_global_admission_is_fifo_across_distinct_runtime_heads(self) -> None:
        """@brief capacity=1 时不同 runtime 的 ready head 严格按到达 FIFO 获准 / With capacity one, ready heads from distinct runtimes are admitted in arrival FIFO order.

        @return None / None.
        """

        scope_one = PersonalRuntimeScope(101)
        scope_two = PersonalRuntimeScope(102)
        scope_three = PersonalRuntimeScope(103)
        key_one = WorkspaceRuntimeKey.new()
        key_two = WorkspaceRuntimeKey.new()
        key_three = WorkspaceRuntimeKey.new()
        process_one = _FakeNativeProcess(block=True)
        process_two = _FakeNativeProcess(block=True)
        process_three = _FakeNativeProcess(block=True)
        runner = WspctlRuntimeProcess(
            registry=_KeyedRegistry(
                {
                    scope_one: key_one,
                    scope_two: key_two,
                    scope_three: key_three,
                }
            ),
            process_factory=_KeyedFactory(
                {
                    key_one: process_one,
                    key_two: process_two,
                    key_three: process_three,
                }
            ),
            max_concurrent_executions=1,
        )
        first = asyncio.create_task(
            runner.run_bash(_command(scope=scope_one, request_id="fifo:one"))
        )
        second: asyncio.Task[RunBashResult] | None = None
        third: asyncio.Task[RunBashResult] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(process_one.entered.wait, 1.0))
            second = asyncio.create_task(
                runner.run_bash(_command(scope=scope_two, request_id="fifo:two"))
            )
            # Client construction is intentionally parallel across keys.  Wait until the second
            # request has become a scheduler-ready head before enqueueing the third, so this test
            # asserts the scheduler's documented FIFO order of *ready* heads rather than relying
            # on thread-pool completion order.
            await _wait_until(
                lambda: tuple(runner._execution_admission._ready_keys) == (key_two,)
            )
            third = asyncio.create_task(
                runner.run_bash(_command(scope=scope_three, request_id="fifo:three"))
            )
            await _wait_until(
                lambda: tuple(runner._execution_admission._ready_keys)
                == (key_two, key_three)
            )

            process_one.release.set()
            self.assertTrue(await asyncio.to_thread(process_two.entered.wait, 1.0))
            self.assertFalse(process_three.entered.is_set())

            process_two.release.set()
            self.assertTrue(await asyncio.to_thread(process_three.entered.wait, 1.0))
            process_three.release.set()
            await asyncio.wait_for(
                asyncio.gather(first, second, third),
                timeout=1.0,
            )
        finally:
            process_one.release.set()
            process_two.release.set()
            process_three.release.set()
            await asyncio.gather(
                first,
                *(
                    task
                    for task in (second, third)
                    if task is not None
                ),
                return_exceptions=True,
            )
            await runner.close()

    async def test_noisy_runtime_rejoins_after_another_ready_runtime(self) -> None:
        """@brief 同一 runtime 的连续命令不能在其他 ready runtime 前再次抢到 slot / Consecutive commands from one runtime cannot recapture a slot ahead of another ready runtime.

        @return None / None.
        """

        noisy_scope = PersonalRuntimeScope(101)
        quiet_scope = PersonalRuntimeScope(102)
        noisy_key = WorkspaceRuntimeKey.new()
        quiet_key = WorkspaceRuntimeKey.new()
        noisy_process = _FakeNativeProcess(block=True)
        quiet_process = _FakeNativeProcess(block=True)
        runner = WspctlRuntimeProcess(
            registry=_KeyedRegistry(
                {
                    noisy_scope: noisy_key,
                    quiet_scope: quiet_key,
                }
            ),
            process_factory=_KeyedFactory(
                {
                    noisy_key: noisy_process,
                    quiet_key: quiet_process,
                }
            ),
            max_concurrent_executions=1,
        )
        noisy_first = asyncio.create_task(
            runner.run_bash(_command(scope=noisy_scope, request_id="noisy:first"))
        )
        noisy_second: asyncio.Task[RunBashResult] | None = None
        quiet: asyncio.Task[RunBashResult] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(noisy_process.entered.wait, 1.0))
            noisy_second = asyncio.create_task(
                runner.run_bash(
                    _command(scope=noisy_scope, request_id="noisy:second")
                )
            )
            quiet = asyncio.create_task(
                runner.run_bash(_command(scope=quiet_scope, request_id="quiet:first"))
            )
            await _wait_until(
                lambda: tuple(runner._execution_admission._ready_keys) == (quiet_key,)
            )

            noisy_process.release.set()
            self.assertTrue(await asyncio.to_thread(quiet_process.entered.wait, 1.0))
            self.assertEqual(len(noisy_process.calls), 1)

            quiet_process.release.set()
            await _wait_until(lambda: len(noisy_process.calls) == 2)
            await asyncio.wait_for(
                asyncio.gather(noisy_first, noisy_second, quiet),
                timeout=1.0,
            )
        finally:
            noisy_process.release.set()
            quiet_process.release.set()
            await asyncio.gather(
                noisy_first,
                *(task for task in (noisy_second, quiet) if task is not None),
                return_exceptions=True,
            )
            await runner.close()

    async def test_same_runtime_commands_are_serialized_above_global_capacity(self) -> None:
        """@brief 即使全局容量更大，同一 runtime 的命令仍只能逐个执行 / Even above global capacity, commands from one runtime still execute one at a time.

        @return None / None.
        """

        scope = PersonalRuntimeScope(101)
        key = WorkspaceRuntimeKey.new()
        process = _FakeNativeProcess(block=True)
        runner = WspctlRuntimeProcess(
            registry=_KeyedRegistry({scope: key}),
            process_factory=_KeyedFactory({key: process}),
            max_concurrent_executions=2,
        )
        first = asyncio.create_task(
            runner.run_bash(_command(scope=scope, request_id="serial:first"))
        )
        second: asyncio.Task[RunBashResult] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(process.entered.wait, 1.0))
            second = asyncio.create_task(
                runner.run_bash(_command(scope=scope, request_id="serial:second"))
            )
            await _wait_until(
                lambda: runner._cache[key].active_count == 2
            )
            await asyncio.sleep(0.02)
            self.assertEqual(len(process.calls), 1)

            process.release.set()
            await _wait_until(lambda: len(process.calls) == 2)
            await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)
        finally:
            process.release.set()
            await asyncio.gather(
                first,
                *(task for task in (second,) if task is not None),
                return_exceptions=True,
            )
            await runner.close()

    async def test_admission_cancellation_and_duplicate_release_do_not_leak_a_slot(self) -> None:
        """@brief 取消等待者与重复 release 都不能遗失公平调度 slot / Cancelling a waiter and double release must not lose a fair-scheduler slot.

        @return None / None.
        """

        scheduler = _FairRuntimeAdmissionScheduler(capacity=1)
        first_key = WorkspaceRuntimeKey.new()
        cancelled_key = WorkspaceRuntimeKey.new()
        final_key = WorkspaceRuntimeKey.new()
        first_lease = await scheduler.acquire(first_key)
        cancelled_waiter = asyncio.create_task(scheduler.acquire(cancelled_key))
        await _wait_until(
            lambda: tuple(scheduler._ready_keys) == (cancelled_key,)
        )
        cancelled_waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled_waiter

        await first_lease.release()
        final_lease = await asyncio.wait_for(
            scheduler.acquire(final_key),
            timeout=1.0,
        )
        await final_lease.release()
        with self.assertRaises(RuntimeError):
            await final_lease.release()

        recovered_lease = await asyncio.wait_for(
            scheduler.acquire(cancelled_key),
            timeout=1.0,
        )
        await recovered_lease.release()

    async def test_client_lifecycle_offload_gate_keeps_slot_until_cancelled_worker_returns(
        self,
    ) -> None:
        """@brief lifecycle worker 在 awaiter 取消后仍持有 slot / A lifecycle worker retains its slot after its awaiter is cancelled.

        @return None / None.
        """

        gate = _NativeClientLifecycleOffloadGate(capacity=1)
        first_entered = threading.Event()
        first_release = threading.Event()
        second_entered = threading.Event()

        def first_operation() -> str:
            """@brief 阻塞首个 lifecycle worker / Block the first lifecycle worker.

            @return 固定完成值 / Fixed completion value.
            """

            first_entered.set()
            if not first_release.wait(timeout=3.0):
                raise TimeoutError("test lifecycle worker was not released")
            return "first"

        def second_operation() -> str:
            """@brief 记录第二个 lifecycle worker 获得执行权 / Record that the second lifecycle worker was admitted.

            @return 固定完成值 / Fixed completion value.
            """

            second_entered.set()
            return "second"

        first = asyncio.create_task(gate.call(first_operation))
        second: asyncio.Task[str] | None = None
        try:
            self.assertTrue(await asyncio.to_thread(first_entered.wait, 1.0))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            second = asyncio.create_task(gate.call(second_operation))
            await asyncio.sleep(0.02)
            self.assertFalse(second_entered.is_set())
            self.assertEqual(len(gate._pending_tasks), 1)

            first_release.set()
            self.assertEqual(await asyncio.wait_for(second, timeout=1.0), "second")
            await _wait_until(lambda: not gate._pending_tasks)
        finally:
            first_release.set()
            await asyncio.gather(
                first,
                *(task for task in (second,) if task is not None),
                return_exceptions=True,
            )

    async def test_cancellation_keeps_active_process_open_until_native_returns(self) -> None:
        """@brief 取消调用方不能提前关闭仍在执行的 native client / Cancelling a caller cannot close a still-running native client early.

        @return None / None.
        """

        process = _FakeNativeProcess(block=True)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=_FakeFactory(process),
            idle_ttl_seconds=0.01,
        )
        invocation = asyncio.create_task(runner.run_bash(_command()))
        entered = await asyncio.to_thread(process.entered.wait, 1.0)
        self.assertTrue(entered)
        invocation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await invocation

        shutdown = asyncio.create_task(runner.close())
        await asyncio.sleep(0.02)
        self.assertEqual(process.close_count, 0)
        process.release.set()
        await asyncio.wait_for(shutdown, timeout=1.0)
        self.assertEqual(process.close_count, 1)

    async def test_cancelled_acquire_closes_an_unclaimed_native_client(self) -> None:
        """@brief acquire 在 native client 创建期间被取消也不会泄漏 client / Cancelling acquire during native-client creation does not leak the client.

        @return None / None.
        """

        process = _FakeNativeProcess()
        factory = _BlockingFactory(process)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=factory,
        )
        invocation = asyncio.create_task(runner.run_bash(_command()))
        entered = await asyncio.to_thread(factory.entered.wait, 1.0)
        self.assertTrue(entered)
        invocation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await invocation

        factory.release.set()
        for _attempt in range(30):
            if process.close_count == 1:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(process.close_count, 1)
        self.assertEqual(process.calls, [])
        await runner.close()

    async def test_close_does_not_wait_for_a_hung_client_creation(self) -> None:
        """@brief close 不等待尚未成为 active command 的 client 创建 / Close does not wait for client creation that has not become an active command.

        @return None / None.
        """

        process = _FakeNativeProcess()
        factory = _BlockingFactory(process)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=factory,
        )
        invocation = asyncio.create_task(runner.run_bash(_command()))
        entered = await asyncio.to_thread(factory.entered.wait, 1.0)
        self.assertTrue(entered)
        shutdown = asyncio.create_task(runner.close())
        await asyncio.wait_for(shutdown, timeout=1.0)
        self.assertEqual(process.close_count, 0)

        factory.release.set()
        with self.assertRaises(WorkspaceRuntimeUnavailableError):
            await asyncio.wait_for(invocation, timeout=1.0)
        await _wait_until(lambda: process.close_count == 1)
        self.assertEqual(process.close_count, 1)

    async def test_lifecycle_closes_runner_when_service_stops(self) -> None:
        """@brief Service lifecycle 在 stop event 后关闭缓存 / Service lifecycle closes the cache after its stop event.

        @return None / None.
        """

        process = _FakeNativeProcess()
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=_FakeFactory(process),
        )
        await runner.run_bash(_command())
        stop_event = asyncio.Event()
        lifecycle_task = asyncio.create_task(
            RuntimeProcessLifecycle(runner).run(stop_event)
        )
        stop_event.set()
        await asyncio.wait_for(lifecycle_task, timeout=1.0)
        self.assertEqual(process.close_count, 1)

    async def test_lifecycle_cancellation_detaches_active_native_call_promptly(self) -> None:
        """@brief service cancellation 立即 detach active call，而非拖住 Bot shutdown / Service cancellation promptly detaches an active call instead of holding Bot shutdown.

        @return None / None.
        """

        process = _FakeNativeProcess(block=True)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=_FakeFactory(process),
        )
        invocation = asyncio.create_task(runner.run_bash(_command()))
        entered = await asyncio.to_thread(process.entered.wait, 1.0)
        self.assertTrue(entered)

        lifecycle_task = asyncio.create_task(
            RuntimeProcessLifecycle(runner).run(asyncio.Event())
        )
        await asyncio.sleep(0)
        lifecycle_task.cancel()
        await asyncio.sleep(0.02)
        self.assertTrue(lifecycle_task.done())
        self.assertEqual(process.close_count, 0)
        with self.assertRaises(asyncio.CancelledError):
            await lifecycle_task

        process.release.set()
        await asyncio.wait_for(invocation, timeout=1.0)
        await asyncio.sleep(0.02)
        self.assertEqual(process.close_count, 1)

    async def test_protocol_mismatch_fails_closed(self) -> None:
        """@brief native result request ID 不匹配时拒绝结果 / Reject a native result whose request ID does not match.

        @return None / None.
        """

        process = _FakeNativeProcess(result_overrides={"request_id": "other-request"})
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=_FakeFactory(process),
        )
        with self.assertRaises(WorkspaceRuntimeProtocolError):
            await runner.run_bash(_command())
        await runner.close()

    async def test_structured_pending_journal_error_becomes_outcome_unknown(self) -> None:
        """@brief 仅结构化 invocation_in_doubt 可终结为不可判定结果 / Only structured invocation_in_doubt terminates as an indeterminate outcome.

        @return None / None.
        """

        process = _FakeNativeProcess(
            execute_error=_NativeRuntimeProcessError("invocation_in_doubt")
        )
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=_FakeFactory(process),
        )
        with self.assertRaises(WorkspaceInvocationOutcomeUnknownError):
            await runner.run_bash(_command())
        await runner.close()

        tool_runner = _RecordingRuntimeProcess(
            exit_code=None,
            outcome_unknown=True,
        )
        result = await execute_run_bash(
            _tool_request(group_id=None, message_thread_id=None),
            runtime_process=tool_runner,
            output_limit_bytes=65_536,
        )
        self.assertEqual(
            result,
            {"status": "outcome_unknown", "replayed_by_runtime": False},
        )

    def test_missing_pybind_module_fails_closed(self) -> None:
        """@brief pybind module 缺失不会回退到 host subprocess / A missing pybind module never falls back to a host subprocess.

        @return None / None.
        """

        factory = WspctlRuntimeProcessFactory("/run/fogmoe-wspctl/wspctld.sock")
        with mock.patch.object(
            importlib,
            "import_module",
            side_effect=ImportError("missing native module"),
        ):
            with self.assertRaises(WorkspaceRuntimeUnavailableError):
                factory.create(WorkspaceRuntimeKey.new(), "activation-test")

    async def test_deadline_admission_never_dispatches_a_command_that_cannot_finish(self) -> None:
        """@brief attempt 余量不足时不触碰 native/journal / Insufficient attempt headroom does not touch native or the journal.

        @return None / None.
        """

        runner = _RecordingRuntimeProcess(exit_code=0)
        request = _tool_request(
            group_id=None,
            message_thread_id=None,
            execution_deadline_monotonic=(
                asyncio.get_running_loop().time() + 10.0
            ),
            timeout_seconds=30,
        )
        result = await execute_run_bash(
            request,
            runtime_process=runner,
            output_limit_bytes=65_536,
        )
        self.assertEqual(runner.commands, [])
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "not_started_deadline_exhausted")
        self.assertEqual(result["requested_timeout_seconds"], 30)
        remaining_seconds = result["remaining_seconds"]
        self.assertIsInstance(remaining_seconds, int)
        assert isinstance(remaining_seconds, int)
        self.assertLessEqual(remaining_seconds, 10)
        self.assertFalse(result["replayed_by_runtime"])

    async def test_deadline_admission_preserves_requested_timeout_and_hash(self) -> None:
        """@brief 余量充足时传递原 timeout/hash，绝不动态缩短 / Sufficient headroom preserves the original timeout/hash without dynamic shortening.

        @return None / None.
        """

        runner = _RecordingRuntimeProcess(exit_code=0)
        request = _tool_request(
            group_id=None,
            message_thread_id=None,
            execution_deadline_monotonic=(
                asyncio.get_running_loop().time() + 60.0
            ),
            timeout_seconds=30,
        )
        result = await execute_run_bash(
            request,
            runtime_process=runner,
            output_limit_bytes=65_536,
        )
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(runner.commands), 1)
        command = runner.commands[0]
        self.assertEqual(command.timeout_seconds, 30)
        self.assertEqual(str(command.request_hash), request.request_hash)

    async def test_cache_entry_binds_activation_once_without_execute_override(self) -> None:
        """@brief 同一 cache entry 只在创建时绑定一次 activation，执行调用不得覆写它 / One cache entry binds an activation only at creation, and execute calls cannot override it.

        @return None / None.
        """

        process = _FakeNativeProcess()
        factory = _FakeFactory(process)
        runner = WspctlRuntimeProcess(
            registry=_StaticRegistry(WorkspaceRuntimeKey.new()),
            process_factory=factory,
        )
        first = _command(request_id="turn:first:call:0")
        second = _command(request_id="turn:second:call:0")
        await runner.run_bash(first)
        await runner.run_bash(second)
        self.assertEqual(len(process.calls), 2)
        first_kwargs = process.calls[0][1]
        second_kwargs = process.calls[1][1]
        self.assertNotEqual(first_kwargs["request_id"], second_kwargs["request_id"])
        self.assertEqual(len(factory.created_activation_ids), 1)
        self.assertTrue(factory.created_activation_ids[0].startswith("activation:"))
        self.assertNotIn("activation_id", first_kwargs)
        self.assertNotIn("activation_id", second_kwargs)
        await runner.close()

    async def test_tool_mapping_uses_user_or_whole_group_not_topic(self) -> None:
        """@brief run_bash 映射用 user ID 或 whole-group ID，且非零 exit 是完成结果 / run_bash mapping uses user or whole-group ID, and a nonzero exit is completed.

        @return None / None.
        """

        private_runner = _RecordingRuntimeProcess(exit_code=17)
        private_request = _tool_request(group_id=None, message_thread_id=None)
        private_result = await execute_run_bash(
            private_request,
            runtime_process=private_runner,
            output_limit_bytes=65_536,
        )
        private_command = private_runner.commands[-1]
        self.assertEqual(private_command.scope, PersonalRuntimeScope(101))
        self.assertIsInstance(private_result, dict)
        assert isinstance(private_result, dict)
        self.assertEqual(private_result["status"], "exited")
        self.assertEqual(
            str(private_command.request_id),
            f"{private_request.context.turn_id}:{private_request.invocation_id}",
        )

        group_runner = _RecordingRuntimeProcess(exit_code=0)
        group_request_one = _tool_request(group_id=-100_333, message_thread_id=9)
        group_request_two = _tool_request(group_id=-100_333, message_thread_id=10)
        await execute_run_bash(
            group_request_one,
            runtime_process=group_runner,
            output_limit_bytes=65_536,
        )
        await execute_run_bash(
            group_request_two,
            runtime_process=group_runner,
            output_limit_bytes=65_536,
        )
        self.assertEqual(
            group_runner.commands[0].scope,
            GroupRuntimeScope(-100_333),
        )
        self.assertEqual(group_runner.commands[0].scope, group_runner.commands[1].scope)


if __name__ == "__main__":
    unittest.main()
