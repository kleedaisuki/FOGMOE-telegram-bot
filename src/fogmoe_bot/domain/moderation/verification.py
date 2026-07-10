"""@brief 成员验证任务领域模型 / Member-verification task domain model."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, auto

from .models import ChatId, MessageId, UserId


class VerificationStatus(Enum):
    """@brief 成员验证任务状态 / Member-verification task status."""

    PENDING = auto()
    PASSED = auto()
    EXPIRED = auto()
    CANCELLED = auto()


@dataclass(frozen=True, slots=True)
class VerificationTask:
    """@brief 一项持久化成员验证任务 / One persisted member-verification task.

    @param chat_id Telegram 群组 ID / Telegram chat ID.
    @param user_id 待验证用户 ID / User ID awaiting verification.
    @param message_id 验证消息 ID / Verification message ID.
    @param token_hash 验证 token 的 SHA-256 摘要 / SHA-256 digest of the verification token.
    @param expires_at 过期时间 / Expiration time.
    @param status 当前状态 / Current status.
    """

    chat_id: ChatId
    user_id: UserId
    message_id: MessageId
    token_hash: str
    expires_at: datetime
    status: VerificationStatus = VerificationStatus.PENDING

    def accepts(self, token: str, now: datetime) -> bool:
        """@brief 判断 token 是否可完成任务 / Check whether a token may complete the task.

        @param token 回调携带的明文 token / Plaintext callback token.
        @param now 当前时间 / Current time.
        @return 可完成返回 True / True when the task may be completed.
        """

        return (
            self.status is VerificationStatus.PENDING
            and now < self.expires_at
            and secrets.compare_digest(self.token_hash, hash_verification_token(token))
        )

    def transition(self, target: VerificationStatus) -> VerificationTask:
        """@brief 推进到合法终态 / Advance to a legal terminal state.

        @param target 目标状态 / Target status.
        @return 新任务快照 / New task snapshot.
        @note 当前任务只有 PENDING 可迁移到终态 / Only PENDING may transition.
        """

        if self.status is not VerificationStatus.PENDING:
            raise RuntimeError(
                f"Invalid verification transition: {self.status.name} -> {target.name}"
            )
        if target not in {
            VerificationStatus.PASSED,
            VerificationStatus.EXPIRED,
            VerificationStatus.CANCELLED,
        }:
            raise RuntimeError(
                f"Invalid verification transition: {self.status.name} -> {target.name}"
            )
        return replace(self, status=target)


def hash_verification_token(token: str) -> str:
    """@brief 计算验证 token 摘要 / Hash a verification token.

    @param token 明文 token / Plaintext token.
    @return 十六进制 SHA-256 摘要 / Hexadecimal SHA-256 digest.
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
