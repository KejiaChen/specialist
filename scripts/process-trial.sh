#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

usage() {
  cat <<'EOF'
用法
  ./scripts/process-trial.sh <setup> <single|dual> <trail_YYMMDD_NNN> --anchor-ids <ID,ID,...> --anchor-size <米> [--state] [--replay]

第一个 Anchor ID 定义世界参考坐标系。所有产物写入对应 setup 的 processed/<模式>/<trial>/。
--state   同时生成训练 state
--replay  处理结束后打开 Rerun

开发环境和日常用法见 docs/workflow.md。执行步骤和日志说明见 docs/process_and_reconstruct.md。
EOF
}

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  usage
  exit 0
fi
if (( $# < 3 )); then
  usage >&2
  exit 2
fi

setup=$1
mode=$2
trial=$3
shift 3
anchor_ids=
anchor_size=
write_state=false
replay=false
while (( $# > 0 )); do
  case "$1" in
    --anchor-ids) anchor_ids=${2:?请填写 Anchor ID}; shift 2 ;;
    --anchor-size) anchor_size=${2:?请填写 Anchor 边长，单位为米}; shift 2 ;;
    --state) write_state=true; shift ;;
    --replay) replay=true; shift ;;
    *) die "无法识别的参数 $1。用 --help 查看用法。" ;;
  esac
done

[[ $setup =~ ^[[:alnum:]][[:alnum:]_-]*$ ]] || die "setup 必须是一个目录名。"
[[ $mode == single || $mode == dual ]] || die "模式必须是 single 或 dual。"
[[ $trial =~ ^trail_[0-9]{6}_[0-9]{3}$ ]] ||
  die "试次目录名应为 trail_YYMMDD_NNN，例如 trail_261116_001。"
[[ $anchor_ids =~ ^(0|[1-9][0-9]*)(,(0|[1-9][0-9]*))*$ ]] ||
  die "请用 --anchor-ids 提供逗号分隔的非负整数 ID。"
[[ $anchor_size =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ && $anchor_size =~ [1-9] ]] ||
  die "请用 --anchor-size 提供大于零的边长，单位为米。"
IFS=, read -r -a anchors <<< "$anchor_ids"
declare -A seen_anchors=()
for id in "${anchors[@]}"; do
  [[ ! ${seen_anchors[$id]+present} ]] || die "重复的 Anchor ID $id。"
  seen_anchors[$id]=1
done

root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# Resolve datasets once; accept either a directory or a symlink on any filesystem.
data_root="$(realpath -e -- "$root/datasets" 2>/dev/null)" ||
  die "datasets 的目标无法访问，请检查数据目录或软链接。"
[[ -d $data_root && -r $data_root && -x $data_root ]] ||
  die "数据根目录不存在、不可读或无法进入：$data_root"
[[ -w $data_root ]] || die "数据根目录不可写：$data_root"

# Keep every input and output directory inside the resolved data root.
require_data_dir() {
  local directory=$1
  [[ -d "$directory" && $(realpath -e -- "$directory") == "$directory" ]] ||
    die "目录缺失或通过软链接跳转：$directory"
  [[ $directory == "$data_root" || $directory == "$data_root/"* ]] ||
    die "目录不在 datasets 数据根目录内：$directory"
  [[ -r $directory && -x $directory ]] || die "目录不可读或无法进入：$directory"
}

setup_dir="$data_root/$setup"
input="$setup_dir/$mode/$trial"
require_data_dir "$input"
exec {trial_lock}<"$input"
flock -n "$trial_lock" || die "此 trial 已有处理任务在运行：$input"

masters=()
lenses=()
camera_dirs=("$input")
if [[ $mode == dual ]]; then
  camera_dirs=("$input/right" "$input/left")
fi
for camera_dir in "${camera_dirs[@]}"; do
  require_data_dir "$camera_dir"
  matches=("$camera_dir"/*_00_*.insv)
  (( ${#matches[@]} == 1 )) ||
    die "$camera_dir 应有且仅有一个 _00_ INSV，找到 ${#matches[@]} 个。"
  master=${matches[0]}
  name=${master##*/}
  rear="$camera_dir/${name/_00_/_10_}"
  for lens in "$master" "$rear"; do
    [[ -f $lens && ! -L $lens && -r $lens && -s $lens ]] ||
      die "INSV 缺失、为空、不可读或是软链接：$lens"
  done
  masters+=("$master")
  lenses+=("$master" "$rear")
done

output_parent="$setup_dir/processed/$mode"
output="$output_parent/$trial"
for directory in "$setup_dir" "$setup_dir/processed" "$output_parent"; do
  if [[ -e $directory || -L $directory ]]; then
    require_data_dir "$directory"
  fi
done
[[ ! -e $output && ! -L $output ]] ||
  die "此 trial 已有输出：$output。可从该目录回放已有结果。"

export PATH="$root/tools/ffmpeg/bin:$HOME/.local/bin:$PATH"
for program in uv ffprobe ffmpeg; do
  command -v "$program" >/dev/null || die "找不到 $program，请检查本机处理环境。"
done
cd -- "$root"
mkdir -p -- "$output_parent"
require_data_dir "$output_parent"

# Publish the dataset only after the reconstruction layers have been verified.
run_dir="$(mktemp -d -- "$output_parent/.$trial.XXXXXXXX")"
dataset="$run_dir/dataset"
reports="$run_dir/reports"
scratch="$run_dir/tmp"
published=false
cleanup() {
  if $published; then
    rm -rf -- "$run_dir"
  else
    rm -rf -- "$dataset" "$scratch"
    printf '诊断日志目录 %s\n' "$reports" >&2
  fi
}
trap cleanup EXIT
mkdir -- "$dataset" "$reports" "$scratch"

check_raw() {
  local lens codec width height stream master
  sha256sum -- "${lenses[@]}" > "$reports/raw_sha256.txt" || return 1
  for lens in "${lenses[@]}"; do
    printf '检查 %s\n' "$lens"
    stream="$(ffprobe -v error -select_streams v:0 \
      -show_entries stream=codec_name,width,height -of csv=p=0 "$lens")" || return 1
    IFS=, read -r codec width height <<< "$stream"
    if [[ $codec != h264 || $width != "$height" || ! $width =~ ^[1-9][0-9]*$ ]]; then
      printf '预期方形 H.264 视频，实际为 %s（%s）\n' "$stream" "$lens" >&2
      return 1
    fi
    ffmpeg -nostdin -v error -xerror -i "$lens" -map 0:v:0 -f null - || return 1
  done
  if [[ $mode == dual ]]; then
    for master in "${masters[@]}"; do
      stream="$(ffprobe -v error -select_streams a:0 \
        -show_entries stream=index -of csv=p=0 "$master")" || return 1
      if [[ -z $stream ]]; then
        printf '双机同步需要音频轨：%s\n' "$master" >&2
        return 1
      fi
    done
  fi
}

run_step() {
  local title=$1 log_file=$2
  shift 2
  printf '[%s]\n' "$title"
  if ! "$@" 2>&1 | tee "$reports/$log_file"; then
    printf '%s失败。日志见 %s\n' "$title" "$reports/$log_file" >&2
    return 1
  fi
}

printf '输入 %s\n产物 %s\n' "$input" "$output"
run_step '输入检查' raw_check.log check_raw
run_step '转换与同步' convert.log env TMPDIR="$scratch" uv run --no-sync \
  python scripts/convert.py --input-insvs "${masters[@]}" --output-dir "$dataset"
bases=("$dataset"/base/*.rrd)
(( ${#bases[@]} == 1 )) && [[ -s ${bases[0]} ]] ||
  die "转换没有生成唯一可用的 base RRD。"
base=${bases[0]}
session=${base##*/}
session=${session%.rrd}

run_step '轨迹与夹爪重建' estimate.log env TMPDIR="$scratch" uv run --no-sync \
  python scripts/estimate.py --base "$base" --anchor.tag-ids "${anchors[@]}" \
  --anchor.tag-size "$anchor_size" --output-dir "$dataset"
assets=("$dataset"/assets/gripper-*.rrd)
(( ${#assets[@]} == 1 )) && [[ -s ${assets[0]} ]] ||
  die "重建没有生成唯一可用的夹爪模型。"
for file in "estimate/$session.rrd" "viz/$session.rrd" "blueprints/specialist.rbl"; do
  [[ -s "$dataset/$file" ]] || die "缺少重建产物 $file。"
done

state_failed=false
if $write_state; then
  if ! run_step '训练 state' state.log env TMPDIR="$scratch" uv run --no-sync \
    python scripts/state.py --base "$base" --estimate "$dataset/estimate/$session.rrd" \
    --output-dir "$dataset" || [[ ! -s "$dataset/state_s8_r100/$session.rrd" ]]; then
    state_failed=true
    rm -rf -- "$dataset/state_s8_r100"
  fi
fi

cp -a -- "$reports" "$dataset/reports"
require_data_dir "$output_parent"
mv -Tn -- "$dataset" "$output"
[[ ! -e $dataset ]] || die "发布产物时目标已存在：$output"
published=true
cleanup
trap - EXIT
exec {trial_lock}<&-

asset="$output/assets/${assets[0]##*/}"
printf '重建结果 %s\n回放命令\n' "$output"
printf 'SPECIALIST_DATASET=%q SPECIALIST_SESSION=%q SPECIALIST_ASSET=%q %q\n' \
  "$output" "$session" "$asset" "$root/scripts/view-reconstruction.sh"
if $state_failed; then
  die "重建已保存。训练 state 失败，日志见 $output/reports/state.log。"
fi
if $replay; then
  SPECIALIST_DATASET="$output" SPECIALIST_SESSION="$session" SPECIALIST_ASSET="$asset" \
    exec "$root/scripts/view-reconstruction.sh"
fi
