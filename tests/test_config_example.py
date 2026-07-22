"""@brief 根配置模板兼容性测试 / Root configuration-template compatibility tests."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from fogmoe_bot import config as bot_config
from fogmoe_config.jsonc import load_jsonc
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

    document = load_jsonc(EXAMPLE_CONFIG_PATH)

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


@pytest.mark.parametrize("reader,error_type", READERS)
def test_readers_map_jsonc_errors_to_their_boundary_error(
    reader: Callable[[Path | None], object],
    error_type: type[ValueError],
    tmp_path: Path,
) -> None:
    """@brief 每个 reader 将共享解码错误映射为自己的边界错误 / Each reader maps shared decoding errors to its boundary error.

    @param reader 待验证的公开配置 reader / Public configuration reader under test.
    @param error_type reader 的公开异常类型 / Public exception type exposed by the reader.
    @param tmp_path pytest 提供的隔离临时目录 / Isolated temporary directory supplied by pytest.
    @return None / None.
    """

    invalid_config = tmp_path / "config.json"
    invalid_config.write_text(
        '{"schema_version": 1, "schema_version": 1}',
        encoding="utf-8",
    )

    with pytest.raises(error_type, match="duplicate object key 'schema_version'"):
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


@pytest.mark.parametrize(
    ("settings_type", "expected_maximum"),
    (
        (bot_config.InboxRuntimeSettings, 0.5),
        (bot_config.InferenceRuntimeSettings, 0.5),
        (bot_config.OutboxRuntimeSettings, 0.5),
        (bot_config.CompactionRuntimeSettings, 5.0),
        (bot_config.DreamingRuntimeSettings, 5.0),
        (bot_config.RetrievalWorkerSettings, 2.0),
    ),
)
def test_adaptive_polling_settings_bound_idle_database_traffic(
    settings_type: Callable[..., object],
    expected_maximum: float,
) -> None:
    """@brief 六个高频 worker 显式约束空闲轮询上限 / Six high-frequency workers explicitly bound their idle-polling caps.

    @param settings_type 待验证设置类型 / Settings type under test.
    @param expected_maximum 生产默认上限 / Production default cap.
    @return None / None.
    """

    settings = settings_type()
    assert getattr(settings, "max_poll_interval_seconds") == expected_maximum
    with pytest.raises(ValueError, match="must be >= poll_interval_seconds"):
        settings_type(
            poll_interval_seconds=1.0,
            max_poll_interval_seconds=0.5,
        )


def test_retrieval_embedding_timeout_must_precede_vector_lease() -> None:
    """@brief 后台 embedding HTTP deadline 必须严格短于 fencing lease / The background embedding HTTP deadline must be strictly shorter than its fencing lease.

    @return None / None.
    """

    settings = bot_config.RetrievalSettings()
    assert settings.embedding.timeout_seconds < settings.worker.lease_seconds

    with pytest.raises(
        ValueError,
        match=r"embedding\.timeout_seconds must be < worker\.lease_seconds",
    ):
        bot_config.RetrievalSettings(
            worker=bot_config.RetrievalWorkerSettings(lease_seconds=30),
            embedding=bot_config.RetrievalEmbeddingSettings(timeout_seconds=30.0),
        )


@pytest.mark.parametrize(
    "settings_type",
    (
        bot_config.InferenceRuntimeSettings,
        bot_config.CompactionRuntimeSettings,
        bot_config.DreamingRuntimeSettings,
    ),
)
def test_provider_timeout_must_precede_attempt_and_lease(
    settings_type: Callable[..., object],
) -> None:
    """@brief Provider deadline 必须严格早于 attempt 与 lease / The provider deadline must precede the attempt and lease.

    @param settings_type 拥有三层 deadline 的运行时配置 / Runtime settings owning all three deadlines.
    @return None / None.
    """

    settings = settings_type()
    assert (
        getattr(settings, "provider_timeout_seconds")
        < getattr(settings, "attempt_timeout_seconds")
        < getattr(settings, "lease_seconds")
    )

    with pytest.raises(
        ValueError,
        match="attempt_timeout_seconds must be > provider_timeout_seconds",
    ):
        settings_type(
            provider_timeout_seconds=90,
            attempt_timeout_seconds=90,
            lease_seconds=120,
        )


def test_working_memory_resilience_defaults_and_positive_bounds() -> None:
    """@brief WorkingMemory 在线召回有独立 deadline 与冷却边界 / WorkingMemory online recall has independent deadline and cooldown bounds."""

    settings = bot_config.WorkingMemorySettings()
    assert settings.timeout_seconds == 5.0
    assert settings.failure_cooldown_seconds == 60.0

    with pytest.raises(ValueError, match="greater than 0"):
        bot_config.WorkingMemorySettings(timeout_seconds=0.0)
    with pytest.raises(ValueError, match="greater than 0"):
        bot_config.WorkingMemorySettings(failure_cooldown_seconds=0.0)


def test_runtime_shutdown_grace_is_a_bounded_escalation_policy() -> None:
    """@brief shutdown grace 是与 Compose 一致的有界升级策略 / Shutdown grace is a bounded escalation policy aligned with Compose."""

    settings = bot_config.RuntimeSettings()
    assert settings.mailbox.shutdown_grace_seconds == 180.0

    assert (
        bot_config.MailboxRuntimeSettings(
            shutdown_grace_seconds=1.0
        ).shutdown_grace_seconds
        == 1.0
    )
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        bot_config.MailboxRuntimeSettings(shutdown_grace_seconds=0.5)
    with pytest.raises(ValueError, match="less than or equal to 190"):
        bot_config.MailboxRuntimeSettings(shutdown_grace_seconds=191.0)


def test_process_managers_outlive_runtime_shutdown_grace() -> None:
    """@brief 外层进程管理器不能提前强杀仍在排空的运行时 / Outer process managers must not kill a runtime that is still draining.

    @return None / None.
    @note 同时约束宿主机脚本与 Compose，防止部署配置独立漂移。/
        Constrains both the host script and Compose to prevent independent deployment drift.
    """

    repository = EXAMPLE_CONFIG_PATH.parent
    runtime_grace = bot_config.MailboxRuntimeSettings().shutdown_grace_seconds
    script = (repository / "runBot.sh").read_text(encoding="utf-8")
    compose = (repository / "docker-compose.yml").read_text(encoding="utf-8")

    script_timeout = re.search(r"BOT_STOP_TIMEOUT_SECONDS:-(\d+)", script)
    compose_timeout = re.search(r"stop_grace_period:\s*(\d+)s", compose)

    assert script_timeout is not None
    assert compose_timeout is not None
    assert int(script_timeout.group(1)) > runtime_grace
    assert int(compose_timeout.group(1)) > runtime_grace
    assert int(compose_timeout.group(1)) >= bot_config.MAX_SHUTDOWN_GRACE_SECONDS + 10
    assert "inner + 10" in script


def test_runbot_uses_checkout_scoped_pid_identity_instead_of_process_grep() -> None:
    """@brief 宿主脚本只管理本 checkout 原子记录的进程实例 / The host script manages only the atomically recorded process instance for this checkout.

    @return None / None.
    """

    repository = EXAMPLE_CONFIG_PATH.parent
    script = (repository / "runBot.sh").read_text(encoding="utf-8")
    ignore = (repository / ".gitignore").read_text(encoding="utf-8")

    assert 'PID_FILE="$STATE_DIR/fogmoe-bot.pid"' in script
    assert "get_process_start_time" in script
    assert 'readlink -f "/proc/$process_pid/cwd"' in script
    assert '"/proc/$process_pid/cmdline"' in script
    assert 'grep -Fqx -- "$VENV_DIR/bin/fogmoe-bot"' in script
    assert "process_is_managed_bot" in script
    assert "flock -n 9" in script
    assert "2>&1 9>&- &" in script
    assert '"$NEW_PID" "$NEW_START_TIME" "$runtime_grace_seconds"' in script
    assert 'validate_stop_timeout "$BOT_SHUTDOWN_GRACE"' in script
    assert "ps -ef" not in script
    assert "[f]ogmoe-bot" not in script
    assert ".runtime/" in ignore
