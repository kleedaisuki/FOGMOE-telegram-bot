"""@brief 部署安装策略的静态回归测试 / Static regression tests for deployment installation policy."""

import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
"""@brief 当前 checkout 根目录 / Current checkout root."""


def test_deployment_scripts_forbid_editable_installs() -> None:
    """@brief 所有部署入口必须构建普通 wheel / All deployment entrypoints must build regular wheels.

    @return None / None.
    @note C++ 扩展要求所有项目安装都使用普通 wheel；本测试约束会改变部署状态的脚本。/
        The C++ extension requires regular wheels for all project installs; this test constrains
        scripts that mutate deployment state.
    """

    deployment_scripts = (
        REPOSITORY_ROOT / "runBot.sh",
        REPOSITORY_ROOT / "install.sh",
        REPOSITORY_ROOT / "scripts" / "start-wspctld.sh",
        REPOSITORY_ROOT / "scripts" / "ensure-wspctl-client.sh",
    )

    for script_path in deployment_scripts:
        script = script_path.read_text(encoding="utf-8")
        assert "pip install -e" not in script
        assert "pip install --editable" not in script
        assert "pip install -e " not in script
        assert "editable Python client 已就绪" not in script


def test_install_entrypoint_has_the_semantic_name_without_a_legacy_shim() -> None:
    """@brief 安装入口必须改名且不保留旧壳 / The installer must use its semantic new name without a legacy shim.

    @return None / None.
    """

    assert (REPOSITORY_ROOT / "install.sh").is_file()
    assert not (REPOSITORY_ROOT / "installWspctl.sh").exists()


def test_regular_wheel_reconciler_owns_editable_and_record_validation() -> None:
    """@brief 只有共享 reconciler 校验 editable 与 RECORD / Only the shared reconciler validates editable metadata and RECORD.

    @return None / None.
    """

    reconciler_path = REPOSITORY_ROOT / "scripts" / "ensure-wspctl-client.sh"
    reconciler = reconciler_path.read_text(encoding="utf-8")
    run_bot = (REPOSITORY_ROOT / "runBot.sh").read_text(encoding="utf-8")
    start_wspctld = (REPOSITORY_ROOT / "scripts" / "start-wspctld.sh").read_text(
        encoding="utf-8"
    )

    assert 'get("editable") is True' in reconciler
    assert "import wspctl._native as native" in reconciler
    assert 'distribution.read_text("RECORD")' in reconciler
    assert "hashlib.new" in reconciler
    assert '"$PYTHON_EXECUTABLE" -I -m pip wheel' in reconciler
    assert '"$PYTHON_EXECUTABLE" -I -m pip install' in reconciler
    assert os.access(reconciler_path, os.X_OK)
    assert "deployment_install_is_regular" not in run_bot
    assert 'pip install "$BOT_DIR"' not in run_bot
    assert "uv --directory \"$BOT_DIR\" sync" in run_bot
    assert "--no-install-project" in run_bot
    assert "ensure-wspctl-client.sh" in run_bot
    assert "deployed_client_is_regular_install" not in start_wspctld
    assert "ensure-wspctl-client.sh" in start_wspctld


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


def test_build_identity_is_deterministic_across_attribute_order() -> None:
    """@brief 构建身份必须独立于属性传入顺序 / Build identity must be independent of attribute order.

    @return None / None.
    """

    identity_tool = REPOSITORY_ROOT / "tools" / "wspctl_build_identity.py"

    def compute_identity(attributes: tuple[str, ...]) -> str:
        """@brief 运行 identity 工具 / Run the identity tool.

        @param attributes 重复传入的属性 / Repeated input attributes.
        @return 小写十六进制身份 / Lowercase hexadecimal identity.
        """

        command = [
            sys.executable,
            str(identity_tool),
            "--source-root",
            str(REPOSITORY_ROOT),
            "--component",
            "image",
        ]
        for attribute in attributes:
            command.extend(("--attribute", attribute))
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout.strip()

    first_identity = compute_identity(("platform=linux/amd64", "rootfs_format=oci-v1"))
    second_identity = compute_identity(("rootfs_format=oci-v1", "platform=linux/amd64"))

    assert first_identity == second_identity
    assert len(first_identity) == 64
    assert all(character in "0123456789abcdef" for character in first_identity)
