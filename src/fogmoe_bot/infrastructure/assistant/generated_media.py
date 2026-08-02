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

_MIN_IMAGE_DIMENSION = 64
"""@brief 单边最小像素 / Minimum pixels on either image edge."""

_MAX_IMAGE_DIMENSION = 4096
"""@brief 单边最大像素 / Maximum pixels on either image edge."""

_DEFAULT_IMAGE_DIMENSION = 1024
"""@brief 通用图片默认单边像素 / Generic default pixels for one image edge."""

_SEEDREAM_45_MODEL = "bytedance-seed/seedream-4.5"
"""@brief Seedream 4.5 模型标识 / Seedream 4.5 model identifier."""

_SEEDREAM_45_DEFAULT_DIMENSION = 2048
"""@brief Seedream 4.5 的安全默认单边像素 / Safe default edge pixels for Seedream 4.5."""

_SEEDREAM_45_MIN_OUTPUT_PIXELS = 3_686_400
"""@brief Seedream 4.5 的最小输出像素 / Minimum output pixels accepted by Seedream 4.5."""

_MAX_PROVIDER_ERROR_TEXT = 2_000
"""@brief provider 错误文本上限 / Maximum provider-error text length."""


@dataclass(frozen=True, slots=True)
class GeneratedMediaSettings:
    """@brief 生成媒体配置 / Generated-media settings."""

    image_url: str
    image_token: str
    image_model: str
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
        dimensions = _validated_image_dimensions(
            model=self._settings.image_model,
            arguments=request.arguments,
        )
        if isinstance(dimensions, dict):
            return dimensions
        width, height = dimensions
        if isinstance(request.arguments.get("seed"), int):
            seed = cast(int, request.arguments["seed"])
        else:
            seed = None
        requested_steps = request.arguments.get("steps")
        payload = _image_request_payload(
            model=self._settings.image_model,
            prompt=prompt,
            width=width,
            height=height,
            steps=int(
                cast(
                    int,
                    requested_steps if requested_steps is not None else 9,
                )
            ),
            seed=seed,
        )
        requested_timeout = request.arguments.get("timeout_seconds")
        timeout = int(
            cast(
                int,
                requested_timeout
                if requested_timeout is not None
                else self._settings.image_timeout_seconds,
            )
        )
        with create_requests_session() as session:
            try:
                response = session.post(
                    self._settings.image_url,
                    headers={"Authorization": f"Bearer {self._settings.image_token}"},
                    json=payload,
                    timeout=timeout,
                    stream=True,
                )
                content = read_limited_response(response, 32 * 1024 * 1024)
            except (requests.RequestException, ValueError) as error:
                return {"error": f"Image generation failed: {error}"}
        if response.status_code >= 400:
            return _image_provider_error(response.status_code, content)
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


def _image_request_payload(
    *,
    model: str,
    prompt: str,
    width: int,
    height: int,
    steps: int,
    seed: int | None,
) -> JsonObject:
    """@brief 构造图片服务请求体 / Build an image-provider request payload.

    @param model OpenRouter 图片模型；为空时使用旧 items 协议 / OpenRouter image model; empty uses the legacy items protocol.
    @param prompt 图片描述 / Image prompt.
    @param width 目标宽度（像素） / Target width in pixels.
    @param height 目标高度（像素） / Target height in pixels.
    @param steps 旧协议采样步数 / Legacy-protocol sampling steps.
    @param seed 可选随机种子 / Optional random seed.
    @return 可 JSON 编码的请求体 / JSON-serializable request payload.
    """

    if model:
        payload: JsonObject = {
            "model": model,
            "prompt": prompt,
            "size": f"{width}x{height}",
        }
        if seed is not None:
            payload["seed"] = seed
        return payload

    item: JsonObject = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "steps": steps,
    }
    if seed is not None:
        item["seed"] = seed
    return {"items": [item]}


def _validated_image_dimensions(
    *, model: str, arguments: JsonObject
) -> tuple[int, int] | JsonObject:
    """@brief 校验并解析图片尺寸 / Validate and resolve image dimensions.

    @param model 图片模型标识 / Image-model identifier.
    @param arguments 已校验或待防御性校验的工具参数 / Validated or defensively checked tool arguments.
    @return 宽高二元组或可回传的校验错误 / Width-height tuple or a model-safe validation error.
    """

    raw_width = arguments.get("width")
    raw_height = arguments.get("height")
    if raw_width is None and raw_height is None:
        return _default_image_dimensions(model)
    if type(raw_width) is not int or type(raw_height) is not int:
        return _image_dimension_error("width and height must be provided together as integers")
    width = raw_width
    height = raw_height
    if not (
        _MIN_IMAGE_DIMENSION <= width <= _MAX_IMAGE_DIMENSION
        and _MIN_IMAGE_DIMENSION <= height <= _MAX_IMAGE_DIMENSION
    ):
        return _image_dimension_error(
            f"width and height must each be between {_MIN_IMAGE_DIMENSION} and "
            f"{_MAX_IMAGE_DIMENSION} pixels"
        )
    minimum_pixels = _minimum_image_pixels(model)
    output_pixels = width * height
    if output_pixels < minimum_pixels:
        return _image_dimension_error(
            f"{model or 'the configured image model'} requires at least "
            f"{minimum_pixels} output pixels; received {width}x{height} "
            f"({output_pixels} pixels)"
        )
    return width, height


def _default_image_dimensions(model: str) -> tuple[int, int]:
    """@brief 返回模型安全默认尺寸 / Return the model-safe default dimensions.

    @param model 图片模型标识 / Image-model identifier.
    @return 默认宽高 / Default width and height.
    """

    if model.strip().casefold() == _SEEDREAM_45_MODEL:
        return _SEEDREAM_45_DEFAULT_DIMENSION, _SEEDREAM_45_DEFAULT_DIMENSION
    return _DEFAULT_IMAGE_DIMENSION, _DEFAULT_IMAGE_DIMENSION


def _minimum_image_pixels(model: str) -> int:
    """@brief 返回模型最小输出像素数 / Return the model's minimum output-pixel count.

    @param model 图片模型标识 / Image-model identifier.
    @return 最小输出像素数 / Minimum output-pixel count.
    """

    if model.strip().casefold() == _SEEDREAM_45_MODEL:
        return _SEEDREAM_45_MIN_OUTPUT_PIXELS
    return _MIN_IMAGE_DIMENSION * _MIN_IMAGE_DIMENSION


def _image_dimension_error(message: str) -> JsonObject:
    """@brief 构造尺寸校验错误 / Build a model-safe dimension-validation error.

    @param message 有界错误说明 / Bounded error explanation.
    @return 可持久化且可回传模型的错误对象 / Persistable model-safe error object.
    """

    return {
        "error": "Image dimensions are invalid",
        "provider_code": "invalid_image_size",
        "provider_message": message[:_MAX_PROVIDER_ERROR_TEXT],
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


def _image_provider_error(status_code: int, content: bytes) -> JsonObject:
    """@brief 保留图片 provider 错误详情 / Preserve image-provider error details.

    @param status_code HTTP 状态码 / HTTP status code.
    @param content provider 返回的有界响应体 / Bounded provider response body.
    @return 包含状态、错误码和诊断文本的 JSON 错误 / JSON error with status, code, and diagnostics.
    @note 只保留有界文本与标量字段，不把任意嵌套 provider payload 原样扩散到模型上下文。/
        Only bounded text and scalar fields are retained; arbitrary nested provider payloads are
        not copied into the model context.
    """

    result: JsonObject = {
        "error": f"Image generation failed (HTTP {status_code})",
        "status": status_code,
    }
    parsed: object | None = None
    try:
        parsed = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    error_value: object = (
        parsed.get("error") if isinstance(parsed, dict) else None
    )
    message = _provider_error_text(error_value)
    if message is None and isinstance(error_value, dict):
        message = _provider_error_text(error_value.get("message"))
        if message is None:
            message = _provider_error_text(error_value.get("detail"))
    if message is None and isinstance(parsed, dict):
        message = _provider_error_text(parsed.get("message"))
    if message is not None:
        result["provider_message"] = message
        result["error"] = f"Image generation failed (HTTP {status_code}): {message}"

    if isinstance(error_value, dict):
        code = _provider_error_scalar(error_value.get("code"))
        if code is not None:
            result["provider_code"] = code
        error_type = _provider_error_text(error_value.get("type"))
        if error_type is not None:
            result["provider_type"] = error_type

    provider_response = _provider_response_preview(parsed, content)
    if provider_response is not None:
        result["provider_response"] = provider_response
    return result


def _provider_error_text(value: object) -> str | None:
    """@brief 提取有界 provider 文本 / Extract bounded provider text.

    @param value 候选字段 / Candidate field.
    @return 清理后的文本或 None / Cleaned text or None.
    """

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:_MAX_PROVIDER_ERROR_TEXT] or None


def _provider_error_scalar(value: object) -> bool | float | int | str | None:
    """@brief 提取 provider 标量错误码 / Extract a scalar provider error code.

    @param value 候选错误码 / Candidate error code.
    @return 可 JSON 编码的标量或 None / JSON-serializable scalar or None.
    """

    if isinstance(value, (str, int, float, bool)):
        return value
    return None


def _provider_response_preview(parsed: object | None, content: bytes) -> str | None:
    """@brief 生成有界 provider 响应预览 / Build a bounded provider-response preview.

    @param parsed 已解析 JSON 或 None / Parsed JSON or None.
    @param content 原始响应体 / Raw response body.
    @return 有界预览或 None / Bounded preview or None.
    """

    if parsed is not None:
        try:
            serialized = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            serialized = ""
        if serialized:
            return serialized[:_MAX_PROVIDER_ERROR_TEXT]
    return content.decode("utf-8", errors="replace").strip()[:_MAX_PROVIDER_ERROR_TEXT] or None


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
