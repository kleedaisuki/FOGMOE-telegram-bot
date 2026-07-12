"""@brief 模型上下文格式化工具 / Model context formatting utilities."""

from typing import Iterable


def xml_escape(value: object | None) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def join_prompt_sections(*sections: object) -> str:
    """@brief 组合提示词段落 / Join prompt sections.

    @param sections 按优先级排列的提示词段落 / Prompt sections ordered by priority.
    @return 以空行分隔、且不含首尾空白的提示词 / Prompt with blank-line separators and no outer whitespace.
    @note 空段落会被忽略，以避免依赖资源文件是否自带换行。
    / Empty sections are ignored so callers do not depend on resource-file trailing newlines.
    """

    return "\n\n".join(
        text for section in sections if (text := str(section or "").strip())
    )


_METADATA_ATTR_ORDER = (
    "type",
    "title",
    "timestamp",
    "user",
    "origin",
    "history_state",
    "scheduled_at",
    "scheduled_for",
)


def format_metadata_attrs(attrs: Iterable[tuple[str, str | None]]) -> str:
    order = {key: idx for idx, key in enumerate(_METADATA_ATTR_ORDER)}
    ordered = sorted(attrs, key=lambda item: (order.get(item[0], len(order)), item[0]))
    return " ".join(f'{key}="{xml_escape(value)}"' for key, value in ordered if value)


def format_user_state_prompt(
    *,
    user_coins: int,
    user_plan: str,
    user_permission: int,
    impression: str,
    personal_info: str = "",
    diary_exists: bool = False,
) -> str:
    permission_labels = {
        0: "Normal",
        1: "Advanced",
        2: "Premium",
        3: "Ultimate",
    }
    permission_label = permission_labels.get(user_permission, "Unknown")
    attrs = [
        ("coins", str(user_coins)),
        ("user_plan", user_plan),
        ("permission", str(user_permission)),
        ("permission_label", permission_label),
        ("diary_exists", "true" if diary_exists else "false"),
    ]
    attr_text = " ".join(
        f'{key}="{xml_escape(value)}"' for key, value in attrs if value
    )
    lines = [f"<user_state {attr_text} />"]
    if impression or personal_info:
        lines.append("<user_profile>")
        if impression:
            lines.append(f"  <impression>{xml_escape(impression)}</impression>")
        if personal_info:
            lines.append(
                f"  <personal_info>{xml_escape(personal_info)}</personal_info>"
            )
        lines.append("</user_profile>")
    return "\n".join(lines)
