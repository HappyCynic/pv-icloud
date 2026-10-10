# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

# Dev image for pv-icloud. The production image is a separate, slim,
# non-root Dockerfile at the repo root; this one carries the dev tooling.
FROM mcr.microsoft.com/devcontainers/python:3-3.14-trixie@sha256:28835c112fd097cb6d2fe1e195b660d66ae051859070a9b871fa2308f35897d3

# uv (pinned minor) for dependency management and running tools.
COPY --from=ghcr.io/astral-sh/uv:0.12@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a /uv /uvx /usr/local/bin/

# sqlite3 CLI for inspecting the per-account mail indexes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Pre-create volume mount points owned by vscode so Docker seeds new named
# volumes with that ownership (otherwise they come up root-owned).
RUN mkdir -p /home/vscode/.venv /home/vscode/.cache/uv /home/vscode/.claude /data \
    && chown -R vscode:vscode /home/vscode/.venv /home/vscode/.cache /home/vscode/.claude /data

# Keep the venv and uv cache off the macOS bind mount: Linux wheels must not
# land in the host's .venv, and bind-mount I/O is slow on macOS.
ENV UV_PROJECT_ENVIRONMENT=/home/vscode/.venv \
    UV_CACHE_DIR=/home/vscode/.cache/uv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
