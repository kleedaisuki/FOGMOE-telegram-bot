NO_RESPONSE_SENTINELS = frozenset(
    {
        "[no_response]",
    }
)


def normalize_ai_reply_text(value: object) -> str:
    text = str(value or "")
    if text.strip().lower() in NO_RESPONSE_SENTINELS:
        return ""
    return text
