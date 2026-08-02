"""@brief 已构建 wspctl._native 的 ABI 契约测试 / ABI contract tests for the built wspctl._native module."""

from __future__ import annotations

from pathlib import Path

from wspctl import NativeError, RuntimeProcess, RuntimeStatus


#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief pybind 实现路径 / pybind implementation path.
BINDINGS_SOURCE = (
    REPOSITORY_ROOT
    / "src"
    / "wspctl"
    / "src"
    / "presentation"
    / "python"
    / "bindings.cpp"
)


def test_constructor_exposes_structured_native_error() -> None:
    """@brief 非法 endpoint 必须保留机器可判定 code / An invalid endpoint must retain a machine-readable code.

    @return None / None.
    """

    try:
        RuntimeProcess(
            "relative.sock",
            "123e4567-e89b-12d3-a456-426614174000",
            "activation-native-binding",
        )
    except NativeError as error:
        assert error.code == "invalid_argument"
        assert isinstance(error.message, str)
        assert error.request_id == ""
    else:
        raise AssertionError("RuntimeProcess accepted a non-absolute endpoint")


def test_connection_error_exposes_structured_native_error() -> None:
    """@brief 不存在 broker 的连接错误必须是稳定 code / A missing broker must produce a stable error code.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/wspctl-no-such-broker.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-binding",
    )
    try:
        process.execute(
            ["/bin/true"],
            request_id="native-binding-test",
            request_hash="a" * 64,
        )
    except NativeError as error:
        assert error.code == "io_failure"
        assert error.request_id == "native-binding-test"
    else:
        raise AssertionError(
            "RuntimeProcess unexpectedly connected to a nonexistent broker"
        )


def test_status_is_read_only_native_abi_without_activation_override() -> None:
    """@brief status 必须是无 activation override 的只读 RPC / status must be a read-only RPC without activation override.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/wspctl-no-such-broker.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-status",
    )
    try:
        process.status()
    except NativeError as error:
        assert error.code == "io_failure"
        assert error.request_id == ""
    else:
        raise AssertionError("RuntimeProcess.status unexpectedly connected to a nonexistent broker")
    try:
        process.status(activation_id="activation-other")
    except TypeError:
        pass
    else:
        raise AssertionError("RuntimeProcess.status accepted an activation override")
    assert hasattr(RuntimeStatus, "dump")


def test_closed_status_fails_locally_before_broker_connection() -> None:
    """@brief closed handle 的 status 必须本地 permission_denied / status on a closed handle must be local permission_denied.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/wspctl-no-such-broker.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-closed-status",
    )
    process.close()
    try:
        process.status()
    except NativeError as error:
        assert error.code == "permission_denied"
        assert error.request_id == ""
    else:
        raise AssertionError("closed RuntimeProcess.status did not fail locally")


def test_runtime_status_dump_has_a_fixed_non_sensitive_allowlist() -> None:
    """@brief dump 只能写入固定 telemetry 字段 / dump may write only fixed telemetry fields.

    这个测试直接固定 pybind serialization boundary，而不是假设 C++ 私有字段不会增多。
    It pins the pybind serialization boundary directly rather than assuming C++ private fields
    will never grow.

    @return None / None.
    """

    source = BINDINGS_SOURCE.read_text(encoding="utf-8")
    dump_start = source.index("[[nodiscard]] py::dict dump() const")
    dump_end = source.index("private:", dump_start)
    dump_block = source[dump_start:dump_end]
    expected_keys = {
        "runtime_key",
        "state",
        "active_activation_id",
        "handle_activation_matches",
        "supervisor_alive",
        "idle_for_ms",
        "idle_ttl_ms",
        "borrowed_dispatches",
        "cleanup_pending",
    }
    actual_keys = {
        line.split('dictionary["', 1)[1].split('"]', 1)[0]
        for line in dump_block.splitlines()
        if 'dictionary["' in line
    }
    assert actual_keys == expected_keys
    for forbidden in (
        "argv",
        "stdin",
        "stdout",
        "stderr",
        "request_id",
        "request_hash",
        "payload",
        "path",
        "pid",
        "cgroup",
        "mount",
        "socket_path",
    ):
        assert forbidden not in dump_block, f"RuntimeStatus.dump leaked {forbidden}"
def test_constructor_binds_one_valid_activation() -> None:
    """@brief 构造函数必须验证并永久绑定 activation / The constructor must validate and permanently bind an activation.

    @return None / None.
    """

    try:
        RuntimeProcess(
            "/tmp/wspctl-no-such-broker.sock",
            "123e4567-e89b-12d3-a456-426614174000",
            "activation/escapes",
        )
    except NativeError as error:
        assert error.code == "invalid_argument"
    else:
        raise AssertionError("RuntimeProcess accepted an unsafe activation identifier")


def test_execute_cannot_override_handle_activation() -> None:
    """@brief execute ABI 不得暴露 activation override / The execute ABI must not expose an activation override.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/wspctl-no-such-broker.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-binding",
    )
    try:
        process.execute(
            ["/bin/true"],
            activation_id="activation-other",
            request_id="native-binding-no-override",
            request_hash="a" * 64,
        )
    except TypeError:
        return
    raise AssertionError("RuntimeProcess.execute accepted an activation override")


def test_add_file_is_the_only_public_mutating_file_ingress_method() -> None:
    """@brief add_file 必须是唯一公开写入 ABI；replay_file 只能读取回执 / add_file must be the sole public mutating ingress ABI; replay_file only reads receipts.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/wspctl-no-such-broker.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-binding",
    )
    assert hasattr(process, "add_file")
    assert hasattr(process, "replay_file")
    assert not hasattr(process, "add_payload")
    try:
        process.add_file(
            "native-file-ingress",
            [b"x"],
            1,
            "a" * 64,
            request_id="native-file-ingress-test",
            request_hash="b" * 64,
        )
    except NativeError as error:
        assert error.code == "io_failure"
        assert error.request_id == "native-file-ingress-test"
    else:
        raise AssertionError(
            "RuntimeProcess.add_file unexpectedly connected to a nonexistent broker"
        )


def test_replay_file_has_no_activation_or_chunk_source_abi() -> None:
    """@brief replay_file 必须是无 activation、无 bytes 流的只读 RPC / replay_file must be an activation-free, byte-stream-free read-only RPC.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/wspctl-no-such-broker.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-binding",
    )
    try:
        process.replay_file(
            "native-file-ingress",
            1,
            "a" * 64,
            request_id="native-file-replay-test",
            request_hash="b" * 64,
        )
    except NativeError as error:
        assert error.code == "io_failure"
        assert error.request_id == "native-file-replay-test"
    else:
        raise AssertionError(
            "RuntimeProcess.replay_file unexpectedly connected to a nonexistent broker"
        )
    try:
        process.replay_file(
            "native-file-ingress",
            1,
            "a" * 64,
            request_id="native-file-replay-no-activation",
            request_hash="b" * 64,
            activation_id="activation-other",
        )
    except TypeError:
        return
    raise AssertionError("RuntimeProcess.replay_file accepted an activation override")


def test_add_file_rejects_one_raw_binary_buffer_as_chunks() -> None:
    """@brief chunks 必须是 Iterable[bytes] 而不是裸 bytes / chunks must be Iterable[bytes], not raw bytes.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/wspctl-no-such-broker.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-binding",
    )
    try:
        process.add_file(
            "native-file-ingress",
            b"x",
            1,
            "a" * 64,
            request_id="native-file-raw-buffer-test",
            request_hash="b" * 64,
        )
    except TypeError:
        return
    raise AssertionError(
        "RuntimeProcess.add_file accepted one raw bytes buffer as the chunk iterable"
    )


def test_fetch_file_rejects_unsafe_paths_before_connecting() -> None:
    """@brief fetch_file 在 socket 前拒绝 traversal，且不接受 activation override / fetch_file rejects traversal before socket I/O and accepts no activation override.

    @return None / None.
    """

    process = RuntimeProcess(
        "/tmp/nonexistent-wspctld.sock",
        "123e4567-e89b-12d3-a456-426614174000",
        "activation-native-fetch-test",
    )
    try:
        process.fetch_file("../etc/passwd")
    except NativeError as error:
        assert getattr(error, "code", None) == "invalid_argument"
    else:
        raise AssertionError("RuntimeProcess.fetch_file accepted parent traversal")
    try:
        process.fetch_file("safe.txt", activation_id="other")  # type: ignore[call-arg]
    except TypeError:
        return
    raise AssertionError("RuntimeProcess.fetch_file accepted an activation override")


def _run_contract_tests() -> None:
    """@brief 以 CTest 直接运行 pybind ABI 契约 / Run pybind ABI contracts directly under CTest.

    pytest 仍可发现同一批 ``test_*`` 函数；这个显式 runner 避免 CTest 把仅导入模块误报为
    通过。/ pytest can still discover the same ``test_*`` functions; this explicit runner prevents
    CTest from treating an import-only module execution as a pass.

    @return None / None.
    """

    test_constructor_exposes_structured_native_error()
    test_connection_error_exposes_structured_native_error()
    test_status_is_read_only_native_abi_without_activation_override()
    test_closed_status_fails_locally_before_broker_connection()
    test_runtime_status_dump_has_a_fixed_non_sensitive_allowlist()
    test_constructor_binds_one_valid_activation()
    test_execute_cannot_override_handle_activation()
    test_add_file_is_the_only_public_mutating_file_ingress_method()
    test_replay_file_has_no_activation_or_chunk_source_abi()
    test_add_file_rejects_one_raw_binary_buffer_as_chunks()
    test_fetch_file_rejects_unsafe_paths_before_connecting()


if __name__ == "__main__":
    _run_contract_tests()
