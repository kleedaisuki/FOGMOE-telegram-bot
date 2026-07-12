"""媒体上下文共享的稳定标识 / Stable identifiers shared by media capabilities."""

from typing import NewType


ArtifactId = NewType("ArtifactId", str)
"""持久化媒体制品或图片报价标识 / Durable media-artifact or picture-offer identifier."""

UserId = NewType("UserId", int)
"""Telegram 用户标识 / Telegram user identifier."""
