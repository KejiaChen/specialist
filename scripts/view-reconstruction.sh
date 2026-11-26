#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
dataset="$(realpath -e -- "${SPECIALIST_DATASET:-$root/datasets/pick_cup_01/processed/recollect_20261116_1720}")"
export PATH="$root/tools/ffmpeg/bin:$PATH:$HOME/.local/bin"
cd -- "$root"

replay="$dataset"
trim=false
viewer_args=()
for arg in "$@"; do
  if [[ $arg == --trim ]]; then trim=true; else viewer_args+=("$arg"); fi
done
manifest="$dataset/state_trim_reports/hold_trim.json"
if [[ ! -f $manifest && -f "$dataset/reports/hold_trim.json" ]]; then
  manifest="$dataset/reports/hold_trim.json"
fi
if [[ -f $manifest && ! -d "$dataset/base" ]]; then trim=true; fi
if $trim; then
  [[ -f $manifest ]] || { echo "该试次没有裁剪清单：$manifest" >&2; exit 2; }
  for arg in "$@"; do
    if [[ $arg == --detach-process ]]; then
      echo "裁剪回放需保持临时文件到查看器退出。请将整条回放脚本命令放入后台运行。" >&2
      exit 2
    fi
  done
  replay=$(mktemp -d -- "${TMPDIR:-/tmp}/specialist-replay.XXXXXXXX")
  trap 'rm -rf -- "$replay"' EXIT
  session_arg=()
  if [[ -n ${SPECIALIST_SESSION:-} ]]; then session_arg=(--session "$SPECIALIST_SESSION"); fi
  "$root/.venv/bin/python" "$root/scripts/materialize_trim.py" "$dataset" "$replay" "${session_arg[@]}"
fi
if [[ -n ${SPECIALIST_SESSION:-} ]]; then
  session=$SPECIALIST_SESSION
else
  shopt -s nullglob
  bases=("$replay"/base/*.rrd)
  (( ${#bases[@]} == 1 )) || { echo "请用 SPECIALIST_SESSION 指定要回放的会话" >&2; exit 1; }
  session=${bases[0]##*/}
  session=${session%.rrd}
fi
if [[ -n ${SPECIALIST_ASSET:-} ]]; then
  asset=$SPECIALIST_ASSET
else
  shopt -s nullglob
  assets=("$replay"/assets/gripper-*.rrd)
  (( ${#assets[@]} == 1 )) || { echo "请用 SPECIALIST_ASSET 指定夹爪模型" >&2; exit 1; }
  asset=${assets[0]}
fi
# Commands printed before compact storage can name an asset under the trim directory.
if [[ $replay != "$dataset" && ! -f $asset && ${asset%/*} == */assets ]]; then
  if [[ $(realpath -m -- "${asset%/*}") == "$dataset/assets" ]]; then asset="$replay/assets/${asset##*/}"; fi
fi
uv run --no-sync rerun \
  "$replay/blueprints/specialist.rbl" \
  "$replay/base/$session.rrd" \
  "$replay/estimate/$session.rrd" \
  "$replay/viz/$session.rrd" \
  --asset "$asset" \
  --video-decoder prefer_software "${viewer_args[@]}"
