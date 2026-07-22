"""@brief 模型上下文格式化工具 / Model context formatting utilities."""

from typing import Iterable

from fogmoe_bot.domain.user_profile.models import UserProfileSnapshot


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
    "username",
    "user_id",
    "thread_id",
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
    profile: UserProfileSnapshot | None,
    personal_info: str = "",
    diary_exists: bool = False,
    user_id: int | None = None,
    username: str | None = None,
    display_name: str = "",
) -> str:
    """@brief 格式化模型可见的用户状态 / Format model-visible user state.

    @param user_coins 用户硬币余额 / User coin balance.
    @param user_plan 用户订阅计划 / User subscription plan.
    @param user_permission 用户权限等级 / User permission level.
    @param profile 冻结的用户画像 / Frozen user profile.
    @param personal_info 用户自定义个人信息 / User-provided personal information.
    @param diary_exists 是否存在用户日记 / Whether the user diary exists.
    @param user_id Telegram 用户 ID / Telegram user identifier.
    @param username Telegram username / Telegram username.
    @param display_name Telegram 显示名 / Telegram display name.
    @return 用户身份与状态提示词 / User identity and state prompt.
    """

    permission_labels = {
        0: "Normal",
        1: "Advanced",
        2: "Premium",
        3: "Ultimate",
    }
    permission_label = permission_labels.get(user_permission, "Unknown")
    identity_attrs = [
        ("display_name", display_name),
        ("username", username),
        ("user_id", str(user_id) if user_id is not None else None),
    ]
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
    identity_attr_text = " ".join(
        f'{key}="{xml_escape(value)}"' for key, value in identity_attrs if value
    )
    lines = []
    if identity_attr_text:
        lines.append(
            f'<user_identity trust="trusted_platform_metadata" {identity_attr_text} />'
        )
    lines.append(f"<user_state {attr_text} />")
    if profile is not None or personal_info:
        revision_attr = f' revision="{profile.revision}"' if profile is not None else ""
        lines.append(f'<user_profile trust="untrusted_derived_data"{revision_attr}>')
        if profile is not None:
            for claim in profile.document.claims:
                lines.append(
                    "  <claim "
                    f'key="{xml_escape(claim.key)}" '
                    f'kind="{claim.kind.value}" '
                    f'confidence="{claim.confidence.value}" '
                    f'observed_at="{claim.observed_at.isoformat()}">'
                    f"{xml_escape(claim.statement)}</claim>"
                )
        if personal_info:
            lines.append(
                f"  <personal_info>{xml_escape(personal_info)}</personal_info>"
            )
        lines.append("</user_profile>")
    return "\n".join(lines)
