"""@brief wspctl 开发态 socket 路径配置测试 / wspctl development socket-path configuration tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fogmoe_bot.config import (
    DEVELOPMENT_WSPCTL_SOCKET_PATH,
    ConfigurationError,
    WorkspaceRuntimeSettings,
    read_bot_settings,
)

#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief 完整 JSONC 示例配置路径 / Complete JSONC example configuration path.
EXAMPLE_CONFIGURATION_PATH = REPOSITORY_ROOT / "example.config.json"


class WorkspaceConfigurationPathTests(unittest.TestCase):
    """@brief workspace socket 的 JSONC 路径边界测试 / JSONC path-boundary tests for the workspace socket."""

    def test_relative_development_socket_resolves_against_config_parent(self) -> None:
        """@brief 相对 .wspctl socket 不依赖调用方工作目录 / A relative .wspctl socket does not depend on the caller working directory.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            configuration_path = Path(temporary_directory) / "config.json"
            configuration_path.write_text(
                EXAMPLE_CONFIGURATION_PATH.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            settings = read_bot_settings(configuration_path)

            self.assertEqual(
                settings.runtime.workspace.broker_socket_path,
                str(
                    (Path(temporary_directory) / ".wspctl" / "run" / "bot" / "wspctld.sock").resolve(
                        strict=False
                    )
                ),
            )

    def test_relative_socket_cannot_escape_development_work_root(self) -> None:
        """@brief 相对 socket 不能借由 parent traversal 逃出 .wspctl / A relative socket cannot escape .wspctl through parent traversal.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            configuration_path = Path(temporary_directory) / "config.json"
            configuration_path.write_text(
                EXAMPLE_CONFIGURATION_PATH.read_text(encoding="utf-8").replace(
                    '"broker_socket_path": ".wspctl/run/bot/wspctld.sock"',
                    '"broker_socket_path": "../wspctld.sock"',
                    1,
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ConfigurationError):
                read_bot_settings(configuration_path)

    def test_explicit_client_visible_socket_remains_absolute(self) -> None:
        """@brief 显式绝对路径表示 client 视图而非 host work root / An explicit absolute path denotes the client view rather than the host work root.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as temporary_directory:
            configuration_path = Path(temporary_directory) / "config.json"
            configuration_path.write_text(
                EXAMPLE_CONFIGURATION_PATH.read_text(encoding="utf-8").replace(
                    '"broker_socket_path": ".wspctl/run/bot/wspctld.sock"',
                    '"broker_socket_path": "/client-visible/wspctld.sock"',
                    1,
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                read_bot_settings(configuration_path).runtime.workspace.broker_socket_path,
                "/client-visible/wspctld.sock",
            )

    def test_direct_model_default_is_source_tree_development_socket(self) -> None:
        """@brief 非 JSONC 组合根仍使用源码树开发态默认值 / A non-JSONC composition root still uses the source-tree development default.

        @return None / None.
        """

        self.assertEqual(
            WorkspaceRuntimeSettings().broker_socket_path,
            DEVELOPMENT_WSPCTL_SOCKET_PATH,
        )


if __name__ == "__main__":
    unittest.main()
