"""@brief wspctl 受控 image builder 的 rootless contract 测试 / Rootless contract tests for the controlled wspctl image builder."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import ModuleType
from unittest.mock import patch


def _load_builder() -> ModuleType:
    """@brief 以独立模块加载工具脚本 / Load the tool script as an isolated module.

    @return 已执行的 builder module / Executed builder module.
    @raise RuntimeError 工具无法加载时抛出 / Raised when the tool cannot be loaded.
    """

    repository_root = Path(__file__).resolve().parents[2]
    source = repository_root / "tools" / "build_wspctl_image.py"
    specification = importlib.util.spec_from_file_location(
        "wspctl_image_builder_test", source
    )
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
        self.assertEqual(
            _BUILDER._validate_generation("2026-07-27.python3.14"),
            "2026-07-27.python3.14",
        )

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
            self.assertTrue(
                rewritten.startswith(b"#!/opt/wspctl/venv/bin/python3.14\n")
            )

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

    def test_headless_profile_excludes_tk_before_elf_closure(self) -> None:
        """@brief headless profile 在 ELF 扫描前排除 Tcl/Tk GUI 子树 / The headless profile excludes Tcl/Tk GUI entries before ELF scanning.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "cpython"
            standard_library = base / "lib" / "python3.14"
            dynamic_extensions = standard_library / "lib-dynload"
            interpreter = base / "bin" / "python3.14"
            rootfs = root / "rootfs"
            interpreter.parent.mkdir(parents=True)
            dynamic_extensions.mkdir(parents=True)
            rootfs.mkdir()
            interpreter.write_bytes(b"base-interpreter")
            (standard_library / "json.py").write_text(
                "VALUE = 'retained'\n", encoding="utf-8"
            )
            for directory_name in ("tkinter", "idlelib", "turtledemo"):
                package = standard_library / directory_name
                package.mkdir()
                (package / "__init__.py").write_text(
                    "VALUE = 'excluded'\n", encoding="utf-8"
                )
            (standard_library / "turtle.py").write_text(
                "VALUE = 'excluded'\n", encoding="utf-8"
            )
            tkinter_extension = (
                dynamic_extensions / "_tkinter.cpython-314-x86_64-linux-gnu.so"
            )
            retained_extension = (
                dynamic_extensions / "_sqlite3.cpython-314-x86_64-linux-gnu.so"
            )
            _write_minimal_elf_header(
                tkinter_extension,
                elf_class=_BUILDER._ELFCLASS64,
                data_encoding=_BUILDER._ELFDATA2LSB,
                machine=62,
            )
            _write_minimal_elf_header(
                retained_extension,
                elf_class=_BUILDER._ELFCLASS64,
                data_encoding=_BUILDER._ELFDATA2LSB,
                machine=62,
            )
            os.symlink(tkinter_extension.name, dynamic_extensions / "tk-alias.so")
            layout = _BUILDER.PythonRuntimeLayout(
                venv=root / "venv",
                base_prefix=base,
                interpreter=interpreter,
                standard_library=standard_library,
                library_directory=base / "lib",
                version="3.14",
            )
            assembler = _BUILDER.RootfsAssembler(
                rootfs=rootfs,
                layout=layout,
                python_sources=(),
                readelf=Path("/usr/bin/readelf"),
                ldconfig=Path("/sbin/ldconfig"),
            )
            with (
                patch.object(_BUILDER.os, "chown", return_value=None),
                patch.object(_BUILDER.os, "fchown", return_value=None),
                patch.object(_BUILDER.os, "lchown", return_value=None),
            ):
                assembler._create_required_directories()
                assembler._copy_python_distribution()

            copied_stdlib = rootfs / "usr" / "local" / "lib" / "python3.14"
            self.assertEqual(
                _BUILDER._HEADLESS_PYTHON_RUNTIME_PROFILE.identifier,
                "headless-python-v1",
            )
            self.assertTrue((copied_stdlib / "json.py").is_file())
            self.assertTrue(
                (copied_stdlib / "lib-dynload" / retained_extension.name).is_file()
            )
            for excluded in (
                copied_stdlib / "tkinter",
                copied_stdlib / "idlelib",
                copied_stdlib / "turtledemo",
                copied_stdlib / "turtle.py",
                copied_stdlib / "lib-dynload" / tkinter_extension.name,
                copied_stdlib / "lib-dynload" / "tk-alias.so",
            ):
                self.assertFalse(os.path.lexists(excluded), excluded)
            queued_sources = {source for source, _ in assembler._elf_queue}
            self.assertIn(retained_extension.resolve(), queued_sources)
            self.assertNotIn(tkinter_extension.resolve(), queued_sources)

    def test_headless_profile_keeps_other_elf_dependencies_fail_closed(self) -> None:
        """@brief profile 不会放宽其他动态模块的 closure 失败语义 / The profile does not relax closure failures for other dynamic modules.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lib-dynload" / "_sqlite3.so"
            source.parent.mkdir(parents=True)
            _write_minimal_elf_header(
                source,
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
            assembler._library_cache = {}
            metadata = _BUILDER.ElfMetadata(
                needed=(),
                interpreter=None,
                search_paths=(),
                abi=_BUILDER._read_elf_abi(source),
            )
            with self.assertRaisesRegex(
                _BUILDER.ImageBuildError, "no candidate with a matching ELF ABI"
            ):
                assembler._resolve_needed_library(
                    source, metadata, "lib-not-present.so"
                )

    def test_layout_probe_never_executes_validated_editable_pth(self) -> None:
        """@brief layout probe 用 ``-S``，不执行可静态接受的 PEP 660 hook / The layout probe uses ``-S`` and never executes a statically accepted PEP 660 hook.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            venv = root / "venv"
            source = root / "src"
            module = source / "demo.py"
            source.mkdir()
            module.write_text("VALUE = 'source'\n", encoding="utf-8")
            (venv / "bin").mkdir(parents=True)
            os.symlink(sys.executable, venv / "bin" / "python")
            (venv / "pyvenv.cfg").write_text(
                f"home = {Path(sys.base_prefix) / 'bin'}\n"
                "include-system-site-packages = false\n"
                f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
                encoding="utf-8",
            )
            site_packages = (
                venv
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            site_packages.mkdir(parents=True)
            helper = site_packages / "_editable_skbc_demo.py"
            helper.write_text(
                "from pathlib import Path\n"
                "Path('probe-was-executed').write_text('ran')\n"
                "class ScikitBuildRedirectingFinder:\n"
                "    pass\n\n"
                "def install(*args):\n"
                "    pass\n\n"
                f"install({{'demo': {str(module)!r}}}, {{}}, {{'demo': [{str(source)!r}]}}, "
                "['demo'], None, False, False, [], [], '')\n",
                encoding="utf-8",
            )
            (site_packages / "_editable_skbc_demo.pth").write_text(
                "import _editable_skbc_demo\n",
                encoding="utf-8",
            )
            plan = _BUILDER._validate_relocatable_venv(venv, python_sources=(source,))
            self.assertEqual(len(plan.scikit_build_hooks), 1)

            previous_directory = Path.cwd()
            try:
                os.chdir(root)
                layout = _BUILDER._parse_python_layout(venv)
            finally:
                os.chdir(previous_directory)

            self.assertFalse((root / "probe-was-executed").exists())
            self.assertEqual(layout.venv, venv)
            self.assertEqual(
                layout.version, f"{sys.version_info.major}.{sys.version_info.minor}"
            )

            repository_root = Path(__file__).resolve().parents[2]
            project_layout = _BUILDER._parse_python_layout(repository_root / ".venv")
            self.assertEqual(project_layout.venv, repository_root / ".venv")
            self.assertEqual(
                project_layout.version,
                f"{sys.version_info.major}.{sys.version_info.minor}",
            )

    def test_complete_venv_and_source_copy_omits_bytecode_host_paths(self) -> None:
        """@brief 完整 venv/source tree 复制会剔除 cache，输出没有 host checkout path / Copying complete venv/source trees omits caches so the output has no host checkout path.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "host-checkout"
            source = checkout / "src"
            venv = checkout / ".venv"
            rootfs = checkout / "rootfs"
            base = checkout / "cpython"
            source_package = source / "demo"
            source_cache = source_package / "__pycache__"
            venv_site_packages = venv / "lib" / "python3.14" / "site-packages"
            venv_cache = venv_site_packages / "__pycache__"
            source_cache.mkdir(parents=True)
            venv_cache.mkdir(parents=True)
            base.mkdir(parents=True)
            rootfs.mkdir(parents=True)
            (source_package / "__init__.py").write_text(
                "VALUE = 'source'\n", encoding="utf-8"
            )
            (venv_site_packages / "dependency.py").write_text(
                "VALUE = 'venv'\n", encoding="utf-8"
            )
            host_path = str(checkout).encode("utf-8")
            (source_cache / "demo.cpython-314.pyc").write_bytes(
                b"source-pyc:" + host_path
            )
            os.symlink(
                source_cache / "demo.cpython-314.pyc",
                source / "cache-bytecode-alias",
            )
            (source / "legacy.pyc").write_bytes(b"legacy-source-pyc:" + host_path)
            (venv_cache / "dependency.cpython-314.pyc").write_bytes(
                b"venv-pyc:" + host_path
            )
            (venv_site_packages / "legacy.pyc").write_bytes(
                b"legacy-venv-pyc:" + host_path
            )
            layout = _BUILDER.PythonRuntimeLayout(
                venv=venv,
                base_prefix=base,
                interpreter=base / "bin" / "python3.14",
                standard_library=base / "lib" / "python3.14",
                library_directory=base / "lib",
                version="3.14",
            )
            assembler = _BUILDER.RootfsAssembler(
                rootfs=rootfs,
                layout=layout,
                python_sources=(source,),
                readelf=Path("/usr/bin/readelf"),
                ldconfig=Path("/sbin/ldconfig"),
            )
            with (
                patch.object(_BUILDER.os, "chown", return_value=None),
                patch.object(_BUILDER.os, "fchown", return_value=None),
                patch.object(_BUILDER.os, "lchown", return_value=None),
            ):
                assembler._copy_tree(source, _BUILDER._RUNTIME_PYTHON_SOURCES / "1-src")
                assembler._copy_tree(venv, _BUILDER._RUNTIME_VENV)

            copied_files = tuple(path for path in rootfs.rglob("*") if path.is_file())
            copied_bytes = b"".join(path.read_bytes() for path in copied_files)
            self.assertIn(
                rootfs
                / "opt"
                / "wspctl"
                / "python-source"
                / "1-src"
                / "demo"
                / "__init__.py",
                copied_files,
            )
            self.assertIn(
                rootfs
                / "opt"
                / "wspctl"
                / "venv"
                / "lib"
                / "python3.14"
                / "site-packages"
                / "dependency.py",
                copied_files,
            )
            self.assertFalse(
                any(
                    "__pycache__" in path.parts or path.suffix == ".pyc"
                    for path in copied_files
                )
            )
            self.assertFalse(
                (
                    rootfs
                    / "opt"
                    / "wspctl"
                    / "python-source"
                    / "1-src"
                    / "cache-bytecode-alias"
                ).exists()
            )
            self.assertNotIn(host_path, copied_bytes)

    def test_regular_copy_defers_fsync_until_atomic_staging_commit(self) -> None:
        """@brief 单文件复制不应双重 fsync，durability 只在发布前的全树提交完成 / A regular copy must not fsync twice; durability is committed once for the full staging tree before publish."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.write_bytes(b"immutable-image-input")
            rootfs = root / "rootfs"
            rootfs.mkdir()
            layout = _BUILDER.PythonRuntimeLayout(
                venv=root / "venv",
                base_prefix=root / "cpython",
                interpreter=source,
                standard_library=root / "cpython",
                library_directory=root / "cpython",
                version="3.14",
            )
            assembler = _BUILDER.RootfsAssembler(
                rootfs=rootfs,
                layout=layout,
                python_sources=(),
                readelf=Path("/usr/bin/readelf"),
                ldconfig=Path("/sbin/ldconfig"),
            )
            with (
                patch.object(_BUILDER.os, "chown", return_value=None),
                patch.object(_BUILDER.os, "fchown", return_value=None),
                patch.object(_BUILDER.os, "fsync") as synchronize,
            ):
                assembler._copy_regular_file(source, PurePosixPath("/opt/input"))

            self.assertEqual(
                (rootfs / "opt" / "input").read_bytes(), b"immutable-image-input"
            )
            synchronize.assert_not_called()

    def test_path_only_pth_requires_explicit_python_source(self) -> None:
        """@brief path-only ``.pth`` 只能引用显式 --python-source / A path-only ``.pth`` may reference only explicit --python-source roots."""

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
            rewritten = assembler._rewrite_pth(
                f"{source}\n# package source root\n".encode()
            )
            self.assertEqual(
                rewritten, b"/opt/wspctl/python-source/1-src\n# package source root\n"
            )
            with self.assertRaises(_BUILDER.ImageBuildError):
                assembler._rewrite_pth(b"/unadmitted/host/source\n")

    def test_documented_gnu_commands_are_explicit_and_unrelated_command_is_denied(
        self,
    ) -> None:
        """@brief 文档中的 GNU command 必须全部通过 allowlist，任意额外命令仍被拒绝 / Every documented GNU command must pass the allowlist while arbitrary extra commands remain denied.

        @return None / None.
        """

        repository_root = Path(__file__).resolve().parents[2]
        deployment_guide = repository_root / "docs" / "wspctl-deployment.md"
        documented = tuple(
            Path(line.strip().split()[1])
            for line in deployment_guide.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("--gnu-command ")
        )
        self.assertIn(Path("/usr/bin/chmod"), documented)
        for command in documented:
            self.assertTrue(
                command.is_file(), f"documented command is absent: {command}"
            )
            self.assertEqual(_BUILDER._validate_command_input(command), command)

        with tempfile.TemporaryDirectory() as directory:
            unrelated = Path(directory) / "unrelated-host-command"
            unrelated.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            unrelated.chmod(0o755)
            with self.assertRaisesRegex(
                _BUILDER.ImageBuildError, "explicit wspctl allowlist"
            ):
                _BUILDER._validate_command_input(unrelated)

    def test_scikit_build_editable_venv_relocates_without_host_path_and_imports(
        self,
    ) -> None:
        """@brief 受限 PEP 660 helper 重写后不泄漏 host 路径且仍可导入 / A constrained PEP 660 helper is host-path-free after rewrite and remains importable.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "host-project"
            source = project / "src"
            package = source / "demo_package"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "VALUE = 'relocated'\n", encoding="utf-8"
            )
            venv = root / "venv"
            site_packages = venv / "lib" / "python3.14" / "site-packages"
            site_packages.mkdir(parents=True)
            helper_module = "_editable_skbc_demo"
            helper = site_packages / f"{helper_module}.py"
            helper.write_text(
                "import importlib.abc\n"
                "import importlib.util\n"
                "import sys\n\n"
                "class ScikitBuildRedirectingFinder(importlib.abc.MetaPathFinder):\n"
                "    def __init__(self, known_source_files):\n"
                "        self.known_source_files = known_source_files\n\n"
                "    def find_spec(self, fullname, path=None, target=None):\n"
                "        origin = self.known_source_files.get(fullname)\n"
                "        if origin is None:\n"
                "            return None\n"
                "        return importlib.util.spec_from_file_location(fullname, origin, submodule_search_locations=[origin.rsplit('/', 1)[0]])\n\n"
                "def install(known_source_files, known_wheel_files, known_directories, known_packages, path, rebuild, verbose, build_options, install_options, install_dir):\n"
                "    sys.meta_path.insert(0, ScikitBuildRedirectingFinder(known_source_files))\n\n"
                f"install({{'demo_package': '{package / '__init__.py'}'}}, {{}}, {{'demo_package': ['{package}']}}, ['demo_package'], None, False, False, [], [], '')\n",
                encoding="utf-8",
            )
            pth = site_packages / f"{helper_module}.pth"
            pth.write_text(f"import {helper_module}\n{source}\n", encoding="utf-8")
            metadata = site_packages / "demo-1.0.dist-info"
            metadata.mkdir()
            (metadata / "METADATA").write_text(
                "Metadata-Version: 2.1\nName: demo\n", encoding="utf-8"
            )
            direct_url = metadata / "direct_url.json"
            direct_url.write_text(
                f'{{"url":"file://{project}","dir_info":{{"editable":true}}}}',
                encoding="utf-8",
            )
            plan = _BUILDER._validate_relocatable_venv(venv, python_sources=(source,))
            runtime_root = root / "runtime"
            runtime_sources = runtime_root / "python-source"
            runtime_site_packages = runtime_root / "site-packages"
            runtime_site_packages.mkdir(parents=True)
            base = root / "cpython"
            base.mkdir()
            layout = _BUILDER.PythonRuntimeLayout(
                venv=venv,
                base_prefix=base,
                interpreter=base / "python3.14",
                standard_library=base,
                library_directory=base,
                version="3.14",
            )
            original_runtime_sources = _BUILDER._RUNTIME_PYTHON_SOURCES
            _BUILDER._RUNTIME_PYTHON_SOURCES = PurePosixPath(str(runtime_sources))
            try:
                assembler = _BUILDER.RootfsAssembler(
                    rootfs=runtime_root,
                    layout=layout,
                    python_sources=(source,),
                    readelf=Path("/usr/bin/readelf"),
                    ldconfig=Path("/sbin/ldconfig"),
                    venv_relocation=plan,
                )
                hook = plan.scikit_build_hooks[0]
                (runtime_site_packages / hook.pth.name).write_bytes(
                    assembler._transform_venv_file(hook.pth, hook.pth.read_bytes())
                )
                (runtime_site_packages / hook.helper.name).write_bytes(
                    assembler._transform_venv_file(
                        hook.helper, hook.helper.read_bytes()
                    )
                )
                relocation = plan.editable_direct_urls[0]
                (runtime_site_packages / "demo-1.0.dist-info").mkdir()
                (
                    runtime_site_packages / "demo-1.0.dist-info" / "direct_url.json"
                ).write_bytes(
                    assembler._transform_venv_file(
                        relocation.metadata, relocation.metadata.read_bytes()
                    )
                )
                shutil.copytree(source, runtime_sources / "1-src")
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-S",
                        "-c",
                        "import site; site.addsitedir(__import__('sys').argv[1]); import demo_package; print(demo_package.VALUE)",
                        str(runtime_site_packages),
                    ],
                    check=False,
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout, "relocated\n")
                copied_text = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in (
                        runtime_site_packages / hook.pth.name,
                        runtime_site_packages / hook.helper.name,
                        runtime_site_packages
                        / "demo-1.0.dist-info"
                        / "direct_url.json",
                    )
                )
                self.assertNotIn(str(project), copied_text)
                self.assertNotIn(str(source), copied_text)
                self.assertIn(f"file://{runtime_sources / '1-src'}", copied_text)
            finally:
                _BUILDER._RUNTIME_PYTHON_SOURCES = original_runtime_sources

    def test_scikit_build_editable_helper_rejects_unrecognized_format(self) -> None:
        """@brief PEP 660 例外不接受缺少 scikit-build terminal mapping 的任意 helper / The PEP 660 exception rejects arbitrary helpers without the scikit-build terminal mapping.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            venv = root / "venv"
            site_packages = venv / "lib" / "python3.14" / "site-packages"
            site_packages.mkdir(parents=True)
            (site_packages / "_editable_skbc_demo.pth").write_text(
                "import _editable_skbc_demo\n", encoding="utf-8"
            )
            (site_packages / "_editable_skbc_demo.py").write_text(
                "class ScikitBuildRedirectingFinder:\n    pass\n\ndef install():\n    pass\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                _BUILDER.ImageBuildError, "terminal install call"
            ):
                _BUILDER._validate_relocatable_venv(venv, python_sources=(source,))

    def test_project_editable_venv_uses_the_supported_scikit_build_shape(self) -> None:
        """@brief 项目实际 ``.venv`` 的 scikit-build helper 可重定位且不保留 checkout 路径 / The project's real ``.venv`` scikit-build helper is relocatable without retaining the checkout path.

        @return None / None.
        """

        repository_root = Path(__file__).resolve().parents[2]
        venv = repository_root / ".venv"
        source = repository_root / "src"
        self.assertTrue(venv.is_dir())
        self.assertTrue(source.is_dir())
        plan = _BUILDER._validate_relocatable_venv(venv, python_sources=(source,))
        self.assertEqual(len(plan.scikit_build_hooks), 1)
        self.assertEqual(len(plan.editable_direct_urls), 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "cpython"
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
                venv_relocation=plan,
            )
            hook = plan.scikit_build_hooks[0]
            rewritten_helper = assembler._transform_venv_file(
                hook.helper, hook.helper.read_bytes()
            )
            rewritten_pth = assembler._transform_venv_file(
                hook.pth, hook.pth.read_bytes()
            )
            relocation = plan.editable_direct_urls[0]
            rewritten_direct_url = assembler._transform_venv_file(
                relocation.metadata, relocation.metadata.read_bytes()
            )
            for rewritten in (rewritten_helper, rewritten_pth, rewritten_direct_url):
                self.assertNotIn(str(repository_root).encode(), rewritten)
                self.assertNotIn(str(source).encode(), rewritten)
            self.assertIn(b"/opt/wspctl/python-source/1-src", rewritten_helper)
            self.assertIn(
                b"file:///opt/wspctl/python-source/1-src", rewritten_direct_url
            )

    def test_scikit_build_editable_helper_rejects_host_wheel_mapping(self) -> None:
        """@brief PEP 660 helper 的 wheel mapping 不能回指 host 绝对路径 / A PEP 660 helper wheel mapping cannot point back to a host absolute path.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            module = source / "demo.py"
            module.write_text("VALUE = 1\n", encoding="utf-8")
            venv = root / "venv"
            site_packages = venv / "lib" / "python3.14" / "site-packages"
            site_packages.mkdir(parents=True)
            (site_packages / "_editable_skbc_demo.pth").write_text(
                "import _editable_skbc_demo\n", encoding="utf-8"
            )
            (site_packages / "_editable_skbc_demo.py").write_text(
                "class ScikitBuildRedirectingFinder:\n    pass\n\n"
                "def install(*args):\n    pass\n\n"
                f"install({{'demo': '{module}'}}, {{'demo_native': '/host/build/demo.so'}}, {{'demo': ['{source}']}}, ['demo'], None, False, False, [], [], '')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                _BUILDER.ImageBuildError, "wheel mapping must be runtime-relative"
            ):
                _BUILDER._validate_relocatable_venv(venv, python_sources=(source,))

    def test_noneditable_setuptools_startup_hook_is_intentionally_rejected(
        self,
    ) -> None:
        """@brief 非 editable setuptools hook 也属于当前 production contract 的拒绝范围 / A non-editable setuptools hook is intentionally outside the current production contract.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            site_packages = venv / "lib" / "python3.14" / "site-packages"
            site_packages.mkdir(parents=True)
            (site_packages / "distutils-precedence.pth").write_text(
                "import os; __import__('_distutils_hack').add_shim()\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                _BUILDER.ImageBuildError, "executable .pth startup hook"
            ):
                _BUILDER._validate_relocatable_venv(venv)

    def test_direct_local_distribution_metadata_is_rejected_before_rootfs_copy(
        self,
    ) -> None:
        """@brief direct-local metadata 不能将 host checkout 泄漏进 image / Direct-local metadata must not leak a host checkout into an image.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            metadata = (
                venv / "lib" / "python3.14" / "site-packages" / "demo-1.0.dist-info"
            )
            metadata.mkdir(parents=True)
            (metadata / "direct_url.json").write_text(
                '{"url": "file:///host/checkout", "dir_info": {"editable": true}}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                _BUILDER.ImageBuildError, "editable or direct-local"
            ):
                _BUILDER._validate_relocatable_venv(venv)

    def test_noneditable_direct_local_metadata_is_rejected_before_rootfs_copy(
        self,
    ) -> None:
        """@brief 非 editable 的本地 direct URL 也不能进入 image / A non-editable local direct URL must also stay out of an image.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            metadata = (
                venv / "lib" / "python3.14" / "site-packages" / "demo-1.0.dist-info"
            )
            metadata.mkdir(parents=True)
            (metadata / "direct_url.json").write_text(
                '{"url": "FILE:///host/checkout", "dir_info": {}}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                _BUILDER.ImageBuildError, "editable or direct-local"
            ):
                _BUILDER._validate_relocatable_venv(venv)

    def test_empty_noneditable_venv_is_accepted_as_relocatable(self) -> None:
        """@brief 无 executable hook/local metadata 的 venv 仍是合法 production 输入 / A venv without executable hooks or local metadata remains a valid production input.

        @return None / None.
        """

        with tempfile.TemporaryDirectory() as directory:
            venv = Path(directory) / "venv"
            site_packages = venv / "lib" / "python3.14" / "site-packages"
            site_packages.mkdir(parents=True)
            (site_packages / "legacy-path-only.pth").write_text(
                "/approved/source\n", encoding="utf-8"
            )

            _BUILDER._validate_relocatable_venv(venv)

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

    def test_ldconfig_wrapper_may_be_a_script_but_runtime_programs_must_be_elf(
        self,
    ) -> None:
        """@brief distribution 的 ldconfig wrapper 可为 script，runtime 程序仍必须为 ELF / A distribution ldconfig wrapper may be a script while runtime programs still require ELF."""

        with tempfile.TemporaryDirectory() as directory:
            wrapper = Path(directory) / "ldconfig-wrapper"
            wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            wrapper.chmod(0o755)
            self.assertEqual(
                _BUILDER._validate_executable_input(
                    wrapper, "ldconfig", require_elf=False
                ),
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
            self.assertEqual(
                (first_destination / "marker").read_text(encoding="utf-8"), "first"
            )
            second_staging = root / "second-staging"
            second_staging.mkdir()
            with self.assertRaises(_BUILDER.ImageBuildError):
                _BUILDER._rename_noreplace(second_staging, first_destination)
            self.assertTrue(second_staging.is_dir())
            self.assertEqual(
                (first_destination / "marker").read_text(encoding="utf-8"), "first"
            )

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
            resolved, logical_path = assembler._resolve_needed_library(
                source, metadata, "libdemo.so"
            )
            self.assertEqual(resolved, x64_library)
            self.assertEqual(logical_path, x64_library)
            self.assertNotEqual(_BUILDER._read_elf_abi(x32_library), source_abi)


if __name__ == "__main__":
    unittest.main()
