"""@brief 根配置模板兼容性测试 / Root configuration-template compatibility tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from fogmoe_bot import config as bot_config
from fogmoe_dashboard import config as dashboard_config
from fogmoe_dbctl import config as dbctl_config


#: @brief 版本控制的用户配置模板 / Version-controlled operator configuration template.
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "example.config.json"

#: @brief 三个配置边界的公开 reader 与错误类型 / Public readers and error types for all boundaries.
READERS: tuple[tuple[Callable[[Path | None], object], type[ValueError]], ...] = (
    (bot_config.read_bot_settings, bot_config.ConfigurationError),
    (dbctl_config.read_dbctl_settings, dbctl_config.ConfigurationError),
    (dashboard_config.read_dashboard_settings, dashboard_config.ConfigurationError),
)


def test_example_config_is_accepted_by_each_configuration_boundary() -> None:
    """@brief 三个程序各自能读取同一用户模板 / Each program reads the same operator template.

    @return None / None.
    @note 本测试只调用公开 reader，不直接依赖 JSONC parser。/
        This test calls only public readers and does not depend on the JSONC parser directly.
    """

    bot = bot_config.read_bot_settings(EXAMPLE_CONFIG_PATH)
    dbctl = dbctl_config.read_dbctl_settings(EXAMPLE_CONFIG_PATH)
    dashboard = dashboard_config.read_dashboard_settings(EXAMPLE_CONFIG_PATH)

    assert bot.database.endpoint.name == dbctl.endpoint.name
    assert bot.database.endpoint.name == dashboard.endpoint.name
    assert bot.database.application.username == dbctl.application.username


def test_example_config_explicitly_declares_every_owned_default() -> None:
    """@brief 模板逐字段固定三个 reader 的实际默认值 / Template pins every reader's actual default.

    @return None / None.
    @note 比较原始投影而非已验证实例，以便模板遗漏一个带默认值的字段时也能失败。/
        Compare raw projections rather than validated instances so omitting a field with
        a model default still fails the contract test.
    """

    document = bot_config._load_jsonc(EXAMPLE_CONFIG_PATH)

    assert bot_config._bot_payload(document) == bot_config.BotSettings().model_dump(
        mode="json"
    )
    assert dbctl_config._dbctl_payload(
        document
    ) == dbctl_config.DbctlSettings().model_dump(mode="json")
    assert dashboard_config._dashboard_payload(
        document
    ) == dashboard_config.DashboardSettings().model_dump(mode="json")


@pytest.mark.parametrize("reader,error_type", READERS)
@pytest.mark.parametrize("version", ("1.0", "true", '"1"'))
def test_readers_reject_non_integer_schema_versions(
    reader: Callable[[Path | None], object],
    error_type: type[ValueError],
    version: str,
    tmp_path: Path,
) -> None:
    """@brief 根版本必须是精确整数 / The root version must be an exact integer.

    @param reader 待验证的公开配置 reader / Public configuration reader under test.
    @param error_type reader 的公开异常类型 / Public exception type exposed by the reader.
    @param version 替换进模板的非整数字面量 / Non-integer literal substituted into the template.
    @param tmp_path pytest 提供的隔离临时目录 / Isolated temporary directory supplied by pytest.
    @return None / None.
    """

    invalid_config = tmp_path / "config.json"
    invalid_config.write_text(
        EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8").replace(
            '"schema_version": 1,',
            f'"schema_version": {version},',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(error_type, match="schema_version"):
        reader(invalid_config)


@pytest.mark.parametrize(
    "settings_type",
    (
        bot_config.InferenceRuntimeSettings,
        bot_config.OutboxRuntimeSettings,
        bot_config.CompactionRuntimeSettings,
        bot_config.DreamingRuntimeSettings,
    ),
)
def test_runtime_settings_require_lease_strictly_longer_than_attempt(
    settings_type: Callable[..., object],
) -> None:
    """@brief 配置层提前拒绝与 worker 不变量冲突的相等 timeout/lease / Configuration rejects equal timeout and lease before violating worker invariants.

    @param settings_type 待构造的 runtime 设置类型 / Runtime settings type to construct.
    @return None / None.
    """

    with pytest.raises(ValueError, match="lease_seconds must be >"):
        settings_type(lease_seconds=60, attempt_timeout_seconds=60)


def test_working_memory_resilience_defaults_and_positive_bounds() -> None:
    """@brief WorkingMemory 在线召回有独立 deadline 与冷却边界 / WorkingMemory online recall has independent deadline and cooldown bounds."""

    settings = bot_config.WorkingMemorySettings()
    assert settings.timeout_seconds == 5.0
    assert settings.failure_cooldown_seconds == 60.0

    with pytest.raises(ValueError, match="greater than 0"):
        bot_config.WorkingMemorySettings(timeout_seconds=0.0)
    with pytest.raises(ValueError, match="greater than 0"):
        bot_config.WorkingMemorySettings(failure_cooldown_seconds=0.0)


def test_runtime_shutdown_grace_covers_serial_durable_drain_phases() -> None:
    """@brief shutdown grace 严格覆盖 phase 10、20、30 的超时下界 / Shutdown grace strictly covers the phase-10/20/30 timeout lower bound."""

    settings = bot_config.RuntimeSettings()
    assert settings.mailbox.shutdown_grace_seconds == 180.0

    with pytest.raises(ValueError, match="serialized durable drain lower bound"):
        bot_config.RuntimeSettings(
            mailbox=bot_config.MailboxRuntimeSettings(shutdown_grace_seconds=155.0)
        )
    valid = bot_config.RuntimeSettings(
        mailbox=bot_config.MailboxRuntimeSettings(shutdown_grace_seconds=156.0)
    )
    assert valid.mailbox.shutdown_grace_seconds == 156.0
