#!/usr/bin/env python
"""@brief 构建受控 wspctl 不可变 base generation / Build a controlled wspctl immutable base generation.

该工具只属于 host control plane（主机控制平面）：所有输入都是部署者提供的绝对可信路径，
从不接收 Bot、Telegram 或 workspace payload 的路径。它先在 artifact store 同一文件系统的私有
staging 目录构建 rootfs，再让 ``wspctl-image --seal`` 写入唯一权威 manifest，最后以
``renameat2(RENAME_NOREPLACE)`` 原子发布 generation。/
This tool belongs only to the host control plane: every input is an absolute trusted path supplied
by an operator, never a Bot, Telegram, or workspace-payload path.  It builds in a private staging
directory on the artifact store filesystem, delegates the authoritative manifest to
``wspctl-image --seal``, then atomically publishes with ``renameat2(RENAME_NOREPLACE)``.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import errno
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final


#: @brief 运行时内 venv 位置 / Runtime-internal venv location.
_RUNTIME_VENV: Final = PurePosixPath("/opt/wspctl/venv")
#: @brief 运行时内显式复制的 Python 源位置 / Runtime-internal location for explicitly copied Python sources.
_RUNTIME_PYTHON_SOURCES: Final = PurePosixPath("/opt/wspctl/python-source")
#: @brief 保持 CPython `$ORIGIN/../lib` 关系的 base interpreter 位置 / Base-interpreter location preserving CPython `$ORIGIN/../lib`.
_RUNTIME_CPYTHON_DIRECTORY: Final = PurePosixPath("/usr/local/bin")
#: @brief Runtime PID 1 固定位置 / Fixed runtime PID 1 location.
_RUNTIME_SUPERVISOR: Final = PurePosixPath("/usr/local/libexec/wspctl/wsp-systemd")
#: @brief verifier 与 native sealer 共用的 manifest 文件名 / Manifest file name shared by verifier and native sealer.
_MANIFEST_NAME: Final = ".wspctl-image-manifest"
#: @brief 单次重写文本的最大大小 / Maximum size of one rewritten text file.
_MAX_REWRITTEN_TEXT_BYTES: Final = 2 * 1024 * 1024
#: @brief 不复制进不可变镜像的 Python bytecode cache 后缀 / Python bytecode-cache suffixes omitted from immutable images.
_PYTHON_BYTECODE_CACHE_SUFFIXES: Final = frozenset({".pyc", ".pyo"})
#: @brief 不支持的 ``.pth`` startup hook 错误 / Error for an unsupported ``.pth`` startup hook.
_EXECUTABLE_PTH_ERROR: Final = (
    "venv contains an unsupported executable .pth startup hook; the controlled production image accepts only "
    "path-only .pth files or the verified scikit-build-core PEP 660 exception"
)
#: @brief 受支持 scikit-build-core PEP 660 helper 的模块名 / Module-name pattern for the supported scikit-build-core PEP 660 helper.
_SCIKIT_BUILD_EDITABLE_MODULE_PATTERN: Final = re.compile(
    r"_editable_skbc_[a-z][a-z0-9_]*\Z"
)
#: @brief 受支持 scikit-build-core ``.pth`` 的唯一语句 / Sole statement allowed in a supported scikit-build-core ``.pth``.
_SCIKIT_BUILD_EDITABLE_PTH_PATTERN: Final = re.compile(
    r"import (?P<module>[A-Za-z_][A-Za-z0-9_]*)\Z"
)
#: @brief 显式允许进入 runtime 的 GNU 基础工具 basename / GNU basic-tool basenames explicitly allowed into the runtime.
_ALLOWED_GNU_COMMANDS: Final = frozenset(
    {
        "awk",
        "basename",
        "cat",
        "chmod",
        "cksum",
        "cmp",
        "comm",
        "cp",
        "cut",
        "date",
        "dd",
        "diff",
        "dirname",
        "du",
        "echo",
        "env",
        "expand",
        "expr",
        "false",
        "find",
        "fmt",
        "fold",
        "grep",
        "head",
        "id",
        "join",
        "ln",
        "ls",
        "mkdir",
        "mktemp",
        "mv",
        "paste",
        "printf",
        "pwd",
        "readlink",
        "realpath",
        "rm",
        "rmdir",
        "sed",
        "seq",
        "sha256sum",
        "sleep",
        "sort",
        "stat",
        "tail",
        "tee",
        "test",
        "touch",
        "tr",
        "true",
        "tsort",
        "uname",
        "uniq",
        "wc",
        "whoami",
        "xargs",
        "yes",
    }
)
#: @brief 与 native verifier 相同的 generation 字符集 / Generation character set matching the native verifier.
_GENERATION_PATTERN: Final = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
#: @brief ELF DT_NEEDED 行解析器 / Parser for an ELF DT_NEEDED line.
_NEEDED_PATTERN: Final = re.compile(r"\(NEEDED\).*?\[(?P<name>[^]\n]+)\]")
#: @brief ELF RPATH/RUNPATH 行解析器 / Parser for an ELF RPATH or RUNPATH line.
_SEARCH_PATH_PATTERN: Final = re.compile(
    r"\((?P<kind>RPATH|RUNPATH)\).*?\[(?P<paths>[^]\n]*)\]"
)
#: @brief ELF PT_INTERP 行解析器 / Parser for an ELF PT_INTERP line.
_INTERPRETER_PATTERN: Final = re.compile(
    r"Requesting program interpreter: (?P<path>[^]\n]+)"
)
#: @brief ldconfig cache 行解析器 / Parser for one ``ldconfig -p`` cache line.
_LDCONFIG_PATTERN: Final = re.compile(r"^\s*(?P<name>\S+)\s+.*?=>\s+(?P<path>/\S+)\s*$")
#: @brief 解析 ABI 所需的最短 ELF header 字节数 / Minimum ELF-header bytes needed to parse the ABI.
_ELF_ABI_HEADER_BYTES: Final = 20
#: @brief ELF magic / ELF magic bytes.
_ELF_MAGIC: Final = b"\x7fELF"
#: @brief ELF identification 中 class 的索引 / Index of ELF class in e_ident.
_ELF_CLASS_INDEX: Final = 4
#: @brief ELF identification 中 data encoding 的索引 / Index of ELF data encoding in e_ident.
_ELF_DATA_INDEX: Final = 5
#: @brief ELFCLASS32 值 / ELFCLASS32 value.
_ELFCLASS32: Final = 1
#: @brief ELFCLASS64 值 / ELFCLASS64 value.
_ELFCLASS64: Final = 2
#: @brief ELFDATA2LSB 值 / ELFDATA2LSB value.
_ELFDATA2LSB: Final = 1
#: @brief ELFDATA2MSB 值 / ELFDATA2MSB value.
_ELFDATA2MSB: Final = 2
#: @brief ELF e_machine 在 ELF header 内的偏移 / Offset of ELF e_machine within the ELF header.
_ELF_MACHINE_OFFSET: Final = 18
#: @brief Linux renameat2 的 AT_FDCWD / Linux ``renameat2`` AT_FDCWD value.
_AT_FDCWD: Final = -100
#: @brief Linux renameat2 的不可覆盖标志 / Linux ``renameat2`` no-replace flag.
_RENAME_NOREPLACE: Final = 1
#: @brief ELF 闭包可以复制到原运行时绝对位置的系统库根 / System library roots safe to retain as runtime absolute paths.
_SYSTEM_LIBRARY_ROOTS: Final = (
    PurePosixPath("/lib"),
    PurePosixPath("/lib64"),
    PurePosixPath("/usr/lib"),
    PurePosixPath("/usr/lib64"),
    PurePosixPath("/usr/local/lib"),
)


class ImageBuildError(RuntimeError):
    """@brief 可信 image build 的 fail-closed 错误 / Fail-closed error raised by the trusted image build."""


def _is_executable_pth_line(line: str) -> bool:
    """@brief 判断一行是否会被 Python ``site`` 当作可执行 ``.pth`` hook / Determine whether Python ``site`` executes a line as a ``.pth`` hook.

    @param line 未修改的 UTF-8 ``.pth`` 单行 / One unmodified UTF-8 ``.pth`` line.
    @return 以 Python ``site`` 的 ``import`` 规则执行时为真 / True when Python ``site`` executes it under its ``import`` rule.

    @note 不先 ``strip``：``site`` 只将首字符就是 ``import``，且紧随空格或 tab 的行解释为
        code。/ Do not ``strip`` first: ``site`` treats a line as code only when its first
        characters are ``import`` followed by a space or tab.
    """

    return line.startswith("import ") or line.startswith("import\t")


def _is_omitted_python_cache_entry(path: Path) -> bool:
    """@brief 判断递归复制时是否应忽略 Python bytecode cache / Determine whether recursive copying must omit a Python bytecode cache.

    @param path 当前正在枚举的 source entry / Source entry currently being enumerated.
    @return ``__pycache__`` 目录或 legacy/current bytecode 文件时为真 /
        True for a ``__pycache__`` directory or a legacy/current bytecode file.

    @note ``.pyc`` code object 的 ``co_filename`` 通常保留构建 host 的绝对 source path。它们
        在只读 lower layer 中既非运行所需，也会把 host checkout 细节带进 runtime；因此对
        CPython stdlib、venv 和显式 ``--python-source`` 的所有递归复制一视同仁地跳过。/
        A ``.pyc`` code object's ``co_filename`` usually retains the build host's absolute source
        path. It is neither required in a read-only lower layer nor safe to carry host checkout
        details into the runtime, so every recursive copy—CPython stdlib, venv, and explicit
        ``--python-source`` alike—omits it.
    """

    return path.name == "__pycache__" or path.suffix in _PYTHON_BYTECODE_CACHE_SUFFIXES


@dataclass(frozen=True, slots=True)
class ScikitBuildEditableHook:
    """@brief 已验证、可重定位的 scikit-build-core PEP 660 hook / Verified relocatable scikit-build-core PEP 660 hook.

    @param pth Python ``site`` 读取的单行 ``.pth`` 文件 / Single-line ``.pth`` file read by Python ``site``.
    @param helper 与 ``.pth`` 同目录的 helper module / Helper module next to the ``.pth`` file.
    @param module helper 的安全 Python module 名 / Safe Python module name of the helper.
    @param source_roots helper terminal mapping 实际引用的显式 source roots /
        Explicit source roots actually referenced by the helper terminal mapping.
    """

    pth: Path
    helper: Path
    module: str
    source_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class EditableDirectUrlRelocation:
    """@brief 一个与已验证 PEP 660 helper 绑定的 direct-url 重写 / Direct-url rewrite bound to a verified PEP 660 helper.

    @param metadata PEP 610 ``direct_url.json`` 路径 / Path of the PEP 610 ``direct_url.json``.
    @param source_root 被重写为 runtime URL 的显式 source root / Explicit source root rewritten into the runtime URL.
    """

    metadata: Path
    source_root: Path


@dataclass(frozen=True, slots=True)
class VenvRelocationPlan:
    """@brief 一次 venv 重定位的已验证 executable-hook 例外 / Verified executable-hook exceptions for one venv relocation.

    @param scikit_build_hooks 仅允许的 scikit-build-core PEP 660 hooks / The only permitted scikit-build-core PEP 660 hooks.
    @param editable_direct_urls 与上述 hooks 一一绑定的 editable direct-url metadata /
        Editable direct-url metadata bound one-to-one to the hooks above.
    """

    scikit_build_hooks: tuple[ScikitBuildEditableHook, ...] = ()
    editable_direct_urls: tuple[EditableDirectUrlRelocation, ...] = ()


def _read_bounded_regular_text(path: Path, description: str) -> str:
    """@brief 以 no-follow 语义读取一个有界 UTF-8 regular file / Read one bounded UTF-8 regular file with no-follow semantics.

    @param path 待读取的可信输入路径 / Trusted input path to read.
    @param description 诊断中的文件语义 / File role used in diagnostics.
    @return 完整 UTF-8 文本 / Complete UTF-8 text.
    @raise ImageBuildError 文件不是 regular inode、过大、不可读取或非 UTF-8 时抛出 /
        Raised when the file is not a regular inode, is too large, cannot be read, or is not UTF-8.
    """

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        raise ImageBuildError(
            f"cannot open {description} without following links"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 0
            or metadata.st_size > _MAX_REWRITTEN_TEXT_BYTES
        ):
            raise ImageBuildError(f"{description} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_REWRITTEN_TEXT_BYTES + 1
        while remaining > 0:
            try:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
            except InterruptedError:
                continue
            except OSError as error:
                raise ImageBuildError(f"cannot read {description}") from error
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_REWRITTEN_TEXT_BYTES:
            raise ImageBuildError(f"{description} exceeds the relocatable-text limit")
    finally:
        os.close(descriptor)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ImageBuildError(f"{description} must be UTF-8") from error


def _normalized_distribution_name(value: str) -> str:
    """@brief 归一化 Python distribution 名称 / Normalize a Python distribution name.

    @param value 原始 distribution 或 helper suffix / Raw distribution name or helper suffix.
    @return 小写、以 ``_`` 连接的比较键 / Lowercase comparison key joined by ``_``.
    """

    return re.sub(r"[-_.]+", "_", value).strip("_").lower()


def _parse_scikit_build_editable_hook(
    pth: Path,
    data: str,
    source_roots: tuple[Path, ...],
) -> ScikitBuildEditableHook:
    """@brief 解析一个严格受限的 scikit-build-core PEP 660 hook / Parse one strictly constrained scikit-build-core PEP 660 hook.

    @param pth 已 no-follow 读取的 ``.pth`` 路径 / ``.pth`` path already read with no-follow semantics.
    @param data ``.pth`` 的 UTF-8 文本 / UTF-8 text of the ``.pth``.
    @param source_roots operator 显式批准的 canonical source roots / Canonical source roots explicitly approved by the operator.
    @return 只含显式 source mapping 的已验证 hook / Verified hook containing explicit source mappings only.
    @raise ImageBuildError hook 不是已支持格式、helper 不在同一 site-packages 或 host mapping
        未被 ``--python-source`` 承认时抛出 / Raised when the hook is not a supported shape, the
        helper is outside the site-packages directory, or a host mapping is not admitted by
        ``--python-source``.
    @note 这里不执行 helper。它只接受本项目实际使用的 ``_editable_skbc_*`` 单行 import
        形式，检查 helper 的 terminal ``install(...)`` mapping，并只重写其中已经存在的绝对
        source 路径。/ This never executes the helper. It accepts only the single-line
        ``_editable_skbc_*`` import form used by this project, checks the helper's terminal
        ``install(...)`` mapping, and rewrites only pre-existing absolute source paths.
    """

    lines = data.splitlines()
    match = _SCIKIT_BUILD_EDITABLE_PTH_PATTERN.fullmatch(lines[0]) if lines else None
    if match is None:
        raise ImageBuildError(_EXECUTABLE_PTH_ERROR)
    module = match.group("module")
    if (
        _SCIKIT_BUILD_EDITABLE_MODULE_PATTERN.fullmatch(module) is None
        or pth.stem != module
    ):
        raise ImageBuildError(
            "unsupported executable .pth hook; only _editable_skbc_* single-import hooks are admitted"
        )
    helper = pth.with_name(f"{module}.py")
    helper_text = _read_bounded_regular_text(helper, "scikit-build editable helper")
    try:
        parsed = ast.parse(helper_text, filename=str(helper), mode="exec")
    except SyntaxError as error:
        raise ImageBuildError(
            "scikit-build editable helper is not valid Python"
        ) from error
    if not any(
        isinstance(statement, ast.ClassDef)
        and statement.name == "ScikitBuildRedirectingFinder"
        for statement in parsed.body
    ):
        raise ImageBuildError(
            "scikit-build editable helper lacks ScikitBuildRedirectingFinder"
        )
    if not any(
        isinstance(statement, ast.FunctionDef) and statement.name == "install"
        for statement in parsed.body
    ):
        raise ImageBuildError("scikit-build editable helper lacks install")
    if (
        not parsed.body
        or not isinstance(parsed.body[-1], ast.Expr)
        or not isinstance(parsed.body[-1].value, ast.Call)
    ):
        raise ImageBuildError(
            "scikit-build editable helper lacks a terminal install call"
        )
    terminal_call = parsed.body[-1].value
    if (
        not isinstance(terminal_call.func, ast.Name)
        or terminal_call.func.id != "install"
        or terminal_call.keywords
    ):
        raise ImageBuildError(
            "scikit-build editable helper terminal call is unsupported"
        )
    arguments = terminal_call.args
    if (
        len(arguments) != 10
        or not all(isinstance(arguments[index], ast.Dict) for index in range(3))
        or not isinstance(arguments[3], ast.List)
        or not isinstance(arguments[4], ast.Constant)
        or arguments[4].value is not None
        or not isinstance(arguments[5], ast.Constant)
        or not isinstance(arguments[5].value, bool)
        or not isinstance(arguments[6], ast.Constant)
        or not isinstance(arguments[6].value, bool)
        or not isinstance(arguments[7], ast.List)
        or not isinstance(arguments[8], ast.List)
        or not isinstance(arguments[9], ast.Constant)
        or arguments[9].value not in {None, ""}
    ):
        raise ImageBuildError(
            "scikit-build editable helper install signature is unsupported"
        )
    source_files = arguments[0]
    wheel_files = arguments[1]
    source_directories = arguments[2]
    packages = arguments[3]
    for key, value in zip(source_files.keys, source_files.values, strict=True):
        if (
            not isinstance(key, ast.Constant)
            or not isinstance(key.value, str)
            or not isinstance(value, ast.Constant)
            or not isinstance(value.value, str)
        ):
            raise ImageBuildError(
                "scikit-build editable helper source-file mapping must contain only string literals"
            )
    for key, value in zip(wheel_files.keys, wheel_files.values, strict=True):
        if (
            not isinstance(key, ast.Constant)
            or not isinstance(key.value, str)
            or not isinstance(value, ast.Constant)
            or not isinstance(value.value, str)
        ):
            raise ImageBuildError(
                "scikit-build editable helper wheel mapping must contain only string literals"
            )
        wheel_path = PurePosixPath(value.value)
        if (
            not value.value
            or wheel_path.is_absolute()
            or ".." in wheel_path.parts
            or "\\" in value.value
            or "\x00" in value.value
        ):
            raise ImageBuildError(
                "scikit-build editable helper wheel mapping must be runtime-relative"
            )
    for key, value in zip(
        source_directories.keys, source_directories.values, strict=True
    ):
        if (
            not isinstance(key, ast.Constant)
            or not isinstance(key.value, str)
            or not isinstance(value, ast.List)
            or not all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in value.elts
            )
        ):
            raise ImageBuildError(
                "scikit-build editable helper source-directory mapping must contain only string literals"
            )
    if not all(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in packages.elts
    ):
        raise ImageBuildError(
            "scikit-build editable helper package list must contain only string literals"
        )
    used_roots: set[Path] = set()
    for line in lines[1:]:
        if _is_executable_pth_line(line):
            raise ImageBuildError(_EXECUTABLE_PTH_ERROR)
        if not line.startswith("/") or line != line.strip():
            raise ImageBuildError(
                "scikit-build editable .pth may contain only absolute path-only source lines after its import"
            )
        try:
            resolved = Path(line).resolve(strict=True)
        except OSError as error:
            raise ImageBuildError(
                "scikit-build editable .pth references a missing source path"
            ) from error
        matches = tuple(
            root for root in source_roots if _is_relative_to(resolved, root)
        )
        if len(matches) != 1:
            raise ImageBuildError(
                "scikit-build editable .pth source is not uniquely admitted by --python-source"
            )
        used_roots.add(matches[0])
    for node in ast.walk(parsed):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
            or node.value in {"", "/"}
        ):
            continue
        literal = node.value
        if not literal.startswith("/"):
            continue
        try:
            resolved = Path(literal).resolve(strict=True)
        except OSError as error:
            raise ImageBuildError(
                "scikit-build editable helper references a missing absolute path"
            ) from error
        matches = tuple(
            root for root in source_roots if _is_relative_to(resolved, root)
        )
        if len(matches) != 1:
            raise ImageBuildError(
                "scikit-build editable helper absolute mapping is not uniquely admitted by --python-source"
            )
        used_roots.add(matches[0])
    if not used_roots:
        raise ImageBuildError(
            "scikit-build editable helper contains no admitted source mapping"
        )
    return ScikitBuildEditableHook(
        pth=pth,
        helper=helper,
        module=module,
        source_roots=tuple(sorted(used_roots, key=str)),
    )


def _distribution_name_from_metadata(metadata_directory: Path) -> str:
    """@brief 读取一个 dist-info 的 distribution Name / Read the distribution Name from one dist-info directory.

    @param metadata_directory ``*.dist-info`` directory / ``*.dist-info`` directory.
    @return 归一化 distribution comparison key / Normalized distribution comparison key.
    @raise ImageBuildError ``METADATA`` 缺失、格式不安全或没有 Name 字段时抛出 /
        Raised when ``METADATA`` is missing, malformed, or has no Name field.
    """

    if not metadata_directory.name.endswith(".dist-info"):
        raise ImageBuildError(
            "editable direct_url metadata is not inside a dist-info directory"
        )
    metadata = _read_bounded_regular_text(
        metadata_directory / "METADATA", "editable distribution METADATA"
    )
    for line in metadata.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.lower() == "name" and value.strip():
            return _normalized_distribution_name(value.strip())
    raise ImageBuildError("editable distribution METADATA has no Name")


def _validate_relocatable_venv(
    venv: Path,
    *,
    python_sources: Sequence[Path] = (),
) -> VenvRelocationPlan:
    """@brief 校验并计划一个可封闭重定位的 Python virtual environment / Validate and plan a closed relocatable Python virtual environment.

    @param venv 已规范化的可信 virtual-environment 根 / Canonical trusted virtual-environment root.
    @param python_sources 显式批准、会复制到 runtime 的 source roots / Explicit approved source roots copied into the runtime.
    @return path-only 文件及受限 PEP 660 exceptions 的重定位计划 / Relocation plan for path-only files and constrained PEP 660 exceptions.
    @raise ImageBuildError 未知 startup hook、不可归属的 editable metadata 或 host path 未受批准时抛出 /
        Raised on an unknown startup hook, unbound editable metadata, or a host path outside approved sources.

    @note Python ``site`` 会在每次解释器启动时执行以 ``import`` 开头的 ``.pth`` 行。通用
        startup code 仍一律拒绝；唯一例外是可静态识别为 scikit-build-core PEP 660 helper 的
        `_editable_skbc_*` hook，且其 terminal mapping 的每个绝对路径都必须落在
        ``--python-source``。/ Python ``site`` executes ``.pth`` lines beginning with ``import``
        at every interpreter startup. Generic startup code remains rejected; the sole exception is
        a statically recognizable scikit-build-core PEP 660 `_editable_skbc_*` hook whose every
        terminal-mapping absolute path lies below ``--python-source``.
    """

    site_packages = venv / "lib"
    if not site_packages.is_dir():
        raise ImageBuildError("venv must contain a lib directory")
    try:
        source_roots = tuple(
            sorted({source.resolve(strict=True) for source in python_sources}, key=str)
        )
    except OSError as error:
        raise ImageBuildError("cannot resolve an admitted python source") from error
    if any(not source.is_dir() for source in source_roots):
        raise ImageBuildError("admitted python source must be a directory")
    hooks: list[ScikitBuildEditableHook] = []
    for pth in sorted(site_packages.rglob("*.pth"), key=lambda item: str(item)):
        data = _read_bounded_regular_text(pth, "venv .pth file")
        lines = data.splitlines()
        executable_lines = [line for line in lines if _is_executable_pth_line(line)]
        if not executable_lines:
            continue
        if len(executable_lines) != 1:
            raise ImageBuildError(_EXECUTABLE_PTH_ERROR)
        hooks.append(_parse_scikit_build_editable_hook(pth, data, source_roots))
    hook_by_distribution: dict[str, ScikitBuildEditableHook] = {}
    for hook in hooks:
        distribution = _normalized_distribution_name(
            hook.module.removeprefix("_editable_skbc_")
        )
        if not distribution or distribution in hook_by_distribution:
            raise ImageBuildError(
                "multiple scikit-build editable helpers claim one distribution"
            )
        hook_by_distribution[distribution] = hook
    direct_url_relocations: list[EditableDirectUrlRelocation] = []
    for direct_url in sorted(
        site_packages.rglob("direct_url.json"), key=lambda item: str(item)
    ):
        try:
            metadata = json.loads(
                _read_bounded_regular_text(direct_url, "venv direct_url metadata")
            )
        except json.JSONDecodeError as error:
            raise ImageBuildError("cannot parse venv direct_url metadata") from error
        if not isinstance(metadata, dict):
            raise ImageBuildError("venv direct_url metadata must be an object")
        url = metadata.get("url")
        directory_info = metadata.get("dir_info")
        editable = (
            isinstance(directory_info, dict) and directory_info.get("editable") is True
        )
        direct_local = isinstance(url, str) and url.lower().startswith("file:")
        if not editable and not direct_local:
            continue
        try:
            distribution = _distribution_name_from_metadata(direct_url.parent)
        except ImageBuildError as error:
            raise ImageBuildError(
                "venv contains an editable or direct-local distribution outside the supported scikit-build relocation contract"
            ) from error
        hook = hook_by_distribution.get(distribution)
        if (
            hook is None
            or not editable
            or not isinstance(url, str)
            or set(metadata) != {"url", "dir_info"}
            or directory_info != {"editable": True}
        ):
            raise ImageBuildError(
                "venv contains an editable or direct-local distribution outside the supported scikit-build relocation contract"
            )
        parsed_url = urllib.parse.urlsplit(url)
        if (
            parsed_url.scheme != "file"
            or parsed_url.netloc
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ImageBuildError(
                "supported editable direct_url must be a local file URL without query or fragment"
            )
        try:
            checkout_root = Path(urllib.parse.unquote(parsed_url.path)).resolve(
                strict=True
            )
        except OSError as error:
            raise ImageBuildError(
                "supported editable direct_url does not resolve to an existing checkout root"
            ) from error
        candidates = tuple(
            source_root
            for source_root in hook.source_roots
            if checkout_root == source_root or checkout_root == source_root.parent
        )
        if len(candidates) != 1:
            raise ImageBuildError(
                "editable direct_url checkout root does not match the helper's admitted source root"
            )
        direct_url_relocations.append(
            EditableDirectUrlRelocation(metadata=direct_url, source_root=candidates[0])
        )
    return VenvRelocationPlan(
        scikit_build_hooks=tuple(hooks),
        editable_direct_urls=tuple(direct_url_relocations),
    )


@dataclass(frozen=True, slots=True)
class PythonRuntimeLayout:
    """@brief 从指定 venv 探测出的 CPython 布局 / CPython layout probed from the selected venv.

    @param venv 可信项目 venv 的规范目录 / Canonical trusted project-venv directory.
    @param base_prefix CPython distribution 根 / CPython-distribution root.
    @param interpreter 解析后的 CPython executable / Resolved CPython executable.
    @param standard_library CPython standard-library root / CPython standard-library root.
    @param library_directory CPython shared-library directory / CPython shared-library directory.
    @param version Python major.minor ABI 版本 / Python major.minor ABI version.
    """

    venv: Path
    base_prefix: Path
    interpreter: Path
    standard_library: Path
    library_directory: Path
    version: str


@dataclass(frozen=True, slots=True)
class PythonRuntimeProfile:
    """@brief 一个明确的 CPython runtime 内容契约 / An explicit CPython runtime-content contract.

    ``wspctl`` 的 base image 不是通用桌面 Python distribution。profile 以 source path 为准，
    在进入 ELF closure 之前排除不属于该 runtime 的 stdlib subtree 和 extension module；因此它不会
    通过忽略一个已复制 ELF 的缺失 dependency 来改变 closure 的 fail-closed 语义。/
    A ``wspctl`` base image is not a general-purpose desktop Python distribution.  This profile
    selects by source path and excludes stdlib subtrees and extension modules before they enter
    the ELF closure; it therefore does not weaken fail-closed closure semantics by ignoring a
    missing dependency of an already-copied ELF.

    @param identifier 稳定、可审计的 profile 标识 / Stable auditable profile identifier.
    @param omitted_stdlib_roots 必须从 stdlib 根排除的顶层 entry 名 / Top-level entry names omitted from stdlib.
    @param omitted_dynload_modules 必须从 ``lib-dynload`` 排除的 extension module 名 /
        Extension-module names omitted from ``lib-dynload``.
    """

    identifier: str
    omitted_stdlib_roots: frozenset[str]
    omitted_dynload_modules: frozenset[str]

    def omits_standard_library_path(self, standard_library: Path, source: Path) -> bool:
        """@brief 判断一个 lexical stdlib path 是否不属于 profile / Decide whether one lexical stdlib path is outside the profile.

        @param standard_library CPython ``sysconfig`` 返回的 stdlib root / CPython stdlib root returned by ``sysconfig``.
        @param source 待复制的 lexical source path；不预先 resolve symlink /
            Lexical source path considered for copying; deliberately not pre-resolved through symlinks.
        @return 被 profile 排除时为真 / True when the profile excludes it.

        @note 递归器还会把这个 predicate 应用于 symlink 的 resolved target，避免一个不在
            allowlist 中的 alias 把排除模块重新带回 image。/ The recursive copier also applies
            this predicate to a symlink's resolved target, so an alias outside the allowlist cannot
            reintroduce an excluded module into the image.
        """

        try:
            relative = source.relative_to(standard_library)
        except ValueError:
            return False
        if not relative.parts:
            return False
        if relative.parts[0] in self.omitted_stdlib_roots:
            return True
        if len(relative.parts) != 2 or relative.parts[0] != "lib-dynload":
            return False
        extension_module = relative.parts[1].split(".", maxsplit=1)[0]
        return extension_module in self.omitted_dynload_modules


#: @brief 供无桌面 Bot workspace 使用的 CPython profile / CPython profile for the headless Bot workspace.
#:
#: Tcl/Tk 仅是 CPython 的 optional module；``idlelib``、``turtle`` 与 ``turtledemo`` 都建立在
#: Tk GUI 上。这个 profile 保留完整的非 GUI stdlib 与所有其余 extension modules，因而任何真正
#: 纳入 image 的 ELF 仍必须拥有完整、ABI 匹配的 dependency closure。/
#: Tcl/Tk is an optional CPython module; ``idlelib``, ``turtle``, and ``turtledemo`` all depend on
#: the Tk GUI.  This profile retains the complete non-GUI stdlib and every other extension module,
#: so each ELF actually admitted to the image must still have a complete ABI-matching dependency
#: closure.
_HEADLESS_PYTHON_RUNTIME_PROFILE: Final = PythonRuntimeProfile(
    identifier="headless-python-v1",
    omitted_stdlib_roots=frozenset({"idlelib", "tkinter", "turtledemo", "turtle.py"}),
    omitted_dynload_modules=frozenset({"_tkinter"}),
)


@dataclass(frozen=True, slots=True)
class ElfAbi:
    """@brief ELF loader ABI 身份 / ELF loader ABI identity.

    ``ldconfig -p`` 可以在同一 SONAME 下列出 x86-64、x32、i386 等多个对象。动态链接器不会
    把不同 ELF class、endianness 或 machine 的对象当作同一候选；builder 必须在复制前表达并检查
    同一约束。/ ``ldconfig -p`` may list x86-64, x32, i386, and other objects under one SONAME.
    The dynamic loader never treats different ELF classes, endiannesses, or machines as equivalent;
    the builder must represent and check the same constraint before copying.

    @param elf_class ELFCLASS32 或 ELFCLASS64 / ELFCLASS32 or ELFCLASS64.
    @param data_encoding ELFDATA2LSB 或 ELFDATA2MSB / ELFDATA2LSB or ELFDATA2MSB.
    @param machine ELF ``e_machine`` 值 / ELF ``e_machine`` value.
    """

    elf_class: int
    data_encoding: int
    machine: int


@dataclass(frozen=True, slots=True)
class ElfMetadata:
    """@brief 一个 ELF 文件的 loader 所需元数据 / Loader-relevant metadata for one ELF file.

    @param needed DT_NEEDED SONAME 序列 / DT_NEEDED SONAME sequence.
    @param interpreter PT_INTERP loader 路径；共享库为 None / PT_INTERP loader path, or None for shared libraries.
    @param search_paths 解析依赖使用的 RPATH/RUNPATH / RPATH/RUNPATH used to resolve dependencies.
    @param abi ELF class、endianness 与 machine / ELF class, endianness, and machine.
    """

    needed: tuple[str, ...]
    interpreter: str | None
    search_paths: tuple[Path, ...]
    abi: ElfAbi


@dataclass(frozen=True, slots=True)
class BuildSpec:
    """@brief 一次受控 generation build 的不可变输入 / Immutable inputs for one controlled generation build.

    @param generation 不含路径语义的 generation 名称 / Generation name without path semantics.
    @param output_root root-owned artifact store 根 / Root-owned artifact-store root.
    @param venv 可信项目 Python venv / Trusted project Python venv.
    @param python_sources 显式允许的 path-only ``.pth`` Python source roots / Explicitly admitted path-only ``.pth`` Python-source roots.
    @param bash 可信 Bash executable / Trusted Bash executable.
    @param gnu_commands 显式 allowlisted GNU command executables / Explicitly allowlisted GNU-command executables.
    @param supervisor 已构建的 wsp-systemd / Built wsp-systemd executable.
    @param sealer 已构建的 wspctl-image / Built wspctl-image executable.
    @param readelf 只读 ELF metadata reader / Read-only ELF metadata reader.
    @param ldconfig 动态 loader cache reader / Dynamic-loader cache reader.
    @param venv_relocation 已验证的 venv startup-hook 重定位计划 / Verified venv startup-hook relocation plan.
    @param python_runtime_profile 固定的 CPython runtime 内容契约 / Fixed CPython runtime-content contract.
    """

    generation: str
    output_root: Path
    venv: Path
    python_sources: tuple[Path, ...]
    bash: Path
    gnu_commands: tuple[Path, ...]
    supervisor: Path
    sealer: Path
    readelf: Path
    ldconfig: Path
    venv_relocation: VenvRelocationPlan = VenvRelocationPlan()
    python_runtime_profile: PythonRuntimeProfile = _HEADLESS_PYTHON_RUNTIME_PROFILE


def _is_relative_to(path: Path, parent: Path) -> bool:
    """@brief 判断路径是否位于 parent 下 / Determine whether a path is below a parent.

    @param path 待检查的规范路径 / Canonical path to inspect.
    @param parent 规范父目录 / Canonical parent directory.
    @return 位于 parent（含自身）时为真 / True when path is beneath parent, including itself.
    """

    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_absolute_existing(
    path_text: str, description: str, *, directory: bool
) -> Path:
    """@brief 解析一个明确的可信绝对输入 / Resolve one explicit trusted absolute input.

    @param path_text CLI 提供的路径文本 / Path text supplied on the CLI.
    @param description 诊断中的输入语义 / Input meaning used in diagnostics.
    @param directory 是否必须为目录 / Whether the input must be a directory.
    @return 存在、规范且类型正确的路径 / Existing canonical path of the required type.
    @raise ImageBuildError 路径不是绝对、安全类型或无法解析时抛出 /
        Raised when the path is not absolute, has an unsafe type, or cannot be resolved.
    """

    if not path_text or "\x00" in path_text:
        raise ImageBuildError(f"{description} must be non-empty and NUL-free")
    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise ImageBuildError(f"{description} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as error:
        raise ImageBuildError(f"cannot resolve {description}") from error
    if directory:
        if not stat.S_ISDIR(metadata.st_mode):
            raise ImageBuildError(f"{description} must be a directory")
    elif not stat.S_ISREG(metadata.st_mode):
        raise ImageBuildError(f"{description} must be a regular file")
    return resolved


def _validate_generation(generation: str) -> str:
    """@brief 校验 generation 只能作为单个目录名 / Validate that a generation is only one directory name.

    @param generation operator 选择的 generation / Operator-selected generation.
    @return 原 generation 字符串 / The unchanged generation string.
    @raise ImageBuildError generation 含路径、为空或超长时抛出 /
        Raised when generation carries path semantics, is blank, or is too long.
    """

    if generation in {".", ".."} or _GENERATION_PATTERN.fullmatch(generation) is None:
        raise ImageBuildError(
            "generation must match [A-Za-z0-9_.-]{1,128} and cannot be . or .."
        )
    return generation


def _validate_controlled_directory(
    path: Path,
    description: str,
    *,
    allow_insecure_development_ancestors: bool,
) -> Path:
    """@brief 证明 artifact store 及祖先属于 root control plane / Prove artifact store and ancestors belong to the root control plane.

    @param path 已存在的绝对目录 / Existing absolute directory.
    @param description 诊断中的目录语义 / Directory meaning used in diagnostics.
    @param allow_insecure_development_ancestors 是否仅放宽非终点祖先 / Whether to relax only non-terminal ancestors.
    @return 规范 artifact-store 目录 / Canonical artifact-store directory.
    @raise ImageBuildError 目录、祖先、owner 或 mode 不符合时抛出 /
        Raised when the directory, an ancestor, owner, or mode violates the contract.
    @note 显式 local-development opt-in 仍要求终点目录为 root-owned 且不可 group/other 写；开发者
        在该模式下属于本机控制平面 TCB。/ Explicit local-development opt-in still requires a
        root-owned, non-group/other-writable endpoint; the developer is part of the local control-plane TCB.
    """

    resolved = _require_absolute_existing(str(path), description, directory=True)
    current = resolved
    while True:
        try:
            metadata = os.lstat(current)
        except OSError as error:
            raise ImageBuildError(f"cannot inspect {description} ancestor") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise ImageBuildError(f"{description} ancestor is not a directory")
        terminal = current == resolved
        if (
            metadata.st_uid != 0 or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ) and (terminal or not allow_insecure_development_ancestors):
            raise ImageBuildError(
                f"{description} and every ancestor must be root-owned and non-group/world-writable"
            )
        if current == current.parent:
            return resolved
        current = current.parent


def _runtime_path(path_text: str | PurePosixPath) -> PurePosixPath:
    """@brief 校验 runtime 内绝对 POSIX 路径 / Validate an absolute POSIX path inside the runtime.

    @param path_text runtime 路径 / Runtime path.
    @return 规范化但不解析 symlink 的 runtime 路径 / Normalized runtime path without resolving symlinks.
    @raise ImageBuildError 路径不是安全绝对 POSIX 路径时抛出 /
        Raised when the path is not a safe absolute POSIX path.
    """

    result = PurePosixPath(path_text)
    if not result.is_absolute() or ".." in result.parts or str(result) == "/":
        raise ImageBuildError(
            "runtime destination must be an absolute non-root POSIX path without .."
        )
    return result


def _runtime_relative_link(source: PurePosixPath, target: PurePosixPath) -> str:
    """@brief 将 runtime 内链接目标转换为相对 symlink / Convert an in-runtime link target into a relative symlink.

    @param source 将要创建的 symlink 位置 / Runtime location of the symlink to create.
    @param target runtime 内最终目标 / Final in-runtime target.
    @return 不含 host absolute 路径的相对 link text / Relative link text containing no host absolute path.
    """

    source_path = _runtime_path(source)
    target_path = _runtime_path(target)
    return posixpath.relpath(
        target_path.as_posix(), start=source_path.parent.as_posix()
    )


def _is_elf(path: Path) -> bool:
    """@brief 以魔数判断 regular file 是否为 ELF / Identify whether a regular file has an ELF magic number.

    @param path 已解析的 regular file / Resolved regular file.
    @return 前四字节为 ELF magic 时为真 / True when the first four bytes are the ELF magic.
    """

    try:
        with path.open("rb", buffering=0) as input_file:
            return input_file.read(4) == b"\x7fELF"
    except OSError as error:
        raise ImageBuildError(f"cannot inspect ELF input {path}") from error


def _read_elf_abi(path: Path) -> ElfAbi:
    """@brief 从 no-follow FD 读取 ELF ABI identity / Read an ELF ABI identity from a no-follow FD.

    @param path 已解析的 ELF regular file / Resolved ELF regular file.
    @return ELF class、endianness 与 e_machine / ELF class, endianness, and e_machine.
    @raise ImageBuildError 文件不是 regular ELF、header 截断或 ABI 字段无效时抛出 /
        Raised when the file is not a regular ELF, the header is truncated, or ABI fields are invalid.
    @note 不从 ``ldconfig`` 的 human-readable architecture label 推断 ABI；x32 的 ``e_machine``
        也可能是 x86-64，因此 ELF class 必须一起比较。 This does not infer ABI from an
        ``ldconfig`` human-readable architecture label: x32 may also use x86-64 ``e_machine``,
        so ELF class must be compared together with it.
    """

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise ImageBuildError(
            "host platform does not expose O_NOFOLLOW for ELF ABI inspection"
        )
    flags = os.O_RDONLY | os.O_CLOEXEC | no_follow
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ImageBuildError(f"cannot open ELF ABI input {path}") from error
    try:
        try:
            metadata = os.fstat(descriptor)
        except OSError as error:
            raise ImageBuildError(f"cannot stat ELF ABI input {path}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ImageBuildError("ELF ABI input must be a regular file")
        chunks: list[bytes] = []
        remaining = _ELF_ABI_HEADER_BYTES
        while remaining > 0:
            try:
                chunk = os.read(descriptor, remaining)
            except InterruptedError:
                continue
            except OSError as error:
                raise ImageBuildError(f"cannot read ELF ABI input {path}") from error
            if not chunk:
                raise ImageBuildError("ELF ABI header is truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        header = b"".join(chunks)
    finally:
        os.close(descriptor)
    if header[: len(_ELF_MAGIC)] != _ELF_MAGIC:
        raise ImageBuildError("ELF ABI input lacks ELF magic")
    elf_class = header[_ELF_CLASS_INDEX]
    data_encoding = header[_ELF_DATA_INDEX]
    if elf_class not in {_ELFCLASS32, _ELFCLASS64}:
        raise ImageBuildError("ELF ABI input has an unsupported ELF class")
    if data_encoding == _ELFDATA2LSB:
        byteorder = "little"
    elif data_encoding == _ELFDATA2MSB:
        byteorder = "big"
    else:
        raise ImageBuildError("ELF ABI input has an unsupported data encoding")
    machine = int.from_bytes(
        header[_ELF_MACHINE_OFFSET : _ELF_MACHINE_OFFSET + 2],
        byteorder=byteorder,
        signed=False,
    )
    if machine == 0:
        raise ImageBuildError("ELF ABI input has an invalid e_machine value")
    return ElfAbi(elf_class=elf_class, data_encoding=data_encoding, machine=machine)


def _has_matching_elf_abi(path: Path, required: ElfAbi) -> bool:
    """@brief 判断 candidate 是否与请求 ELF ABI 完全一致 / Determine whether a candidate exactly matches a required ELF ABI.

    @param path candidate regular file / Candidate regular file.
    @param required 依赖源所需 ABI / ABI required by the dependency source.
    @return candidate 是可读 ELF 且 ABI 相同时为真 / True when the candidate is a readable ELF with the same ABI.
    @note cache 中的错误 class/machine 条目只是同 SONAME 的其他 multiarch variant，不能作为
        当前 closure 的依赖。 A cache entry with the wrong class/machine is merely another
        multiarch variant of the same SONAME and cannot serve the current closure.
    """

    try:
        return _read_elf_abi(path) == required
    except ImageBuildError:
        return False


def _sanitized_mode(source_mode: int, *, directory: bool) -> int:
    """@brief 从可信输入派生无 suid/sgid/write 的 image mode / Derive an image mode without suid, sgid, or writable bits.

    @param source_mode 输入 inode mode / Input inode mode.
    @param directory 是否为目录 / Whether the inode is a directory.
    @return 只保留 read/execute 的安全 mode / Safe mode retaining only read and execute permissions.
    """

    if directory:
        return 0o755
    return (
        0o755 if source_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) else 0o644
    )


def _write_all(descriptor: int, data: bytes) -> None:
    """@brief 将全部字节写入一个已打开 FD / Write all bytes to an already-open file descriptor.

    @param descriptor 已打开的 destination FD / Open destination FD.
    @param data 待写入字节 / Bytes to write.
    @return None / None.
    @raise ImageBuildError 写入失败或短写无法恢复时抛出 / Raised when writing fails or a short write cannot be recovered.
    """

    offset = 0
    while offset < len(data):
        try:
            written = os.write(descriptor, data[offset:])
        except InterruptedError:
            continue
        except OSError as error:
            raise ImageBuildError("cannot write image file") from error
        if written <= 0:
            raise ImageBuildError("cannot make progress while writing image file")
        offset += written


def _read_all(descriptor: int, maximum: int) -> bytes:
    """@brief 从 FD 读取有硬上限的文本文件 / Read a bounded text file from a file descriptor.

    @param descriptor 已打开的 source FD / Open source FD.
    @param maximum 可接受的最大字节数 / Maximum accepted byte count.
    @return 完整文件字节 / Complete file bytes.
    @raise ImageBuildError 文件超过上限或读取失败时抛出 / Raised when the file exceeds the limit or reading fails.
    """

    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
        except InterruptedError:
            continue
        except OSError as error:
            raise ImageBuildError("cannot read image source file") from error
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > maximum:
            raise ImageBuildError(
                "text file needing relocation exceeds the safe size limit"
            )
        chunks.append(chunk)


def _parse_python_layout(venv: Path) -> PythonRuntimeLayout:
    """@brief 通过指定 venv 的 Python 探测 CPython ABI 布局 / Probe the CPython ABI layout through the selected venv Python.

    @param venv 已验证的可信 venv / Validated trusted venv.
    @return 可复制的 CPython layout / Copyable CPython layout.
    @raise ImageBuildError venv 缺少解释器、probe 失败或布局越界时抛出 /
        Raised when the venv lacks an interpreter, probing fails, or layout escapes its base prefix.
    @note ``-I`` 只隔离 user environment，仍会导入 venv ``site-packages`` 并执行 ``.pth``。
        probe 必须同时传入 ``-S``，使 layout discovery 永远不执行待验证的 startup hook。/
        ``-I`` isolates only the user environment; it still imports venv ``site-packages`` and
        executes ``.pth`` files. The probe must also pass ``-S`` so layout discovery never runs an
        as-yet-unvalidated startup hook.
    """

    executable = venv / "bin" / "python"
    if not executable.exists():
        raise ImageBuildError("venv must contain bin/python")
    probe = (
        "import json, sys, sysconfig; "
        "print(json.dumps({'base_prefix': sys.base_prefix, 'executable': sys.executable, "
        "'stdlib': sysconfig.get_path('stdlib'), 'libdir': sysconfig.get_config_var('LIBDIR') or '', "
        "'version': f'{sys.version_info.major}.{sys.version_info.minor}'}))"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-I", "-S", "-c", probe],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout=15,
        )
    except OSError as error:
        raise ImageBuildError("cannot execute the trusted venv Python probe") from error
    except subprocess.TimeoutExpired as error:
        raise ImageBuildError("trusted venv Python probe timed out") from error
    if completed.returncode != 0:
        raise ImageBuildError("trusted venv Python probe failed")
    try:
        values = json.loads(completed.stdout)
        base_prefix = _require_absolute_existing(
            str(values["base_prefix"]), "CPython base prefix", directory=True
        )
        interpreter = _require_absolute_existing(
            str(values["executable"]), "CPython interpreter", directory=False
        )
        standard_library = _require_absolute_existing(
            str(values["stdlib"]), "CPython standard library", directory=True
        )
        library_directory = _require_absolute_existing(
            str(values["libdir"]), "CPython library directory", directory=True
        )
        version = str(values["version"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ImageBuildError(
            "trusted venv Python probe returned an invalid layout"
        ) from error
    if re.fullmatch(r"\d+\.\d+", version) is None:
        raise ImageBuildError("CPython version must be a major.minor ABI string")
    if not _is_relative_to(interpreter, base_prefix):
        raise ImageBuildError("CPython interpreter must be inside sys.base_prefix")
    if not _is_relative_to(standard_library, base_prefix) or not _is_relative_to(
        library_directory, base_prefix
    ):
        raise ImageBuildError(
            "CPython standard library and LIBDIR must be inside sys.base_prefix"
        )
    return PythonRuntimeLayout(
        venv=venv,
        base_prefix=base_prefix,
        interpreter=interpreter,
        standard_library=standard_library,
        library_directory=library_directory,
        version=version,
    )


class RootfsAssembler:
    """@brief 将可信 build inputs 复制为封闭 rootfs / Copy trusted build inputs into a closed rootfs.

    本类没有来自 Bot 的接口；它只由 root-owned image builder 构造。所有 destination 都是
    ``rootfs`` 下的固定 runtime path，所有 symlink 都被重写为相对且在 rootfs 内闭合。/
    This class has no Bot-facing interface; only the root-owned image builder constructs it.  Every
    destination is a fixed runtime path below ``rootfs`` and every symlink is rewritten relative
    and closed within that rootfs.
    """

    def __init__(
        self,
        *,
        rootfs: Path,
        layout: PythonRuntimeLayout,
        python_sources: Sequence[Path],
        readelf: Path,
        ldconfig: Path,
        venv_relocation: VenvRelocationPlan = VenvRelocationPlan(),
        python_runtime_profile: PythonRuntimeProfile = _HEADLESS_PYTHON_RUNTIME_PROFILE,
    ) -> None:
        """@brief 初始化一个尚未 seal 的 rootfs assembler / Initialize an as-yet-unsealed rootfs assembler.

        @param rootfs staging 内新建的 rootfs 目录 / Newly created rootfs directory inside staging.
        @param layout 已探测的 CPython layout / Probed CPython layout.
        @param python_sources 允许复制的 path-only ``.pth`` source roots / Path-only ``.pth`` source roots admitted for copying.
        @param readelf 可信 readelf binary / Trusted readelf binary.
        @param ldconfig 可信 ldconfig binary / Trusted ldconfig binary.
        @param venv_relocation 已验证的 venv startup-hook 重定位计划 / Verified venv startup-hook relocation plan.
        @param python_runtime_profile 固定的 CPython runtime 内容契约 / Fixed CPython runtime-content contract.
        @return None / None.
        """

        self._rootfs = rootfs
        self._layout = layout
        self._readelf = readelf
        self._ldconfig = ldconfig
        self._venv_relocation = venv_relocation
        self._python_runtime_profile = python_runtime_profile
        self._source_maps: list[tuple[Path, PurePosixPath]] = [
            (layout.venv, _RUNTIME_VENV),
            (layout.base_prefix, PurePosixPath("/usr/local")),
        ]
        for index, source in enumerate(python_sources, start=1):
            name = source.name or f"source-{index}"
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            self._source_maps.append(
                (source, _RUNTIME_PYTHON_SOURCES / f"{index}-{safe_name}")
            )
        self._editable_hooks_by_pth: dict[Path, ScikitBuildEditableHook] = {
            hook.pth.resolve(strict=True): hook
            for hook in venv_relocation.scikit_build_hooks
        }
        self._editable_hooks_by_helper: dict[Path, ScikitBuildEditableHook] = {
            hook.helper.resolve(strict=True): hook
            for hook in venv_relocation.scikit_build_hooks
        }
        self._editable_direct_urls: dict[Path, EditableDirectUrlRelocation] = {
            relocation.metadata.resolve(strict=True): relocation
            for relocation in venv_relocation.editable_direct_urls
        }
        self._base_interpreter = _RUNTIME_CPYTHON_DIRECTORY / f"cpython{layout.version}"
        self._special_source_destinations: dict[Path, PurePosixPath] = {
            layout.interpreter: self._base_interpreter,
        }
        self._destination_sources: dict[PurePosixPath, Path] = {}
        self._elf_queue: deque[tuple[Path, PurePosixPath]] = deque()
        self._inspected_elf: set[PurePosixPath] = set()
        self._copying: set[tuple[Path, PurePosixPath]] = set()
        self._library_cache: dict[str, tuple[Path, ...]] | None = None

    def build(
        self,
        *,
        bash: Path,
        gnu_commands: Sequence[Path],
        supervisor: Path,
    ) -> None:
        """@brief 构建 rootfs 内容与 ELF ABI closure / Build rootfs content and the ELF ABI closure.

        @param bash 可信 Bash executable / Trusted Bash executable.
        @param gnu_commands 受 allowlist 限制的 GNU executables / GNU executables constrained by the allowlist.
        @param supervisor wsp-systemd executable / wsp-systemd executable.
        @return None / None.
        @raise ImageBuildError 任一对象不能安全复制或 ELF 闭包不完整时抛出 /
            Raised when any object cannot be copied safely or the ELF closure is incomplete.
        """

        self._create_required_directories()
        self._copy_python_distribution()
        self._copy_admitted_python_sources()
        self._copy_tree(
            self._layout.venv,
            _RUNTIME_VENV,
            transform=self._transform_venv_file,
            transform_when=self._requires_venv_text_relocation,
        )
        self._create_python_entrypoints()
        self._copy_regular_file(bash, PurePosixPath("/usr/bin/bash"))
        self._create_symlink(PurePosixPath("/bin/bash"), PurePosixPath("/usr/bin/bash"))
        self._create_symlink(PurePosixPath("/bin/sh"), PurePosixPath("/bin/bash"))
        for command in gnu_commands:
            self._copy_regular_file(command, PurePosixPath("/usr/bin") / command.name)
        self._copy_regular_file(supervisor, _RUNTIME_SUPERVISOR)
        self._copy_elf_closure()
        self._validate_rootfs()

    def _create_required_directories(self) -> None:
        """@brief 创建不暴露 host 的固定 runtime 目录 / Create fixed runtime directories without exposing the host.

        @return None / None.
        @note ``/workspace`` 保持 root-owned 01777：每个 runtime 的 OverlayFS upper layer 独立，
            而 task UID 65534 仍可在 merged view 创建文件。/ ``/workspace`` remains root-owned
            01777: each runtime has an independent OverlayFS upper layer while task UID 65534 can
            still create files in the merged view.
        """

        for directory in (
            PurePosixPath("/bin"),
            PurePosixPath("/dev"),
            PurePosixPath("/etc"),
            PurePosixPath("/lib"),
            PurePosixPath("/lib64"),
            PurePosixPath("/opt"),
            PurePosixPath("/proc"),
            PurePosixPath("/run"),
            PurePosixPath("/tmp"),
            PurePosixPath("/usr"),
            PurePosixPath("/usr/bin"),
            PurePosixPath("/usr/lib"),
            PurePosixPath("/usr/lib64"),
            PurePosixPath("/usr/local"),
            PurePosixPath("/usr/local/bin"),
            PurePosixPath("/usr/local/lib"),
            PurePosixPath("/usr/local/libexec"),
            PurePosixPath("/usr/local/libexec/wspctl"),
            _RUNTIME_VENV,
            _RUNTIME_PYTHON_SOURCES,
        ):
            self._ensure_directory(directory, 0o755)
        self._ensure_directory(PurePosixPath("/tmp"), 0o1777)
        self._ensure_directory(PurePosixPath("/workspace"), 0o1777)

    def _host_destination(self, runtime_path: PurePosixPath) -> Path:
        """@brief 将 runtime path 映射到 staging rootfs / Map a runtime path into the staging rootfs.

        @param runtime_path runtime 内的绝对路径 / Absolute path inside the runtime.
        @return rootfs 下的实体 destination / Concrete destination below rootfs.
        """

        checked = _runtime_path(runtime_path)
        result = self._rootfs.joinpath(*checked.parts[1:])
        try:
            result.relative_to(self._rootfs)
        except ValueError as error:
            raise ImageBuildError(
                "runtime destination escaped staging rootfs"
            ) from error
        return result

    def _ensure_directory(self, runtime_path: PurePosixPath, mode: int) -> None:
        """@brief 创建并固定一个 rootfs 目录 / Create and fix one rootfs directory.

        @param runtime_path runtime 内目录 / Runtime-internal directory.
        @param mode 所需权限位 / Required permission bits.
        @return None / None.
        @raise ImageBuildError 已有 symlink、非目录或 owner 操作失败时抛出 /
            Raised when an existing path is a symlink/non-directory or ownership fixing fails.
        """

        destination = self._host_destination(runtime_path)
        relative_parts = destination.relative_to(self._rootfs).parts
        current = self._rootfs
        for index, component in enumerate(relative_parts):
            current = current / component
            try:
                metadata = os.lstat(current)
            except FileNotFoundError:
                try:
                    os.mkdir(current, 0o755)
                    os.chown(current, 0, 0)
                except OSError as error:
                    raise ImageBuildError("cannot create rootfs directory") from error
                metadata = os.lstat(current)
            except OSError as error:
                raise ImageBuildError("cannot inspect rootfs directory") from error
            if not stat.S_ISDIR(metadata.st_mode):
                raise ImageBuildError(
                    "rootfs destination parent is not a real directory"
                )
            if index == len(relative_parts) - 1:
                try:
                    os.chown(current, 0, 0)
                    os.chmod(current, mode)
                except OSError as error:
                    raise ImageBuildError(
                        "cannot fix rootfs directory ownership or mode"
                    ) from error

    def _ensure_parent(self, runtime_path: PurePosixPath) -> None:
        """@brief 确保 file/symlink destination 的父目录存在 / Ensure a file/symlink destination parent exists.

        @param runtime_path runtime 内 file/symlink 位置 / Runtime-internal file/symlink location.
        @return None / None.
        """

        self._ensure_directory(_runtime_path(runtime_path).parent, 0o755)

    def _runtime_exists(self, runtime_path: PurePosixPath) -> bool:
        """@brief 判断 rootfs 中是否已有一个路径项 / Determine whether a rootfs path entry already exists.

        @param runtime_path runtime 内路径 / Runtime-internal path.
        @return 包含 dangling symlink 在内的目录项存在时为真 / True when a directory entry exists, including a dangling symlink.
        """

        return os.path.lexists(self._host_destination(runtime_path))

    def _map_source_to_runtime(self, source: Path) -> PurePosixPath | None:
        """@brief 把受准 host source 映射到 runtime 内路径 / Map an admitted host source into a runtime path.

        @param source 已解析 host source / Resolved host source.
        @return 对应 runtime path；不受准时为 None / Corresponding runtime path, or None when not admitted.
        """

        resolved = source.resolve(strict=True)
        special = self._special_source_destinations.get(resolved)
        if special is not None:
            return special
        for source_root, runtime_root in self._source_maps:
            if _is_relative_to(resolved, source_root):
                return runtime_root / resolved.relative_to(source_root).as_posix()
        return None

    def _copy_regular_file(
        self,
        source: Path,
        runtime_path: PurePosixPath,
        *,
        transform: Callable[[bytes], bytes] | None = None,
    ) -> None:
        """@brief 以 no-follow FD 复制一个 regular file / Copy one regular file with no-follow file descriptors.

        @param source 可信 source regular file / Trusted source regular file.
        @param runtime_path 固定 runtime destination / Fixed runtime destination.
        @param transform 可选的小文本重写函数 / Optional small-text rewriting function.
        @return None / None.
        @raise ImageBuildError source 类型变化、destination 冲突或复制失败时抛出 /
            Raised when source type changes, destination collides, or copying fails.
        """

        checked_source = source.resolve(strict=True)
        checked_runtime = _runtime_path(runtime_path)
        destination = self._host_destination(checked_runtime)
        known_source = self._destination_sources.get(checked_runtime)
        if known_source is not None:
            if known_source == checked_source:
                return
            raise ImageBuildError(
                f"two distinct trusted inputs collide at runtime path {checked_runtime}"
            )
        if os.path.lexists(destination):
            raise ImageBuildError(
                f"unexpected pre-existing rootfs destination {checked_runtime}"
            )
        try:
            source_metadata = os.lstat(checked_source)
        except OSError as error:
            raise ImageBuildError("cannot lstat trusted source file") from error
        if not stat.S_ISREG(source_metadata.st_mode):
            raise ImageBuildError("trusted source must resolve to a regular file")
        self._ensure_parent(checked_runtime)
        try:
            source_fd = os.open(
                checked_source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except OSError as error:
            raise ImageBuildError(
                "cannot open trusted source file without following links"
            ) from error
        try:
            opened_metadata = os.fstat(source_fd)
            if (
                opened_metadata.st_dev != source_metadata.st_dev
                or opened_metadata.st_ino != source_metadata.st_ino
                or not stat.S_ISREG(opened_metadata.st_mode)
            ):
                raise ImageBuildError("trusted source changed while being copied")
            try:
                destination_fd = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
            except OSError as error:
                raise ImageBuildError(
                    "cannot create rootfs file destination"
                ) from error
            try:
                if transform is not None:
                    rewritten = transform(
                        _read_all(source_fd, _MAX_REWRITTEN_TEXT_BYTES)
                    )
                    _write_all(destination_fd, rewritten)
                else:
                    while True:
                        try:
                            chunk = os.read(source_fd, 64 * 1024)
                        except InterruptedError:
                            continue
                        except OSError as error:
                            raise ImageBuildError(
                                "cannot read trusted source file"
                            ) from error
                        if not chunk:
                            break
                        _write_all(destination_fd, chunk)
                os.fchmod(
                    destination_fd,
                    _sanitized_mode(source_metadata.st_mode, directory=False),
                )
                os.fchown(destination_fd, 0, 0)
                # staging 中的每个 inode 都会在 publish 前由 _fsync_tree 统一落盘。
                # 此处逐文件 fsync 既不能使未 rename 的 generation 可见，也不能增强
                # crash consistency，只会把大 venv 的构建 I/O 放大为两次同步。
                # Every staging inode is durably synchronized as a single commit by
                # _fsync_tree immediately before publication. Per-file fsync here
                # cannot make an unrenamed generation visible or improve crash
                # consistency; it only doubles synchronous I/O for large venvs.
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
        self._destination_sources[checked_runtime] = checked_source
        if _is_elf(checked_source):
            self._elf_queue.append((checked_source, checked_runtime))

    def _create_symlink(
        self, runtime_path: PurePosixPath, target: PurePosixPath
    ) -> None:
        """@brief 创建一个 rootfs-contained relative symlink / Create one rootfs-contained relative symlink.

        @param runtime_path symlink 的 runtime 位置 / Runtime location of the symlink.
        @param target symlink 的 runtime 最终目标 / Runtime final target of the symlink.
        @return None / None.
        @raise ImageBuildError destination 冲突或 symlink 创建失败时抛出 /
            Raised when the destination collides or symlink creation fails.
        """

        checked_runtime = _runtime_path(runtime_path)
        checked_target = _runtime_path(target)
        destination = self._host_destination(checked_runtime)
        if os.path.lexists(destination):
            try:
                existing_target = os.readlink(destination)
            except OSError as error:
                raise ImageBuildError(
                    f"unexpected non-symlink rootfs destination {checked_runtime}"
                ) from error
            if existing_target == _runtime_relative_link(
                checked_runtime, checked_target
            ):
                return
            raise ImageBuildError(
                f"two distinct rootfs symlinks collide at runtime path {checked_runtime}"
            )
        self._ensure_parent(checked_runtime)
        relative_target = _runtime_relative_link(checked_runtime, checked_target)
        try:
            os.symlink(relative_target, destination)
            os.lchown(destination, 0, 0)
        except OSError as error:
            raise ImageBuildError("cannot create rootfs-relative symlink") from error

    def _copy_entry(
        self,
        source: Path,
        runtime_path: PurePosixPath,
        *,
        transform: Callable[[Path, bytes], bytes] | None = None,
        transform_when: Callable[[Path], bool] | None = None,
        omit: Callable[[Path], bool] | None = None,
    ) -> None:
        """@brief 递归复制一个受准 inode / Recursively copy one admitted inode.

        @param source source inode / Source inode.
        @param runtime_path 对应 runtime destination / Corresponding runtime destination.
        @param transform regular file 的可选转换 / Optional transformation for regular files.
        @param transform_when 选择需要转换的 regular file 的 predicate / Predicate selecting regular files to transform.
        @param omit 可选的 source-path exclusion predicate / Optional source-path exclusion predicate.
        @return None / None.
        @raise ImageBuildError 遇到 device、FIFO、外逃链接或循环时抛出 /
            Raised on a device, FIFO, escaping link, or copy cycle.
        """

        checked_runtime = _runtime_path(runtime_path)
        if _is_omitted_python_cache_entry(source) or (
            omit is not None and omit(source)
        ):
            return
        try:
            metadata = os.lstat(source)
        except OSError as error:
            raise ImageBuildError("cannot inspect trusted tree entry") from error
        identity = (source.resolve(strict=False), checked_runtime)
        if identity in self._copying:
            raise ImageBuildError("trusted input contains a recursive copy cycle")
        if stat.S_ISDIR(metadata.st_mode):
            self._copying.add(identity)
            try:
                self._ensure_directory(
                    checked_runtime, _sanitized_mode(metadata.st_mode, directory=True)
                )
                try:
                    entries = sorted(os.scandir(source), key=lambda entry: entry.name)
                except OSError as error:
                    raise ImageBuildError(
                        "cannot enumerate trusted source directory"
                    ) from error
                for entry in entries:
                    child_source = Path(entry.path)
                    child_runtime = checked_runtime / entry.name
                    self._copy_entry(
                        child_source,
                        child_runtime,
                        transform=transform,
                        transform_when=transform_when,
                        omit=omit,
                    )
            finally:
                self._copying.remove(identity)
            return
        if stat.S_ISREG(metadata.st_mode):
            should_transform = transform is not None and (
                transform_when is None or transform_when(source)
            )
            if not should_transform:
                self._copy_regular_file(source, checked_runtime)
            else:

                def file_transform(data: bytes) -> bytes:
                    """@brief 将当前 regular file 交给 tree transform / Send the current regular file to the tree transform.

                    @param data 原始 file bytes / Original file bytes.
                    @return 转换后的 file bytes / Transformed file bytes.
                    """

                    return transform(source, data)

                self._copy_regular_file(
                    source, checked_runtime, transform=file_transform
                )
            return
        if stat.S_ISLNK(metadata.st_mode):
            self._copy_symlink(
                source,
                checked_runtime,
                transform=transform,
                transform_when=transform_when,
                omit=omit,
            )
            return
        raise ImageBuildError(
            "trusted input tree contains an unsupported special inode"
        )

    def _copy_tree(
        self,
        source: Path,
        runtime_path: PurePosixPath,
        *,
        transform: Callable[[Path, bytes], bytes] | None = None,
        transform_when: Callable[[Path], bool] | None = None,
        omit: Callable[[Path], bool] | None = None,
    ) -> None:
        """@brief 复制一个受准目录树 / Copy one admitted directory tree.

        @param source 已解析的 source directory / Resolved source directory.
        @param runtime_path 固定 runtime root / Fixed runtime root.
        @param transform regular file 的可选转换 / Optional transformation for regular files.
        @param transform_when 选择需要转换的 regular file 的 predicate / Predicate selecting regular files to transform.
        @param omit 可选的 source-path exclusion predicate / Optional source-path exclusion predicate.
        @return None / None.
        """

        if not source.is_dir():
            raise ImageBuildError("trusted tree source must be a directory")
        self._copy_entry(
            source,
            runtime_path,
            transform=transform,
            transform_when=transform_when,
            omit=omit,
        )

    def _copy_symlink(
        self,
        source: Path,
        runtime_path: PurePosixPath,
        *,
        transform: Callable[[Path, bytes], bytes] | None,
        transform_when: Callable[[Path], bool] | None,
        omit: Callable[[Path], bool] | None,
    ) -> None:
        """@brief 重定位 source symlink，不保留任何 host-absolute target / Relocate a source symlink without retaining any host-absolute target.

        @param source source symlink / Source symlink.
        @param runtime_path symlink 的 runtime 位置 / Runtime location of the symlink.
        @param transform 递归 materialization 时使用的 file 转换 / File transformation used during recursive materialization.
        @param transform_when 选择 materialized regular file 的转换 predicate /
            Predicate selecting materialized regular files for transformation.
        @param omit 可选的 source-path exclusion predicate / Optional source-path exclusion predicate.
        @return None / None.
        @raise ImageBuildError target 不存在、未受准或逃出 rootfs 时抛出 /
            Raised when the target is missing, unadmitted, or would escape rootfs.
        """

        try:
            raw_target = os.readlink(source)
            resolved_target = (source.parent / raw_target).resolve(strict=True)
        except OSError as error:
            raise ImageBuildError("cannot resolve trusted symlink target") from error
        if _is_omitted_python_cache_entry(resolved_target) or (
            omit is not None and omit(resolved_target)
        ):
            return
        target_runtime = self._map_source_to_runtime(resolved_target)
        if target_runtime is None:
            raise ImageBuildError(
                f"trusted symlink target is outside admitted image inputs: {source}"
            )
        if not self._runtime_exists(target_runtime):
            self._copy_entry(
                resolved_target,
                target_runtime,
                transform=transform,
                transform_when=transform_when,
                omit=omit,
            )
        self._create_symlink(runtime_path, target_runtime)

    def _copy_python_distribution(self) -> None:
        """@brief 复制 CPython interpreter、stdlib 与 libpython / Copy CPython interpreter, stdlib, and libpython.

        @return None / None.
        @note base interpreter 使用 ``cpythonX.Y`` 名称；普通 ``python`` entrypoint 指向 venv，
            从而保留 site-packages 和 pyvenv.cfg 语义。/ The base interpreter is named
            ``cpythonX.Y``; ordinary ``python`` entrypoints point at the venv to retain
            site-packages and pyvenv.cfg semantics.
        """

        self._copy_regular_file(self._layout.interpreter, self._base_interpreter)
        stdlib_runtime = (
            PurePosixPath("/usr/local")
            / self._layout.standard_library.relative_to(
                self._layout.base_prefix
            ).as_posix()
        )
        self._copy_tree(
            self._layout.standard_library,
            stdlib_runtime,
            omit=self._omits_python_runtime_profile_path,
        )
        pattern = f"libpython{self._layout.version}.so*"
        for library in sorted(
            self._layout.library_directory.glob(pattern), key=lambda item: item.name
        ):
            runtime_library = (
                PurePosixPath("/usr/local")
                / library.relative_to(self._layout.base_prefix).as_posix()
            )
            self._copy_entry(library, runtime_library)

    def _omits_python_runtime_profile_path(self, source: Path) -> bool:
        """@brief 将当前 stdlib source 交给固定 runtime profile / Apply the fixed runtime profile to one stdlib source.

        @param source 待递归复制的 lexical stdlib path / Lexical stdlib path considered by recursive copying.
        @return 该 source 应从 base image 排除时为真 / True when the source must stay out of the base image.
        """

        return self._python_runtime_profile.omits_standard_library_path(
            self._layout.standard_library, source
        )

    def _copy_admitted_python_sources(self) -> None:
        """@brief 复制显式允许的 path-only ``.pth`` Python sources / Copy explicitly admitted path-only ``.pth`` Python sources.

        @return None / None.
        """

        for source_root, runtime_root in self._source_maps[2:]:
            self._copy_tree(source_root, runtime_root)

    def _create_python_entrypoints(self) -> None:
        """@brief 建立 ``python`` 到 relocated venv 的入口 / Create ``python`` entrypoints to the relocated venv.

        @return None / None.
        """

        venv_python = _RUNTIME_VENV / "bin" / f"python{self._layout.version}"
        if not self._runtime_exists(venv_python):
            raise ImageBuildError(
                "relocated venv did not contain its versioned python entrypoint"
            )
        for entrypoint in (
            PurePosixPath("/usr/local/bin/python"),
            PurePosixPath("/usr/local/bin/python3"),
            PurePosixPath("/usr/local/bin") / f"python{self._layout.version}",
            PurePosixPath("/usr/bin/python"),
            PurePosixPath("/usr/bin/python3"),
            PurePosixPath("/usr/bin") / f"python{self._layout.version}",
        ):
            self._create_symlink(entrypoint, venv_python)

    def _transform_venv_file(self, source: Path, data: bytes) -> bytes:
        """@brief 重写 venv 内 host-specific metadata / Rewrite host-specific metadata inside the venv.

        @param source 正在复制的 venv source file / Venv source file being copied.
        @param data 原始文件字节 / Original file bytes.
        @return 只引用 runtime 内路径的替换字节 / Replacement bytes referring only to runtime paths.
        """

        try:
            resolved = source.resolve(strict=True)
        except OSError as error:
            raise ImageBuildError(
                "cannot resolve venv file selected for relocation"
            ) from error
        if source == self._layout.venv / "pyvenv.cfg":
            return self._rewrite_pyvenv_configuration(data)
        helper = self._editable_hooks_by_helper.get(resolved)
        if helper is not None:
            return self._rewrite_scikit_build_editable_helper(helper, data)
        direct_url = self._editable_direct_urls.get(resolved)
        if direct_url is not None:
            return self._rewrite_editable_direct_url(direct_url, data)
        if source.suffix == ".pth":
            return self._rewrite_pth(data, source=resolved)
        try:
            relative = source.relative_to(self._layout.venv)
        except ValueError:
            return data
        if relative.parts and relative.parts[0] == "bin":
            return self._rewrite_shebang(data)
        return data

    def _requires_venv_text_relocation(self, source: Path) -> bool:
        """@brief 判断 venv regular file 是否需要有界文本重写 / Decide whether a venv regular file needs bounded text rewriting.

        @param source 已 lstat 为 regular 的 venv 文件 / Venv file already lstat-checked as regular.
        @return 仅 ``pyvenv.cfg``、``.pth`` 和 shebang script 时为真 /
            True only for ``pyvenv.cfg``, ``.pth``, and shebang scripts.
        @raise ImageBuildError 无法以 no-follow 方式读取候选文件时抛出 /
            Raised when the candidate cannot be read with no-follow semantics.
        @note venv ``bin`` 可包含大型 ELF 工具（例如 Ruff）；把所有 ``bin`` 文件当文本会错误拒绝
            合法 image。/ A venv ``bin`` may contain large ELF tools (for example Ruff); treating
            every ``bin`` file as text would incorrectly reject a valid image.
        """

        try:
            resolved = source.resolve(strict=True)
        except OSError as error:
            raise ImageBuildError(
                "cannot resolve venv file selected for relocation"
            ) from error
        if (
            source == self._layout.venv / "pyvenv.cfg"
            or source.suffix == ".pth"
            or resolved in self._editable_hooks_by_helper
            or resolved in self._editable_direct_urls
        ):
            return True
        try:
            relative = source.relative_to(self._layout.venv)
        except ValueError:
            return False
        if not relative.parts or relative.parts[0] != "bin":
            return False
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                return os.read(descriptor, 2) == b"#!"
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ImageBuildError(
                "cannot inspect venv executable for a shebang"
            ) from error

    def _runtime_path_for_text_path(self, text_path: str) -> PurePosixPath | None:
        """@brief 将一个已有 host absolute 文本路径映射到 runtime / Map an existing host absolute text path into the runtime.

        @param text_path 文本中出现的 host path / Host path occurring in text.
        @return 映射后的 runtime path；路径不存在或未受准时为 None /
            Mapped runtime path, or None when path is absent or unadmitted.
        """

        candidate = Path(text_path)
        if not candidate.is_absolute():
            return None
        try:
            return self._map_source_to_runtime(candidate.resolve(strict=True))
        except OSError:
            return None

    def _runtime_path_for_explicit_source_root(
        self, source_root: Path
    ) -> PurePosixPath:
        """@brief 将一个已批准 source root 映射为其固定 runtime root / Map one approved source root to its fixed runtime root.

        @param source_root 已验证 PEP 660 plan 引用的 canonical source root / Canonical source root referenced by the verified PEP 660 plan.
        @return 对应的 runtime-internal root / Corresponding runtime-internal root.
        @raise ImageBuildError plan 与 assembler 的 ``--python-source`` 输入不一致时抛出 /
            Raised when the plan and assembler ``--python-source`` inputs disagree.
        """

        for candidate, runtime in self._source_maps[2:]:
            if candidate == source_root:
                return runtime
        raise ImageBuildError(
            "editable relocation plan references a source root absent from this assembler"
        )

    def _runtime_path_for_shebang(self, text_path: str) -> PurePosixPath | None:
        """@brief 优先保留 venv entrypoint 的 shebang 语义 / Prefer preserving a venv entrypoint's shebang semantics.

        @param text_path shebang 中的 absolute interpreter path / Absolute interpreter path from a shebang.
        @return venv-first 的 runtime interpreter path；未受准时为 None /
            Venv-first runtime interpreter path, or None when unadmitted.
        @note ``.venv/bin/python`` 经常是指向 base CPython 的 host-absolute symlink。shebang
            必须指向 relocated venv link，而不是其最终 base target，否则 console script 会丢失
            site-packages。/ ``.venv/bin/python`` is often a host-absolute symlink to base CPython.
            A shebang must point to the relocated venv link rather than its final base target, or a
            console script loses site-packages.
        """

        candidate = Path(text_path)
        if candidate.is_absolute() and _is_relative_to(candidate, self._layout.venv):
            return _RUNTIME_VENV / candidate.relative_to(self._layout.venv).as_posix()
        return self._runtime_path_for_text_path(text_path)

    def _rewrite_pyvenv_configuration(self, data: bytes) -> bytes:
        """@brief 去除 pyvenv.cfg 中的 host 路径 / Remove host paths from pyvenv.cfg.

        @param data 原 pyvenv.cfg / Original pyvenv.cfg.
        @return relocated pyvenv.cfg / Relocated pyvenv.cfg.
        @raise ImageBuildError 可执行解释器字段无法映射时抛出 /
            Raised when an executable interpreter field cannot be mapped.
        """

        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ImageBuildError("pyvenv.cfg must be UTF-8") from error
        rewritten: list[str] = []
        for line in lines:
            key, separator, value = line.partition("=")
            normalized_key = key.strip().lower()
            normalized_value = value.strip()
            if not separator:
                rewritten.append(line)
            elif normalized_key in {"home", "executable"}:
                mapped = self._runtime_path_for_text_path(normalized_value)
                if mapped is None:
                    raise ImageBuildError(
                        f"pyvenv.cfg {normalized_key} is outside admitted image inputs"
                    )
                rewritten.append(f"{key.strip()} = {mapped.as_posix()}")
            elif normalized_key == "command":
                rewritten.append("command = wspctl-image-relocated")
            else:
                rewritten.append(line)
        return ("\n".join(rewritten) + "\n").encode("utf-8")

    def _rewrite_scikit_build_editable_helper(
        self, hook: ScikitBuildEditableHook, data: bytes
    ) -> bytes:
        """@brief 将已验证 scikit-build PEP 660 helper 的 source mapping 重写到 runtime / Rewrite source mappings of a verified scikit-build PEP 660 helper into the runtime.

        @param hook validate 阶段产生的不可变 helper plan / Immutable helper plan produced during validation.
        @param data 原 helper UTF-8 字节 / Original helper UTF-8 bytes.
        @return 只含 runtime source roots 的 helper bytes / Helper bytes containing runtime source roots only.
        @raise ImageBuildError helper 在 validation 后改变、含非 runtime absolute mapping 或 plan/source
            mapping 不一致时抛出 / Raised when the helper changed after validation, carries a
            non-runtime absolute mapping, or the plan/source mapping disagrees.
        """

        try:
            rewritten = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ImageBuildError(
                "scikit-build editable helper must be UTF-8"
            ) from error
        runtime_roots: list[PurePosixPath] = []
        for source_root in sorted(
            hook.source_roots, key=lambda item: len(str(item)), reverse=True
        ):
            runtime_root = self._runtime_path_for_explicit_source_root(source_root)
            host_root = str(source_root)
            if host_root not in rewritten:
                raise ImageBuildError(
                    "scikit-build editable helper changed after its relocation plan was validated"
                )
            rewritten = rewritten.replace(host_root, runtime_root.as_posix())
            runtime_roots.append(runtime_root)
        try:
            parsed = ast.parse(rewritten, filename=str(hook.helper), mode="exec")
        except SyntaxError as error:
            raise ImageBuildError(
                "rewritten scikit-build editable helper is not valid Python"
            ) from error
        for node in ast.walk(parsed):
            if (
                not isinstance(node, ast.Constant)
                or not isinstance(node.value, str)
                or node.value in {"", "/"}
            ):
                continue
            if not node.value.startswith("/"):
                continue
            runtime_path = PurePosixPath(node.value)
            if not any(
                runtime_path == root or root in runtime_path.parents
                for root in runtime_roots
            ):
                raise ImageBuildError(
                    "rewritten scikit-build editable helper retains a non-runtime absolute mapping"
                )
        return rewritten.encode("utf-8")

    def _rewrite_editable_direct_url(
        self, relocation: EditableDirectUrlRelocation, data: bytes
    ) -> bytes:
        """@brief 重写已验证 editable direct-url，绝不保留 host checkout URL / Rewrite a verified editable direct URL without retaining a host checkout URL.

        @param relocation validate 阶段产生的 direct-url plan / Direct-url plan produced during validation.
        @param data 原 PEP 610 metadata bytes / Original PEP 610 metadata bytes.
        @return 只引用 runtime source root 的 canonical metadata / Canonical metadata referring only to the runtime source root.
        @raise ImageBuildError metadata 在 validation 后改变或不是 object 时抛出 /
            Raised when metadata changed after validation or is not an object.
        """

        try:
            metadata = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ImageBuildError(
                "editable direct_url metadata changed after validation"
            ) from error
        if (
            not isinstance(metadata, dict)
            or set(metadata) != {"url", "dir_info"}
            or metadata.get("dir_info") != {"editable": True}
        ):
            raise ImageBuildError(
                "editable direct_url metadata changed after validation"
            )
        runtime_root = self._runtime_path_for_explicit_source_root(
            relocation.source_root
        )
        rewritten = {
            "url": f"file://{runtime_root.as_posix()}",
            "dir_info": {"editable": True},
        }
        return (
            json.dumps(rewritten, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")

    def _rewrite_pth(self, data: bytes, *, source: Path | None = None) -> bytes:
        """@brief 重写 path-only ``.pth`` 或已验证 PEP 660 ``.pth`` 的绝对 source 路径 / Rewrite absolute source paths in a path-only or verified PEP 660 ``.pth`` file.

        @param data 原 .pth 字节 / Original .pth bytes.
        @param source 可选的 canonical ``.pth`` source path / Optional canonical ``.pth`` source path.
        @return 只引用 runtime 内 source 的 .pth 字节 / .pth bytes referring only to runtime-internal sources.
        @raise ImageBuildError 发现未显式批准的 absolute source line 或非计划 startup hook 时抛出 /
            Raised when an absolute source line was not explicitly admitted or a startup hook is not planned.
        """

        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ImageBuildError(".pth file must be UTF-8") from error
        hook = self._editable_hooks_by_pth.get(source) if source is not None else None
        rewritten: list[str] = []
        for index, line in enumerate(lines):
            if _is_executable_pth_line(line):
                if hook is None or index != 0 or line != f"import {hook.module}":
                    raise ImageBuildError(_EXECUTABLE_PTH_ERROR)
                rewritten.append(line)
                continue
            stripped = line.strip()
            if stripped.startswith("/"):
                if hook is not None and (line != stripped or index == 0):
                    raise ImageBuildError(
                        "scikit-build editable .pth changed after validation"
                    )
                mapped = self._runtime_path_for_text_path(stripped)
                if mapped is None:
                    raise ImageBuildError(
                        ".pth references a host path not supplied with --python-source"
                    )
                rewritten.append(mapped.as_posix())
            else:
                if hook is not None:
                    raise ImageBuildError(
                        "scikit-build editable .pth changed after validation"
                    )
                rewritten.append(line)
        return ("\n".join(rewritten) + "\n").encode("utf-8")

    def _rewrite_shebang(self, data: bytes) -> bytes:
        """@brief 将 venv script 的 host shebang 改为 runtime 路径 / Rewrite a venv script's host shebang to a runtime path.

        @param data 原 script bytes / Original script bytes.
        @return 若需要则替换首行后的 script bytes / Script bytes with its first line replaced when needed.
        @raise ImageBuildError host interpreter 未受准或无法映射时抛出 /
            Raised when the host interpreter is unadmitted or cannot be mapped.
        """

        first_line, separator, remainder = data.partition(b"\n")
        if not first_line.startswith(b"#!"):
            return data
        try:
            declaration = first_line[2:].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ImageBuildError("venv script shebang must be UTF-8") from error
        interpreter, spacing, suffix = declaration.partition(" ")
        if not interpreter.startswith("/"):
            raise ImageBuildError(
                "venv script shebang must name an absolute interpreter"
            )
        mapped = self._runtime_path_for_shebang(interpreter)
        if mapped is None:
            runtime_interpreter = PurePosixPath(interpreter)
            if runtime_interpreter in {
                PurePosixPath("/bin/sh"),
                PurePosixPath("/bin/bash"),
                PurePosixPath("/usr/bin/env"),
            } and self._runtime_exists(runtime_interpreter):
                return data
            raise ImageBuildError(
                "venv script shebang references an unadmitted host interpreter"
            )
        replacement = f"#!{mapped.as_posix()}{spacing}{suffix}".encode("utf-8")
        return replacement + (separator + remainder if separator else b"")

    def _read_elf_metadata(self, source: Path) -> ElfMetadata:
        """@brief 使用 readelf（绝不使用 ldd）读取 ELF metadata / Read ELF metadata with readelf (never ldd).

        @param source 已验证 ELF source / Validated ELF source.
        @return DT_NEEDED、PT_INTERP 与搜索路径 metadata / DT_NEEDED, PT_INTERP, and search-path metadata.
        @raise ImageBuildError readelf 失败或 ELF 含不安全 absolute RPATH 时抛出 /
            Raised when readelf fails or the ELF carries an unsafe absolute RPATH.
        """

        abi = _read_elf_abi(source)
        dynamic_output = self._run_host_tool(
            self._readelf, ("-dW", "--", str(source)), "read ELF dynamic metadata"
        )
        program_output = self._run_host_tool(
            self._readelf, ("-lW", "--", str(source)), "read ELF program metadata"
        )
        needed = tuple(
            match.group("name") for match in _NEEDED_PATTERN.finditer(dynamic_output)
        )
        for name in needed:
            if "/" in name or name in {".", ".."} or "\x00" in name:
                raise ImageBuildError("ELF DT_NEEDED contains an unsafe library name")
        raw_rpath: str | None = None
        raw_runpath: str | None = None
        for match in _SEARCH_PATH_PATTERN.finditer(dynamic_output):
            if match.group("kind") == "RUNPATH":
                raw_runpath = match.group("paths")
            else:
                raw_rpath = match.group("paths")
        selected_paths = raw_runpath if raw_runpath is not None else raw_rpath
        search_paths: list[Path] = []
        if selected_paths:
            for raw_entry in selected_paths.split(":"):
                if not raw_entry:
                    continue
                expanded = raw_entry.replace("${ORIGIN}", str(source.parent)).replace(
                    "$ORIGIN", str(source.parent)
                )
                if "$" in expanded:
                    raise ImageBuildError(
                        "ELF RPATH/RUNPATH contains an unsupported variable"
                    )
                if raw_entry.startswith(
                    "/"
                ) and not self._is_runtime_stable_library_path(
                    PurePosixPath(raw_entry)
                ):
                    raise ImageBuildError(
                        "ELF carries a host-specific absolute RPATH/RUNPATH"
                    )
                candidate = Path(expanded)
                if not candidate.is_absolute():
                    raise ImageBuildError(
                        "ELF RPATH/RUNPATH did not resolve to an absolute directory"
                    )
                try:
                    resolved = candidate.resolve(strict=True)
                except OSError as error:
                    raise ImageBuildError(
                        "ELF RPATH/RUNPATH references a missing directory"
                    ) from error
                if not resolved.is_dir():
                    raise ImageBuildError("ELF RPATH/RUNPATH entry is not a directory")
                search_paths.append(resolved)
        interpreter_match = _INTERPRETER_PATTERN.search(program_output)
        interpreter = (
            interpreter_match.group("path") if interpreter_match is not None else None
        )
        if interpreter is not None and not Path(interpreter).is_absolute():
            raise ImageBuildError("ELF PT_INTERP must be absolute")
        return ElfMetadata(
            needed=needed,
            interpreter=interpreter,
            search_paths=tuple(search_paths),
            abi=abi,
        )

    def _run_host_tool(
        self, executable: Path, arguments: Sequence[str], purpose: str
    ) -> str:
        """@brief 在固定 locale、无 stdin 下运行可信 metadata 工具 / Run a trusted metadata tool with fixed locale and no stdin.

        @param executable 已验证 host tool / Validated host tool.
        @param arguments 不含用户 payload 的固定参数 / Fixed arguments containing no user payload.
        @param purpose 诊断语义 / Diagnostic purpose.
        @return UTF-8/ASCII decoded stdout / Decoded stdout.
        @raise ImageBuildError 工具失败、超时或输出无法解析时抛出 /
            Raised when the tool fails, times out, or returns undecodable output.
        """

        try:
            completed = subprocess.run(
                [str(executable), *arguments],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"LC_ALL": "C", "PATH": ""},
                timeout=15,
            )
        except OSError as error:
            raise ImageBuildError(f"cannot {purpose}") from error
        except subprocess.TimeoutExpired as error:
            raise ImageBuildError(f"timeout while attempting to {purpose}") from error
        if completed.returncode != 0:
            raise ImageBuildError(f"trusted metadata tool failed to {purpose}")
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ImageBuildError(
                f"trusted metadata tool returned non-UTF-8 while attempting to {purpose}"
            ) from error

    def _load_library_cache(self) -> dict[str, tuple[Path, ...]]:
        """@brief 从 ldconfig cache 建立 SONAME 到绝对候选路径映射 / Build a SONAME-to-absolute-candidate map from ldconfig cache.

        @return SONAME 到 cache entries 的映射 / Mapping from SONAME to cache entries.
        """

        if self._library_cache is not None:
            return self._library_cache
        output = self._run_host_tool(self._ldconfig, ("-p",), "read ldconfig cache")
        values: dict[str, list[Path]] = {}
        for line in output.splitlines():
            match = _LDCONFIG_PATTERN.fullmatch(line)
            if match is None:
                continue
            logical_path = Path(match.group("path"))
            try:
                resolved = logical_path.resolve(strict=True)
                metadata = resolved.stat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                values.setdefault(match.group("name"), []).append(logical_path)
        self._library_cache = {name: tuple(paths) for name, paths in values.items()}
        return self._library_cache

    def _resolve_needed_library(
        self, source: Path, metadata: ElfMetadata, soname: str
    ) -> tuple[Path, Path]:
        """@brief 使用 source RPATH 与 ldconfig 解析一个 DT_NEEDED / Resolve one DT_NEEDED through source RPATH and ldconfig.

        @param source 正在分析的 ELF / ELF currently being analyzed.
        @param metadata 已解析的 ELF metadata / Parsed ELF metadata.
        @param soname 所需 SONAME / Required SONAME.
        @return ``(resolved_source, logical_host_path)`` / ``(resolved_source, logical_host_path)``.
        @raise ImageBuildError 无法在受控 loader search 中解析时抛出 /
            Raised when the dependency cannot be resolved through controlled loader search.
        """

        search_directories = (*metadata.search_paths, source.parent)
        for directory in search_directories:
            candidate = directory / soname
            if not os.path.lexists(candidate):
                continue
            try:
                resolved = candidate.resolve(strict=True)
                source_metadata = resolved.stat()
            except OSError:
                continue
            if stat.S_ISREG(source_metadata.st_mode) and _has_matching_elf_abi(
                resolved, metadata.abi
            ):
                return resolved, candidate
        for logical_path in self._load_library_cache().get(soname, ()):
            try:
                resolved = logical_path.resolve(strict=True)
                source_metadata = resolved.stat()
            except OSError:
                continue
            if stat.S_ISREG(source_metadata.st_mode) and _has_matching_elf_abi(
                resolved, metadata.abi
            ):
                return resolved, logical_path
        raise ImageBuildError(
            f"ELF dependency {soname!r} from {source} has no candidate with a matching ELF ABI"
        )

    def _is_runtime_stable_library_path(self, path: PurePosixPath) -> bool:
        """@brief 判断 absolute library path 是否可在 runtime 保留 / Determine whether an absolute library path can be retained in runtime.

        @param path absolute library path / Absolute library path.
        @return 位于受准 system library root 时为真 / True when below an admitted system-library root.
        """

        return any(
            path == root or root in path.parents for root in _SYSTEM_LIBRARY_ROOTS
        )

    def _dependency_runtime_path(
        self, resolved_source: Path, logical_path: Path
    ) -> PurePosixPath:
        """@brief 为一个解析后的 library 选择 runtime destination / Choose a runtime destination for one resolved library.

        @param resolved_source library 的规范 source / Canonical source of the library.
        @param logical_path loader cache/RPATH 中的 logical host path / Logical host path from loader cache/RPATH.
        @return 固定 runtime destination / Fixed runtime destination.
        @raise ImageBuildError library 只能通过 host-specific path 访问时抛出 /
            Raised when a library is only reachable through a host-specific path.
        """

        mapped = self._map_source_to_runtime(resolved_source)
        if mapped is not None:
            return mapped
        if not logical_path.is_absolute():
            raise ImageBuildError("ELF dependency logical path must be absolute")
        runtime_path = PurePosixPath(logical_path)
        if not self._is_runtime_stable_library_path(runtime_path):
            raise ImageBuildError(
                "ELF dependency would require copying a host-specific path into the runtime: "
                f"{logical_path} (resolved {resolved_source})"
            )
        return runtime_path

    def _copy_elf_closure(self) -> None:
        """@brief 递归复制每个 ELF loader、SONAME 依赖和依赖的依赖 / Recursively copy every ELF loader, SONAME dependency, and transitive dependency.

        @return None / None.
        @raise ImageBuildError 任何 ELF closure 无法被 rootfs 内对象满足时抛出 /
            Raised when any ELF closure cannot be satisfied by objects in rootfs.
        """

        while self._elf_queue:
            source, runtime_path = self._elf_queue.popleft()
            if runtime_path in self._inspected_elf:
                continue
            self._inspected_elf.add(runtime_path)
            metadata = self._read_elf_metadata(source)
            if metadata.interpreter is not None:
                loader_logical_path = Path(metadata.interpreter)
                try:
                    loader_source = loader_logical_path.resolve(strict=True)
                except OSError as error:
                    raise ImageBuildError(
                        "ELF PT_INTERP loader does not exist on the trusted build host"
                    ) from error
                if (
                    not loader_source.is_file()
                    or not self._is_runtime_stable_library_path(
                        PurePosixPath(metadata.interpreter)
                    )
                    or not _has_matching_elf_abi(loader_source, metadata.abi)
                ):
                    raise ImageBuildError(
                        "ELF PT_INTERP loader is not an admitted system-library object"
                    )
                self._copy_regular_file(
                    loader_source, PurePosixPath(metadata.interpreter)
                )
            for soname in metadata.needed:
                dependency_source, logical_path = self._resolve_needed_library(
                    source, metadata, soname
                )
                destination = self._dependency_runtime_path(
                    dependency_source, logical_path
                )
                self._copy_regular_file(dependency_source, destination)

    def _validate_rootfs(self) -> None:
        """@brief 对输出施加 verifier-compatible structural invariants / Apply verifier-compatible structural invariants to the output.

        @return None / None.
        @raise ImageBuildError 发现外逃 symlink、special inode、host path 或缺少入口时抛出 /
            Raised on an escaping symlink, special inode, host path, or missing entrypoint.
        """

        required = (
            PurePosixPath("/bin/bash"),
            PurePosixPath("/bin/sh"),
            _RUNTIME_SUPERVISOR,
            PurePosixPath("/usr/bin/python"),
            _RUNTIME_VENV / "bin" / f"python{self._layout.version}",
        )
        for runtime_path in required:
            if not self._runtime_exists(runtime_path):
                raise ImageBuildError(
                    f"required runtime entrypoint is absent: {runtime_path}"
                )
        workspace = self._host_destination(PurePosixPath("/workspace"))
        temporary = self._host_destination(PurePosixPath("/tmp"))
        workspace_metadata = os.lstat(workspace)
        if (
            not stat.S_ISDIR(workspace_metadata.st_mode)
            or stat.S_IMODE(workspace_metadata.st_mode) != 0o1777
        ):
            raise ImageBuildError(
                "/workspace must be a rootfs directory with mode 01777"
            )
        temporary_metadata = os.lstat(temporary)
        if (
            not stat.S_ISDIR(temporary_metadata.st_mode)
            or stat.S_IMODE(temporary_metadata.st_mode) != 0o1777
        ):
            raise ImageBuildError("/tmp must be a rootfs directory with mode 01777")
        for entry in self._walk_rootfs_entries():
            metadata = os.lstat(entry)
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(entry)
                if not target or os.path.isabs(target):
                    raise ImageBuildError(
                        "rootfs contains an empty or absolute symlink target"
                    )
                try:
                    resolved = (entry.parent / target).resolve(strict=True)
                except OSError as error:
                    raise ImageBuildError(
                        "rootfs contains a dangling symlink"
                    ) from error
                if not _is_relative_to(resolved, self._rootfs):
                    raise ImageBuildError("rootfs symlink escapes rootfs")
            elif not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(
                metadata.st_mode
            ):
                raise ImageBuildError("rootfs contains an unsupported special inode")
            if metadata.st_uid != 0:
                raise ImageBuildError(
                    "rootfs build did not produce root-owned metadata"
                )
            runtime_entry = (
                PurePosixPath("/") / entry.relative_to(self._rootfs).as_posix()
            )
            intentional_sticky_writable = runtime_entry in {
                PurePosixPath("/tmp"),
                PurePosixPath("/workspace"),
            }
            unsafe_mode = metadata.st_mode & (
                stat.S_ISUID | stat.S_ISGID | stat.S_IWGRP | stat.S_IWOTH
            )
            if unsafe_mode and not intentional_sticky_writable:
                raise ImageBuildError(
                    "rootfs contains unsafe suid/sgid/group/world-writable metadata"
                )

    def _walk_rootfs_entries(self) -> Iterator[Path]:
        """@brief 以不跟随 symlink 的方式枚举 rootfs / Enumerate rootfs without following symlinks.

        @return rootfs entries iterator / Iterator of rootfs entries.
        """

        pending: list[Path] = [self._rootfs]
        while pending:
            directory = pending.pop()
            try:
                children = sorted(
                    os.scandir(directory), key=lambda entry: entry.name, reverse=True
                )
            except OSError as error:
                raise ImageBuildError(
                    "cannot enumerate rootfs during validation"
                ) from error
            for child in children:
                path = Path(child.path)
                yield path
                try:
                    metadata = os.lstat(path)
                except OSError as error:
                    raise ImageBuildError(
                        "cannot lstat rootfs entry during validation"
                    ) from error
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)


def _validate_command_input(path: Path) -> Path:
    """@brief 校验一个 GNU allowlist input / Validate one GNU allowlist input.

    @param path 已解析 executable 路径 / Resolved executable path.
    @return 原路径 / Unchanged path.
    @raise ImageBuildError basename 不在 hard allowlist 或文件不可执行时抛出 /
        Raised when the basename is absent from the hard allowlist or the file is not executable.
    """

    if path.name not in _ALLOWED_GNU_COMMANDS:
        raise ImageBuildError(
            f"GNU command {path.name!r} is not in the explicit wspctl allowlist"
        )
    if not os.access(path, os.X_OK):
        raise ImageBuildError(f"GNU command {path} is not executable")
    return path


def _validate_executable_input(
    path: Path, description: str, *, require_elf: bool = True
) -> Path:
    """@brief 校验一个 operator-selected executable input / Validate one operator-selected executable input.

    @param path 已解析 regular file / Resolved regular file.
    @param description 输入诊断名 / Input diagnostic name.
    @param require_elf 是否必须是 ELF；`ldconfig` 可为发行版 wrapper script /
        Whether an ELF is required; `ldconfig` may be a distribution wrapper script.
    @return 原路径 / Unchanged path.
    @raise ImageBuildError 文件不可执行或要求 ELF 而实际不是时抛出 /
        Raised when the file is not executable, or an ELF is required but absent.
    """

    if not os.access(path, os.X_OK):
        raise ImageBuildError(f"{description} must be executable")
    if require_elf and not _is_elf(path):
        raise ImageBuildError(
            f"{description} must be an ELF executable; scripts are not admitted here"
        )
    return path


def _rename_noreplace(source: Path, destination: Path) -> None:
    """@brief 用 renameat2 原子发布且绝不覆盖 destination / Atomically publish with renameat2 and never overwrite destination.

    @param source 同一 filesystem 的 staging generation / Staging generation on the same filesystem.
    @param destination 最终 generation 目录 / Final generation directory.
    @return None / None.
    @raise ImageBuildError 内核不支持 no-replace rename 或 destination 已存在时抛出 /
        Raised when the kernel lacks no-replace rename or the destination already exists.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ImageBuildError(
            "host libc does not expose renameat2; refusing a non-atomic publish fallback"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ImageBuildError(
            "generation destination already exists; generations are never overwritten"
        )
    raise ImageBuildError(
        f"atomic generation publish failed: {os.strerror(error_number)}"
    )


def _fsync_tree(root: Path) -> None:
    """@brief 在 publish 前持久化 rootfs 与 manifest / Persist rootfs and manifest before publishing.

    @param root staging generation 根 / Staging-generation root.
    @return None / None.
    @raise ImageBuildError fsync 任一 regular file 或目录失败时抛出 /
        Raised when fsync fails for any regular file or directory.
    """

    directories: list[Path] = []
    files: list[Path] = []
    pending: list[Path] = [root]
    while pending:
        current = pending.pop()
        directories.append(current)
        try:
            entries = list(os.scandir(current))
        except OSError as error:
            raise ImageBuildError("cannot enumerate staging tree for fsync") from error
        for entry in entries:
            path = Path(entry.path)
            metadata = os.lstat(path)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                files.append(path)
    for file_path in files:
        try:
            descriptor = os.open(file_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ImageBuildError("cannot fsync staging regular file") from error
    for directory in reversed(directories):
        try:
            descriptor = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ImageBuildError("cannot fsync staging directory") from error


def _read_manifest_digest(rootfs: Path, generation: str) -> str:
    """@brief 检查 native sealer 写出的四行 manifest / Check the four-line manifest written by the native sealer.

    @param rootfs 已 seal 的 rootfs / Sealed rootfs.
    @param generation 预期 generation / Expected generation.
    @return rootfs digest / Rootfs digest.
    @raise ImageBuildError manifest 格式、generation 或 digest 不符合时抛出 /
        Raised when manifest format, generation, or digest is invalid.
    """

    manifest = rootfs / _MANIFEST_NAME
    try:
        metadata = os.lstat(manifest)
        content = manifest.read_text(encoding="utf-8")
    except OSError as error:
        raise ImageBuildError(
            "native sealer did not create a readable manifest"
        ) from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o444:
        raise ImageBuildError("native sealer did not create an immutable manifest file")
    lines = content.splitlines()
    if len(lines) != 4:
        raise ImageBuildError("native sealer produced an invalid manifest line count")
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in fields:
            raise ImageBuildError("native sealer produced an invalid manifest field")
        fields[key] = value
    rootfs_digest = fields.get("rootfs_digest", "")
    if fields != {
        "version": "1",
        "generation": generation,
        "rootfs_digest": rootfs_digest,
        "digest": fields.get("digest", ""),
    }:
        raise ImageBuildError("native sealer produced an unexpected manifest schema")
    if (
        re.fullmatch(r"[0-9a-f]{64}", rootfs_digest) is None
        or re.fullmatch(r"[0-9a-f]{64}", fields["digest"]) is None
    ):
        raise ImageBuildError("native sealer produced a non-SHA-256 manifest digest")
    return rootfs_digest


def _seal_rootfs(spec: BuildSpec, rootfs: Path) -> str:
    """@brief 调用 native authority 写入 manifest / Invoke the native authority to write the manifest.

    @param spec 受控 build spec / Controlled build spec.
    @param rootfs 完整但尚未 seal 的 rootfs / Complete but unsealed rootfs.
    @return native manifest 的 rootfs digest / Rootfs digest from the native manifest.
    @raise ImageBuildError native sealer 失败或输出不符合契约时抛出 /
        Raised when the native sealer fails or its output violates the contract.
    """

    try:
        completed = subprocess.run(
            [
                str(spec.sealer),
                "--seal",
                "--base-root",
                str(rootfs),
                "--generation",
                spec.generation,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"LC_ALL": "C", "PATH": ""},
            timeout=60,
        )
    except OSError as error:
        raise ImageBuildError("cannot invoke trusted wspctl-image sealer") from error
    except subprocess.TimeoutExpired as error:
        raise ImageBuildError("trusted wspctl-image sealer timed out") from error
    if completed.returncode != 0:
        raise ImageBuildError("trusted wspctl-image sealer rejected the staging rootfs")
    return _read_manifest_digest(rootfs, spec.generation)


def build_image(spec: BuildSpec) -> tuple[Path, str]:
    """@brief 构建、seal 并原子发布一个 generation / Build, seal, and atomically publish one generation.

    @param spec 已验证的受控 build spec / Validated controlled build spec.
    @return ``(published_generation_dir, rootfs_digest)`` / ``(published_generation_dir, rootfs_digest)``.
    @raise ImageBuildError 构建、seal 或 publish 任一步失败时抛出 /
        Raised when any build, seal, or publish step fails.
    """

    destination = spec.output_root / spec.generation
    if os.path.lexists(destination):
        raise ImageBuildError(
            "generation destination already exists; immutable generations cannot be overwritten"
        )
    try:
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{spec.generation}.staging-", dir=spec.output_root
            )
        )
        os.chown(staging, 0, 0)
        os.chmod(staging, 0o700)
    except OSError as error:
        raise ImageBuildError(
            "cannot create private artifact-store staging directory"
        ) from error
    published = False
    try:
        rootfs = staging / "rootfs"
        try:
            os.mkdir(rootfs, 0o755)
            os.chown(rootfs, 0, 0)
            os.chmod(rootfs, 0o755)
        except OSError as error:
            raise ImageBuildError("cannot create staging rootfs") from error
        layout = _parse_python_layout(spec.venv)
        assembler = RootfsAssembler(
            rootfs=rootfs,
            layout=layout,
            python_sources=spec.python_sources,
            readelf=spec.readelf,
            ldconfig=spec.ldconfig,
            venv_relocation=spec.venv_relocation,
            python_runtime_profile=spec.python_runtime_profile,
        )
        assembler.build(
            bash=spec.bash, gnu_commands=spec.gnu_commands, supervisor=spec.supervisor
        )
        digest = _seal_rootfs(spec, rootfs)
        _fsync_tree(staging)
        _rename_noreplace(staging, destination)
        published = True
        try:
            descriptor = os.open(
                spec.output_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ImageBuildError(
                "generation was published but artifact-store fsync failed; operator intervention is required"
            ) from error
        return destination, digest
    finally:
        if not published and os.path.lexists(staging):
            shutil.rmtree(staging)


def _build_spec(arguments: argparse.Namespace) -> BuildSpec:
    """@brief 将 argparse 值转为经过安全验证的 BuildSpec / Convert argparse values into a security-validated BuildSpec.

    @param arguments argparse 解析结果 / Parsed argparse result.
    @return 只包含可信绝对路径的 build spec / Build spec containing only trusted absolute paths.
    @raise ImageBuildError 权限、路径、input 类型或 allowlist 不满足时抛出 /
        Raised when privilege, paths, input types, or allowlist requirements are not met.
    """

    if os.geteuid() != 0:
        raise ImageBuildError(
            "wspctl image builds must run as root so every rootfs inode is root-owned"
        )
    generation = _validate_generation(arguments.generation)
    output_root = _validate_controlled_directory(
        Path(arguments.output_root),
        "artifact store",
        allow_insecure_development_ancestors=arguments.allow_insecure_development_output,
    )
    venv = _require_absolute_existing(arguments.venv, "venv", directory=True)
    python_sources = tuple(
        _require_absolute_existing(source, "python source", directory=True)
        for source in arguments.python_source
    )
    if len(set(python_sources)) != len(python_sources):
        raise ImageBuildError("--python-source entries must be unique")
    venv_relocation = _validate_relocatable_venv(venv, python_sources=python_sources)
    bash = _validate_executable_input(
        _require_absolute_existing(arguments.bash, "bash", directory=False),
        "bash",
    )
    if bash.name != "bash":
        raise ImageBuildError("--bash must resolve to an executable named bash")
    gnu_commands = tuple(
        _validate_command_input(
            _validate_executable_input(
                _require_absolute_existing(command, "GNU command", directory=False),
                "GNU command",
            )
        )
        for command in arguments.gnu_command
    )
    if len({command.name for command in gnu_commands}) != len(gnu_commands):
        raise ImageBuildError("--gnu-command basenames must be unique")
    supervisor = _validate_executable_input(
        _require_absolute_existing(
            arguments.wsp_systemd, "wsp-systemd", directory=False
        ),
        "wsp-systemd",
    )
    sealer = _validate_executable_input(
        _require_absolute_existing(
            arguments.sealer, "wspctl-image sealer", directory=False
        ),
        "wspctl-image sealer",
    )
    readelf = _validate_executable_input(
        _require_absolute_existing(arguments.readelf, "readelf", directory=False),
        "readelf",
    )
    ldconfig = _validate_executable_input(
        _require_absolute_existing(arguments.ldconfig, "ldconfig", directory=False),
        "ldconfig",
        require_elf=False,
    )
    return BuildSpec(
        generation=generation,
        output_root=output_root,
        venv=venv,
        python_sources=python_sources,
        bash=bash,
        gnu_commands=gnu_commands,
        supervisor=supervisor,
        sealer=sealer,
        readelf=readelf,
        ldconfig=ldconfig,
        venv_relocation=venv_relocation,
    )


def _argument_parser() -> argparse.ArgumentParser:
    """@brief 创建 strict image-builder CLI parser / Create the strict image-builder CLI parser.

    @return 配置完成的 argparse parser / Fully configured argparse parser.
    """

    parser = argparse.ArgumentParser(
        description="Build one root-owned immutable wspctl base generation from explicit trusted host inputs."
    )
    parser.add_argument(
        "--generation", required=True, help="safe immutable generation name"
    )
    parser.add_argument(
        "--output-root",
        required=True,
        help="existing root-owned artifact-store directory",
    )
    parser.add_argument(
        "--venv", required=True, help="absolute trusted project .venv path"
    )
    parser.add_argument(
        "--python-source",
        action="append",
        default=[],
        help="absolute trusted source root required by a path-only .pth; repeatable",
    )
    parser.add_argument(
        "--bash", required=True, help="absolute trusted Bash executable"
    )
    parser.add_argument(
        "--gnu-command",
        action="append",
        required=True,
        help="absolute trusted GNU command in the fixed allowlist; repeatable",
    )
    parser.add_argument(
        "--wsp-systemd", required=True, help="absolute built wsp-systemd executable"
    )
    parser.add_argument(
        "--sealer", required=True, help="absolute built wspctl-image executable"
    )
    parser.add_argument(
        "--readelf", required=True, help="absolute trusted readelf executable"
    )
    parser.add_argument(
        "--ldconfig", required=True, help="absolute trusted ldconfig executable"
    )
    parser.add_argument(
        "--allow-insecure-development-output",
        action="store_true",
        help="explicitly trust only artifact-store ancestors in a local developer checkout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """@brief image builder CLI 入口 / Image-builder CLI entry point.

    @param argv 可选参数序列；None 时使用 sys.argv / Optional argument sequence; uses sys.argv when None.
    @return 成功为 0，使用/安全错误为 64，构建错误为 78 / Zero on success, 64 on usage/security error, 78 on build error.
    """

    parser = _argument_parser()
    try:
        spec = _build_spec(parser.parse_args(argv))
        published, digest = build_image(spec)
    except ImageBuildError as error:
        print(f"build_wspctl_image: {error}", file=sys.stderr)
        return 78
    print(f"generation={spec.generation}")
    print(f"rootfs={published / 'rootfs'}")
    print(f"digest={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
