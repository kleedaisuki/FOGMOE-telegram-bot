"""@brief 群组自动回复值对象 / Group auto-reply value objects."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KeywordReply:
    """@brief 一条关键词自动回复 / One keyword auto-reply.

    @param keyword 触发关键词 / Trigger keyword.
    @param response 回复内容 / Reply content.
    """

    keyword: str
    response: str
