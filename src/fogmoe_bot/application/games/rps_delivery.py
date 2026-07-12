"""@brief 猜拳的框架无关投递地址 / Framework-neutral RPS delivery addresses."""

from __future__ import annotations

from dataclasses import dataclass

from fogmoe_bot.domain.games import RpsDomainError, UserId


@dataclass(frozen=True, slots=True)
class MessageAddress:
    """@brief 可编辑聊天消息地址 / Address of an editable chat message.

    @param chat_id 聊天稳定标识 / Stable chat identifier.
    @param message_id 聊天内消息标识 / Message identifier within the chat.
    """

    chat_id: int
    message_id: int

    def __post_init__(self) -> None:
        """@brief 校验消息地址 / Validate the message address."""

        if isinstance(self.chat_id, bool) or not isinstance(self.chat_id, int):
            raise TypeError("chat_id must be an integer")
        if isinstance(self.message_id, bool) or not isinstance(self.message_id, int):
            raise TypeError("message_id must be an integer")
        if self.message_id <= 0:
            raise ValueError("message_id must be positive")


@dataclass(frozen=True, slots=True)
class PlayerMessage:
    """@brief 一位玩家的选择消息地址 / Choice-message address for one player.

    @param user_id 消息所属玩家 / Player owning the message.
    @param address 可编辑地址 / Editable address.
    """

    user_id: UserId
    address: MessageAddress

    def __post_init__(self) -> None:
        """@brief 校验玩家消息 / Validate the player message."""

        if not isinstance(self.user_id, UserId):
            raise TypeError("user_id must be a UserId")
        if not isinstance(self.address, MessageAddress):
            raise TypeError("address must be a MessageAddress")


@dataclass(frozen=True, slots=True)
class GameDelivery:
    """@brief 一局游戏的全部可编辑投递地址 / All editable delivery addresses for one game.

    @param announcement 公共邀请或状态消息 / Public invitation or status message.
    @param player_messages 两位玩家的私有选择消息 / Private choice messages for both players.
    """

    announcement: MessageAddress | None
    player_messages: tuple[PlayerMessage, PlayerMessage]

    def __post_init__(self) -> None:
        """@brief 校验投递地址的玩家唯一性 / Validate unique player ownership."""

        if self.announcement is not None and not isinstance(
            self.announcement, MessageAddress
        ):
            raise TypeError("announcement must be a MessageAddress or None")
        if len(self.player_messages) != 2 or any(
            not isinstance(message, PlayerMessage) for message in self.player_messages
        ):
            raise TypeError(
                "player_messages must contain exactly two PlayerMessage values"
            )
        if self.player_messages[0].user_id == self.player_messages[1].user_id:
            raise RpsDomainError("delivery messages must belong to distinct players")

    def for_player(self, user_id: UserId) -> MessageAddress:
        """@brief 返回指定玩家的选择消息 / Return one player's choice-message address.

        @param user_id 玩家身份 / Player identity.
        @return 对应可编辑消息地址 / Corresponding editable message address.
        """

        for message in self.player_messages:
            if message.user_id == user_id:
                return message.address
        raise KeyError(user_id)
