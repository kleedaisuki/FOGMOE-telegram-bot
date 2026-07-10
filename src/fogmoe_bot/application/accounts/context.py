from __future__ import annotations

from typing import Any

from . import service as user_service
from fogmoe_bot.domain.context import UserState
from fogmoe_bot.infrastructure.database.repositories import conversation_repository


def normalize_user_impression(value: str | None) -> str:
    """@brief 规范化用户印象 / Normalize user impression.

    @param value 原始印象文本 / Raw impression text.
    @return 适合 prompt 的印象文本 / Prompt-ready impression text.
    """

    impression = (value or "").strip()
    if not impression:
        return "Not recorded"
    impression = impression.replace("\r", " ").replace("\n", " ")
    if len(impression) > 500:
        return impression[:497] + "..."
    return impression


def normalize_personal_info(value: str | None) -> str:
    """@brief 规范化个人信息 / Normalize personal information.

    @param value 原始个人信息 / Raw personal information.
    @return 适合 prompt 的个人信息 / Prompt-ready personal information.
    """

    personal_info = (value or "").strip()
    if len(personal_info) > 500:
        return personal_info[:500]
    return personal_info


async def load_user_state(
    user_id: int,
    *,
    account: Any | None = None,
    coins: int | None = None,
    plan: str | None = None,
) -> UserState | None:
    """@brief 加载用户上下文状态 / Load user context state.

    @param user_id Telegram 用户 ID / Telegram user id.
    @param account 可复用的用户账户对象 / Optional reusable user account object.
    @param coins 覆盖硬币数 / Optional coin balance override.
    @param plan 覆盖订阅计划 / Optional plan override.
    @return 用户状态，不存在账户时返回 None / User state, or None when account does not exist.
    """

    if account is None:
        account = await user_service.get_user_account(user_id)
    if not account:
        return None

    user_coins = int(coins if coins is not None else account.total_coins)
    user_plan = plan or user_service.resolve_user_plan(user_id, account.coins_paid)
    impression_raw = await user_service.async_get_user_impression(user_id)
    diary_exists = await conversation_repository.user_diary_exists(user_id)

    return UserState(
        coins=user_coins,
        plan=user_plan,
        permission=int(account.permission),
        impression=normalize_user_impression(impression_raw),
        personal_info=normalize_personal_info(account.info),
        diary_exists=diary_exists,
    )
