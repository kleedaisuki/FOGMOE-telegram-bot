"""@brief OpenAI-compatible embedding adapter contract 测试 / Contract tests for the OpenAI-compatible embedding adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from aiohttp import web

from fogmoe_bot.application.observability.telemetry import Telemetry, TelemetryBuffer
from fogmoe_bot.domain.observability.signals import SpanSignal
from fogmoe_bot.domain.retrieval import EmbeddingSpace
from fogmoe_bot.infrastructure.retrieval import OpenAICompatibleEmbeddings


def _space() -> EmbeddingSpace:
    """@brief 构造二维本地 contract 空间 / Build a two-dimensional local contract space."""

    return EmbeddingSpace(
        space_id="local.contract.v1",
        model="qwen/qwen3-embedding-8b",
        dimensions=2,
        query_instruction="Retrieve relevant prior conversation evidence.",
        passage_format_version=1,
    )


def test_adapter_formats_qwen_query_and_restores_provider_index_order() -> None:
    """@brief Query instruction 和 response index 均属于强协议 / Query instruction and response indexes are strong contracts."""

    async def scenario() -> None:
        """@brief 运行本地 HTTP contract 场景 / Run a local HTTP contract scenario."""

        requests: list[Mapping[str, object]] = []

        async def embeddings(request: web.Request) -> web.Response:
            """@brief 捕获请求并逆序返回 vectors / Capture a request and return vectors in reverse order."""

            payload = await request.json()
            assert isinstance(payload, Mapping)
            requests.append(payload)
            inputs = payload["input"]
            assert isinstance(inputs, list)
            data = [
                {"index": index, "embedding": [float(index + 1), 1.0]}
                for index in reversed(range(len(inputs)))
            ]
            return web.json_response({"object": "list", "data": data})

        application = web.Application()
        application.router.add_post("/v1/embeddings", embeddings)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = getattr(site._server, "sockets", None)
        assert sockets
        port = sockets[0].getsockname()[1]
        telemetry_buffer = TelemetryBuffer(16)
        client = OpenAICompatibleEmbeddings(
            api_key="test-key",
            api_base=f"http://127.0.0.1:{port}/v1",
            timeout_seconds=2.0,
            telemetry=Telemetry(telemetry_buffer),
        )
        try:
            documents = await client.embed_documents(("alpha", "beta"), space=_space())
            query = await client.embed_query("tea preference", space=_space())
        finally:
            await client.aclose()
            await runner.cleanup()
        assert [vector.values for vector in documents] == [(1.0, 1.0), (2.0, 1.0)]
        assert query.values == (1.0, 1.0)
        assert requests[0] == {
            "model": "qwen/qwen3-embedding-8b",
            "input": ["alpha", "beta"],
            "dimensions": 2,
            "encoding_format": "float",
            "input_type": "search_document",
        }
        assert requests[1]["input"] == [
            "Instruct: Retrieve relevant prior conversation evidence.\n"
            "Query: tea preference"
        ]
        assert requests[1]["input_type"] == "search_query"
        spans = tuple(
            signal
            for signal in telemetry_buffer.drain(16)
            if isinstance(signal, SpanSignal)
        )
        assert [span.name for span in spans] == [
            "retrieval.embedding.request",
            "retrieval.embedding.request",
        ]
        assert spans[0].attributes["retrieval.batch.size"] == 2
        assert spans[0].attributes["http.response.status_code"] == 200

    asyncio.run(scenario())
