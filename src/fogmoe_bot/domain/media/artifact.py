"""生成媒体制品的持久化清单模型 / Durable manifest models for generated-media artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .identifiers import ArtifactId


class ArtifactKind(StrEnum):
    """生成媒体制品类型 / Generated-media artifact kind."""

    IMAGE = "image"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """文件系统持久化的生成媒体清单 / Filesystem-persisted generated-media manifest."""

    artifact_id: ArtifactId
    kind: ArtifactKind
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        """校验制品元数据 / Validate artifact metadata."""

        if not str(self.artifact_id).strip():
            raise ValueError("artifact_id must not be blank")
        if not self.filename.strip() or not self.mime_type.strip():
            raise ValueError("filename and mime_type must not be blank")
        if self.size_bytes <= 0:
            raise ValueError("size_bytes must be positive")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
