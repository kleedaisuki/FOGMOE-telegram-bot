"""@brief wspctl 受控 image builder 的 rootless contract 测试 / Rootless contract tests for the controlled wspctl image builder."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import ModuleType


def _load_builder() -> ModuleType:
    """@brief 以独立模块加载工具脚本 / Load the tool script as an isolated module.

    @return 已执行的 builder module / Executed builder module.
    @raise RuntimeError 工具无法加载时抛出 / Raised when the tool cannot be loaded.
    """

    repository_root = Path(__file__).resolve().parents[2]
    source = repository_root / "tools" / "build_wspctl_image.py"
    specification = importlib.util.spec_from_file_location("wspctl_image_builder_test", source)
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load wspctl image builder")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


_BUILDER = _load_builder()
"""@brief 被测 image builder module / Image builder module under test."""


def _write_minimal_elf_header(
    path: Path,
    *,
    elf_class: int,
    data_encoding: int,
    machine: int,
) -> None:
    """@brief 写入仅供 ABI parser 使用的最小 ELF header / Write a minimal ELF header used only by the ABI parser.

    @param path 要写入的 regular file / Regular file to write.
    @param elf_class ELF class 值 / ELF class value.
    @param data_encoding ELF data encoding 值 / ELF data-encoding value.
    @param machine ELF ``e_machine`` 值 / ELF ``e_machine`` value.
    @return None / None.
    """

    header = bytearray(_BUILDER._ELF_ABI_HEADER_BYTES)
    header[:4] = b"\x7fELF"
    header[4] = elf_class
    header[5] = data_encoding
    header[6] = 1
    byteorder = "little" if data_encoding == _BUILDER._ELFDATA2LSB else "big"
    header[18:20] = machine.to_bytes(2, byteorder=byteorder, signed=False)
    path.write_bytes(header)


class _RecordingAssembler(_BUILDER.RootfsAssembler):
    """@brief 仅记录目录创建契约的 assembler / Assembler that records only directory-creation contracts."""

    def __init__(self) -> None:
        """@brief 构造不接触 filesystem 的 fake layout / Construct a fake layout without touching the filesystem.

        @return None / None.
        """

        temporary = Path(tempfile.gettempdir()) / "wspctl-image-builder-contract"
        layout = _BUILDER.PythonRuntimeLayout(
            venv=temporary / "venv",
            base_prefix=temporary / "cpython",
            interpreter=temporary / "cpython" / "bin" / "python3.14",
            standard_library=temporary / "cpython" / "lib" / "python3.14",
            library_directory=temporary / "cpython" / "lib",
            version="3.14",
        )
        super().__init__(
            rootfs=temporary / "rootfs",
            layout=layout,
            python_sources=(),
            readelf=Path("/usr/bin/readelf"),
            ldconfig=Path("/sbin/ldconfig"),
        )
        self.created: dict[PurePosixPath, int] = {}
        """@brief 记录的 ``runtime_path -> mode`` / Recorded ``runtime_path -> mode`` values."""

    def _ensure_directory(self, runtime_path: PurePosixPath, mode: int) -> None:
        """@brief 记录请求而不真正创建目录 / Record a request without creating a directory.

        @param runtime_path runtime directory / Runtime directory.
        @param mode requested mode / Requested mode.
        @return None / None.
        """

        self.created[runtime_path] = mode


class ImageBuilderContractTests(unittest.TestCase):
    """@brief 不需要 root/cgroup/overlay 的 image-builder 语义测试 / Image-builder semantic tests requiring no root, cgroup, or overlay."""

    def test_generation_rejects_path_semantics(self) -> None:
        """@brief generation 不能变成路径或覆盖目标 / A generation cannot become a path or overwrite target."""

        for unsafe in ("", ".", "..", "x/y", "x\\y", "has space", "a" * 129):
            with self.assertRaises(_BUILDER.ImageBuildError):
                _BUILDER._validate_generation(unsafe)
        self.assertEqual(_BUILDER._validate_generation("2026-07-27.python3.14"), "2026-07-27.python3.14")

    def test_relocated_symlink_is_relative_and_rootfs_contained(self) -> None:
        """@brief host absolute interpreter link 被改写为 rootfs-relative link / A host-absolute interpreter link becomes a rootfs-relative link."""

        source = PurePosixPath("/opt/wspctl/venv/bin/python3.14")
        target = PurePosixPath("/usr/local/bin/cpython3.14")
        link = _BUILDER._runtime_relative_link(source, target)
        self.assertFalse(link.startswith("/"))
        self.assertEqual(link, "../../../../usr/local/bin/cpython3.14")

    def test_required_lower_directories_include_writable_workspace(self) -> None:
        """@brief lower rootfs 显式提供 /workspace、/tmp、/proc、/dev / Lower rootfs explicitly provides /workspace, /tmp, /proc, and /dev."""

        assembler = _RecordingAssembler()
        assembler._create_required_directories()
        self.assertEqual(assembler.created[PurePosixPath("/workspace")], 0o1777)
        self.assertEqual(assembler.created[PurePosixPath("/tmp")], 0o1777)
        self.assertEqual(assembler.created[PurePosixPath("/proc")], 0o755)
        self.assertEqual(assembler.created[PurePosixPath("/dev")], 0o755)

    def test_shebang_keeps_venv_entrypoint_not_base_interpreter(self) -> None:
        """@brief venv console script 的 shebang 仍走 relocated venv / A venv console script shebang still enters the relocated venv."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / "venv"
            base = root / "cpython"
            (venv / "bin").mkdir(parents=True)
            (base / "bin").mkdir(parents=True)
            interpreter = base / "bin" / "python3.14"
            interpreter.write_bytes(b"not-used")
            layout = _BUILDER.PythonRuntimeLayout(
                venv=venv,
                base_prefix=base,
                interpreter=interpreter,
                standard_library=base,
                library_directory=base,
                version="3.14",
            )
            assembler = _BUILDER.RootfsAssembler(
                rootfs=root / "rootfs",
                layout=layout,
                python_sources=(),
                readelf=Path("/usr/bin/readelf"),
                ldconfig=Path("/sbin/ldconfig"),
            )
            rewritten = assembler._rewrite_shebang(
                f"#!{venv / 'bin' / 'python3.14'}\nprint('ok')\n".encode()
            )
            self.assertTrue(rewritten.startswith(b"#!/opt/wspctl/venv/bin/python3.14\n"))

    def test_venv_binary_is_not_misclassified_as_relocatable_text(self) -> None:
        """@brief venv/bin 的大型 ELF 直接复制，只有 shebang script 需要文本重写 / A large ELF in venv/bin is copied directly; only a shebang script needs text rewriting."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / "venv"
            base = root / "cpython"
            (venv / "bin").mkdir(parents=True)
            base.mkdir()
            binary = venv / "bin" / "native-tool"
            binary.write_bytes(b"\x7fELF" + b"x" * (2 * 1024 * 1024))
            script = venv / "bin" / "console-tool"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            pth = venv / "package.pth"
            pth.write_text("import package\n", encoding="utf-8")
            layout = _BUILDER.PythonRuntimeLayout(
                venv=venv,
                base_prefix=base,
                interpreter=base / "python3.14",
                standard_library=base,
                library_directory=base,
                version="3.14",
            )
            assembler = _BUILDER.RootfsAssembler(
                rootfs=root / "rootfs",
                layout=layout,
                python_sources=(),
                readelf=Path("/usr/bin/readelf"),
                ldconfig=Path("/sbin/ldconfig"),
            )
            self.assertFalse(assembler._requires_venv_text_relocation(binary))
            self.assertTrue(assembler._requires_venv_text_relocation(script))
            self.assertTrue(assembler._requires_venv_text_relocation(pth))

    def test_pth_requires_explicit_python_source(self) -> None:
        """@brief editable .pth 只能引用显式 --python-source / An editable .pth may reference only explicit --python-source roots."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / "venv"
            source = root / "src"
            base = root / "cpython"
            venv.mkdir()
            source.mkdir()
            base.mkdir()
            layout = _BUILDER.PythonRuntimeLayout(
                venv=venv,
                base_prefix=base,
                interpreter=base / "python3.14",
                standard_library=base,
                library_directory=base,
                version="3.14",
            )
            assembler = _BUILDER.RootfsAssembler(
                rootfs=root / "rootfs",
                layout=layout,
                python_sources=(source,),
                readelf=Path("/usr/bin/readelf"),
                ldconfig=Path("/sbin/ldconfig"),
            )
            rewritten = assembler._rewrite_pth(f"{source}\nimport package\n".encode())
            self.assertEqual(rewritten, b"/opt/wspctl/python-source/1-src\nimport package\n")
            with self.assertRaises(_BUILDER.ImageBuildError):
                assembler._rewrite_pth(b"/unadmitted/host/source\n")

    def test_native_manifest_contract_is_four_deterministic_fields(self) -> None:
        """@brief builder 只接受 native sealer 的四字段 manifest / Builder accepts only the native sealer's four-field manifest."""

        with tempfile.TemporaryDirectory() as directory:
            rootfs = Path(directory)
            digest = "a" * 64
            manifest_digest = "b" * 64
            manifest = rootfs / ".wspctl-image-manifest"
            manifest.write_text(
                "version=1\n"
                "generation=example\n"
                f"rootfs_digest={digest}\n"
                f"digest={manifest_digest}\n",
                encoding="utf-8",
            )
            os.chmod(manifest, 0o444)
            self.assertEqual(_BUILDER._read_manifest_digest(rootfs, "example"), digest)
            manifest.chmod(0o644)
            with self.assertRaises(_BUILDER.ImageBuildError):
                _BUILDER._read_manifest_digest(rootfs, "example")

    def test_ldconfig_wrapper_may_be_a_script_but_runtime_programs_must_be_elf(self) -> None:
        """@brief distribution 的 ldconfig wrapper 可为 script，runtime 程序仍必须为 ELF / A distribution ldconfig wrapper may be a script while runtime programs still require ELF."""

        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "ldconfig-wrapper"
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            self.assertEqual(
                _BUILDER._validate_executable_input(wrapper, "ldconfig", require_elf=False),
                wrapper,
            )
            with self.assertRaises(_BUILDER.ImageBuildError):
                _BUILDER._validate_executable_input(wrapper, "wsp-systemd")

    def test_atomic_publish_never_replaces_existing_generation(self) -> None:
        """@brief renameat2 publish 不能覆盖已存在 generation / renameat2 publishing cannot replace an existing generation."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_staging = root / "first-staging"
            first_destination = root / "generation"
            first_staging.mkdir()
            (first_staging / "marker").write_text("first", encoding="utf-8")
            _BUILDER._rename_noreplace(first_staging, first_destination)
            self.assertEqual((first_destination / "marker").read_text(encoding="utf-8"), "first")
            second_staging = root / "second-staging"
            second_staging.mkdir()
            with self.assertRaises(_BUILDER.ImageBuildError):
                _BUILDER._rename_noreplace(second_staging, first_destination)
            self.assertTrue(second_staging.is_dir())
            self.assertEqual((first_destination / "marker").read_text(encoding="utf-8"), "first")

    def test_ldconfig_resolution_rejects_same_soname_with_wrong_elf_class(self) -> None:
        """@brief 同 SONAME 的 x32 cache 条目不能覆盖 x86-64 dependency / An x32 cache entry with the same SONAME cannot satisfy an x86-64 dependency."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bin" / "program"
            x32_library = root / "libx32" / "libdemo.so"
            x64_library = root / "lib64" / "libdemo.so"
            source.parent.mkdir(parents=True)
            x32_library.parent.mkdir(parents=True)
            x64_library.parent.mkdir(parents=True)
            _write_minimal_elf_header(
                source,
                elf_class=_BUILDER._ELFCLASS64,
                data_encoding=_BUILDER._ELFDATA2LSB,
                machine=62,
            )
            _write_minimal_elf_header(
                x32_library,
                elf_class=_BUILDER._ELFCLASS32,
                data_encoding=_BUILDER._ELFDATA2LSB,
                machine=62,
            )
            _write_minimal_elf_header(
                x64_library,
                elf_class=_BUILDER._ELFCLASS64,
                data_encoding=_BUILDER._ELFDATA2LSB,
                machine=62,
            )
            layout = _BUILDER.PythonRuntimeLayout(
                venv=root / "venv",
                base_prefix=root / "cpython",
                interpreter=source,
                standard_library=root / "cpython",
                library_directory=root / "cpython",
                version="3.14",
            )
            assembler = _BUILDER.RootfsAssembler(
                rootfs=root / "rootfs",
                layout=layout,
                python_sources=(),
                readelf=Path("/usr/bin/readelf"),
                ldconfig=Path("/sbin/ldconfig"),
            )
            assembler._library_cache = {"libdemo.so": (x32_library, x64_library)}
            source_abi = _BUILDER._read_elf_abi(source)
            metadata = _BUILDER.ElfMetadata(
                needed=(),
                interpreter=None,
                search_paths=(),
                abi=source_abi,
            )
            resolved, logical_path = assembler._resolve_needed_library(source, metadata, "libdemo.so")
            self.assertEqual(resolved, x64_library)
            self.assertEqual(logical_path, x64_library)
            self.assertNotEqual(_BUILDER._read_elf_abi(x32_library), source_abi)


if __name__ == "__main__":
    unittest.main()
