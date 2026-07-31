"""@brief Admin announcement DDD 静态边界测试 / Admin announcement DDD static-boundary tests."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""@brief 项目根目录 / Project root."""

SRC_ROOT = PROJECT_ROOT / "src" / "fogmoe_bot"
"""@brief Python 包根目录 / Python package root."""


def test_announcement_recipient_lifecycle_is_domain_owned() -> None:
    """@brief recipient 状态、能力与决策只由领域拥有 / Recipient states, capability, and decisions are domain-owned."""

    recipient_path = SRC_ROOT / "domain" / "admin" / "recipient.py"
    state_path = SRC_ROOT / "domain" / "admin" / "recipient_state.py"
    recipient_source = recipient_path.read_text(encoding="utf-8")
    state_source = state_path.read_text(encoding="utf-8")
    domain_source = recipient_source + state_source
    announcement_source = (
        SRC_ROOT / "domain" / "admin" / "announcement.py"
    ).read_text(
        encoding="utf-8"
    )
    runtime_source = (SRC_ROOT / "application" / "admin" / "runtime.py").read_text(
        encoding="utf-8"
    )
    adapter_source = (
        SRC_ROOT / "infrastructure" / "admin" / "announcements.py"
    ).read_text(encoding="utf-8")

    for symbol in (
        "class AnnouncementRecipient:",
        "type AnnouncementRecipientState =",
        "class BlockedAnnouncementRecipient:",
        "class ProcessingAnnouncementRecipient:",
        "class AnnouncementRecipientClaim:",
        "class AnnouncementRecipientExpanded:",
        "class AnnouncementRecipientRetryScheduled:",
        "class AnnouncementRecipientDeadLettered:",
        "class AnnouncementCompletionReleased:",
        "class AnnouncementRecipientLeaseRecovered:",
    ):
        assert symbol in domain_source
    assert "fogmoe_bot.application" not in domain_source
    assert "fogmoe_bot.infrastructure" not in domain_source
    assert "fogmoe_bot.application" not in announcement_source
    assert not (SRC_ROOT / "domain" / "admin" / "models.py").exists()
    assert len(recipient_source.splitlines()) < 1_000
    assert len(state_source.splitlines()) < 1_000
    assert "status = 'retry_wait'" not in runtime_source
    assert "status = 'failed_final'" not in runtime_source
    assert "mark_expanded" not in runtime_source
    assert "schedule_retry" not in runtime_source
    assert "mark_failed_final" not in runtime_source
    assert "persist_expanded" in adapter_source
    assert "persist_retry" in adapter_source
    assert "persist_dead_letter" in adapter_source
    assert "blocked.release_completion(" in adapter_source
    assert "processing.recover_expired(" in adapter_source
    assert "recipient.claim(" in adapter_source
    assert "decision.apply_to(pre_state)" in adapter_source


def test_recipient_decisions_are_sealed_without_forwarding_getters() -> None:
    """@brief aggregate、claim 与 decision 使用公开只读字段而非 Java 式 getter / Aggregate, claim, and decisions expose readonly fields without Java-style forwarding getters."""

    recipient_source = (
        SRC_ROOT / "domain" / "admin" / "recipient.py"
    ).read_text(encoding="utf-8")
    state_source = (
        SRC_ROOT / "domain" / "admin" / "recipient_state.py"
    ).read_text(encoding="utf-8")

    assert recipient_source.count("@dataclass(frozen=True, slots=True, init=False)") >= 7
    for backing_field in (
        "self._recipient",
        "self._content",
        "self._capability",
        "self._announcement_id",
        "self._attempt_count",
    ):
        assert backing_field not in recipient_source
    for forwarding_getter in (
        "def announcement_id(self)",
        "def attempt_count(self)",
        "def body(self)",
        "def token(self)",
    ):
        assert forwarding_getter not in recipient_source
    assert "class AnnouncementRecipientClaim:" in recipient_source
    assert "recipient: AnnouncementRecipient" in recipient_source
    assert "content: AnnouncementDispatchContent" in recipient_source
    assert "capability: AnnouncementClaimCapability" in recipient_source
    assert "status: ClassVar[AnnouncementRecipientStatus]" in state_source


def test_announcement_recipient_cas_uses_real_schema_authority() -> None:
    """@brief announcement recipient CAS 只依赖 identity/status/token / Announcement recipient CAS uses only identity, status, and token."""

    adapter_source = (
        SRC_ROOT / "infrastructure" / "admin" / "announcements.py"
    ).read_text(encoding="utf-8")

    assert "AND status = 'processing' AND claim_token = CAST(%s AS UUID)" in (
        adapter_source
    )
    assert "recipient.version" not in adapter_source
    assert "version = version + 1" not in adapter_source
    assert "FOR UPDATE OF recipient SKIP LOCKED" in adapter_source
    assert 'f"RETURNING {_RECIPIENT_COLUMNS}"' in adapter_source
    assert "FOR UPDATE" in adapter_source
