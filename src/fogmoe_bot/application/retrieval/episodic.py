"""@brief Conversation 情景历史的 passage 形成 / Passage formation for conversational episodic history."""

from __future__ import annotations

from dataclasses import dataclass

from fogmoe_bot.application.retrieval.ports import (
    CONVERSATION_TURN_SOURCE_KIND,
    EPISODIC_CORPUS_ID,
    EpisodicTurn,
)
from fogmoe_bot.domain.retrieval import RetrievalPassage


EPISODIC_PASSAGE_FORMAT_VERSION = 1
"""@brief 当前情景 passage renderer 版本 / Current episodic-passage renderer version."""


@dataclass(frozen=True, slots=True)
class EpisodicPassageRenderer:
    """@brief 按自然 Turn 边界形成有界 passage / Form bounded passages at natural turn boundaries.

    @param max_characters 单 passage 最大 Unicode 字符数 / Maximum Unicode characters per passage.
    @param format_version 输出格式版本 / Output format version.
    """

    max_characters: int = 6_000
    format_version: int = EPISODIC_PASSAGE_FORMAT_VERSION

    def __post_init__(self) -> None:
        """@brief 校验 renderer 配置 / Validate renderer configuration.

        @return None / None.
        @raise ValueError 边界非法 / Invalid bounds.
        """

        if not 500 <= self.max_characters <= 20_000:
            raise ValueError("Episodic passage max_characters must be 500-20000")
        if isinstance(self.format_version, bool) or self.format_version < 1:
            raise ValueError("Episodic passage format_version must be positive")

    def render(self, turn: EpisodicTurn) -> tuple[RetrievalPassage, ...]:
        """@brief 将完整 Turn 渲染为稳定 passages / Render a complete turn into stable passages.

        @param turn 已验证 Assistant Turn / Validated Assistant turn.
        @return 至少一个稳定 passage / At least one stable passage.
        """

        timestamp = turn.occurred_at.isoformat().replace("+00:00", "Z")
        body = f"Time: {timestamp}\nUser: {turn.user_text}\nAssistant: {turn.assistant_text}"
        parts = _split_bounded(body, self.max_characters)
        return tuple(
            RetrievalPassage.create(
                corpus_id=EPISODIC_CORPUS_ID,
                scope=turn.scope,
                source_kind=CONVERSATION_TURN_SOURCE_KIND,
                source_id=turn.turn_id,
                ordinal=ordinal,
                format_version=self.format_version,
                text=text,
                occurred_at=turn.occurred_at,
            )
            for ordinal, text in enumerate(parts)
        )


def _split_bounded(text: str, limit: int) -> tuple[str, ...]:
    """@brief 优先按段落切分并硬限制超长段 / Prefer paragraph splits and hard-bound oversized paragraphs.

    @param text 规范 Turn 文本 / Canonical turn text.
    @param limit 单段字符上限 / Per-part character limit.
    @return 非空有界文本 / Non-empty bounded text parts.
    """

    if len(text) <= limit:
        return (text,)
    paragraphs = text.splitlines(keepends=True)
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        remaining = paragraph
        while remaining:
            capacity = limit - len(current)
            if capacity == 0:
                parts.append(current.strip())
                current = ""
                capacity = limit
            current += remaining[:capacity]
            remaining = remaining[capacity:]
            if len(current) == limit:
                parts.append(current.strip())
                current = ""
    if current.strip():
        parts.append(current.strip())
    return tuple(part for part in parts if part)


__all__ = ["EPISODIC_PASSAGE_FORMAT_VERSION", "EpisodicPassageRenderer"]
