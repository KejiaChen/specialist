#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
dataset="${SPECIALIST_DATASET:-$root/datasets/pick_cup_01/processed/recollect_20261116_1720}"
session="${SPECIALIST_SESSION:-20261116_172034_IAQEB260436VUM}"
asset="${SPECIALIST_ASSET:-$dataset/assets/gripper-17390ff8a953.rrd}"
export PATH="$root/tools/ffmpeg/bin:$PATH:$HOME/.local/bin"
cd -- "$root"

exec uv run --no-sync rerun \
  "$dataset/blueprints/specialist.rbl" \
  "$dataset/base/$session.rrd" \
  "$dataset/estimate/$session.rrd" \
  "$dataset/viz/$session.rrd" \
  --asset "$asset" \
  --video-decoder prefer_software "$@"
