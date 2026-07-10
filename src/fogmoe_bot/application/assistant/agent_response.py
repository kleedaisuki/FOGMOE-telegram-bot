"""@brief Agent 响应契约 / Agent response contract."""

from typing import NamedTuple

from fogmoe_bot.domain.agent_runtime.events import RuntimeEvent


class AgentResponse(NamedTuple):
    """@brief 一次 Agent 调用的最终响应 / Final response from one Agent invocation.

    @param text 可直接发送的文本；空字符串表示已由可见输出端口发送 /
    Text ready to send; empty when a visible output sink already sent it.
    @param events Runtime 产生、需要审计或持久化的事件 / Runtime events to audit or persist.
    """

    text: str
    events: list[RuntimeEvent]
