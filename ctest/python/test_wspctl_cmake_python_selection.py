"""@brief wspctl CMake 的项目 virtual-environment 选择验收 / Acceptance test for wspctl CMake project-virtual-environment selection."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


#: @brief 仓库根目录 / Repository root directory.
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
#: @brief 项目受控 Python 路径 / Project-controlled Python path.
PROJECT_VENV_PYTHON = REPOSITORY_ROOT / ".venv" / "bin" / "python"


def test_bare_cmake_configuration_prefers_project_venv_python() -> None:
    """@brief 未传 Python 选项时 CMake 必须使用项目 .venv / Bare CMake must use the project .venv when no Python option is passed.

    @return None / None.
    """

    if shutil.which("cmake") is None:
        raise RuntimeError("CTest host must provide cmake")
    if not PROJECT_VENV_PYTHON.is_file():
        raise RuntimeError("CTest source tree must provide .venv/bin/python")
    with tempfile.TemporaryDirectory() as directory:
        build_directory = Path(directory) / "build"
        configured = subprocess.run(
            [
                "cmake",
                "-S",
                str(REPOSITORY_ROOT),
                "-B",
                str(build_directory),
                "-DWSPCTL_BUILD_TESTING=OFF",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if configured.returncode != 0:
            raise AssertionError(
                "bare CMake configuration failed:\n"
                f"stdout:\n{configured.stdout}\n"
                f"stderr:\n{configured.stderr}"
            )
        cache = (build_directory / "CMakeCache.txt").read_text(encoding="utf-8")
        expected = str(PROJECT_VENV_PYTHON)
        assert f"Python_EXECUTABLE:FILEPATH={expected}" in cache


def test_host_unit_tracks_configure_time_install_prefix() -> None:
    """@brief host unit 的 broker 路径必须跟随配置期 install prefix / The host unit's broker path must follow the configure-time install prefix.

    @return None / None.
    """

    if shutil.which("cmake") is None:
        raise RuntimeError("CTest host must provide cmake")
    if not PROJECT_VENV_PYTHON.is_file():
        raise RuntimeError("CTest source tree must provide .venv/bin/python")
    with tempfile.TemporaryDirectory() as directory:
        build_directory = Path(directory) / "build"
        install_prefix = "/opt/wspctl-cmake-contract"
        configured = subprocess.run(
            [
                "cmake",
                "-S",
                str(REPOSITORY_ROOT),
                "-B",
                str(build_directory),
                "-DWSPCTL_BUILD_TESTING=OFF",
                "-DWSPCTL_INSTALL_HOST_TOOLS=ON",
                "-DWSPCTL_HOST_WORKDIR=/srv/wspctl-cmake-contract",
                f"-DCMAKE_INSTALL_PREFIX={install_prefix}",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        if configured.returncode != 0:
            raise AssertionError(
                "host deployment CMake configuration failed:\n"
                f"stdout:\n{configured.stdout}\n"
                f"stderr:\n{configured.stderr}"
            )
        unit = (
            build_directory / "deploy" / "wspctl" / "systemd" / "wspctld.service"
        ).read_text(encoding="utf-8")
        assert f"ExecStart={install_prefix}/bin/wspctld " in unit


if __name__ == "__main__":
    test_bare_cmake_configuration_prefers_project_venv_python()
    test_host_unit_tracks_configure_time_install_prefix()
