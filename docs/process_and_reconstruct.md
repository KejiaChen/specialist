# Specialist 处理流程

本文按 `scripts/process-trial.sh` 的执行顺序说明每一步，并给出可单独运行的命令。手动处理时先设置第 1 步的变量，再跳到所需阶段。完整流程的入口和环境配置见[使用指南](workflow.md)。

已有重建结果，可直接跳到[第 7 步回放](#7-回放)。

## 1. 解析参数并定位数据

脚本读取 setup、模式、试次和锚点参数，检查外置盘与输出路径，锁定当前试次，再为每台相机查找 `_00_` 和 `_10_` 配对。通过后在外置盘创建暂存目录。 [参数解析](../scripts/process-trial.sh#L32) · [路径检查](../scripts/process-trial.sh#L65) · [配对](../scripts/process-trial.sh#L92) · [暂存](../scripts/process-trial.sh#L132)

单独执行后续命令时，在同一个 Bash 终端设置以下变量。使用已有产物时，把 `dataset` 改成实际绝对路径。写入前确认外置盘已挂载且可写。

```bash
cd ~/specialist-reconstruction/specialist
root="$PWD"
setup='<setup>'
mode='<single|dual>'
trial='<trail_YYMMDD_NNN>'
input="$root/datasets/$setup/$mode/$trial"
dataset="$root/datasets/$setup/processed/$mode/$trial"
export PATH="$root/tools/ffmpeg/bin:$HOME/.local/bin:$PATH"
set -o pipefail
```

只在检查 INSV 或转换时设置 `masters`。下面是单机写法。双机将数组改为两项，依次填入 `"$input/right/<right_00_filename>"` 和 `"$input/left/<left_00_filename>"`。

```bash
masters=("$input/<master_00_filename>")
```

## 2. 检查输入

脚本计算 SHA-256，检查方形 H.264 视频并完整解码。双机还检查两份主文件的音频轨。下方命令将这些信息打印到终端，供单独检查时核对。 [输入检查代码](../scripts/process-trial.sh#L149)

```bash
(
  set -e
  for master in "${masters[@]}"; do
    name="${master##*/}"
    rear="${master%/*}/${name/_00_/_10_}"
    for lens in "$master" "$rear"; do
      sha256sum -- "$lens"
      ffprobe -v error -show_entries stream=codec_type,codec_name,width,height \
        -of csv=p=0 "$lens"
      ffmpeg -nostdin -v error -xerror -i "$lens" -map 0:v:0 -f null -
    done
  done
)
```

## 3. 转换与同步

使用第 1 步设置的 `masters`。双机用第一份主文件作为时钟参考，按音频同步后保留共同区间。生成 `base/<session_id>.rrd`，脚本从文件名取得会话 ID。同步出错时看 `convert.log`。 [转换命令](../scripts/process-trial.sh#L187)

```bash
mkdir -p -- "$dataset/reports" &&
  TMPDIR="$dataset" uv run --no-sync python scripts/convert.py \
    --input-insvs "${masters[@]}" --output-dir "$dataset" \
    2>&1 | tee -a "$dataset/reports/convert.log"
```

## 4. 重建轨迹与夹爪

把 `base/<session_id>.rrd` 的文件名去掉 `.rrd` 后填入 `session`。按实际场地填写锚点 ID 和内方形边长，单位为米。第一个 ID 是世界参考锚点。生成 `estimate/`、`viz/`、`blueprints/` 和 `assets/`。 [重建命令](../scripts/process-trial.sh#L196)

```bash
session='<session_id>'
anchor_ids=('<reference_anchor_id>' '<additional_anchor_id>')
anchor_size='<anchor_size_m>'
mkdir -p -- "$dataset/reports" &&
  TMPDIR="$dataset" uv run --no-sync python scripts/estimate.py \
    --base "$dataset/base/$session.rrd" --anchor.tag-ids "${anchor_ids[@]}" \
    --anchor.tag-size "$anchor_size" --output-dir "$dataset" \
    2>&1 | tee -a "$dataset/reports/estimate.log"
```

只有一个锚点时，删掉数组里的第二项。重建失败时先看 `estimate.log`。

## 5. 生成训练 state

完整脚本在传入 `--state` 时执行本步。单独运行时使用同一会话的 `base` 和 `estimate`。成功后生成 `state_s8_r100/<session_id>.rrd`。开头 Hold 或结尾 Tap 检查失败时看 `state.log`。 [state 命令](../scripts/process-trial.sh#L207)

如果 `process-trial.sh` 处理后再单独生成训练state，直接使用它的产物目录作为 `dataset`。`tee -a` 会把本次输出追加到 `reports/state.log`，首次运行时会创建该文件。

```bash
cd ~/specialist-reconstruction/specialist
# dataset="$PWD/datasets/pick_cup_01/processed/dual/trail_261116_001"
# session='20261116_172034_IAQEB260436VUM'
set -o pipefail

  TMPDIR="$dataset" uv run --no-sync python scripts/state.py \
    --base "$dataset/base/$session.rrd" --estimate "$dataset/estimate/$session.rrd" \
    --output-dir "$dataset" \
    2>&1 | tee -a "$dataset/reports/state.log"
```

## 6. 发布结果

完整脚本把暂存区里的产物和日志发布到 `processed/<mode>/<trial>/`，然后清理临时文件。训练 state 失败时，脚本仍会保存已完成的重建和日志。手动命令已直接写入 `dataset`，单独执行时可跳过这一步。 [发布代码](../scripts/process-trial.sh#L216)

## 7. 回放

完整脚本会打印回放命令，传入 `--replay` 时会直接调用查看脚本。已有结果时，从仓库根目录运行 `./scripts/view-reconstruction.sh` 可打开本机默认的双机结果。其他结果使用下面的命令。 [回放入口](../scripts/process-trial.sh#L225) · [Rerun 加载参数](../scripts/view-reconstruction.sh#L11)

```bash
cd ~/specialist-reconstruction/specialist
dataset="$PWD/datasets/<setup>/processed/<mode>/<trial>"
ls "$dataset/base" "$dataset/assets"
```

`dataset` 填实际产物目录的绝对路径。将选中的 `base/*.rrd` 文件名去掉 `.rrd` 后填为 `session`，模型文件名保留 `.rrd`。

```bash
session='<session_id>'
asset="$dataset/assets/<gripper_asset_filename>"
SPECIALIST_DATASET="$dataset" SPECIALIST_SESSION="$session" SPECIALIST_ASSET="$asset" \
  ./scripts/view-reconstruction.sh
```

Rerun 会加载同一会话的各层，使用 `video_time` 时间轴查看。
