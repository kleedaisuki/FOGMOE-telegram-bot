"""@brief 已构建 wspctl._native 的 ABI 契约测试 / ABI contract tests for the built wspctl._native module."""

from __future__ import annotations

from wspctl import NativeError, RuntimeProcess


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
