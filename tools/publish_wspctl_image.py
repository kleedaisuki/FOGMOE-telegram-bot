#!/usr/bin/env python
"""@brief 验证并发布标准 OCI workspace image / Verify and publish a standard OCI workspace image.

本工具不构建镜像，也不解释 Python/ELF 依赖。它以明确的 OCI manifest digest 从候选 layout
复制到 root-owned staging，通过 descriptor graph 与 runtime config policy 后，委托 umoci
按标准 layer/whiteout 语义物化 rootfs，最后调用 native sealer 并原子发布。/
This tool does not build images or infer Python/ELF dependencies. It copies an explicitly pinned
OCI manifest from a candidate layout into root-owned staging, validates the descriptor graph and
runtime-config policy, delegates layer/whiteout materialization to umoci, then invokes the native
sealer and publishes atomically.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final


#: @brief OCI image layout 版本文件 / OCI image-layout version file.
_OCI_LAYOUT_FILE: Final = "oci-layout"
#: @brief OCI image index 文件 / OCI image-index file.
_OCI_INDEX_FILE: Final = "index.json"
#: @brief 固定的 staging reference / Fixed staging reference.
_STAGING_REFERENCE: Final = "wspctl-import"
#: @brief 标准 OCI manifest media type / Canonical OCI manifest media type.
_OCI_MANIFEST_MEDIA_TYPE: Final = "application/vnd.oci.image.manifest.v1+json"
#: @brief 标准 OCI config media type / Canonical OCI config media type.
_OCI_CONFIG_MEDIA_TYPE: Final = "application/vnd.oci.image.config.v1+json"
#: @brief 支持的标准 OCI layer media types / Supported canonical OCI layer media types.
_OCI_LAYER_MEDIA_TYPES: Final = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
    }
)
#: @brief Buildah/OCI reference-name annotation / Buildah/OCI reference-name annotation.
_REFERENCE_ANNOTATION: Final = "org.opencontainers.image.ref.name"
#: @brief wspctl runtime contract label / wspctl runtime-contract label.
_CONTRACT_LABEL: Final = "io.fogmoe.wspctl.contract"
#: @brief 固定 supervisor 入口 / Fixed supervisor entrypoint.
_SUPERVISOR: Final = "/usr/local/libexec/wspctl/wsp-systemd"
#: @brief renameat2 的 no-replace flag / renameat2 no-replace flag.
_RENAME_NOREPLACE: Final = 1
#: @brief AT_FDCWD / AT_FDCWD.
_AT_FDCWD: Final = -100


class ImagePublishError(RuntimeError):
    """@brief OCI image 发布失败 / OCI image publication failure."""


@dataclass(frozen=True, slots=True)
class Sha256Digest:
    """@brief 强类型 OCI SHA-256 digest / Strongly typed OCI SHA-256 digest.

    @param value 规范 ``sha256:<64 lowercase hex>`` / Canonical ``sha256:<64 lowercase hex>``.
    """

    value: str

    def __post_init__(self) -> None:
        """@brief 校验 digest 规范形式 / Validate the canonical digest form.

        @return None / None.
        @raise ValueError digest 非规范时抛出 / Raised when the digest is non-canonical.
        """

        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.value) is None:
            raise ValueError("digest must be canonical sha256:<64 lowercase hex>")

    @property
    def hex(self) -> str:
        """@brief 返回 path-safe hex 部分 / Return the path-safe hexadecimal component.

        @return 64 位小写 hex / 64-character lowercase hexadecimal value.
        """

        return self.value.removeprefix("sha256:")


@dataclass(frozen=True, slots=True)
class OciPlatform:
    """@brief 强类型 OCI 目标平台 / Strongly typed OCI target platform.

    @param os 操作系统名 / Operating-system name.
    @param architecture 架构名 / Architecture name.
    """

    os: str
    architecture: str

    def __post_init__(self) -> None:
        """@brief 限制为 wspctl 当前支持的平台 / Restrict to platforms currently supported by wspctl.

        @return None / None.
        @raise ValueError 平台不受支持时抛出 / Raised for an unsupported platform.
        """

        if self.os != "linux" or self.architecture not in {"amd64", "arm64"}:
            raise ValueError("wspctl supports only linux/amd64 and linux/arm64")

    @property
    def rendered(self) -> str:
        """@brief 渲染 OCI 平台字符串 / Render the OCI platform string.

        @return ``os/architecture`` / ``os/architecture``.
        """

        return f"{self.os}/{self.architecture}"

    @classmethod
    def parse(cls, value: str) -> OciPlatform:
        """@brief 解析规范 OCI 平台 / Parse a canonical OCI platform.

        @param value ``os/architecture`` 文本 / ``os/architecture`` text.
        @return 强类型平台 / Strongly typed platform.
        @raise ValueError 形式或平台不受支持时抛出 / Raised for malformed or unsupported input.
        """

        operating_system, separator, architecture = value.partition("/")
        if not separator or "/" in architecture:
            raise ValueError("platform must be canonical os/architecture")
        return cls(os=operating_system, architecture=architecture)


@dataclass(frozen=True, slots=True)
class OciDescriptor:
    """@brief 经类型检查的 OCI descriptor / Type-checked OCI descriptor.

    @param media_type descriptor media type / Descriptor media type.
    @param digest descriptor 内容摘要 / Descriptor content digest.
    @param size blob 大小 / Blob size.
    """

    media_type: str
    digest: Sha256Digest
    size: int

    @classmethod
    def parse(cls, value: object, *, context: str) -> OciDescriptor:
        """@brief 从 JSON object 解析 descriptor / Parse a descriptor from a JSON object.

        @param value JSON value / JSON value.
        @param context 错误上下文 / Error context.
        @return 已验证 descriptor / Validated descriptor.
        @raise ImagePublishError descriptor 无效时抛出 / Raised for an invalid descriptor.
        """

        if not isinstance(value, Mapping):
            raise ImagePublishError(f"{context} descriptor must be an object")
        media_type = value.get("mediaType")
        digest = value.get("digest")
        size = value.get("size")
        if (
            not isinstance(media_type, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ImagePublishError(f"{context} descriptor has invalid fields")
        try:
            parsed_digest = Sha256Digest(digest)
        except ValueError as error:
            raise ImagePublishError(f"{context} descriptor has invalid digest") from error
        return cls(media_type=media_type, digest=parsed_digest, size=size)


@dataclass(frozen=True, slots=True)
class VerifiedOciImage:
    """@brief 已验证、位于 root-owned staging 的 OCI image / Verified OCI image in root-owned staging.

    @param manifest_digest 权威 manifest digest / Authoritative manifest digest.
    @param platform image config 平台 / Image-config platform.
    """

    manifest_digest: Sha256Digest
    platform: OciPlatform


class OciLayout:
    """@brief 只读 OCI image-layout descriptor verifier / Read-only OCI image-layout descriptor verifier."""

    def __init__(self, root: Path, *, owner_uid: int = 0, owner_gid: int = 0) -> None:
        """@brief 绑定一个 root-owned OCI layout / Bind a root-owned OCI layout.

        @param root OCI layout 根 / OCI layout root.
        @param owner_uid 可信 owner UID；production 固定为 root / Trusted owner UID; root in production.
        @param owner_gid 可信 owner GID；production 固定为 root / Trusted owner GID; root in production.
        @return None / None.
        @raise ImagePublishError layout 路径无效时抛出 / Raised for an invalid layout path.
        """

        self._root = root.resolve(strict=True)
        self._owner_uid = owner_uid
        self._owner_gid = owner_gid
        metadata = os.lstat(self._root)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != self._owner_uid
            or metadata.st_gid != self._owner_gid
        ):
            raise ImagePublishError("staged OCI layout must be a root-owned directory")

    def verify_reference(
        self,
        reference: str,
        expected_digest: Sha256Digest,
        expected_platform: OciPlatform,
    ) -> VerifiedOciImage:
        """@brief 验证 reference、descriptor graph 与 runtime config / Verify a reference, descriptor graph, and runtime config.

        @param reference OCI reference annotation / OCI reference annotation.
        @param expected_digest 操作者固定的 manifest digest / Operator-pinned manifest digest.
        @param expected_platform 期望平台 / Expected platform.
        @return 已验证 OCI image / Verified OCI image.
        @raise ImagePublishError graph、digest 或 policy 不匹配时抛出 /
            Raised when the graph, digest, or policy does not match.
        """

        layout = self._read_json_file(self._root / _OCI_LAYOUT_FILE, "oci-layout")
        if layout != {"imageLayoutVersion": "1.0.0"}:
            raise ImagePublishError("unsupported or non-canonical OCI layout version")
        index = self._read_json_file(self._root / _OCI_INDEX_FILE, "index")
        manifests = index.get("manifests")
        if index.get("schemaVersion") != 2 or not isinstance(manifests, list):
            raise ImagePublishError("OCI index must have schemaVersion=2 and manifests")
        selected: list[OciDescriptor] = []
        for raw_descriptor in manifests:
            if not isinstance(raw_descriptor, Mapping):
                raise ImagePublishError("OCI index contains a non-object descriptor")
            annotations = raw_descriptor.get("annotations", {})
            if (
                isinstance(annotations, Mapping)
                and annotations.get(_REFERENCE_ANNOTATION) == reference
            ):
                selected.append(
                    OciDescriptor.parse(raw_descriptor, context="manifest")
                )
        if len(selected) != 1:
            raise ImagePublishError(
                "OCI reference must resolve to exactly one manifest descriptor"
            )
        manifest_descriptor = selected[0]
        if (
            manifest_descriptor.media_type != _OCI_MANIFEST_MEDIA_TYPE
            or manifest_descriptor.digest != expected_digest
        ):
            raise ImagePublishError(
                "OCI reference does not match the pinned image-manifest digest"
            )
        manifest = self._read_descriptor_json(manifest_descriptor, "manifest")
        config_descriptor = OciDescriptor.parse(
            manifest.get("config"), context="config"
        )
        raw_layers = manifest.get("layers")
        if (
            manifest.get("schemaVersion") != 2
            or config_descriptor.media_type != _OCI_CONFIG_MEDIA_TYPE
            or not isinstance(raw_layers, list)
            or not raw_layers
        ):
            raise ImagePublishError("OCI manifest violates the runnable-image contract")
        layers = tuple(
            OciDescriptor.parse(layer, context="layer") for layer in raw_layers
        )
        if any(layer.media_type not in _OCI_LAYER_MEDIA_TYPES for layer in layers):
            raise ImagePublishError("OCI manifest contains an unsupported layer media type")
        for layer in layers:
            self._verify_blob(layer)
        config = self._read_descriptor_json(config_descriptor, "config")
        platform = self._verify_config(config, layers)
        if platform != expected_platform:
            raise ImagePublishError(
                f"OCI image platform is {platform.rendered}, expected {expected_platform.rendered}"
            )
        return VerifiedOciImage(
            manifest_digest=manifest_descriptor.digest,
            platform=platform,
        )

    def _verify_config(
        self, config: Mapping[str, object], layers: Sequence[OciDescriptor]
    ) -> OciPlatform:
        """@brief 验证 runnable image config / Verify the runnable image config.

        @param config 已解析 config JSON / Parsed config JSON.
        @param layers manifest layer descriptors / Manifest layer descriptors.
        @return config 平台 / Config platform.
        @raise ImagePublishError runtime contract 不满足时抛出 /
            Raised when the runtime contract is not satisfied.
        """

        operating_system = config.get("os")
        architecture = config.get("architecture")
        if not isinstance(operating_system, str) or not isinstance(
            architecture, str
        ):
            raise ImagePublishError("OCI config is missing os/architecture")
        try:
            platform = OciPlatform(
                os=operating_system, architecture=architecture
            )
        except ValueError as error:
            raise ImagePublishError("OCI config platform is unsupported") from error
        rootfs = config.get("rootfs")
        runtime_config = config.get("config")
        if not isinstance(rootfs, Mapping) or not isinstance(runtime_config, Mapping):
            raise ImagePublishError("OCI config is missing rootfs/config objects")
        diff_ids = rootfs.get("diff_ids")
        if (
            rootfs.get("type") != "layers"
            or not isinstance(diff_ids, list)
            or len(diff_ids) != len(layers)
        ):
            raise ImagePublishError("OCI config diff_ids do not match manifest layers")
        try:
            tuple(Sha256Digest(value) for value in diff_ids if isinstance(value, str))
        except ValueError as error:
            raise ImagePublishError("OCI config contains an invalid DiffID") from error
        if len([value for value in diff_ids if isinstance(value, str)]) != len(diff_ids):
            raise ImagePublishError("OCI config contains a non-string DiffID")
        entrypoint = runtime_config.get("Entrypoint")
        labels = runtime_config.get("Labels")
        if entrypoint != [_SUPERVISOR]:
            raise ImagePublishError("OCI config has an unexpected supervisor entrypoint")
        if not isinstance(labels, Mapping) or labels.get(_CONTRACT_LABEL) != "2":
            raise ImagePublishError("OCI config is missing wspctl runtime contract label")
        return platform

    def _read_descriptor_json(
        self, descriptor: OciDescriptor, context: str
    ) -> Mapping[str, object]:
        """@brief 校验并解析 JSON blob / Verify and parse a JSON blob.

        @param descriptor blob descriptor / Blob descriptor.
        @param context 错误上下文 / Error context.
        @return JSON object / JSON object.
        @raise ImagePublishError blob 或 JSON 无效时抛出 / Raised for an invalid blob or JSON.
        """

        blob = self._verify_blob(descriptor)
        return self._read_json_file(blob, context)

    def _verify_blob(self, descriptor: OciDescriptor) -> Path:
        """@brief 验证一个 content-addressed blob / Verify one content-addressed blob.

        @param descriptor blob descriptor / Blob descriptor.
        @return 规范 blob 路径 / Canonical blob path.
        @raise ImagePublishError inode、size 或 digest 不匹配时抛出 /
            Raised on inode, size, or digest mismatch.
        """

        blob = self._root / "blobs" / "sha256" / descriptor.digest.hex
        try:
            metadata = os.lstat(blob)
        except OSError as error:
            raise ImagePublishError(
                f"OCI blob is missing: {descriptor.digest.value}"
            ) from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != self._owner_uid
            or metadata.st_gid != self._owner_gid
            or metadata.st_size != descriptor.size
        ):
            raise ImagePublishError(
                f"OCI blob metadata mismatch: {descriptor.digest.value}"
            )
        with blob.open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != descriptor.digest.hex:
            raise ImagePublishError(
                f"OCI blob digest mismatch: {descriptor.digest.value}"
            )
        return blob

    @staticmethod
    def _read_json_file(path: Path, context: str) -> Mapping[str, object]:
        """@brief 读取有界 JSON object / Read a bounded JSON object.

        @param path regular JSON 文件 / Regular JSON file.
        @param context 错误上下文 / Error context.
        @return JSON object / JSON object.
        @raise ImagePublishError 文件过大或 JSON 无效时抛出 /
            Raised when the file is oversized or invalid JSON.
        """

        try:
            metadata = os.lstat(path)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16 * 1024 * 1024:
                raise ImagePublishError(f"{context} must be a bounded regular file")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ImagePublishError(f"cannot parse {context} JSON") from error
        if not isinstance(value, Mapping):
            raise ImagePublishError(f"{context} JSON must be an object")
        return value


@dataclass(frozen=True, slots=True)
class PublishSpec:
    """@brief 一次 OCI image 发布的完整输入 / Complete inputs for one OCI image publication.

    @param source_layout 用户提供的候选 OCI layout / User-provided candidate OCI layout.
    @param source_reference 候选 layout reference / Candidate-layout reference.
    @param manifest_digest 固定 OCI manifest digest / Pinned OCI manifest digest.
    @param platform 固定目标平台 / Pinned target platform.
    @param artifact_store root-owned content-addressed store / Root-owned content-addressed store.
    @param sealer native wspctl-image executable / Native wspctl-image executable.
    @param skopeo skopeo executable / Skopeo executable.
    @param umoci umoci executable / Umoci executable.
    """

    source_layout: Path
    source_reference: str
    manifest_digest: Sha256Digest
    platform: OciPlatform
    artifact_store: Path
    sealer: Path
    skopeo: Path
    umoci: Path


class ImagePublisher:
    """@brief OCI image 的验证式 importer/publisher / Verifying importer/publisher for OCI images."""

    def __init__(self, spec: PublishSpec) -> None:
        """@brief 创建 publisher / Create a publisher.

        @param spec 已验证输入 / Validated inputs.
        @return None / None.
        """

        self._spec = spec

    def publish(self) -> Path:
        """@brief 导入、物化、seal 并原子发布 image / Import, materialize, seal, and atomically publish an image.

        @return 发布后的 image 目录 / Published image directory.
        @raise ImagePublishError 任一步失败时抛出 / Raised when any step fails.
        """

        algorithm_root = self._spec.artifact_store / "sha256"
        algorithm_root.mkdir(mode=0o700, exist_ok=True)
        os.chown(algorithm_root, 0, 0)
        os.chmod(algorithm_root, 0o700)
        destination = algorithm_root / self._spec.manifest_digest.hex
        if destination.exists():
            self._verify_existing(destination)
            return destination
        staging = Path(
            tempfile.mkdtemp(prefix=".import-staging-", dir=algorithm_root)
        )
        os.chown(staging, 0, 0)
        os.chmod(staging, 0o700)
        published = False
        try:
            staged_layout = staging / "oci"
            self._copy_candidate(staged_layout)
            verified = OciLayout(staged_layout).verify_reference(
                _STAGING_REFERENCE,
                self._spec.manifest_digest,
                self._spec.platform,
            )
            bundle = staging / "bundle"
            self._run(
                [
                    str(self._spec.umoci),
                    "unpack",
                    "--image",
                    f"{staged_layout}:{_STAGING_REFERENCE}",
                    str(bundle),
                ],
                "umoci failed to materialize the verified OCI image",
            )
            rootfs = bundle / "rootfs"
            if not rootfs.is_dir():
                raise ImagePublishError("umoci did not produce bundle/rootfs")
            published_rootfs = staging / "rootfs"
            os.rename(rootfs, published_rootfs)
            runtime_config = bundle / "config.json"
            if runtime_config.is_file():
                os.rename(runtime_config, staging / "runtime-config.json")
            os.rename(bundle, staging / "umoci-metadata")
            completed = self._run(
                [
                    str(self._spec.sealer),
                    "--seal",
                    "--base-root",
                    str(published_rootfs),
                    "--source-oci-manifest-digest",
                    verified.manifest_digest.value,
                    "--platform",
                    verified.platform.rendered,
                ],
                "native sealer rejected the materialized runtime contract",
            )
            if (
                f"source_oci_manifest_digest={verified.manifest_digest.value}"
                not in completed.stdout.splitlines()
            ):
                raise ImagePublishError(
                    "native sealer returned an unexpected image identity"
                )
            _rename_noreplace(staging, destination)
            published = True
            return destination
        finally:
            if not published and os.path.lexists(staging):
                shutil.rmtree(staging)

    def _copy_candidate(self, destination: Path) -> None:
        """@brief 用 skopeo 将候选复制到 root-owned CAS staging / Copy the candidate into root-owned CAS staging with Skopeo.

        @param destination 新 staging OCI layout / New staging OCI layout.
        @return None / None.
        @raise ImagePublishError skopeo copy 失败时抛出 / Raised when Skopeo copy fails.
        """

        self._run(
            [
                str(self._spec.skopeo),
                "copy",
                "--preserve-digests",
                f"oci:{self._spec.source_layout}:{self._spec.source_reference}",
                f"oci:{destination}:{_STAGING_REFERENCE}",
            ],
            "skopeo failed to ingest the candidate OCI image",
        )
        for directory, directories, files in os.walk(destination):
            os.chown(directory, 0, 0)
            for name in directories:
                os.chown(Path(directory) / name, 0, 0)
            for name in files:
                os.chown(Path(directory) / name, 0, 0)

    def _verify_existing(self, destination: Path) -> None:
        """@brief 验证同 digest 的既有 publication / Verify an existing publication with the same digest.

        @param destination content-addressed image 目录 / Content-addressed image directory.
        @return None / None.
        @raise ImagePublishError 既有目录与 digest 不一致时抛出 /
            Raised when the existing directory does not match the digest.
        """

        rootfs = destination / "rootfs"
        completed = self._run(
            [
                str(self._spec.sealer),
                "--inspect",
                "true",
                "--base-root",
                str(rootfs),
            ],
            "existing content-addressed image failed verification",
        )
        expected = (
            f"source_oci_manifest_digest={self._spec.manifest_digest.value}"
        )
        if expected not in completed.stdout.splitlines():
            raise ImagePublishError("existing image identity does not match request")

    @staticmethod
    def _run(arguments: Sequence[str], message: str) -> subprocess.CompletedProcess[str]:
        """@brief 运行一个无 shell 的有界发布命令 / Run one bounded publication command without a shell.

        @param arguments argv / argv.
        @param message 失败消息 / Failure message.
        @return completed process / Completed process.
        @raise ImagePublishError 命令失败或超时时抛出 / Raised on failure or timeout.
        """

        try:
            completed = subprocess.run(
                arguments,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={"LC_ALL": "C", "PATH": ""},
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ImagePublishError(message) from error
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-4096:]
            raise ImagePublishError(f"{message}: {detail or 'no diagnostic'}")
        return completed


def _rename_noreplace(source: Path, destination: Path) -> None:
    """@brief 用 renameat2 原子发布且不覆盖 / Atomically publish with renameat2 and no replacement.

    @param source staging 目录 / Staging directory.
    @param destination content-addressed destination / Content-addressed destination.
    @return None / None.
    @raise ImagePublishError destination 已存在或 syscall 失败时抛出 /
        Raised when the destination exists or the syscall fails.
    """

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ImagePublishError("renameat2 is required for atomic publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
        == 0
    ):
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ImagePublishError("content-addressed image already exists")
    raise ImagePublishError(
        f"renameat2 publication failed: {os.strerror(error_number)}"
    )


def _validated_executable(value: str, label: str) -> Path:
    """@brief 校验一个绝对 regular executable / Validate an absolute regular executable.

    @param value CLI path / CLI path.
    @param label 错误标签 / Error label.
    @return 规范路径 / Canonical path.
    @raise ImagePublishError 路径不满足时抛出 / Raised when the path is unsuitable.
    """

    path = Path(value)
    if not path.is_absolute():
        raise ImagePublishError(f"{label} must be an absolute path")
    resolved = path.resolve(strict=True)
    metadata = os.stat(resolved)
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise ImagePublishError(f"{label} must be an executable regular file")
    return resolved


def _build_spec(arguments: argparse.Namespace) -> PublishSpec:
    """@brief 将 CLI 输入转成强类型发布 spec / Convert CLI input into a strongly typed publication spec.

    @param arguments argparse namespace / Argparse namespace.
    @return 发布 spec / Publication spec.
    @raise ImagePublishError privilege、路径或类型无效时抛出 /
        Raised for invalid privilege, paths, or values.
    """

    if os.geteuid() != 0:
        raise ImagePublishError("OCI image publication must run as root")
    source_layout = Path(arguments.source_layout)
    artifact_store = Path(arguments.artifact_store)
    if not source_layout.is_absolute() or not artifact_store.is_absolute():
        raise ImagePublishError("source layout and artifact store must be absolute")
    source_layout = source_layout.resolve(strict=True)
    artifact_store = artifact_store.resolve(strict=True)
    store_metadata = os.stat(artifact_store)
    if (
        not stat.S_ISDIR(store_metadata.st_mode)
        or store_metadata.st_uid != 0
        or store_metadata.st_gid != 0
        or store_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ImagePublishError(
            "artifact store must be a root-owned, non-group/world-writable directory"
        )
    try:
        manifest_digest = Sha256Digest(arguments.manifest_digest)
        platform = OciPlatform.parse(arguments.platform)
    except ValueError as error:
        raise ImagePublishError(str(error)) from error
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", arguments.source_reference)
        is None
    ):
        raise ImagePublishError("source reference is unsafe")
    return PublishSpec(
        source_layout=source_layout,
        source_reference=arguments.source_reference,
        manifest_digest=manifest_digest,
        platform=platform,
        artifact_store=artifact_store,
        sealer=_validated_executable(arguments.sealer, "sealer"),
        skopeo=_validated_executable(arguments.skopeo, "skopeo"),
        umoci=_validated_executable(arguments.umoci, "umoci"),
    )


def _parser() -> argparse.ArgumentParser:
    """@brief 创建严格 CLI parser / Create the strict CLI parser.

    @return 配置后的 parser / Configured parser.
    """

    parser = argparse.ArgumentParser(
        description="Verify, materialize, seal, and atomically publish one pinned OCI wspctl image."
    )
    parser.add_argument("--source-layout", required=True)
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--manifest-digest", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--artifact-store", required=True)
    parser.add_argument("--sealer", required=True)
    parser.add_argument("--skopeo", required=True)
    parser.add_argument("--umoci", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """@brief 发布 CLI 入口 / Publication CLI entry point.

    @param argv 可选 argv / Optional argv.
    @return 成功为零，输入/发布错误为 78 / Zero on success, 78 on input/publication error.
    """

    try:
        spec = _build_spec(_parser().parse_args(argv))
        destination = ImagePublisher(spec).publish()
    except ImagePublishError as error:
        print(f"publish_wspctl_image: {error}", file=os.sys.stderr)
        return 78
    print(f"source_oci_manifest_digest={spec.manifest_digest.value}")
    print(f"image={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
