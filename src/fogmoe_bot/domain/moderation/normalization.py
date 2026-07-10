"""@brief 审核文本规范化 / Moderation text normalization."""


def normalize_for_matching(value: str) -> str:
    """@brief 规范化用于兼容匹配的文本 / Normalize text for compatibility matching.

    当前仅执行与旧实现一致的小写转换，避免重构时扩大或缩小拦截范围。
    / Currently mirrors the legacy lowercase behavior to avoid changing policy semantics.

    @param value 原始文本 / Original text.
    @return 规范化文本 / Normalized text.
    """

    return value.lower()
