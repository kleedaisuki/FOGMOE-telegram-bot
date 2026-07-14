"""@brief Telegram 确认按钮的紧凑、无密钥引用 / Compact, non-secret references for Telegram confirmation buttons."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from uuid import UUID

from fogmoe_bot.domain.asset_actions.confirmation import AssetActionDecision


_PREFIX = "asset_confirm"
"""@brief 独占 Telegram callback 命名空间 / Exclusive Telegram callback namespace."""

_ACTION_CODES = MappingProxyType(
    {
        AssetActionDecision.APPROVE: "a",
        AssetActionDecision.CANCEL: "c",
    }
)
"""@brief 决定到单字节 callback 码的不可变映射 / Immutable mapping from decisions to one-byte callback codes."""

_CODE_ACTIONS = MappingProxyType({value: key for key, value in _ACTION_CODES.items()})
"""@brief callback 码到决定的反向映射 / Reverse mapping from callback codes to decisions."""


@dataclass(frozen=True, slots=True)
class AssetActionCallbackData:
    """@brief Telegram 按钮携带的确认引用 / Confirmation reference carried by a Telegram button.

    callback_data 不含金额、用途、owner 或任何秘密；所有授权和动作语义均从数据库中的
    confirmation ID 重新读取。/ callback_data contains neither amount, purpose, owner, nor a
    secret; all authorization and action semantics are reloaded from the database by confirmation ID.

    @param confirmation_id 持久确认 ID / Durable confirmation identifier.
    @param decision 按钮代表的 owner 选择 / Owner choice represented by the button.
    """

    confirmation_id: UUID
    decision: AssetActionDecision

    def encode(self) -> str:
        """@brief 编码为 Telegram callback_data / Encode as Telegram callback_data.

        @return 小于 Telegram 64-byte 上限的 ASCII 引用 / ASCII reference below Telegram's 64-byte limit.
        @raise ValueError 编码意外超长时抛出 / Raised if the encoding unexpectedly exceeds the limit.
        """

        value = f"{_PREFIX}:{_ACTION_CODES[self.decision]}:{self.confirmation_id}"
        if len(value.encode("utf-8")) > 64:
            raise ValueError("Asset-action callback data exceeds Telegram's 64-byte limit")
        return value

    @classmethod
    def decode(cls, value: str) -> AssetActionCallbackData:
        """@brief 严格解码 callback_data / Strictly decode callback_data.

        @param value Telegram callback_data / Telegram callback data.
        @return 类型化确认引用 / Typed confirmation reference.
        @raise ValueError 命名空间、动作码或 UUID 非法时抛出 / Raised for an invalid namespace, action code, or UUID.
        """

        parts = value.split(":")
        if len(parts) != 3 or parts[0] != _PREFIX:
            raise ValueError("Invalid asset-action callback namespace")
        decision = _CODE_ACTIONS.get(parts[1])
        if decision is None:
            raise ValueError("Invalid asset-action callback decision")
        try:
            confirmation_id = UUID(parts[2])
        except ValueError as error:
            raise ValueError("Invalid asset-action callback confirmation ID") from error
        return cls(confirmation_id=confirmation_id, decision=decision)


__all__ = ["AssetActionCallbackData"]
