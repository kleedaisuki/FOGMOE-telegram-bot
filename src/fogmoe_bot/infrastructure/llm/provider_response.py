"""@brief Provider codec 的已解码响应 / Decoded responses from provider codecs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from fogmoe_bot.domain.assistant.messages import CanonicalMessage

from .messages import MessageContractError


@dataclass(frozen=True, slots=True)
class DecodedProviderCompletion:
    """@brief 传输无关的已解码 provider completion / Transport-neutral decoded provider completion.

    @param message 可持久化的 Canonical Message V2 / Persistable Canonical Message V2.
    @param input_tokens 可选输入 token 计数 / Optional input-token count.
    @param output_tokens 可选输出 token 计数 / Optional output-token count.
    @param cached_input_tokens 可选缓存读取 token 计数 / Optional cache-read token count.
    @param cache_write_input_tokens 可选缓存写入 token 计数 / Optional cache-write token count.
    """

    message: CanonicalMessage
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None


@dataclass(slots=True)
class _OpenAIToolCallState:
    """@brief 一个分片 OpenAI function call 的聚合状态 / Aggregation state for one chunked OpenAI function call."""

    call_id: str | None = None
    name: str | None = None
    argument_chunks: list[str] = field(default_factory=list)

    def consume(self, value: Mapping[str, object], *, index: int) -> None:
        """@brief 合并一个 function-call delta / Merge one function-call delta.

        @param value 原始 tool-call delta / Raw tool-call delta.
        @param index wire tool-call index / Wire tool-call index.
        @return None / None.
        @raise MessageContractError ID、类型或 function 字段冲突时抛出 /
            Raised for conflicting IDs, types, or function fields.
        """

        raw_type = value.get("type")
        if raw_type not in {None, "function"}:
            raise MessageContractError(
                f"OpenAI stream tool_call {index}.type must be function"
            )
        raw_id = value.get("id")
        if raw_id is not None:
            self.call_id = _assign_stream_string(
                current=self.call_id,
                value=raw_id,
                context=f"OpenAI stream tool_call {index}.id",
            )
        function = value.get("function")
        if function is None:
            return
        if not isinstance(function, Mapping):
            raise MessageContractError(
                f"OpenAI stream tool_call {index}.function must be an object"
            )
        raw_function = cast(Mapping[str, object], function)
        raw_name = raw_function.get("name")
        if raw_name is not None:
            self.name = _assign_stream_string(
                current=self.name,
                value=raw_name,
                context=f"OpenAI stream tool_call {index}.function.name",
            )
        arguments = raw_function.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                raise MessageContractError(
                    f"OpenAI stream tool_call {index}.function.arguments must be a string"
                )
            self.argument_chunks.append(arguments)

    def wire_value(self, *, index: int) -> dict[str, object]:
        """@brief 构造非流式 decoder 可复用的完整 wire tool call / Build a complete wire tool call reusable by the non-streaming decoder.

        @param index wire tool-call index / Wire tool-call index.
        @return 完整 function tool-call object / Complete function tool-call object.
        @raise MessageContractError 必需身份缺失时抛出 / Raised when required identity is missing.
        """

        if self.call_id is None or self.name is None:
            raise MessageContractError(
                f"OpenAI stream tool_call {index} is incomplete"
            )
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": "".join(self.argument_chunks),
            },
        }


@dataclass(slots=True)
class OpenAIChatStreamAccumulator:
    """@brief 严格聚合 OpenAI Chat Completions SSE chunks / Strictly aggregate OpenAI Chat Completions SSE chunks."""

    _text_chunks: list[str] = field(default_factory=list)
    _tool_calls: dict[int, _OpenAIToolCallState] = field(default_factory=dict)
    _usage: dict[str, object] | None = None
    _saw_choice: bool = False
    _choice_finished: bool = False
    _done: bool = False

    def consume(self, payload: Mapping[str, object]) -> tuple[str, ...]:
        """@brief 消费一个 ``data: JSON`` chunk / Consume one ``data: JSON`` chunk.

        @param payload 已解析 JSON object / Parsed JSON object.
        @return 本 chunk 的非空文本增量 / Non-empty text deltas in this chunk.
        @raise MessageContractError chunk 顺序或字段违反协议时抛出 /
            Raised when chunk ordering or fields violate the protocol.
        """

        if self._done:
            raise MessageContractError("OpenAI stream emitted data after [DONE]")
        usage = payload.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise MessageContractError("OpenAI stream usage must be an object")
            self._usage = dict(cast(Mapping[str, object], usage))
        choices = payload.get("choices")
        if not isinstance(choices, list):
            raise MessageContractError("OpenAI stream choices must be an array")
        if not choices:
            if usage is None:
                raise MessageContractError(
                    "OpenAI stream empty choices require a usage object"
                )
            return ()
        if len(choices) != 1 or not isinstance(choices[0], Mapping):
            raise MessageContractError(
                "OpenAI stream must contain exactly one choice"
            )
        if self._choice_finished:
            raise MessageContractError(
                "OpenAI stream emitted a choice after finish_reason"
            )
        choice = cast(Mapping[str, object], choices[0])
        if choice.get("index") != 0:
            raise MessageContractError("OpenAI stream choice index must be zero")
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            raise MessageContractError("OpenAI stream choice.delta must be an object")
        raw_delta = cast(Mapping[str, object], delta)
        role = raw_delta.get("role")
        if role not in {None, "assistant"}:
            raise MessageContractError(
                "OpenAI stream delta role must be assistant"
            )
        refusal = raw_delta.get("refusal")
        if refusal is not None and refusal != "":
            raise MessageContractError(
                "OpenAI streamed refusals are not persistable canonical text"
            )
        emitted: list[str] = []
        content = raw_delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise MessageContractError(
                    "OpenAI stream delta.content must be a string"
                )
            if content:
                self._text_chunks.append(content)
                emitted.append(content)
        raw_tool_calls = raw_delta.get("tool_calls")
        if raw_tool_calls is not None:
            if not isinstance(raw_tool_calls, list):
                raise MessageContractError(
                    "OpenAI stream delta.tool_calls must be an array"
                )
            for raw_call in raw_tool_calls:
                if not isinstance(raw_call, Mapping):
                    raise MessageContractError(
                        "OpenAI stream tool_call delta must be an object"
                    )
                call = cast(Mapping[str, object], raw_call)
                raw_index = call.get("index")
                if (
                    not isinstance(raw_index, int)
                    or isinstance(raw_index, bool)
                    or raw_index < 0
                ):
                    raise MessageContractError(
                        "OpenAI stream tool_call index must be a non-negative integer"
                    )
                self._tool_calls.setdefault(
                    raw_index,
                    _OpenAIToolCallState(),
                ).consume(call, index=raw_index)
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str) or not finish_reason:
                raise MessageContractError(
                    "OpenAI stream finish_reason must be a non-empty string or null"
                )
            self._choice_finished = True
        self._saw_choice = True
        return tuple(emitted)

    def consume_done(self) -> None:
        """@brief 消费唯一 ``data: [DONE]`` 终止标记 / Consume the sole ``data: [DONE]`` terminator.

        @return None / None.
        @raise MessageContractError 标记重复或早于完成 choice 时抛出 /
            Raised when the marker is duplicated or precedes a finished choice.
        """

        if self._done:
            raise MessageContractError("OpenAI stream repeated [DONE]")
        if not self._saw_choice or not self._choice_finished:
            raise MessageContractError(
                "OpenAI stream ended before its choice completed"
            )
        self._done = True

    def final_payload(self) -> Mapping[str, object]:
        """@brief 构造非流式 decoder 的等价完整响应 / Build an equivalent full response for the non-streaming decoder.

        @return OpenAI-style 完整 completion JSON / Full OpenAI-style completion JSON.
        @raise MessageContractError 流未出现完整终态时抛出 /
            Raised when the stream lacks a complete terminal state.
        """

        if not self._done:
            raise MessageContractError("OpenAI stream ended without [DONE]")
        indices = sorted(self._tool_calls)
        if indices != list(range(len(indices))):
            raise MessageContractError(
                "OpenAI stream tool_call indices must be contiguous"
            )
        message: dict[str, object] = {
            "role": "assistant",
            "content": "".join(self._text_chunks) or None,
        }
        if indices:
            message["tool_calls"] = [
                self._tool_calls[index].wire_value(index=index)
                for index in indices
            ]
        payload: dict[str, object] = {
            "choices": [{"index": 0, "message": message}],
        }
        if self._usage is not None:
            payload["usage"] = self._usage
        return payload


@dataclass(slots=True)
class _AnthropicBlockState:
    """@brief 一个 Anthropic content block 的流式聚合状态 / Streaming aggregation state for one Anthropic content block."""

    kind: str
    text_chunks: list[str] = field(default_factory=list)
    call_id: str | None = None
    name: str | None = None
    initial_input: dict[str, object] | None = None
    input_json_chunks: list[str] = field(default_factory=list)
    open: bool = True

    def wire_value(self, *, index: int) -> dict[str, object] | None:
        """@brief 构造最终 Anthropic content block / Build the final Anthropic content block.

        @param index content block index / Content-block index.
        @return 可供非流式 decoder 使用的 block；不可持久化 block 返回 None /
            Block reusable by the non-streaming decoder, or None for non-persistable blocks.
        @raise MessageContractError tool input 不完整或 JSON 非法时抛出 /
            Raised for incomplete tool input or invalid JSON.
        """

        if self.open:
            raise MessageContractError(
                f"Anthropic stream content block {index} did not stop"
            )
        if self.kind == "text":
            return {"type": "text", "text": "".join(self.text_chunks)}
        if self.kind == "tool_use":
            if self.call_id is None or self.name is None:
                raise MessageContractError(
                    f"Anthropic stream tool_use block {index} is incomplete"
                )
            if self.input_json_chunks:
                if self.initial_input:
                    raise MessageContractError(
                        f"Anthropic stream tool_use block {index} mixed initial and delta input"
                    )
                try:
                    raw_input = json.loads("".join(self.input_json_chunks))
                except json.JSONDecodeError as error:
                    raise MessageContractError(
                        f"Anthropic stream tool_use block {index} input is not valid JSON"
                    ) from error
                if not isinstance(raw_input, Mapping):
                    raise MessageContractError(
                        f"Anthropic stream tool_use block {index} input must be an object"
                    )
                tool_input: dict[str, object] = dict(
                    cast(Mapping[str, object], raw_input)
                )
            else:
                tool_input = dict(self.initial_input or {})
            return {
                "type": "tool_use",
                "id": self.call_id,
                "name": self.name,
                "input": tool_input,
            }
        if self.kind in {"thinking", "redacted_thinking", "fallback"}:
            return None
        raise MessageContractError(
            f"Unsupported Anthropic stream content block type: {self.kind!r}"
        )


@dataclass(slots=True)
class AnthropicMessagesStreamAccumulator:
    """@brief 按官方事件状态机聚合 Anthropic Messages SSE / Aggregate Anthropic Messages SSE by its documented event state machine."""

    _blocks: dict[int, _AnthropicBlockState] = field(default_factory=dict)
    _usage: dict[str, object] = field(default_factory=dict)
    _started: bool = False
    _saw_message_delta: bool = False
    _stopped: bool = False

    def consume(
        self,
        event_name: str,
        payload: Mapping[str, object],
    ) -> tuple[str, ...]:
        """@brief 消费一个具名 Anthropic SSE event / Consume one named Anthropic SSE event.

        @param event_name SSE ``event`` 字段 / SSE ``event`` field.
        @param payload 已解析 JSON object / Parsed JSON object.
        @return 本事件的非空文本增量 / Non-empty text deltas in this event.
        @raise MessageContractError 事件名、顺序或字段不合法时抛出 /
            Raised for an invalid event name, order, or field.
        """

        raw_type = payload.get("type")
        if not isinstance(raw_type, str) or not raw_type:
            raise MessageContractError("Anthropic stream event.type must be a string")
        if event_name != raw_type:
            raise MessageContractError(
                "Anthropic SSE event name must match data.type"
            )
        if raw_type == "ping":
            return ()
        if self._stopped:
            raise MessageContractError(
                "Anthropic stream emitted data after message_stop"
            )
        if raw_type == "message_start":
            self._consume_message_start(payload)
            return ()
        if not self._started:
            raise MessageContractError(
                "Anthropic stream must begin with message_start"
            )
        if raw_type == "content_block_start":
            return self._consume_block_start(payload)
        if raw_type == "content_block_delta":
            return self._consume_block_delta(payload)
        if raw_type == "content_block_stop":
            self._consume_block_stop(payload)
            return ()
        if raw_type == "message_delta":
            self._consume_message_delta(payload)
            return ()
        if raw_type == "message_stop":
            if any(block.open for block in self._blocks.values()):
                raise MessageContractError(
                    "Anthropic message_stop arrived before content blocks stopped"
                )
            if not self._saw_message_delta:
                raise MessageContractError(
                    "Anthropic stream ended without message_delta"
                )
            self._stopped = True
            return ()
        # Anthropic's versioning policy permits new event types. They must not mutate
        # known block state until this adapter intentionally supports their semantics.
        return ()

    def final_payload(self) -> Mapping[str, object]:
        """@brief 构造非流式 decoder 的等价完整响应 / Build an equivalent full response for the non-streaming decoder.

        @return Anthropic-style 完整 message JSON / Full Anthropic-style message JSON.
        @raise MessageContractError 流未完成或 block index 有洞时抛出 /
            Raised when the stream is incomplete or block indices have gaps.
        """

        if not self._stopped:
            raise MessageContractError(
                "Anthropic stream ended without message_stop"
            )
        indices = sorted(self._blocks)
        if indices != list(range(len(indices))):
            raise MessageContractError(
                "Anthropic stream content block indices must be contiguous"
            )
        content: list[dict[str, object]] = []
        for index in indices:
            block = self._blocks[index].wire_value(index=index)
            if block is not None:
                content.append(block)
        return {
            "content": content,
            "usage": dict(self._usage),
        }

    def _consume_message_start(self, payload: Mapping[str, object]) -> None:
        """@brief 校验并记录 message_start / Validate and record message_start.

        @param payload message_start data / message_start data.
        @return None / None.
        """

        if self._started:
            raise MessageContractError("Anthropic stream repeated message_start")
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise MessageContractError(
                "Anthropic message_start.message must be an object"
            )
        raw_message = cast(Mapping[str, object], message)
        if raw_message.get("role") != "assistant":
            raise MessageContractError(
                "Anthropic stream message role must be assistant"
            )
        content = raw_message.get("content")
        if content != []:
            raise MessageContractError(
                "Anthropic message_start content must be empty"
            )
        self._merge_usage(raw_message.get("usage"))
        self._started = True

    def _consume_block_start(
        self,
        payload: Mapping[str, object],
    ) -> tuple[str, ...]:
        """@brief 打开一个 content block / Open one content block.

        @param payload content_block_start data / content_block_start data.
        @return start event 自带的文本 / Text carried by the start event.
        """

        index = _stream_index(payload, context="Anthropic content_block_start")
        if index in self._blocks:
            raise MessageContractError(
                f"Anthropic stream repeated content block {index}"
            )
        raw_block = payload.get("content_block")
        if not isinstance(raw_block, Mapping):
            raise MessageContractError(
                "Anthropic content_block_start.content_block must be an object"
            )
        block = cast(Mapping[str, object], raw_block)
        kind = block.get("type")
        if kind not in {
            "text",
            "tool_use",
            "thinking",
            "redacted_thinking",
            "fallback",
        }:
            raise MessageContractError(
                f"Unsupported Anthropic stream content block type: {kind!r}"
            )
        state = _AnthropicBlockState(kind=kind)
        emitted: tuple[str, ...] = ()
        if kind == "text":
            text = block.get("text", "")
            if not isinstance(text, str):
                raise MessageContractError(
                    f"Anthropic text block {index}.text must be a string"
                )
            if text:
                state.text_chunks.append(text)
                emitted = (text,)
        elif kind == "tool_use":
            state.call_id = _required_stream_string(
                block.get("id"),
                context=f"Anthropic tool_use block {index}.id",
            )
            state.name = _required_stream_string(
                block.get("name"),
                context=f"Anthropic tool_use block {index}.name",
            )
            raw_input = block.get("input", {})
            if not isinstance(raw_input, Mapping):
                raise MessageContractError(
                    f"Anthropic tool_use block {index}.input must be an object"
                )
            state.initial_input = dict(cast(Mapping[str, object], raw_input))
        self._blocks[index] = state
        return emitted

    def _consume_block_delta(
        self,
        payload: Mapping[str, object],
    ) -> tuple[str, ...]:
        """@brief 合并一个 content-block delta / Merge one content-block delta.

        @param payload content_block_delta data / content_block_delta data.
        @return 非空文本增量 / Non-empty text delta.
        """

        index = _stream_index(payload, context="Anthropic content_block_delta")
        state = self._blocks.get(index)
        if state is None or not state.open:
            raise MessageContractError(
                f"Anthropic stream delta targets unopened block {index}"
            )
        raw_delta = payload.get("delta")
        if not isinstance(raw_delta, Mapping):
            raise MessageContractError(
                "Anthropic content_block_delta.delta must be an object"
            )
        delta = cast(Mapping[str, object], raw_delta)
        delta_type = delta.get("type")
        if state.kind == "text":
            if delta_type != "text_delta" or not isinstance(
                text := delta.get("text"), str
            ):
                raise MessageContractError(
                    f"Anthropic text block {index} requires text_delta"
                )
            if not text:
                return ()
            state.text_chunks.append(text)
            return (text,)
        if state.kind == "tool_use":
            if delta_type != "input_json_delta" or not isinstance(
                partial_json := delta.get("partial_json"), str
            ):
                raise MessageContractError(
                    f"Anthropic tool_use block {index} requires input_json_delta"
                )
            state.input_json_chunks.append(partial_json)
            return ()
        if state.kind in {"thinking", "redacted_thinking"}:
            if delta_type not in {"thinking_delta", "signature_delta"}:
                raise MessageContractError(
                    f"Anthropic thinking block {index} received an invalid delta"
                )
            return ()
        raise MessageContractError(
            f"Anthropic {state.kind} block {index} cannot receive deltas"
        )

    def _consume_block_stop(self, payload: Mapping[str, object]) -> None:
        """@brief 关闭一个 content block / Close one content block.

        @param payload content_block_stop data / content_block_stop data.
        @return None / None.
        """

        index = _stream_index(payload, context="Anthropic content_block_stop")
        state = self._blocks.get(index)
        if state is None or not state.open:
            raise MessageContractError(
                f"Anthropic stream stop targets unopened block {index}"
            )
        state.open = False

    def _consume_message_delta(self, payload: Mapping[str, object]) -> None:
        """@brief 合并 message-level delta 与累计 usage / Merge message-level delta and cumulative usage.

        @param payload message_delta data / message_delta data.
        @return None / None.
        """

        raw_delta = payload.get("delta")
        if not isinstance(raw_delta, Mapping):
            raise MessageContractError(
                "Anthropic message_delta.delta must be an object"
            )
        self._merge_usage(payload.get("usage"))
        self._saw_message_delta = True

    def _merge_usage(self, value: object) -> None:
        """@brief 合并 Anthropic 分阶段 usage / Merge staged Anthropic usage.

        @param value 原始 usage object 或 None / Raw usage object or None.
        @return None / None.
        """

        if value is None:
            return
        if not isinstance(value, Mapping):
            raise MessageContractError("Anthropic stream usage must be an object")
        self._usage.update(cast(Mapping[str, object], value))


def _assign_stream_string(
    *,
    current: str | None,
    value: object,
    context: str,
) -> str:
    """@brief 只允许一个流式身份字段被一致赋值 / Allow a streamed identity field to be assigned consistently once.

    @param current 已聚合值 / Already aggregated value.
    @param value 新 wire 值 / New wire value.
    @param context 错误上下文 / Error context.
    @return 一致的非空字符串 / Consistent non-empty string.
    @raise MessageContractError 值为空或与已有值冲突时抛出 /
        Raised when the value is blank or conflicts with the existing value.
    """

    normalized = _required_stream_string(value, context=context)
    if current is not None and current != normalized:
        raise MessageContractError(f"{context} changed during the stream")
    return normalized


def _required_stream_string(value: object, *, context: str) -> str:
    """@brief 验证流式协议中的非空字符串 / Validate a non-empty streaming-protocol string.

    @param value 原始 wire 值 / Raw wire value.
    @param context 错误上下文 / Error context.
    @return 非空原值 / Non-empty original value.
    @raise MessageContractError 值为空时抛出 / Raised when the value is empty.
    """

    if not isinstance(value, str) or not value:
        raise MessageContractError(f"{context} must be a non-empty string")
    return value


def _stream_index(payload: Mapping[str, object], *, context: str) -> int:
    """@brief 验证 content block index / Validate a content-block index.

    @param payload 含 index 的 event data / Event data containing index.
    @param context 错误上下文 / Error context.
    @return 非负整数 index / Non-negative integer index.
    @raise MessageContractError index 非法时抛出 / Raised for an invalid index.
    """

    raw = payload.get("index")
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise MessageContractError(f"{context}.index must be a non-negative integer")
    return raw


__all__ = [
    "AnthropicMessagesStreamAccumulator",
    "DecodedProviderCompletion",
    "OpenAIChatStreamAccumulator",
]
