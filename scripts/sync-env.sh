#!/usr/bin/env bash
set -euo pipefail

specialist_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if command -v uv >/dev/null 2>&1; then
  specialist_uv=$(command -v uv)
else
  specialist_uv="$HOME/.local/bin/uv"
fi

# Resolve the upstream SSH dependency from the pinned local clone for this command.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="url.file://${specialist_root%/*}/specialist_description.insteadOf"
export GIT_CONFIG_VALUE_0=ssh://git@github.com/specialist-ai/specialist_description.git
cd -- "$specialist_root"
exec "$specialist_uv" sync --locked --python 3.14 "$@"
