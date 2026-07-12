"""Assistant 外部读取 HTTP adapter / Assistant external-read HTTP adapter."""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from typing import cast
from urllib.parse import quote

import requests

from fogmoe_bot.application.assistant.tool_runtime import ToolEffectRequest
from fogmoe_bot.application.observability.telemetry import Telemetry
from fogmoe_bot.domain.conversation.payloads import JsonObject, JsonValue
from fogmoe_bot.domain.observability.conventions import MetricName, Outcome
from fogmoe_bot.domain.observability.signals import SpanKind, SpanStatus
from fogmoe_bot.infrastructure.blocking import AsyncBlockingBulkhead
from fogmoe_bot.infrastructure.network.proxy import create_requests_session

from .requests_response import read_limited_response


_MAX_TEXT_RESPONSE = 2 * 1024 * 1024
"""@brief 文本 HTTP 响应字节上限 / Text-HTTP response byte limit."""


@dataclass(frozen=True, slots=True)
class ExternalReadSettings:
    """@brief 外部读取配置 / External-read settings."""

    serpapi_key: str
    judge0_url: str
    judge0_key: str
    timeout_seconds: int = 10


class RequestsExternalReadTools:
    """@brief 使用一次性 requests session 的有界读取 adapter / Bounded read adapter using per-call requests sessions."""

    def __init__(
        self,
        settings: ExternalReadSettings,
        *,
        bulkhead: AsyncBlockingBulkhead,
        telemetry: Telemetry,
    ) -> None:
        """@brief 保存不可变配置 / Store immutable settings.

        @param settings 外部读取配置 / External-read settings.
        @param bulkhead 专用同步调用隔舱 / Dedicated blocking-call bulkhead.
        """

        if settings.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._settings = settings
        self._bulkhead = bulkhead
        self._telemetry = telemetry

    async def execute(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 在线程边界执行一个读取 / Execute one read behind a thread boundary.

        @param request 工具请求 / Tool request.
        @return JSON 结果 / JSON result.
        """

        with self._telemetry.span(
            "external.read",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.tool.name": request.tool_name,
                "fogmoe.dependency.name": _dependency_name(request.tool_name),
            },
        ) as span:
            try:
                result = await self._bulkhead.call(lambda: self._execute_sync(request))
            except Exception:
                self._telemetry.counter(
                    MetricName.DEPENDENCY_OUTCOMES,
                    attributes={
                        "outcome": Outcome.FAILURE,
                        "gen_ai.tool.name": request.tool_name,
                        "fogmoe.dependency.name": _dependency_name(request.tool_name),
                    },
                )
                raise
            if isinstance(result, dict) and "error" in result:
                span.set_status(SpanStatus.ERROR, str(result["error"]))
                span.set_attribute("error.type", "external_dependency_error")
                outcome = Outcome.FAILURE
            else:
                outcome = Outcome.SUCCESS
            self._telemetry.counter(
                MetricName.DEPENDENCY_OUTCOMES,
                attributes={
                    "outcome": outcome,
                    "gen_ai.tool.name": request.tool_name,
                    "fogmoe.dependency.name": _dependency_name(request.tool_name),
                },
            )
            return result

    def _execute_sync(self, request: ToolEffectRequest) -> JsonValue:
        """@brief 执行同步请求 / Execute a synchronous request.

        @param request 工具请求 / Tool request.
        @return JSON 结果 / JSON result.
        """

        if request.tool_name == "google_search":
            return self._search(request.arguments)
        if request.tool_name == "fetch_url":
            return self._fetch_url(request.arguments)
        if request.tool_name == "execute_python_code":
            return self._execute_python(request.arguments)
        return {"error": f"Unsupported external read tool: {request.tool_name}"}

    def _search(self, arguments: JsonObject) -> JsonValue:
        """@brief 调用 SerpApi / Call SerpApi.

        @param arguments 已校验参数 / Validated arguments.
        @return JSON 结果 / JSON result.
        """

        if not self._settings.serpapi_key:
            return {"error": "SerpApi key is not configured"}
        with create_requests_session() as session:
            try:
                response = session.get(
                    "https://serpapi.com/search",
                    params={
                        "engine": (
                            "google" if arguments.get("detailed") else "google_light"
                        ),
                        "q": str(arguments["query"]),
                        "api_key": self._settings.serpapi_key,
                    },
                    timeout=self._settings.timeout_seconds,
                )
                response.raise_for_status()
                data = cast(object, response.json())
            except (requests.RequestException, ValueError) as error:
                return {"error": f"SerpApi request failed: {error}"}
        if arguments.get("show_full_json"):
            safe = deepcopy(data)
            if isinstance(safe, dict):
                safe.pop("api_key", None)
                params = safe.get("search_parameters")
                if isinstance(params, dict):
                    params.pop("api_key", None)
            return cast(JsonValue, safe)
        if not isinstance(data, dict):
            return {"error": "SerpApi returned an invalid response"}
        results: list[JsonValue] = []
        raw_results = data.get("organic_results")
        if isinstance(raw_results, list):
            for index, item in enumerate(raw_results[:20], start=1):
                if not isinstance(item, dict):
                    continue
                results.append(
                    {
                        "rank": int(item.get("position") or index),
                        "title": str(item.get("title") or ""),
                        "url": str(item.get("link") or ""),
                        "snippet": str(item.get("snippet") or ""),
                    }
                )
        return {"query": str(arguments["query"]), "results": results}

    def _fetch_url(self, arguments: JsonObject) -> JsonValue:
        """@brief 经 Jina Reader 获取有界网页 / Fetch a bounded page through Jina Reader.

        @param arguments 已校验参数 / Validated arguments.
        @return JSON 结果 / JSON result.
        """

        original = str(arguments["url"]).strip()
        normalized = (
            original
            if original.startswith(("http://", "https://"))
            else f"https://{original}"
        )
        safe_url_characters = ":/?&=#[]@!$&'()*+,;"
        target = f"https://r.jina.ai/{quote(normalized, safe=safe_url_characters)}"
        with create_requests_session() as session:
            try:
                response = session.get(
                    target,
                    timeout=self._settings.timeout_seconds,
                    stream=True,
                )
                content = read_limited_response(response, _MAX_TEXT_RESPONSE)
            except (requests.RequestException, ValueError) as error:
                return {"error": f"Failed to fetch URL: {error}"}
        text = content.decode(errors="replace")
        if response.status_code >= 400:
            return {
                "error": "Upstream fetch failed",
                "status_code": response.status_code,
                "details": text[:500],
            }
        return {
            "url": normalized,
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type"),
            "content": text,
        }

    def _execute_python(self, arguments: JsonObject) -> JsonValue:
        """@brief 调用 Judge0 / Call Judge0.

        @param arguments 已校验参数 / Validated arguments.
        @return JSON 结果 / JSON result.
        """

        base_url = self._settings.judge0_url.strip()
        if not base_url:
            return {"error": "Judge0 API URL is not configured"}
        source = str(arguments["source_code"])
        payload: JsonObject = {
            "language_id": 71,
            "source_code": base64.b64encode(source.encode()).decode(),
        }
        stdin = arguments.get("stdin")
        if isinstance(stdin, str):
            payload["stdin"] = base64.b64encode(stdin.encode()).decode()
        headers = {"Content-Type": "application/json"}
        if self._settings.judge0_key:
            headers["X-Auth-Token"] = self._settings.judge0_key
        with create_requests_session() as session:
            try:
                response = session.post(
                    f"{base_url.rstrip('/')}/submissions?base64_encoded=true&wait=true",
                    json=payload,
                    headers=headers,
                    timeout=self._settings.timeout_seconds,
                )
                response.raise_for_status()
                result = response.json()
            except (requests.RequestException, ValueError) as error:
                return {"error": f"Judge0 request failed: {error}"}
        if not isinstance(result, dict):
            return {"error": "Judge0 returned an invalid response"}
        raw_status = result.get("status")
        status: dict[object, object] = (
            raw_status if isinstance(raw_status, dict) else {}
        )
        return {
            "status_id": cast(JsonValue, status.get("id")),
            "status_description": cast(JsonValue, status.get("description")),
            "stdout": _decode_base64(result.get("stdout")),
            "stderr": _decode_base64(result.get("stderr")),
            "compile_output": _decode_base64(result.get("compile_output")),
            "time": cast(JsonValue, result.get("time")),
            "memory": cast(JsonValue, result.get("memory")),
        }


def _decode_base64(value: object) -> str:
    """@brief 解码 Judge0 字段 / Decode a Judge0 field.

    @param value base64 值 / Base64 value.
    @return 文本 / Text.
    """

    if not isinstance(value, str) or not value:
        return ""
    try:
        return base64.b64decode(value).decode(errors="replace")
    except ValueError:
        return value


def _dependency_name(tool_name: str) -> str:
    """@brief 映射工具到稳定依赖名称 / Map a tool to a stable dependency name.

    @param tool_name 工具目录名称 / Tool-catalog name.
    @return 低基数依赖标识 / Low-cardinality dependency identifier.
    """

    return {
        "google_search": "serpapi",
        "fetch_url": "jina_reader",
        "execute_python_code": "judge0",
    }.get(tool_name, "unknown")
