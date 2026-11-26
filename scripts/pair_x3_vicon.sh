#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob inherit_errexit

# 1. 参数与输出目录
usage() {
  cat <<'EOF'
用法：pair_x3_vicon.sh --dataset 数据集目录 --trials 048 049 ...
  --anchor-ids 0,1             线速度轨迹的锚点 ID，第一个为参考锚点
  --anchor-size 0.100          线速度轨迹的锚点边长（米）
  --min-overlap-fraction 0.5   最低重叠比例（0 到 1）
  --output-dir 目录            默认：数据集/single/pairing
输入为已归档的 single/raw/x3/trial_YYMMDD_NNN/ 和 single/raw/vicon/*_YYMMDD_NNN.csv。
角速度匹配只需 base；线速度回退使用已有轨迹。
输出目录的 summary.csv 和 motive_csv/ 按试次更新；每次运行的参数和日志在 run_*/。
EOF
}
die() { echo "$*" >&2; exit 1; }
scripts=$(cd -- "$(dirname -- "$0")" && pwd -P)
specialist=$(dirname -- "$scripts")
repo=$(dirname -- "$specialist")
py="$specialist/.venv/bin/python"
export PATH="$specialist/tools/ffmpeg/bin:$PATH"
dataset= output= anchor_ids= anchor_size= overlap=0.5
trials=()
while (($#)); do
  case $1 in
    --dataset) dataset=${2:?缺少数据集目录}; shift 2 ;;
    --anchor-ids) anchor_ids=${2:?缺少锚点 ID}; shift 2 ;;
    --anchor-size) anchor_size=${2:?缺少锚点边长}; shift 2 ;;
    --output-dir) output=${2:?缺少输出目录}; shift 2 ;;
    --min-overlap-fraction) overlap=${2:?缺少重叠比例}; shift 2 ;;
    --trials)
      shift
      [[ $# -gt 0 && $1 != -* ]] || die "--trials 缺少试次号"
      while (($#)) && [[ $1 != -* ]]; do trials+=("$1"); shift; done ;;
    --help|-h) usage; exit 0 ;;
    *) die "未知参数 $1；请用 --help 查看用法" ;;
  esac
done
[[ -n $dataset && -d $dataset ]] || die "请用 --dataset 指定任务数据集"
dataset=$(realpath -e -- "$dataset")
layout=$("$py" "$scripts/dataset_layout.py" "$dataset" single)
mapfile -t paths <<< "$layout"
raw=${paths[0]} vicon=${paths[1]} pairing=${paths[2]} processed=${paths[3]}
mode_suffix=
if [[ ${paths[4]} == legacy ]]; then mode_suffix=/single; fi
[[ -d $raw && -d $vicon ]] || die "缺少输入目录：$raw 或 $vicon"
((${#trials[@]})) || die "请用 --trials 指定三位试次号"
declare -A seen=()
for n in "${trials[@]}"; do
  [[ $n =~ ^[0-9]{3}$ ]] || die "试次号须为三位数字：$n"
  [[ ! -v seen[$n] ]] || die "重复试次：$n"
  seen[$n]=1
done
mapfile -t trials < <(printf '%s\n' "${trials[@]}" | LC_ALL=C sort)
"$py" - "$overlap" "$anchor_ids" "$anchor_size" <<'PY'
import math, re, sys
overlap, ids, size = sys.argv[1:]
if ids and (not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:,(?:0|[1-9][0-9]*))*", ids)
            or len(set(ids.split(","))) != len(ids.split(","))):
    sys.exit("锚点 ID 应为逗号分隔的非负整数，且不能重复")
if size and (not re.fullmatch(r"(?:[0-9]+(?:[.][0-9]+)?|[.][0-9]+)", size)
             or not 0 < float(size) < math.inf):
    sys.exit("锚点边长必须是大于零的有限十进制数（米）")
try:
    if 0 <= float(overlap) <= 1:
        sys.exit(0)
except ValueError:
    pass
sys.exit("重叠比例必须在 0 到 1 之间")
PY
output=${output:-"$pairing"}
mkdir -p -- "$output"
output=$(realpath -e -- "$output")
exec {lock_fd}<"$output"
flock -n "$lock_fd" || die "同一输出目录已有任务在运行：$output"
[[ ! -e "$output/latest" || -L "$output/latest" ]] || die "latest 已被普通文件或目录占用"
# 如上次发布被中断，先完成它，再开始下一次配对。
if [[ -L "$output/.publishing" ]]; then
  "$py" "$scripts/publish_pairing.py" "$output" "$(realpath -e -- "$output/.publishing")"
fi
run=$(mktemp -d -- "$output/run_$(date +%Y%m%d_%H%M%S)_XXXX")
products="$run/.products"
mkdir -p -- "$run/logs" "$products"/{motive_csv,paired_mocap}
printf 'dataset=%s\ntrials=%s\nanchor_ids=%s\nanchor_size_m=%s\nmin_overlap_fraction=%s\n' \
  "$dataset" "${trials[*]}" "$anchor_ids" "$anchor_size" "$overlap" > "$run/parameters.txt"
echo "本次运行 $run"

# 2. 核对已归档的试次
process_one() {
  local n=$1 folders fronts csvs trial trialdir name rear reco base file result source
  folders=("$raw"/trial_[0-9][0-9][0-9][0-9][0-9][0-9]_"$n")
  ((${#folders[@]} == 1)) || die "$n 应有唯一的原始试次目录"
  trialdir=${folders[0]}
  trial=${trialdir##*/}
  fronts=("$trialdir"/*_00_*.insv)
  csvs=("$vicon"/*_"${trial#trial_}".csv)
  ((${#fronts[@]} == 1 && ${#csvs[@]} == 1)) || die "$trial 缺少唯一的主镜头或 Vicon CSV"
  name=${fronts[0]##*/}
  rear="$trialdir/${name/_00_/_10_}"
  for source in "${fronts[0]}" "$rear" "${csvs[0]}"; do
    [[ -f $source && -r $source && -s $source ]] || die "缺少可读输入：$source"
  done

  # 3. 获取包含陀螺仪的 X3 base
  reco="$processed$mode_suffix/$trial"
  base=$(prepare_base "$reco" "${fronts[0]}" "$rear")
  file=${base##*/}
  echo "匹配使用 base：$base"

  # 4. 转换 Vicon 刚体轨迹
  "$py" "$repo/vicon/processing/vicon_to_motive.py" "${csvs[0]}" --output-dir "$products/motive_csv"

  # 5. 角速度匹配与线速度回退
  local log="$run/logs/${n}_sync.log" rc=0 estimate
  local sync=("$py" "$scripts/convert_mocap.py"
    "$products/motive_csv/${csvs[0]##*/}" "$base" "$products" --min-overlap-fraction "$overlap")
  "${sync[@]}" > "$log" 2>&1 || rc=$?
  if (( rc != 0 )) && grep -q '^rotation rate .*pass --estimate' "$log"; then
    local candidates=("$reco/estimate/$file" "$pairing/linear_speed/estimate/$file")
    if [[ -n $anchor_ids || -n $anchor_size ]]; then
      candidates+=("$processed"/anchors_*"$mode_suffix"/"$trial/estimate/$file")
    fi
    estimate=$(select_estimate "${file%.rrd}" "${candidates[@]}" 2>> "$log")
    if [[ -z $estimate ]]; then
      echo "角速度未通过，缺少符合条件的同会话轨迹，未尝试线速度。" >> "$log"
      printf '%s,%s,needs_estimate,,,\n' "$n" "${file%.rrd}" > "$row"
      return
    fi
    echo "线速度回退使用 estimate：$estimate" >> "$log"
    rc=0
    "${sync[@]}" --estimate "$estimate" >> "$log" 2>&1 || rc=$?
  fi
  if (( rc == 0 )); then
    result=$(sed -nE 's/^session clock \((rotation|speed)\): offset=([-0-9.]+)s corr=([-0-9.]+).*/\1,\2,\3/p' "$log")
    [[ -s "$products/paired_mocap/$file" && -n $result ]] || die "缺少同步结果：$log"
    printf '%s,%s,pass,%s\n' "$n" "${file%.rrd}" "$result" > "$row"
  elif grep -Eq '^(no rigid body identified a capture|no rigid body could be matched)' "$log"; then
    printf '%s,%s,review,,,\n' "$n" "${file%.rrd}" > "$row"
  else
    tail -n 8 "$log" >&2; die "同步程序出错：$log"
  fi
}

# 第 5 节：优先使用重建参数记录；旧归档从 RRD 的静态锚点读取配置。
select_estimate() {
  "$py" "$scripts/match_estimate.py" "$anchor_ids" "$anchor_size" "$@"
}

# 第 3 节：与完整重建共用试次锁，只发布已校验的 base。
prepare_base() (
  local reco=$1 front=$2 rear=$3 stage base
  local check=("$py" "$scripts/checked_single_base.py")
  exec {base_lock}<"${front%/*}"
  flock "$base_lock"
  if [[ -d "$reco/base" ]]; then
    "${check[@]}" "$reco" "$front" "$rear"
    return
  fi

  mkdir -p -- "${reco%/*}"
  stage=$(mktemp -d -- "${reco%/*}/.base_XXXXXX")
  trap 'rm -r -- "$stage"' EXIT
  echo "从 INSV 生成 base：$front" >&2
  "$py" "$scripts/convert.py" --input-insvs "$front" --output-dir "$stage" >&2
  sha256sum -- "$front" "$rear" > "$stage/base/raw_sha256.txt"
  base=$("${check[@]}" "$stage" "$front" "$rear")
  if [[ -d $reco ]]; then
    mv -Tn -- "$stage/base" "$reco/base"
  else
    chmod --reference="${reco%/*}" "$stage"
    mv -Tn -- "$stage" "$reco"
    if [[ ! -e $stage ]]; then trap - EXIT; fi
  fi
  [[ ! -e "$stage/base" ]] || die "目标 base 已存在：$reco/base"
  printf '%s/base/%s\n' "$reco" "${base##*/}"
)

# 6. 逐条运行并汇总
summary="$run/summary.csv"
printf 'trial,session_id,status,signal,offset_s,corr\n' > "$summary"
errors=0
for n in "${trials[@]}"; do
  row="$run/.row"
  echo "处理 $n"
  set +e
  (set -e; process_one "$n") > "$run/logs/${n}_process.log" 2>&1
  rc=$?
  set -e
  if (( rc != 0 )); then
    printf '%s,,error,,,\n' "$n" > "$row"
    ((errors+=1))
    tail -n 3 "$run/logs/${n}_process.log" >&2
  fi
  cat "$row" | tee -a "$summary"
  rm -- "$row"
done
"$py" "$scripts/publish_pairing.py" "$output" "$run"
awk -F, 'NR>1 {n++; counts[$3]++}
  END {printf "共 %d 条，通过 %d（%.1f%%），缺轨迹 %d，待复核 %d，执行出错 %d\n",
       n,counts["pass"],100*counts["pass"]/n,counts["needs_estimate"],counts["review"],counts["error"]}' "$summary"
echo "$output/summary.csv"
((errors == 0))
