import json

from fogmoe_bot.infrastructure.assistant.generated_media import (
    _image_provider_error,
    _image_request_payload,
    _validated_image_dimensions,
)


def test_openrouter_image_payload_preserves_bot_selected_dimensions() -> None:
    """@brief OpenRouter payload 保留 bot 尺寸 / Preserve bot-selected dimensions in the OpenRouter payload."""

    payload = _image_request_payload(
        model="bytedance-seed/seedream-4.5",
        prompt="a misty mountain lake",
        width=1920,
        height=1080,
        steps=9,
        seed=42,
    )

    assert payload == {
        "model": "bytedance-seed/seedream-4.5",
        "prompt": "a misty mountain lake",
        "size": "1920x1080",
        "seed": 42,
    }


def test_legacy_image_payload_is_unchanged_without_an_openrouter_model() -> None:
    """@brief legacy provider 载荷保持兼容 / Keep the legacy-provider payload compatible."""

    payload = _image_request_payload(
        model="",
        prompt="a misty mountain lake",
        width=1024,
        height=1024,
        steps=9,
        seed=None,
    )

    assert payload == {
        "items": [
            {
                "prompt": "a misty mountain lake",
                "width": 1024,
                "height": 1024,
                "steps": 9,
            }
        ]
    }


def test_seedream_uses_a_safe_default_when_bot_omits_dimensions() -> None:
    """@brief Seedream 缺省尺寸满足模型下限 / Seedream's omitted dimensions satisfy its minimum."""

    assert _validated_image_dimensions(
        model="bytedance-seed/seedream-4.5",
        arguments={"prompt": "Klee"},
    ) == (2048, 2048)


def test_seedream_rejects_dimensions_below_model_pixel_limit() -> None:
    """@brief 在请求前拒绝过小尺寸 / Reject undersized images before the request."""

    result = _validated_image_dimensions(
        model="bytedance-seed/seedream-4.5",
        arguments={"prompt": "Klee", "width": 1024, "height": 1024},
    )

    assert result == {
        "error": "Image dimensions are invalid",
        "provider_code": "invalid_image_size",
        "provider_message": (
            "bytedance-seed/seedream-4.5 requires at least 3686400 output pixels; "
            "received 1024x1024 (1048576 pixels)"
        ),
    }


def test_provider_error_keeps_status_code_message_and_bounded_response() -> None:
    """@brief provider 400 诊断信息完整保留 / Preserve provider 400 diagnostics."""

    content = json.dumps(
        {
            "error": {
                "code": 400,
                "type": "invalid_request",
                "message": "use a larger resolution",
            }
        }
    ).encode()

    result = _image_provider_error(400, content)

    assert result["status"] == 400
    assert result["provider_code"] == 400
    assert result["provider_type"] == "invalid_request"
    assert result["provider_message"] == "use a larger resolution"
    assert json.loads(str(result["provider_response"])) == json.loads(content)
    assert result["error"] == "Image generation failed (HTTP 400): use a larger resolution"
