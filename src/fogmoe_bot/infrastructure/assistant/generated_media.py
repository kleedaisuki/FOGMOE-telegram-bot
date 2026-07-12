"""Assistant 生成媒体 adapter / Assistant generated-media adapter."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import requests

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.media.artifact import ArtifactKind
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind, SpanStatus
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.media.file_artifact_store import FileArtifactStore
from fogmoe_bot.infrastructure.media.file_rate_limiter import FileSlidingWindowLimiter
from fogmoe_bot.infrastructure.network.proxy import create_requests_session

from .requests_response import read_limited_response


_MAX_IMAGE_BYTES = 16 * 1024 * 1024
"""@brief 单图字节上限 / Per-image byte limit."""

_MAX_AUDIO_BYTES = 24 * 1024 * 1024
"""@brief 单音频字节上限 / Per-audio byte limit."""


@dataclass(frozen=True, slots=True)
class GeneratedMediaSettings:
    """@brief 生成媒体配置 / Generated-media settings."""

    image_url: str
    image_token: str
    fish_audio_key: str
    fish_audio_model: str
    fish_audio_reference_id: str
    image_timeout_seconds: int = 30


class RequestsGeneratedMediaTools:
    """@brief 生成 artifact 而不直接投递的 adapter / Adapter generating artifacts without direct delivery."""

    def __init__(
        self,
        *,
        settings: GeneratedMediaSettings,
        artifacts: FileArtifactStore,
        limiter: FileSlidingWindowLimiter,
        bulkhead: AsyncBlockingBulkhead,
        telemetry: Telemetry,
    ) -> None:
        """@brief 注入配置与 durable file services / Inject settings and durable file services.

        @param settings API 配置 / API settings.
        @param artifacts durable artifact store / Durable artifact store.
        @param limiter 跨进程 rate limiter / Cross-process rate limiter.
        @param bulkhead 专用生成隔舱 / Dedicated generation bulkhead.
        """

        if settings.image_timeout_seconds <= 0:
            raise ValueError("image_timeout_seconds must be positive")
        self._settings = settings
        self._artifacts = artifacts
        self._limiter = limiter
        self._bulkhead = bulkhead
        self._telemetry = telemetry

    async def generate(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 在线程边界生成媒体 / Generate media behind a thread boundary.

        @param request 工具请求 / Tool request.
        @return artifact references / Artifact references.
        """

        dependency = _dependency_name(request.tool_name)
        with self._telemetry.span(
            "media.generate",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.tool.name": request.tool_name,
                "fogmoe.dependency.name": dependency,
            },
        ) as span:
            try:
                result = await self._bulkhead.call(lambda: self._generate_sync(request))
            except Exception:
                self._telemetry.counter(
                    MetricName.DEPENDENCY_OUTCOMES,
                    attributes={
                        "outcome": Outcome.FAILURE,
                        "fogmoe.dependency.name": dependency,
                    },
                )
                raise
            if isinstance(result, dict) and "error" in result:
                span.set_status(SpanStatus.ERROR, str(result["error"]))
                span.set_attribute("error.type", "media_generation_error")
                outcome = Outcome.FAILURE
            else:
                outcome = Outcome.SUCCESS
            self._telemetry.counter(
                MetricName.DEPENDENCY_OUTCOMES,
                attributes={
                    "outcome": outcome,
                    "fogmoe.dependency.name": dependency,
                },
            )
            return result

    def _generate_sync(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 同步生成实现 / Synchronous generation implementation.

        @param request 工具请求 / Tool request.
        @return JSON 结果 / JSON result.
        """

        self._artifacts.cleanup_expired(scan_limit=1000)
        maximum = 2 if request.tool_name == "generate_image" else 3
        decision = self._limiter.reserve(
            f"assistant-{request.tool_name}-{request.context.user_id}",
            window_seconds=300,
            max_requests=maximum,
        )
        if not decision.allowed:
            return {
                "error": "Media generation rate limit exceeded",
                "retry_after_seconds": decision.retry_after_seconds,
            }
        succeeded = False
        try:
            result = (
                self._image(request)
                if request.tool_name == "generate_image"
                else self._voice(request)
            )
            succeeded = isinstance(result, dict) and result.get("status") == "generated"
            return result
        finally:
            if not succeeded:
                self._limiter.release(
                    f"assistant-{request.tool_name}-{request.context.user_id}",
                    decision.reservation,
                )

    def _image(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 请求并保存一张图片 / Request and persist one image.

        @param request 工具请求 / Tool request.
        @return JSON 结果 / JSON result.
        """

        if not self._settings.image_url or not self._settings.image_token:
            return {"error": "Image generation is not configured"}
        prompt = str(request.arguments["prompt"])
        item: JsonObject = {
            "prompt": prompt,
            "width": int(cast(int, request.arguments.get("width", 1024))),
            "height": int(cast(int, request.arguments.get("height", 1024))),
            "steps": int(cast(int, request.arguments.get("steps", 9))),
        }
        if isinstance(request.arguments.get("seed"), int):
            item["seed"] = cast(int, request.arguments["seed"])
        timeout = int(
            cast(
                int,
                request.arguments.get(
                    "timeout_seconds", self._settings.image_timeout_seconds
                ),
            )
        )
        with create_requests_session() as session:
            try:
                response = session.post(
                    self._settings.image_url,
                    headers={"Authorization": f"Bearer {self._settings.image_token}"},
                    json={"items": [item]},
                    timeout=timeout,
                    stream=True,
                )
                content = read_limited_response(response, 32 * 1024 * 1024)
            except (requests.RequestException, ValueError) as error:
                return {"error": f"Image generation failed: {error}"}
        if response.status_code >= 400:
            return {"error": "Image generation failed", "status": response.status_code}
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return {"error": f"Image provider returned invalid JSON: {error}"}
        images = _image_values(payload)
        if not images:
            return {"error": "Image provider returned no image"}
        try:
            raw = base64.b64decode(_strip_data_uri(images[0]), validate=False)
        except ValueError as error:
            return {"error": f"Image provider returned invalid base64: {error}"}
        extension, mime = _image_meta(raw)
        record = self._artifacts.store(
            kind=ArtifactKind.IMAGE,
            content=raw,
            filename=_filename(prompt, extension, "generated_image"),
            mime_type=mime,
            ttl=timedelta(hours=1),
            max_bytes=_MAX_IMAGE_BYTES,
        )
        return {
            "status": "generated",
            "artifacts": [
                {
                    "artifact_id": str(record.artifact_id),
                    "kind": record.kind.value,
                    "filename": record.filename,
                    "mime_type": record.mime_type,
                    "size_bytes": record.size_bytes,
                }
            ],
        }

    def _voice(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 请求并保存一段音频 / Request and persist one audio clip.

        @param request 工具请求 / Tool request.
        @return JSON 结果 / JSON result.
        """

        if not self._settings.fish_audio_key:
            return {"error": "Voice generation is not configured"}
        text = str(request.arguments["text"])
        with create_requests_session() as session:
            try:
                response = session.post(
                    "https://api.fish.audio/v1/tts",
                    headers={
                        "Authorization": f"Bearer {self._settings.fish_audio_key}",
                        "Content-Type": "application/json",
                        "model": self._settings.fish_audio_model,
                    },
                    json={
                        "text": text,
                        "reference_id": self._settings.fish_audio_reference_id,
                        "format": "opus",
                    },
                    timeout=60,
                    stream=True,
                )
                content = read_limited_response(response, _MAX_AUDIO_BYTES)
            except (requests.RequestException, ValueError) as error:
                return {"error": f"Voice generation failed: {error}"}
        if response.status_code >= 400:
            return {"error": "Voice generation failed", "status": response.status_code}
        extension, mime = _audio_meta(response.headers.get("Content-Type"))
        record = self._artifacts.store(
            kind=ArtifactKind.AUDIO,
            content=content,
            filename=_filename(text, extension, "generated_audio"),
            mime_type=mime,
            ttl=timedelta(hours=1),
            max_bytes=_MAX_AUDIO_BYTES,
        )
        return {
            "status": "generated",
            "artifacts": [
                {
                    "artifact_id": str(record.artifact_id),
                    "kind": record.kind.value,
                    "filename": record.filename,
                    "mime_type": record.mime_type,
                    "size_bytes": record.size_bytes,
                }
            ],
        }


def _image_values(value: object) -> list[str]:
    """@brief 递归提取 base64 images / Recursively extract base64 images.

    @param value Provider JSON / Provider JSON.
    @return images / Images.
    """

    if isinstance(value, str):
        return [value] if value.startswith("data:image/") or len(value) > 128 else []
    if isinstance(value, list):
        return [item for child in value for item in _image_values(child)]
    if not isinstance(value, dict):
        return []
    results: list[str] = []
    for key, child in value.items():
        if key in {"b64", "b64_json", "base64", "image_base64", "image", "content"}:
            results.extend(_image_values(child))
        elif key in {"items", "images", "data", "results", "outputs", "output"}:
            results.extend(_image_values(child))
    return results


def _strip_data_uri(value: str) -> str:
    """@brief 去除 data URI prefix / Strip a data-URI prefix.

    @param value 输入 / Input.
    @return raw base64 / Raw base64.
    """

    return re.sub(r"^data:image/[^;]+;base64,", "", value.strip(), flags=re.I)


def _image_meta(content: bytes) -> tuple[str, str]:
    """@brief 识别 image 格式 / Detect image format.

    @param content bytes / Bytes.
    @return extension 与 MIME / Extension and MIME.
    """

    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".png", "image/png"


def _audio_meta(content_type: str | None) -> tuple[str, str]:
    """@brief 识别 audio 格式 / Detect audio format.

    @param content_type Content-Type / Content-Type.
    @return extension 与 MIME / Extension and MIME.
    """

    normalized = (content_type or "").split(";", 1)[0].lower()
    if normalized in {"audio/wav", "audio/wave", "audio/x-wav"}:
        return ".wav", "audio/wav"
    if normalized in {"audio/mpeg", "audio/mp3"}:
        return ".mp3", "audio/mpeg"
    return ".ogg", "audio/ogg"


def _dependency_name(tool_name: str) -> str:
    """@brief 映射媒体工具到依赖名称 / Map a media tool to a dependency name.

    @param tool_name 工具目录名称 / Tool-catalog name.
    @return 低基数 provider 标识 / Low-cardinality provider identifier.
    """

    return {
        "generate_image": "image_generation",
        "generate_voice": "fish_audio",
    }.get(tool_name, "unknown")


def _filename(text: str, extension: str, fallback: str) -> str:
    """@brief 构造安全 artifact filename / Build a safe artifact filename.

    @param text 用户文本 / User text.
    @param extension 扩展名 / Extension.
    @param fallback fallback stem / Fallback stem.
    @return filename / Filename.
    """

    stem = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", text.strip())[:80].strip("_")
    return f"{stem or fallback}{extension}"
