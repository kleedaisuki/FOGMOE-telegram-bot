"""@brief Crypto Telegram callback 协议测试 / Tests for the Crypto Telegram callback protocol."""

import pytest

from fogmoe_bot.domain.crypto import PredictionDirection
from fogmoe_bot.presentation.telegram.crypto_handlers import prediction


def test_current_callback_protocol_binds_owner_and_action() -> None:
    """@brief 当前 callback 精确携带 owner、方向与数量 / Current callback precisely carries owner, direction, and amount."""

    amount = prediction._parse_callback("crypto_amount_50_user_42")
    assert isinstance(amount, prediction._AmountCallback)
    assert amount.owner_id == 42
    assert amount.amount is not None and int(amount.amount) == 50
    direction = prediction._parse_callback("crypto_predict_down_user_42_100")
    assert isinstance(direction, prediction._DirectionCallback)
    assert direction.owner_id == 42
    assert direction.direction is PredictionDirection.DOWN
    assert int(direction.amount) == 100


def test_legacy_callback_protocol_remains_accepted_but_unknown_is_rejected() -> None:
    """@brief 旧 callback 继续可用，未知输入 fail closed / Legacy callback remains usable while unknown input fails closed."""

    custom = prediction._parse_callback("crypto_amount_custom")
    assert isinstance(custom, prediction._AmountCallback)
    assert custom.amount is None and custom.owner_id is None
    cancel = prediction._parse_callback("crypto_cancel")
    assert isinstance(cancel, prediction._CancelCallback)
    with pytest.raises(ValueError):
        prediction._parse_callback("crypto_predict_sideways_20")


def test_generated_callback_data_stays_below_telegram_limit() -> None:
    """@brief 新键盘 callback_data 不超过 Telegram 64-byte 上限 / Generated callback data stays within Telegram's 64-byte limit."""

    keyboard = prediction._direction_keyboard(
        9_223_372_036_854_775_807, prediction.CoinStake(10_000_000)
    )
    values = [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    assert all(isinstance(value, str) and len(value.encode()) <= 64 for value in values)
