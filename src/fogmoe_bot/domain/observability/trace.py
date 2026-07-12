"""@brief W3C Trace Context 值对象 / W3C Trace Context value objects."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True, slots=True)
class TraceId:
    """@brief 128 位非零 trace identity / Non-zero 128-bit trace identity.

    @param value 精确 16 字节的 identity / Identity containing exactly 16 bytes.
    """

    value: bytes

    def __post_init__(self) -> None:
        """@brief 校验长度与非零约束 / Validate length and non-zero constraints.

        @return None / None.
        """

        if len(self.value) != 16 or not any(self.value):
            raise ValueError("TraceId must contain exactly 16 non-zero bytes")

    @classmethod
    def new(cls) -> Self:
        """@brief 创建密码学随机 trace ID / Create a cryptographically random trace ID.

        @return 新 trace ID / New trace ID.
        """

        while not any(value := secrets.token_bytes(16)):
            pass
        return cls(value)

    @classmethod
    def from_hex(cls, value: str) -> Self:
        """@brief 解析 32 位小写或大写十六进制 / Parse 32 hexadecimal characters.

        @param value 十六进制文本 / Hexadecimal text.
        @return trace ID / Trace ID.
        """

        if len(value) != 32:
            raise ValueError("TraceId hex value must contain 32 characters")
        try:
            return cls(bytes.fromhex(value))
        except ValueError as error:
            raise ValueError(
                "TraceId contains invalid hexadecimal characters"
            ) from error

    def __str__(self) -> str:
        """@brief 返回规范小写十六进制 / Return canonical lowercase hexadecimal text.

        @return 32 位文本 / 32-character text.
        """

        return self.value.hex()


@dataclass(frozen=True, slots=True)
class SpanId:
    """@brief 64 位非零 span identity / Non-zero 64-bit span identity.

    @param value 精确 8 字节的 identity / Identity containing exactly 8 bytes.
    """

    value: bytes

    def __post_init__(self) -> None:
        """@brief 校验长度与非零约束 / Validate length and non-zero constraints.

        @return None / None.
        """

        if len(self.value) != 8 or not any(self.value):
            raise ValueError("SpanId must contain exactly 8 non-zero bytes")

    @classmethod
    def new(cls) -> Self:
        """@brief 创建密码学随机 span ID / Create a cryptographically random span ID.

        @return 新 span ID / New span ID.
        """

        while not any(value := secrets.token_bytes(8)):
            pass
        return cls(value)

    @classmethod
    def from_hex(cls, value: str) -> Self:
        """@brief 解析 16 位十六进制 / Parse 16 hexadecimal characters.

        @param value 十六进制文本 / Hexadecimal text.
        @return span ID / Span ID.
        """

        if len(value) != 16:
            raise ValueError("SpanId hex value must contain 16 characters")
        try:
            return cls(bytes.fromhex(value))
        except ValueError as error:
            raise ValueError(
                "SpanId contains invalid hexadecimal characters"
            ) from error

    def __str__(self) -> str:
        """@brief 返回规范小写十六进制 / Return canonical lowercase hexadecimal text.

        @return 16 位文本 / 16-character text.
        """

        return self.value.hex()


@dataclass(frozen=True, slots=True)
class TraceContext:
    """@brief 可持久传播的 W3C traceparent / Persistable W3C traceparent.

    @param trace_id trace identity / Trace identity.
    @param span_id 当前远端或本地父 span / Current remote or local parent span.
    @param trace_flags W3C 一字节 flags / W3C one-byte flags.
    """

    trace_id: TraceId
    span_id: SpanId
    trace_flags: int = 1

    def __post_init__(self) -> None:
        """@brief 校验 W3C flags / Validate W3C flags.

        @return None / None.
        """

        if isinstance(self.trace_flags, bool) or not 0 <= self.trace_flags <= 255:
            raise ValueError("Trace flags must be an unsigned byte")

    @classmethod
    def new_root(cls, *, sampled: bool = True) -> Self:
        """@brief 创建根上下文 / Create a root context.

        @param sampled 是否设置 sampled flag / Whether to set the sampled flag.
        @return 新根上下文 / New root context.
        """

        return cls(TraceId.new(), SpanId.new(), int(sampled))

    @classmethod
    def parse(cls, value: str) -> Self:
        """@brief 严格解析 W3C traceparent v00 / Strictly parse W3C traceparent v00.

        @param value traceparent 文本 / Traceparent text.
        @return 已校验上下文 / Validated context.
        """

        parts = value.strip().split("-")
        if len(parts) != 4 or parts[0] != "00" or len(parts[3]) != 2:
            raise ValueError("Unsupported or malformed traceparent")
        try:
            flags = int(parts[3], 16)
        except ValueError as error:
            raise ValueError("Traceparent flags are not hexadecimal") from error
        return cls(TraceId.from_hex(parts[1]), SpanId.from_hex(parts[2]), flags)

    def child(self) -> Self:
        """@brief 为同一 trace 创建子 span 上下文 / Create a child-span context for this trace.

        @return 子上下文 / Child context.
        """

        return type(self)(self.trace_id, SpanId.new(), self.trace_flags)

    def to_traceparent(self) -> str:
        """@brief 编码 W3C traceparent v00 / Encode W3C traceparent v00.

        @return 55 字符 carrier / 55-character carrier.
        """

        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags:02x}"


__all__ = ["SpanId", "TraceContext", "TraceId"]
