"""@brief add_file 应用模型与路径能力边界的 CTest / CTest for add_file application models and path-capability boundaries."""

from __future__ import annotations

import sys
import unittest
from collections.abc import Iterator
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""@brief 仓库根目录 / Repository root directory."""

_SOURCE_ROOT = _PROJECT_ROOT / "src"
"""@brief Python src-layout 根目录 / Python src-layout root directory."""

if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from fogmoe_bot.application.workspace.models import (  # noqa: E402
    AddFileCommand,
    AddFileResult,
)
from fogmoe_bot.domain.workspace.runtime import (  # noqa: E402
    WorkspaceRequestHash,
    WorkspaceRequestId,
)
from fogmoe_bot.domain.workspace.scope import PersonalRuntimeScope  # noqa: E402


def _chunks() -> Iterator[bytes]:
    """@brief 产出一次性测试 payload stream / Produce a single-consumption test payload stream.

    @return 两个二进制 chunks / Two binary chunks.
    """

    yield b"hello "
    yield b"world"


def _command(*, opaque_id: str = "opaque42") -> AddFileCommand:
    """@brief 构造最小有效 add_file 命令 / Construct a minimally valid add_file command.

    @param opaque_id 测试 opaque ID / Test opaque ID.
    @return 已验证命令 / Validated command.
    """

    return AddFileCommand(
        scope=PersonalRuntimeScope(101),
        opaque_id=opaque_id,
        chunks=_chunks(),
        byte_size=11,
        sha256="b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        request_id=WorkspaceRequestId("payload:turn:0"),
        request_hash=WorkspaceRequestHash("a" * 64),
    )


class AddFileModelTests(unittest.TestCase):
    """@brief add_file 模型测试 / Tests for add_file models."""

    def test_command_derives_the_only_publishable_runtime_path(self) -> None:
        """@brief 命令绝不接收任意路径，而是唯一派生 uploads 路径 / A command never accepts an arbitrary path and derives the sole uploads path.

        @return None / None.
        """

        command = _command(opaque_id="a_safe-id")
        self.assertEqual(
            command.runtime_path,
            "/workspace/uploads/a_safe-id/payload",
        )

    def test_command_validation_does_not_consume_chunk_stream(self) -> None:
        """@brief 模型验证不预读一次性 stream / Model validation does not pre-read a single-use stream.

        @return None / None.
        """

        command = _command()
        self.assertEqual(list(command.chunks), [b"hello ", b"world"])

    def test_command_rejects_path_shaped_or_raw_byte_inputs(self) -> None:
        """@brief opaque ID 不能注入路径，裸 bytes 也不能伪装成 chunk iterable / An opaque ID cannot inject a path, and bare bytes cannot masquerade as a chunk iterable.

        @return None / None.
        """

        with self.assertRaises(ValueError):
            _command(opaque_id="../escape")
        with self.assertRaises(TypeError):
            AddFileCommand(
                scope=PersonalRuntimeScope(101),
                opaque_id="opaque42",
                chunks=b"not-a-chunk-iterable",
                byte_size=1,
                sha256="a" * 64,
                request_id=WorkspaceRequestId("payload:turn:0"),
                request_hash=WorkspaceRequestHash("b" * 64),
            )

    def test_result_accepts_only_the_fixed_file_tree(self) -> None:
        """@brief 返回收据也不能声明任意 runtime 路径 / A returned receipt cannot claim an arbitrary runtime path.

        @return None / None.
        """

        result = AddFileResult(
            request_id=WorkspaceRequestId("payload:turn:0"),
            replayed=False,
            path="/workspace/uploads/opaque42/payload",
            byte_size=11,
            sha256="b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9",
        )
        self.assertFalse(result.replayed)
        with self.assertRaises(ValueError):
            AddFileResult(
                request_id=WorkspaceRequestId("payload:turn:0"),
                replayed=False,
                path="/workspace/other/payload",
                byte_size=11,
                sha256="a" * 64,
            )


if __name__ == "__main__":
    unittest.main()
