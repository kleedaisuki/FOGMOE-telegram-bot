"""@brief wspctl OCI publisher 的 descriptor/policy 测试 / Descriptor and policy tests for the wspctl OCI publisher."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType


#: @brief 仓库根 / Repository root.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief standalone publisher source / Standalone publisher source.
PUBLISHER_PATH = REPOSITORY_ROOT / "tools" / "publish_wspctl_image.py"


def _load_publisher() -> ModuleType:
    """@brief 加载 standalone publisher module / Load the standalone publisher module.

    @return publisher module / Publisher module.
    """

    specification = importlib.util.spec_from_file_location(
        "wspctl_oci_publisher_test", PUBLISHER_PATH
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("cannot load OCI publisher")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


#: @brief 被测 publisher / Publisher under test.
_PUBLISHER = _load_publisher()


def _write_blob(layout: Path, content: bytes) -> tuple[str, int]:
    """@brief 写入一个 content-addressed OCI blob / Write one content-addressed OCI blob.

    @param layout OCI layout 根 / OCI layout root.
    @param content blob bytes / Blob bytes.
    @return ``(digest, size)`` / ``(digest, size)``.
    """

    digest_hex = hashlib.sha256(content).hexdigest()
    destination = layout / "blobs" / "sha256" / digest_hex
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return f"sha256:{digest_hex}", len(content)


def _json_bytes(value: object) -> bytes:
    """@brief 生成规范测试 JSON / Produce canonical test JSON.

    @param value JSON value / JSON value.
    @return UTF-8 JSON bytes / UTF-8 JSON bytes.
    """

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _create_layout(root: Path) -> str:
    """@brief 创建最小合法 runnable OCI image layout / Create a minimal valid runnable OCI image layout.

    @param root 新 layout 根 / New layout root.
    @return manifest digest / Manifest digest.
    """

    root.mkdir()
    (root / "oci-layout").write_text(
        '{"imageLayoutVersion":"1.0.0"}', encoding="utf-8"
    )
    layer_digest, layer_size = _write_blob(root, b"empty-test-layer")
    config = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
        "config": {
            "Entrypoint": [
                "/usr/local/libexec/wspctl/wsp-systemd",
            ],
            "Labels": {"io.fogmoe.wspctl.contract": "2"},
        },
    }
    config_digest, config_size = _write_blob(root, _json_bytes(config))
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": config_digest,
            "size": config_size,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": layer_digest,
                "size": layer_size,
            }
        ],
    }
    manifest_digest, manifest_size = _write_blob(root, _json_bytes(manifest))
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": manifest_digest,
                "size": manifest_size,
                "annotations": {
                    "org.opencontainers.image.ref.name": "wspctl-runtime"
                },
            }
        ],
    }
    (root / "index.json").write_bytes(_json_bytes(index))
    return manifest_digest


def test_digest_and_platform_are_strongly_validated() -> None:
    """@brief digest/platform value objects 拒绝模糊输入 / Digest/platform value objects reject ambiguous input.

    @return None / None.
    """

    digest = _PUBLISHER.Sha256Digest("sha256:" + "a" * 64)
    assert digest.hex == "a" * 64
    assert _PUBLISHER.OciPlatform.parse("linux/amd64").rendered == "linux/amd64"
    for invalid in ("a" * 64, "sha256:" + "A" * 64, "sha512:" + "a" * 64):
        try:
            _PUBLISHER.Sha256Digest(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid digest accepted: {invalid}")


def test_layout_verifies_pinned_manifest_graph_and_runtime_policy() -> None:
    """@brief OCI graph、platform 和 runtime policy 必须一起验证 / OCI graph, platform, and runtime policy are verified together.

    @return None / None.
    """

    with tempfile.TemporaryDirectory() as directory:
        layout = Path(directory) / "layout"
        manifest_digest = _create_layout(layout)
        verified = _PUBLISHER.OciLayout(
            layout, owner_uid=os.getuid(), owner_gid=os.getgid()
        ).verify_reference(
            "wspctl-runtime",
            _PUBLISHER.Sha256Digest(manifest_digest),
            _PUBLISHER.OciPlatform.parse("linux/amd64"),
        )
        assert verified.manifest_digest.value == manifest_digest
        assert verified.platform.rendered == "linux/amd64"


def test_layout_rejects_blob_tampering_after_descriptor_selection() -> None:
    """@brief descriptor 指向的 blob 被替换时必须 fail closed / Replacing a descriptor-addressed blob fails closed.

    @return None / None.
    """

    with tempfile.TemporaryDirectory() as directory:
        layout = Path(directory) / "layout"
        manifest_digest = _create_layout(layout)
        manifest_blob = (
            layout / "blobs" / "sha256" / manifest_digest.removeprefix("sha256:")
        )
        manifest_blob.write_bytes(manifest_blob.read_bytes() + b"\n")
        try:
            _PUBLISHER.OciLayout(
                layout, owner_uid=os.getuid(), owner_gid=os.getgid()
            ).verify_reference(
                "wspctl-runtime",
                _PUBLISHER.Sha256Digest(manifest_digest),
                _PUBLISHER.OciPlatform.parse("linux/amd64"),
            )
        except _PUBLISHER.ImagePublishError as error:
            assert "metadata mismatch" in str(error) or "digest mismatch" in str(error)
        else:
            raise AssertionError("tampered OCI blob was accepted")


def test_umoci_bundle_metadata_is_preserved_by_the_publisher() -> None:
    """@brief publisher 必须保留 umoci mtree/provenance，不能假设 bundle 只含 rootfs / Publisher preserves umoci mtree/provenance and must not assume the bundle contains only rootfs.

    @return None / None.
    """

    source = PUBLISHER_PATH.read_text(encoding="utf-8")
    assert 'os.rename(bundle, staging / "umoci-metadata")' in source
    assert "os.rmdir(bundle)" not in source


if __name__ == "__main__":
    test_digest_and_platform_are_strongly_validated()
    test_layout_verifies_pinned_manifest_graph_and_runtime_policy()
    test_layout_rejects_blob_tampering_after_descriptor_selection()
    test_umoci_bundle_metadata_is_preserved_by_the_publisher()
