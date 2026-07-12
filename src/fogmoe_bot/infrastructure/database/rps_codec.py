"""@brief RPS 领域值与 JSONB 的严格编解码 / Strict codecs between RPS domain values and JSONB."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import json
from typing import cast

from fogmoe_bot.application.games.rps_delivery import (
    GameDelivery,
    MessageAddress,
    PlayerMessage,
)
from fogmoe_bot.domain.games import (
    Choice,
    GameCancellation,
    GameId,
    GameOutcome,
    GameSession,
    GameStatus,
    GameVersion,
    OutcomeKind,
    Payout,
    Player,
    UserId,
    WaitingRoom,
)


def encode_waiting(room: WaitingRoom) -> str:
    """@brief 编码等待房间 / Encode a waiting room.

    @param room 等待房间 / Waiting room.
    @return JSON 文本 / JSON text.
    """

    return _dump(
        {
            "schema": 1,
            "kind": "waiting",
            "game_id": str(room.game_id),
            "version": room.version.value,
            "host": _player_to_json(room.host),
            "created_at": room.created_at.isoformat(),
            "expires_at": room.expires_at.isoformat(),
        }
    )


def decode_waiting(value: object) -> WaitingRoom:
    """@brief 解码并重新验证等待房间 / Decode and revalidate a waiting room.

    @param value JSONB 值 / JSONB value.
    @return 领域等待房间 / Domain waiting room.
    """

    payload = _object(value, "waiting")
    _require_schema(payload, "waiting")
    if _string(payload.get("kind"), "waiting.kind") != "waiting":
        raise ValueError("waiting.kind must be waiting")
    return WaitingRoom(
        game_id=GameId(_string(payload.get("game_id"), "waiting.game_id")),
        version=GameVersion(_integer(payload.get("version"), "waiting.version")),
        host=_player(payload.get("host"), "waiting.host"),
        created_at=_datetime(payload.get("created_at"), "waiting.created_at"),
        expires_at=_datetime(payload.get("expires_at"), "waiting.expires_at"),
    )


def encode_session(session: GameSession) -> str:
    """@brief 编码游戏会话 / Encode a game session.

    @param session 游戏会话 / Game session.
    @return JSON 文本 / JSON text.
    """

    outcome: dict[str, object] | None = None
    if session.outcome is not None:
        outcome = {
            "kind": session.outcome.kind.value,
            "winner": (
                None if session.outcome.winner is None else session.outcome.winner.value
            ),
            "payouts": [
                {"user_id": payout.user_id.value, "coins": payout.coins}
                for payout in session.outcome.payouts
            ],
        }
    return _dump(
        {
            "schema": 1,
            "kind": "game",
            "game_id": str(session.game_id),
            "version": session.version.value,
            "player_one": _player_to_json(session.player_one),
            "player_two": _player_to_json(session.player_two),
            "status": session.status.value,
            "player_one_choice": _choice_value(session.player_one_choice),
            "player_two_choice": _choice_value(session.player_two_choice),
            "started_at": session.started_at.isoformat(),
            "expires_at": session.expires_at.isoformat(),
            "outcome": outcome,
            "cancellation": (
                None if session.cancellation is None else session.cancellation.value
            ),
        }
    )


def decode_session(value: object) -> GameSession:
    """@brief 解码并重新验证游戏会话 / Decode and revalidate a game session.

    @param value JSONB 值 / JSONB value.
    @return 领域游戏会话 / Domain game session.
    """

    payload = _object(value, "session")
    _require_schema(payload, "session")
    if _string(payload.get("kind"), "session.kind") != "game":
        raise ValueError("session.kind must be game")
    outcome_value = payload.get("outcome")
    outcome = None if outcome_value is None else _outcome(outcome_value)
    cancellation_value = payload.get("cancellation")
    return GameSession(
        game_id=GameId(_string(payload.get("game_id"), "session.game_id")),
        version=GameVersion(_integer(payload.get("version"), "session.version")),
        player_one=_player(payload.get("player_one"), "session.player_one"),
        player_two=_player(payload.get("player_two"), "session.player_two"),
        status=GameStatus(_string(payload.get("status"), "session.status")),
        player_one_choice=_choice(payload.get("player_one_choice")),
        player_two_choice=_choice(payload.get("player_two_choice")),
        started_at=_datetime(payload.get("started_at"), "session.started_at"),
        expires_at=_datetime(payload.get("expires_at"), "session.expires_at"),
        outcome=outcome,
        cancellation=(
            None
            if cancellation_value is None
            else GameCancellation(_string(cancellation_value, "session.cancellation"))
        ),
    )


def encode_waiting_delivery(invitation: MessageAddress) -> str:
    """@brief 编码等待邀请地址 / Encode a waiting-invitation address.

    @param invitation 邀请地址 / Invitation address.
    @return JSON 文本 / JSON text.
    """

    return _dump({"schema": 1, "announcement": _address_to_json(invitation)})


def decode_waiting_delivery(value: object) -> MessageAddress | None:
    """@brief 解码可选等待邀请地址 / Decode an optional waiting-invitation address.

    @param value JSONB 值或 None / JSONB value or None.
    @return 邀请地址或 None / Invitation address or None.
    """

    if value is None:
        return None
    payload = _object(value, "waiting_delivery")
    _require_schema(payload, "waiting_delivery")
    return _address(payload.get("announcement"), "waiting_delivery.announcement")


def encode_game_delivery(delivery: GameDelivery) -> str:
    """@brief 编码活动对局投递地址 / Encode active-game delivery addresses.

    @param delivery 投递地址 / Delivery addresses.
    @return JSON 文本 / JSON text.
    """

    return _dump(
        {
            "schema": 1,
            "announcement": (
                None
                if delivery.announcement is None
                else _address_to_json(delivery.announcement)
            ),
            "player_messages": [
                {
                    "user_id": message.user_id.value,
                    "address": _address_to_json(message.address),
                }
                for message in delivery.player_messages
            ],
        }
    )


def decode_game_delivery(value: object) -> GameDelivery | None:
    """@brief 解码可选活动投递地址 / Decode optional active-game delivery addresses.

    @param value JSONB 值或 None / JSONB value or None.
    @return 完整地址；尚未绑定时为 None / Complete addresses, or None before binding.
    """

    if value is None:
        return None
    payload = _object(value, "game_delivery")
    _require_schema(payload, "game_delivery")
    messages_value = payload.get("player_messages")
    if messages_value is None:
        return None
    messages = _sequence(messages_value, "game_delivery.player_messages")
    if len(messages) != 2:
        raise ValueError("game_delivery.player_messages must contain two values")
    decoded: list[PlayerMessage] = []
    for index, raw_message in enumerate(messages):
        item = _object(raw_message, f"game_delivery.player_messages[{index}]")
        decoded.append(
            PlayerMessage(
                UserId(
                    _integer(
                        item.get("user_id"),
                        f"game_delivery.player_messages[{index}].user_id",
                    )
                ),
                _address(
                    item.get("address"),
                    f"game_delivery.player_messages[{index}].address",
                ),
            )
        )
    announcement_value = payload.get("announcement")
    return GameDelivery(
        announcement=(
            None
            if announcement_value is None
            else _address(announcement_value, "game_delivery.announcement")
        ),
        player_messages=(decoded[0], decoded[1]),
    )


def _player_to_json(player: Player) -> dict[str, object]:
    """@brief 编码玩家 / Encode a player."""

    return {"user_id": player.user_id.value, "display_name": player.display_name}


def _player(value: object, field: str) -> Player:
    """@brief 解码玩家 / Decode a player."""

    payload = _object(value, field)
    return Player(
        UserId(_integer(payload.get("user_id"), f"{field}.user_id")),
        _string(payload.get("display_name"), f"{field}.display_name"),
    )


def _outcome(value: object) -> GameOutcome:
    """@brief 解码终局结果 / Decode a terminal outcome."""

    payload = _object(value, "session.outcome")
    winner_value = payload.get("winner")
    payout_values = _sequence(payload.get("payouts"), "session.outcome.payouts")
    payouts: list[Payout] = []
    for index, raw_payout in enumerate(payout_values):
        item = _object(raw_payout, f"session.outcome.payouts[{index}]")
        payouts.append(
            Payout(
                UserId(
                    _integer(
                        item.get("user_id"),
                        f"session.outcome.payouts[{index}].user_id",
                    )
                ),
                _integer(
                    item.get("coins"),
                    f"session.outcome.payouts[{index}].coins",
                ),
            )
        )
    return GameOutcome(
        kind=OutcomeKind(_string(payload.get("kind"), "session.outcome.kind")),
        winner=(
            None
            if winner_value is None
            else UserId(_integer(winner_value, "session.outcome.winner"))
        ),
        payouts=tuple(payouts),
    )


def _choice(value: object) -> Choice | None:
    """@brief 解码可选手势 / Decode an optional choice."""

    return None if value is None else Choice(_string(value, "choice"))


def _choice_value(choice: Choice | None) -> str | None:
    """@brief 编码可选手势 / Encode an optional choice."""

    return None if choice is None else choice.value


def _address_to_json(address: MessageAddress) -> dict[str, int]:
    """@brief 编码消息地址 / Encode a message address."""

    return {"chat_id": address.chat_id, "message_id": address.message_id}


def _address(value: object, field: str) -> MessageAddress:
    """@brief 解码消息地址 / Decode a message address."""

    payload = _object(value, field)
    return MessageAddress(
        _integer(payload.get("chat_id"), f"{field}.chat_id"),
        _integer(payload.get("message_id"), f"{field}.message_id"),
    )


def _require_schema(payload: Mapping[str, object], field: str) -> None:
    """@brief 校验 JSON schema 版本 / Validate a JSON schema version."""

    if _integer(payload.get("schema"), f"{field}.schema") != 1:
        raise ValueError(f"{field}.schema must be 1")


def _object(value: object, field: str) -> Mapping[str, object]:
    """@brief 收窄 JSON 对象 / Narrow a JSON object."""

    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, field: str) -> Sequence[object]:
    """@brief 收窄 JSON 数组 / Narrow a JSON array."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{field} must be an array")
    return cast(Sequence[object], value)


def _string(value: object, field: str) -> str:
    """@brief 收窄字符串 / Narrow a string."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _integer(value: object, field: str) -> int:
    """@brief 收窄严格整数 / Narrow a strict integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _datetime(value: object, field: str) -> datetime:
    """@brief 解码带时区时间 / Decode a timezone-aware timestamp."""

    parsed = datetime.fromisoformat(_string(value, field))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _dump(value: Mapping[str, object]) -> str:
    """@brief 生成确定性紧凑 JSON / Render deterministic compact JSON."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
