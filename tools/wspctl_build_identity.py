#!/usr/bin/env python3
"""@brief 计算 wspctl 构建输入的内容身份 / Compute content identities for wspctl build inputs.

安装器只把可追溯的源码输入、目标平台和构建协议写入收据（receipt）。这避免以目录
时间戳或 CMake cache 猜测产物是否新鲜，也让普通 wheel 与 host 工件使用同一语义。
The installer records only traceable source inputs, target-platform attributes, and this build
protocol in receipts. This avoids guessing freshness from mtimes or a CMake cache and gives the
regular wheel and host artifacts one shared meaning.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


#: @brief 构建身份协议版本；变更哈希语义时必须递增 / Build-identity protocol version; bump when hash semantics change.
PROTOCOL_VERSION = "wspctl-build-identity-v1"


@dataclass(frozen=True)
class ComponentInputs:
    """@brief 一个构建组件的显式源码边界 / Explicit source boundary for one build component.

    @param name 组件名 / Component name.
    @param paths 相对于源码根目录的输入路径 / Input paths relative to the source root.
    """

    name: str
    paths: tuple[str, ...]


#: @brief Python wheel、特权 host 工件与 OCI rootfs 的独立输入集合 / Independent input sets for the Python wheel, privileged host artifacts, and OCI rootfs.
COMPONENTS: dict[str, ComponentInputs] = {
    "client": ComponentInputs(
        name="client",
        paths=(
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "CMakeLists.txt",
            "src/fogmoe_bot",
            "src/fogmoe_config",
            "src/fogmoe_dashboard",
            "src/fogmoe_dbctl",
            "src/wspctl",
        ),
    ),
    "host": ComponentInputs(
        name="host",
        paths=(
            "CMakeLists.txt",
            "src/wspctl",
            "deploy/wspctl",
            "tools/publish_wspctl_image.py",
        ),
    ),
    "image": ComponentInputs(
        name="image",
        paths=(
            "CMakeLists.txt",
            "src/wspctl",
            "deploy/wspctl/image",
        ),
    ),
}


def parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    """@brief 解析命令行参数 / Parse command-line arguments.

    @param arguments 原始参数序列 / Raw argument sequence.
    @return 已验证的 argparse 命名空间 / Validated argparse namespace.
    """

    parser = argparse.ArgumentParser(
        description="Compute a deterministic source identity for a wspctl build component."
    )
    parser.add_argument(
        "--source-root",
        required=True,
        type=Path,
        help="repository root containing the declared build inputs",
    )
    parser.add_argument(
        "--component",
        required=True,
        choices=tuple(sorted(COMPONENTS)),
        help="declared build-input component",
    )
    parser.add_argument(
        "--attribute",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="additional deterministic build attribute; may be repeated",
    )
    return parser.parse_args(arguments)


def parse_attributes(raw_attributes: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """@brief 将 ``NAME=VALUE`` 属性规范化 / Canonicalize ``NAME=VALUE`` attributes.

    @param raw_attributes 未排序的原始属性 / Unsorted raw attributes.
    @return 按名称和值排序且无重复的属性 / Attributes sorted by name and value without duplicates.
    @raises ValueError 属性格式错误或名称重复时抛出 / Raised for malformed or duplicate attribute names.
    """

    parsed: dict[str, str] = {}
    for raw_attribute in raw_attributes:
        name, separator, value = raw_attribute.partition("=")
        if separator == "" or name == "" or "\x00" in name or "\x00" in value:
            raise ValueError(f"invalid build attribute: {raw_attribute!r}")
        if name in parsed:
            raise ValueError(f"duplicate build attribute: {name}")
        parsed[name] = value
    return tuple(sorted(parsed.items()))


def is_generated_path(path: Path) -> bool:
    """@brief 排除解释器与测试生成物 / Exclude interpreter and test generated files.

    @param path 相对于某个已声明输入目录的路径 / Path relative to a declared input directory.
    @return 是生成物时为真 / True when the path is generated.
    """

    generated_directory_names = {"__pycache__", ".pytest_cache", ".mypy_cache"}
    generated_suffixes = {".pyc", ".pyo", ".so", ".dylib", ".dll"}
    return any(part in generated_directory_names for part in path.parts) or path.suffix in generated_suffixes


def iter_declared_files(source_root: Path, component: ComponentInputs) -> Iterable[Path]:
    """@brief 按稳定顺序枚举组件的真实输入文件 / Enumerate a component's real input files in stable order.

    @param source_root 规范化后的仓库根目录 / Canonical repository root.
    @param component 声明的组件输入 / Declared component inputs.
    @return 相对于源码根目录的文件路径序列 / File paths relative to the source root.
    @raises FileNotFoundError 声明的输入不存在时抛出 / Raised when a declared input is absent.
    """

    files: list[Path] = []
    for declared_path in component.paths:
        candidate = source_root / declared_path
        if candidate.is_file() or candidate.is_symlink():
            files.append(candidate.relative_to(source_root))
            continue
        if not candidate.is_dir():
            raise FileNotFoundError(f"declared build input does not exist: {candidate}")
        for nested_path in candidate.rglob("*"):
            relative_path = nested_path.relative_to(source_root)
            if is_generated_path(relative_path) or nested_path.is_dir():
                continue
            if nested_path.is_file() or nested_path.is_symlink():
                files.append(relative_path)
    return tuple(sorted(set(files), key=lambda item: item.as_posix()))


def hash_file(source_root: Path, relative_path: Path) -> bytes:
    """@brief 计算一个源码文件或链接的内容摘要 / Hash one source file or symbolic link.

    @param source_root 规范化后的仓库根目录 / Canonical repository root.
    @param relative_path 相对于仓库根目录的文件路径 / File path relative to the repository root.
    @return 包含类型、路径及内容的 SHA-256 摘要 / SHA-256 digest containing type, path, and contents.
    """

    absolute_path = source_root / relative_path
    digest = hashlib.sha256()
    digest.update(relative_path.as_posix().encode("utf-8"))
    digest.update(b"\x00")
    if absolute_path.is_symlink():
        digest.update(b"symlink\x00")
        digest.update(absolute_path.readlink().as_posix().encode("utf-8"))
        return digest.digest()
    digest.update(b"file\x00")
    with absolute_path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def compute_identity(
    source_root: Path,
    component: ComponentInputs,
    attributes: Sequence[tuple[str, str]],
) -> str:
    """@brief 计算组件内容身份 / Compute a component content identity.

    @param source_root 规范化后的仓库根目录 / Canonical repository root.
    @param component 构建组件 / Build component.
    @param attributes 目标平台或 ABI 等额外语义输入 / Extra semantic inputs such as target platform or ABI.
    @return 小写十六进制 SHA-256 身份 / Lowercase hexadecimal SHA-256 identity.
    """

    digest = hashlib.sha256()
    digest.update(f"protocol={PROTOCOL_VERSION}\ncomponent={component.name}\n".encode("utf-8"))
    for name, value in attributes:
        digest.update(f"attribute={name}\x00{value}\n".encode("utf-8"))
    for relative_path in iter_declared_files(source_root, component):
        digest.update(b"input=")
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hash_file(source_root, relative_path))
        digest.update(b"\n")
    return digest.hexdigest()


def main(arguments: Sequence[str]) -> int:
    """@brief 运行命令行入口 / Run the command-line entrypoint.

    @param arguments 原始命令行参数 / Raw command-line arguments.
    @return 成功为零，输入无效时为非零 / Zero on success and nonzero for invalid inputs.
    """

    parsed_arguments = parse_arguments(arguments)
    source_root = parsed_arguments.source_root.resolve()
    if not source_root.is_dir():
        raise ValueError(f"source root is not a directory: {source_root}")
    component = COMPONENTS[parsed_arguments.component]
    attributes = parse_attributes(parsed_arguments.attribute)
    print(compute_identity(source_root, component, attributes))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError) as error:
        print(f"wspctl build identity failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
