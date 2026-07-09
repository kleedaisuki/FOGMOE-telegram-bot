import asyncio

from fogmoe_bot.infrastructure.database import connection as db_connection


def test_decode_json_list_accepts_postgresql_jsonb_list():
    """@brief JSONB 原生列表可直接使用 / JSONB native list is accepted directly."""

    messages = [{"role": "user", "content": "hello"}]

    assert db_connection._decode_json_list(messages) == messages


def test_decode_json_list_accepts_legacy_json_text():
    """@brief 旧 JSON 文本仍可解析 / Legacy JSON text is still parsed."""

    assert db_connection._decode_json_list('[{"role": "user", "content": "hello"}]') == [
        {"role": "user", "content": "hello"}
    ]


def test_get_chat_history_accepts_postgresql_jsonb_list(monkeypatch):
    """@brief 聊天历史读取接受 JSONB 列表 / Chat history accepts a JSONB list."""

    messages = [{"role": "user", "content": "hello"}]

    async def fake_fetch_one(*args, **kwargs):
        return (messages,)

    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)

    assert asyncio.run(db_connection.get_chat_history(123)) == messages


def test_update_latest_history_state_summary_accepts_postgresql_jsonb_list(monkeypatch):
    """@brief 更新历史状态摘要接受 JSONB 列表 / Updating history summary accepts a JSONB list."""

    messages = [
        {
            "role": "user",
            "content": (
                '<metadata type="system" origin="history_state" '
                'history_state="compressed">\n</metadata>'
            ),
        }
    ]
    calls = []

    async def fake_fetch_one(*args, **kwargs):
        return (messages,)

    async def fake_execute(sql, params):
        calls.append((sql, params))
        return 1

    monkeypatch.setattr(db_connection, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(db_connection, "execute", fake_execute)

    updated = asyncio.run(
        db_connection.async_update_latest_history_state_summary(123, "summary")
    )

    assert updated is True
    assert calls
    assert "<summary>summary</summary>" in calls[0][1][0]
