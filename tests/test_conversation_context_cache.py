from fogmoe_bot.application.assistant import conversation_context_cache
from fogmoe_bot.domain.context import ContextState, ConversationScope, UserState


def _context() -> ContextState:
    """@brief 创建最小会话状态 / Create a minimal conversation state.

    @return 供缓存测试使用的状态 / State for cache tests.
    """

    return ContextState(
        scope=ConversationScope(user_id=7),
        user_state=UserState(
            coins=10,
            plan="free",
            permission=0,
            impression="Not recorded",
        ),
        messages=[{"role": "system", "content": "system"}],
        tool_context={"user_id": 7},
    )


def test_cache_admits_without_evicting_existing_entry_when_full():
    """@brief 满容量时仅拒绝新会话 / Reject new context without eviction at capacity."""

    cache = conversation_context_cache.ConversationContextCache(capacity=1, ttl_seconds=60)
    first = _context()

    assert cache.commit(7, first)
    assert not cache.commit(8, _context())
    assert cache.get(7) is first
    assert cache.get(8) is None


def test_cache_expires_on_read_and_purge(monkeypatch):
    """@brief 读取立即判定 TTL，维护清理回收其余项 / Check TTL on read and purge remaining entries."""

    now = 100.0
    monkeypatch.setattr(conversation_context_cache.time, "monotonic", lambda: now)
    cache = conversation_context_cache.ConversationContextCache(capacity=2, ttl_seconds=10)
    cache.commit(7, _context())
    cache.commit(8, _context())

    now = 111.0
    assert cache.get(7) is None
    assert cache.purge_expired() == 1
    assert cache.size == 0


def test_context_turn_restores_canonical_message_after_runtime_replacement():
    """@brief 多模态临时替换不会进入缓存状态 / Runtime multimodal replacement never remains cached."""

    context = _context()
    context.append_persisted_records([("user", "persisted image description")])
    context.refresh_turn(
        system_prompt="policy",
        scope=ConversationScope(user_id=7, message_id=11),
        user_state=context.user_state,
        runtime_replacements=[],
    )
    committed_count = len(context.messages)
    context.messages[-1] = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}],
    }
    context.text_fallback_messages = [
        *context.messages[:-1],
        {"role": "user", "content": "persisted image description"},
    ]
    context.messages.append({"role": "tool", "content": "transient"})

    context.discard_runtime_messages(committed_message_count=committed_count)

    assert context.text_fallback_messages is None
    assert context.messages[-1]["content"] == "persisted image description"
