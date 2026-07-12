"""Assistant requests 响应读取边界 / Assistant requests-response read boundary."""

import requests


def read_limited_response(response: requests.Response, max_bytes: int) -> bytes:
    """@brief 读取有界 HTTP body / Read a bounded HTTP body.

    @param response requests response / Requests response.
    @param max_bytes 字节上限 / Byte limit.
    @return body / Body.
    """

    declared = response.headers.get("Content-Length")
    if declared and int(declared) > max_bytes:
        raise ValueError(f"response exceeds {max_bytes} bytes")
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(64 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise ValueError(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
