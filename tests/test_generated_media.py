from fogmoe_bot.infrastructure.assistant.generated_media import _image_request_payload


def test_openrouter_image_payload_maps_dimensions_to_supported_parameters():
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
        "resolution": "2K",
        "aspect_ratio": "16:9",
        "seed": 42,
    }


def test_legacy_image_payload_is_unchanged_without_an_openrouter_model():
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
