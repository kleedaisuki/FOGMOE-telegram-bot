# syntax=docker/dockerfile:1.7

# Build the pybind11 client wheel in an isolated stage.  CMake is explicitly told not
# to install privileged host tools into this wheel.
FROM python:3.14-slim-bookworm AS wheel-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential \
        libcap-dev \
        libseccomp-dev \
        libssl-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Debian bookworm's system CMake is older than the project contract.  Install the
# hermetic PyPI CMake/Ninja tools used by scikit-build-core instead.
RUN python -m pip install --upgrade "cmake>=3.28" "ninja>=1.11" pip

WORKDIR /build

COPY pyproject.toml uv.lock README.md LICENSE CMakeLists.txt ./
COPY src ./src

RUN python -m pip wheel \
    --config-settings=cmake.define.BUILD_TESTING=OFF \
    --config-settings=cmake.define.WSPCTL_BUILD_TESTING=OFF \
    --config-settings=cmake.define.WSPCTL_BUILD_HOST_PUBLISHER=OFF \
    --config-settings=cmake.define.WSPCTL_BUILD_HOST_RUNTIME=OFF \
    --config-settings=cmake.define.WSPCTL_BUILD_WORKSPACE_SUPERVISOR=OFF \
    --wheel-dir /wheels .


# This is intentionally a Bot-only image.  `wspctld` runs as a separately provisioned
# root-owned host systemd service and is reached only through a mounted Unix socket.
FROM python:3.14-slim-bookworm AS bot-runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        libcap2 \
        libseccomp2 \
        libssl3 \
    && groupadd --system --gid 65532 fogmoe \
    && useradd --system --uid 65532 --gid fogmoe \
        --home-dir /nonexistent --shell /usr/sbin/nologin fogmoe \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=wheel-builder /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels fogmoe-telegram-bot \
    && rm -rf /wheels

# The source layout is retained only for immutable Bot resources: its established
# resource resolver anchors `resources/` at /app.  No `src/wspctl` tree is copied, so
# Python loads the compiled unprivileged client from the wheel rather than a source tree.
COPY src/fogmoe_bot ./src/fogmoe_bot
COPY src/fogmoe_config ./src/fogmoe_config
COPY src/fogmoe_dashboard ./src/fogmoe_dashboard
COPY src/fogmoe_dbctl ./src/fogmoe_dbctl
COPY resources ./resources
COPY alembic.ini ./alembic.ini

RUN install --directory --owner=65532 --group=65532 --mode=0750 /app/logs

USER 65532:65532

# Runtime configuration is deliberately not baked into the image.  Compose mounts the
# operator-owned /app/config.json read-only and passes an explicit path.
CMD ["fogmoe-bot", "--config", "/app/config.json"]
