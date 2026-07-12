"""@brief Assistant 路由纯策略 / Pure Assistant routing policies."""

from fnmatch import fnmatchcase
from typing import Iterable


def model_supports_vision(
    model: str | None,
    text_only_patterns: Iterable[str],
) -> bool:
    """@brief 判断模型是否支持视觉输入 / Check whether a model supports vision input.

    @param model 模型名称 / Model name.
    @param text_only_patterns 文本模型通配模式 / Text-only model glob patterns.
    @return True 表示允许多模态输入 / True when multimodal input is allowed.
    """
    normalized_model = (model or "").strip().lower()
    if not normalized_model:
        return True
    for pattern in text_only_patterns:
        normalized_pattern = str(pattern or "").strip().lower()
        if normalized_pattern and fnmatchcase(normalized_model, normalized_pattern):
            return False
    return True
