#!/bin/sh
# SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
# SPDX-License-Identifier: Apache-2.0

# Runs on the host before the dev container starts (initializeCommand). Creates the
# gitignored local files from their templates so the container never references a
# missing file.
set -eu
cd "$(dirname "$0")/.."
copy_once() { [ -f "$2" ] || cp "$1" "$2"; }
copy_once .devcontainer/devcontainer.env.example .devcontainer/devcontainer.env
copy_once .devcontainer/compose.local.example.yaml .devcontainer/compose.local.yaml
copy_once config/pv-icloud.example.yaml config/pv-icloud.yaml
