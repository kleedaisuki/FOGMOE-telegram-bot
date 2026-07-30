"""@brief wspctl OCI publisher 的 descriptor/policy 测试 / Descriptor and policy tests for the wspctl OCI publisher."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest


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


def test_standalone_publisher_parses_with_the_distro_python() -> None:
    """@brief root publisher 必须能由声明支持的 distro Python 解析 / The root publisher must parse with the supported distro Python.

    @return None / None.
    @note 发布入口刻意不执行用户可写的项目 venv；这个测试覆盖实际的 ``/usr/bin/python3`` 边界。/
        Publication deliberately avoids the user-writable project venv; this test covers the real
        ``/usr/bin/python3`` boundary.
    """

    checked = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            "import pathlib,sys; source=pathlib.Path(sys.argv[1]).read_bytes(); "
            "compile(source, sys.argv[1], 'exec')",
            str(PUBLISHER_PATH),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert checked.returncode == 0, checked.stderr


def test_subprocess_environment_forwards_only_standard_proxy_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """@brief publisher 子进程只继承代理 allowlist / Publisher subprocesses inherit only the proxy allowlist.

    @param monkeypatch pytest 环境隔离工具 / Pytest environment-isolation helper.
    @return None / None.
    """

    for variable_name in _PUBLISHER._PROXY_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://172.29.64.1:10809")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/ssh-agent")
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "must-not-cross-boundary")

    environment = _PUBLISHER._subprocess_environment()

    assert environment == {
        "LC_ALL": "C",
        "PATH": "",
        "HTTPS_PROXY": "http://172.29.64.1:10809",
        "no_proxy": "127.0.0.1,localhost",
    }


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
    (root / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}', encoding="utf-8")
    layer_digest, layer_size = _write_blob(root, b"empty-test-layer")
    config = {
        "architecture": "amd64",
        "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [layer_digest]},
        "config": {
            "Entrypoint": [
                "/usr/local/libexec/wspctl/wsp-systemd",
            ],
            "Labels": {"io.fogmoe.wspctl.contract": "3"},
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
                "annotations": {"org.opencontainers.image.ref.name": "wspctl-runtime"},
            }
        ],
    }
    (root / "index.json").write_bytes(_json_bytes(index))
    return manifest_digest


def _write_executable(path: Path, source: str) -> Path:
    """@brief 写入可在空 PATH 下运行的测试程序 / Write a test program runnable with an empty PATH.

    @param path 程序路径 / Program path.
    @param source Python 程序正文 / Python program body.
    @return 可执行文件路径 / Executable path.
    """

    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _create_fake_toolchain(
    root: Path, *, fail_umoci: bool = False
) -> tuple[Path, Path, Path, Path]:
    """@brief 创建隔离的 skopeo/umoci/sealer 假实现 / Create isolated fake Skopeo, umoci, and sealer programs.

    @param root 工具目录 / Tool directory.
    @param fail_umoci 是否让 umoci 在留下部分 bundle 后失败 /
        Whether umoci should fail after leaving a partial bundle.
    @return ``(skopeo, umoci, sealer, call_log)`` /
        ``(skopeo, umoci, sealer, call_log)``.
    """

    root.mkdir()
    call_log = root / "calls.log"
    skopeo = _write_executable(
        root / "skopeo",
        f"""
import json
import shutil
import sys
from pathlib import Path


def endpoint(value):
    if not value.startswith("oci:"):
        raise SystemExit("expected oci transport")
    return value.removeprefix("oci:").rsplit(":", 1)


source, source_reference = endpoint(sys.argv[-2])
destination, destination_reference = endpoint(sys.argv[-1])
shutil.copytree(source, destination)
index_path = Path(destination) / "index.json"
index = json.loads(index_path.read_text(encoding="utf-8"))
for descriptor in index["manifests"]:
    annotations = descriptor.setdefault("annotations", {{}})
    if annotations.get("org.opencontainers.image.ref.name") == source_reference:
        annotations["org.opencontainers.image.ref.name"] = destination_reference
index_path.write_text(
    json.dumps(index, sort_keys=True, separators=(",", ":")),
    encoding="utf-8",
)
with Path({str(call_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write("skopeo-copy\\n")
""",
    )
    umoci_failure = (
        """
(bundle / "rootfs" / "partial").write_text("partial", encoding="utf-8")
print("synthetic umoci failure", file=sys.stderr)
raise SystemExit(42)
"""
        if fail_umoci
        else """
(bundle / "rootfs" / "payload").write_text("complete", encoding="utf-8")
(bundle / "config.json").write_text('{"ociVersion":"1.0.2"}', encoding="utf-8")
(bundle / "umoci.json").write_text('{"version":"fake-umoci"}', encoding="utf-8")
"""
    )
    umoci = _write_executable(
        root / "umoci",
        f"""
import sys
from pathlib import Path


bundle = Path(sys.argv[-1])
(bundle / "rootfs").mkdir(parents=True)
with Path({str(call_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write("umoci-unpack\\n")
{umoci_failure}
""",
    )
    sealer = _write_executable(
        root / "wspctl-image",
        f"""
import json
import sys
from pathlib import Path


arguments = sys.argv[1:]
rootfs = Path(arguments[arguments.index("--base-root") + 1])
with Path({str(call_log)!r}).open("a", encoding="utf-8") as stream:
    stream.write("sealer-inspect\\n" if "--inspect" in arguments else "sealer-seal\\n")
if "--seal" in arguments:
    if (rootfs / "payload").read_text(encoding="utf-8") != "complete":
        raise SystemExit("materialized payload is incomplete")
    digest = arguments[arguments.index("--source-oci-manifest-digest") + 1]
    platform = arguments[arguments.index("--platform") + 1]
    (rootfs / ".wspctl-image-manifest").write_text(
        json.dumps({{"source_oci_manifest_digest": digest, "platform": platform}}),
        encoding="utf-8",
    )
else:
    manifest = json.loads(
        (rootfs / ".wspctl-image-manifest").read_text(encoding="utf-8")
    )
    digest = manifest["source_oci_manifest_digest"]
print(f"source_oci_manifest_digest={{digest}}")
""",
    )
    return skopeo, umoci, sealer, call_log


def _publish_spec(
    *,
    source_layout: Path,
    manifest_digest: str,
    artifact_store: Path,
    skopeo: Path,
    umoci: Path,
    sealer: Path,
) -> object:
    """@brief 创建完整发布输入 / Create a complete publication specification.

    @param source_layout 候选 OCI layout / Candidate OCI layout.
    @param manifest_digest 固定 manifest digest / Pinned manifest digest.
    @param artifact_store 发布 store / Publication store.
    @param skopeo 测试 skopeo / Test Skopeo executable.
    @param umoci 测试 umoci / Test umoci executable.
    @param sealer 测试 native sealer / Test native sealer executable.
    @return ``PublishSpec`` 实例 / A ``PublishSpec`` instance.
    """

    return _PUBLISHER.PublishSpec(
        source_layout=source_layout,
        source_reference="wspctl-runtime",
        manifest_digest=_PUBLISHER.Sha256Digest(manifest_digest),
        platform=_PUBLISHER.OciPlatform.parse("linux/amd64"),
        artifact_store=artifact_store,
        skopeo=skopeo,
        umoci=umoci,
        sealer=sealer,
    )


def _allow_unprivileged_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    """@brief 仅替换测试环境无法满足的 root ownership 边界 / Replace only the root-ownership boundary unavailable in tests.

    @param monkeypatch pytest patch controller / Pytest patch controller.
    @return None / None.
    @note subprocess、staging、descriptor 验证和 renameat2 仍走生产实现 /
        Subprocesses, staging, descriptor validation, and renameat2 still use production code.
    """

    production_layout = _PUBLISHER.OciLayout

    class CurrentUserOciLayout(production_layout):
        """@brief 用当前测试 UID 验证 staging / Verify staging with the current test UID."""

        def __init__(self, root: Path) -> None:
            """@brief 绑定当前用户拥有的测试 layout / Bind a test layout owned by the current user.

            @param root OCI layout 根 / OCI layout root.
            @return None / None.
            """

            super().__init__(root, owner_uid=os.getuid(), owner_gid=os.getgid())

    monkeypatch.setattr(_PUBLISHER.os, "chown", lambda *_arguments: None)
    monkeypatch.setattr(_PUBLISHER, "OciLayout", CurrentUserOciLayout)


def _read_calls(call_log: Path) -> list[str]:
    """@brief 读取有序 fake-tool 调用记录 / Read the ordered fake-tool call log.

    @param call_log 调用日志 / Call log.
    @return 调用名称列表 / Ordered call names.
    """

    return call_log.read_text(encoding="utf-8").splitlines()


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


def test_publisher_materializes_seals_and_preserves_umoci_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """@brief 完整 fake toolchain 发布 rootfs 并保留 umoci 元数据 / The complete fake toolchain publishes rootfs and preserves umoci metadata.

    @return None / None.
    """

    source_layout = tmp_path / "source"
    manifest_digest = _create_layout(source_layout)
    artifact_store = tmp_path / "images"
    artifact_store.mkdir()
    skopeo, umoci, sealer, call_log = _create_fake_toolchain(tmp_path / "tools")
    _allow_unprivileged_publication(monkeypatch)

    destination = _PUBLISHER.ImagePublisher(
        _publish_spec(
            source_layout=source_layout,
            manifest_digest=manifest_digest,
            artifact_store=artifact_store,
            skopeo=skopeo,
            umoci=umoci,
            sealer=sealer,
        )
    ).publish()

    assert destination == (
        artifact_store / "sha256" / manifest_digest.removeprefix("sha256:")
    )
    assert (destination / "rootfs" / "payload").read_text() == "complete"
    assert json.loads(
        (destination / "runtime-config.json").read_text(encoding="utf-8")
    ) == {"ociVersion": "1.0.2"}
    assert json.loads(
        (destination / "umoci-metadata" / "umoci.json").read_text(encoding="utf-8")
    ) == {"version": "fake-umoci"}
    assert (destination / "oci" / "oci-layout").is_file()
    assert _read_calls(call_log) == [
        "skopeo-copy",
        "umoci-unpack",
        "sealer-seal",
    ]
    assert not list((artifact_store / "sha256").glob(".import-staging-*"))


def test_existing_publication_is_verified_without_rematerializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """@brief 同 digest 重复发布只验证既有不可变对象 / Republishing the same digest only verifies the existing immutable object.

    @return None / None.
    """

    source_layout = tmp_path / "source"
    manifest_digest = _create_layout(source_layout)
    artifact_store = tmp_path / "images"
    artifact_store.mkdir()
    skopeo, umoci, sealer, call_log = _create_fake_toolchain(tmp_path / "tools")
    _allow_unprivileged_publication(monkeypatch)
    publisher = _PUBLISHER.ImagePublisher(
        _publish_spec(
            source_layout=source_layout,
            manifest_digest=manifest_digest,
            artifact_store=artifact_store,
            skopeo=skopeo,
            umoci=umoci,
            sealer=sealer,
        )
    )

    first = publisher.publish()
    before = (first / "rootfs" / "payload").stat()
    second = publisher.publish()

    assert second == first
    assert (second / "rootfs" / "payload").stat() == before
    assert _read_calls(call_log) == [
        "skopeo-copy",
        "umoci-unpack",
        "sealer-seal",
        "sealer-inspect",
    ]


def test_atomic_publication_never_replaces_a_concurrent_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """@brief renameat2 竞争失败保留胜者并清理 staging / A lost renameat2 race preserves the winner and removes staging.

    @return None / None.
    """

    source_layout = tmp_path / "source"
    manifest_digest = _create_layout(source_layout)
    artifact_store = tmp_path / "images"
    artifact_store.mkdir()
    skopeo, umoci, sealer, _call_log = _create_fake_toolchain(tmp_path / "tools")
    _allow_unprivileged_publication(monkeypatch)
    production_rename = _PUBLISHER._rename_noreplace

    def concurrent_publish(source: Path, destination: Path) -> None:
        """@brief 在 CAS rename 前模拟另一个成功 publisher / Simulate another successful publisher before the CAS rename.

        @param source 当前 staging / Current staging directory.
        @param destination CAS destination / CAS destination.
        @return None / None.
        """

        destination.mkdir()
        (destination / "winner").write_text("untouched", encoding="utf-8")
        production_rename(source, destination)

    monkeypatch.setattr(_PUBLISHER, "_rename_noreplace", concurrent_publish)
    publisher = _PUBLISHER.ImagePublisher(
        _publish_spec(
            source_layout=source_layout,
            manifest_digest=manifest_digest,
            artifact_store=artifact_store,
            skopeo=skopeo,
            umoci=umoci,
            sealer=sealer,
        )
    )

    with pytest.raises(
        _PUBLISHER.ImagePublishError,
        match="content-addressed image already exists",
    ):
        publisher.publish()

    destination = artifact_store / "sha256" / manifest_digest.removeprefix("sha256:")
    assert (destination / "winner").read_text(encoding="utf-8") == "untouched"
    assert not (destination / "rootfs").exists()
    assert not list((artifact_store / "sha256").glob(".import-staging-*"))


def test_failed_umoci_unpack_leaves_no_destination_or_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """@brief 部分 unpack 失败不污染 CAS destination / A partial unpack failure does not pollute the CAS destination.

    @return None / None.
    """

    source_layout = tmp_path / "source"
    manifest_digest = _create_layout(source_layout)
    artifact_store = tmp_path / "images"
    artifact_store.mkdir()
    skopeo, umoci, sealer, call_log = _create_fake_toolchain(
        tmp_path / "tools", fail_umoci=True
    )
    _allow_unprivileged_publication(monkeypatch)
    publisher = _PUBLISHER.ImagePublisher(
        _publish_spec(
            source_layout=source_layout,
            manifest_digest=manifest_digest,
            artifact_store=artifact_store,
            skopeo=skopeo,
            umoci=umoci,
            sealer=sealer,
        )
    )

    with pytest.raises(
        _PUBLISHER.ImagePublishError,
        match="umoci failed.*synthetic umoci failure",
    ):
        publisher.publish()

    algorithm_root = artifact_store / "sha256"
    assert not (algorithm_root / manifest_digest.removeprefix("sha256:")).exists()
    assert not list(algorithm_root.glob(".import-staging-*"))
    assert _read_calls(call_log) == ["skopeo-copy", "umoci-unpack"]
    assert (source_layout / "index.json").is_file()


def test_activator_installs_reboot_restorable_mount_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """@brief activator 持久安装 mount unit 且重复激活不修改 readonly root / The activator installs a persistent mount unit and reactivation is idempotent.

    @param tmp_path pytest temporary path / Pytest temporary path.
    @param monkeypatch pytest patch controller / Pytest patch controller.
    @return None / None.
    """

    digest = _PUBLISHER.Sha256Digest("sha256:" + "a" * 64)
    artifact = tmp_path / "artifacts" / "sha256" / digest.hex
    (artifact / "rootfs").mkdir(parents=True)
    images_root = tmp_path / "images"
    images_root.mkdir()
    unit_root = tmp_path / "units"
    unit_root.mkdir()
    state = tmp_path / "active"
    tools = tmp_path / "activation-tools"
    tools.mkdir()
    systemd_escape = _write_executable(
        tools / "systemd-escape",
        'print("test-image.mount")\n',
    )
    systemctl = _write_executable(
        tools / "systemctl",
        f"""
import sys
from pathlib import Path
state = Path({str(state)!r})
arguments = sys.argv[1:]
if arguments[:2] == ["is-active", "--quiet"]:
    raise SystemExit(0 if state.exists() else 3)
if "enable" in arguments:
    state.write_text("active", encoding="utf-8")
elif "disable" in arguments:
    state.unlink(missing_ok=True)
""",
    )
    mountpoint = _write_executable(
        tools / "mountpoint",
        f"""
from pathlib import Path
raise SystemExit(0 if Path({str(state)!r}).exists() else 1)
""",
    )
    findmnt = _write_executable(tools / "findmnt", 'print("rw,ro,nosuid,nodev")\n')
    sealer = _write_executable(
        tools / "wspctl-image",
        f'print("source_oci_manifest_digest={digest.value}")\n',
    )
    monkeypatch.setattr(_PUBLISHER.os, "chown", lambda *_arguments: None)
    monkeypatch.setattr(
        _PUBLISHER.ImageActivator,
        "_require_root_owned_directory",
        lambda *_arguments: None,
    )
    activation = _PUBLISHER.ImageActivator(
        _PUBLISHER.ActivationSpec(
            images_root=images_root,
            current_image_file=tmp_path / "current-image-digest",
            sealer=sealer,
            systemctl=systemctl,
            systemd_escape=systemd_escape,
            findmnt=findmnt,
            mountpoint=mountpoint,
            unit_root=unit_root,
        ),
        digest,
    )

    first = activation.activate(artifact)
    second = activation.activate(artifact)

    assert first == second == images_root / "sha256" / digest.hex / "rootfs"
    unit = (unit_root / "test-image.mount").read_text(encoding="utf-8")
    assert f"What={artifact / 'rootfs'}" in unit
    assert f"Where={first}" in unit
    assert "Options=bind,ro,nosuid,nodev" in unit
    assert "WantedBy=multi-user.target" in unit
    assert (tmp_path / "current-image-digest").read_text(encoding="ascii") == (
        f"{digest.value}\n"
    )
