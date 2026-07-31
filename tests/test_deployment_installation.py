"""@brief 部署安装策略的静态回归测试 / Static regression tests for deployment installation policy."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
"""@brief 当前 checkout 根目录 / Current checkout root."""


def test_deployment_scripts_forbid_editable_installs() -> None:
    """@brief 所有部署入口必须构建普通 wheel / All deployment entrypoints must build regular wheels.

    @return None / None.
    @note 文档中的开发环境示例可以继续使用 editable；本测试仅约束会改变部署状态的脚本。/
        Development examples in documentation may remain editable; this test constrains only
        scripts that mutate deployment state.
    """

    deployment_scripts = (
        REPOSITORY_ROOT / "runBot.sh",
        REPOSITORY_ROOT / "installWspctl.sh",
        REPOSITORY_ROOT / "scripts" / "start-wspctld.sh",
    )

    for script_path in deployment_scripts:
        script = script_path.read_text(encoding="utf-8")
        assert "pip install -e" not in script
        assert "pip install --editable" not in script
        assert "pip install -e " not in script
        assert "editable Python client 已就绪" not in script


def test_deployment_scripts_reject_editable_metadata() -> None:
    """@brief Bot 与 wspctl 部署均校验 editable 元数据 / Bot and wspctl deployments both validate editable metadata.

    @return None / None.
    """

    run_bot = (REPOSITORY_ROOT / "runBot.sh").read_text(encoding="utf-8")
    start_wspctld = (
        REPOSITORY_ROOT / "scripts" / "start-wspctld.sh"
    ).read_text(encoding="utf-8")

    assert "deployment_install_is_regular" in run_bot
    assert 'get("editable") is True' in run_bot
    assert "deployed_client_is_regular_install" in start_wspctld
    assert 'get("editable") is True' in start_wspctld


def test_bot_runtime_cannot_shadow_installed_wheel_with_source_tree() -> None:
    """@brief Bot 运行时不得用源码树遮蔽已安装 wheel / Bot runtime must not shadow the installed wheel with the source tree.

    @return None / None.
    @note ``src/wspctl`` 不含编译扩展；注入 ``PYTHONPATH=src`` 会让它遮蔽 wheel 中完整的
        ``wspctl`` 包。/ ``src/wspctl`` has no compiled extension; injecting
        ``PYTHONPATH=src`` shadows the complete ``wspctl`` package from the wheel.
    """

    run_bot = (REPOSITORY_ROOT / "runBot.sh").read_text(encoding="utf-8")

    assert "PYTHONPATH=" not in run_bot
    assert (
        'nohup "$VENV_DIR/bin/fogmoe-bot" --config "$CONFIG_FILE"' in run_bot
    )
    assert 'if [ ! -x "$VENV_DIR/bin/fogmoe-bot" ]' in run_bot
